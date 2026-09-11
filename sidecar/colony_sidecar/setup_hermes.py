"""The guided Hermes path of ``colony init``.

Use canonical adapter resources and one private instance. This is an installer,
not a release controller: existing instances are retained and runtime upgrades
remain explicit. No model, container, OS service or Hermes core is downloaded.
"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import types
from urllib.parse import urlsplit
import zipfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml


def _private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content if isinstance(content, bytes) else content.encode())
        stream.flush()
        os.fsync(stream.fileno())


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def _receipt_preference(config, choice):
    """Project one native shared key, respecting Hermes' existing YAML layouts.

    Shared-key bridging chooses whatsapp, gateway.platforms.whatsapp, then
    platforms.whatsapp. Nested extra maps are merged separately. Synchronize
    existing aliases so an older spelling cannot override the explicit choice.
    Copy each touched mapping to avoid changing unrelated YAML anchor aliases.
    """
    if choice not in {'on', 'off'}:
        raise ValueError('WhatsApp read receipts must be on or off')
    candidate = dict(config)
    key, enabled, changes = 'send_read_receipts', choice == 'on', []

    def mapping(parent, name, create=False):
        if name not in parent and not create:
            return None
        value = parent.get(name, {})
        if not isinstance(value, dict):
            raise ValueError('WhatsApp configuration sections and extra settings must be mappings')
        parent[name] = dict(value)
        return parent[name]

    slots = []
    for path, merged in [(('whatsapp',), False),
                         (('gateway', 'platforms', 'whatsapp'), True),
                         (('platforms', 'whatsapp'), True),
                         (('gateway', 'whatsapp'), True)]:
        node = candidate
        for part in path:
            node = mapping(node, part)
            if node is None:
                break
        if node is not None:
            extra = mapping(node, 'extra')
            slots.append((path, node, extra, merged))
    chosen = next((row for row in slots if row[0] != ('gateway', 'whatsapp')), None)
    effective = bool(chosen and key in chosen[1]) or any(
        merged and extra is not None and key in extra for _, _, extra, merged in slots)
    # Stock Hermes treats any merged extras as connection configuration. Do not
    # introduce that signal when channel selection exists only outside YAML.
    explicit_selection = any('enabled' in node for _, node, _, merged in slots
                             if merged or node is (chosen[1] if chosen else None))
    existing_extras = any(merged and extra for _, _, extra, merged in slots)
    if not (explicit_selection or effective or existing_extras):
        raise ValueError('Select WhatsApp enabled: true or enabled: false explicitly in Hermes config.yaml before adding a receipt preference')

    def assign(node, path):
        if node.get(key) is not enabled:
            node[key] = enabled
            changes.append('.'.join((*path, key)))

    for path, node, extra, _ in slots:
        if key in node:
            assign(node, path)
        if extra is not None and key in extra:
            assign(extra, (*path, 'extra'))
    if not effective:
        if chosen:
            assign(chosen[1], chosen[0])
        elif slots:  # gateway.whatsapp.extra is merged, its direct keys are not.
            path, node, _, _ = slots[0]
            assign(mapping(node, 'extra', True), (*path, 'extra'))
        else:
            platforms = mapping(candidate, 'platforms', True)
            whatsapp = mapping(platforms, 'whatsapp', True)
            assign(mapping(whatsapp, 'extra', True), ('platforms', 'whatsapp', 'extra'))
    return candidate, changes


def _write_receipt_preference(home, choice, preview=False):
    from . import setup
    path = home/'config.yaml'
    original, config = setup._read_hermes_config(path)
    if original is None:
        raise ValueError('Preference updates require an existing Hermes config.yaml')
    candidate, changes = _receipt_preference(config, choice)
    if not preview and changes:
        setup._atomic_hermes_config_write(path, original,
            yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode())
    print(('Preview' if preview else 'Configured') + ': WhatsApp read receipts ' + choice)
    print('Changed preference paths: ' + (', '.join(changes) if changes else 'none'))
    if not preview:
        print('Use the selected gateway lifecycle to reconnect and load this setting; no process was restarted.')


def _agent_preferences(ask, args, config):
    """Private identity and time preferences; no automatic permission grants."""
    value_text = ask('Guiding values (comma-separated, optional)',
                     getattr(args, 'agent_values', None) or '')
    values = list(dict.fromkeys(item.strip() for item in value_text.split(',') if item.strip()))
    if len(values) > 12 or any(len(value) > 120 for value in values):
        raise ValueError('Use at most twelve guiding values, each at most 120 characters')
    timezone_name = ask('Timezone for deadlines and follow-ups',
                        getattr(args, 'timezone', None) or config.get('timezone') or 'UTC', True)
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Use a named timezone such as UTC or Europe/Paris') from None
    quiet = ask('Quiet hours for optional follow-ups (HH:MM-HH:MM, optional)',
                getattr(args, 'quiet_hours', None) or '')
    if quiet:
        times = quiet.split('-')
        valid_time = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
        if len(times) != 2 or not all(valid_time.fullmatch(value) for value in times) or times[0] == times[1]:
            raise ValueError('Quiet hours require two different times, HH:MM-HH:MM')
    return {'values': values, 'timezone': timezone_name, 'quiet_hours': quiet}


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or port == 0):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use an HTTP(S) API root with a valid port and no embedded credential') from None
    return value.rstrip('/') + ('/v1' if not parsed.path.strip('/') else '')


def _verify_local_endpoint(endpoint):
    """Declare the selected host and reuse the runtime's address check."""
    from colony_sidecar.router.router import LLMRouter
    hosts = [urlsplit(endpoint).hostname]
    router = LLMRouter(tiers={}, self_learner=object())
    router.configure({'provider': 'local', 'baseUrl': endpoint, 'apiKey': 'local-no-key',
                      'models': {'small': 'setup-endpoint-check'}, 'localHosts': hosts})
    if not asyncio.run(router._local_addresses(router._snapshot, router._snapshot.bindings['small'])):
        raise ValueError('The selected model endpoint must resolve to the configured local networks')
    return hosts


def _interpreter(candidate):
    if not candidate:
        hermes = shutil.which('hermes')
        if hermes:
            first = Path(hermes).read_text().splitlines()[0]
            if first.startswith('#!/') and ' ' not in first[2:]:
                candidate = first[2:]
        if not candidate:
            candidate = sys.executable
    python = Path(candidate).expanduser().absolute()
    probe = subprocess.run([str(python), '-I', '-c',
        'import importlib.metadata,json; import httpx,httpcore,yaml; '
        'from agent.memory_manager import MemoryManager; '
        'from hermes_cli.plugins import get_plugin_manager; '
        'print(json.dumps({"version":importlib.metadata.version("hermes-agent")}))'],
        capture_output=True, text=True, timeout=30)
    if probe.returncode or json.loads(probe.stdout.splitlines()[-1]).get('version') not in {'0.21.0', '0.21.1'}:
        raise ValueError('Select the Python interpreter of Hermes 0.21.0 or 0.21.1 with its native dependencies installed')
    return python


def _profile_homes(python):
    """Ask the selected native runtime for live profile paths, without reading their config."""
    probe = subprocess.run([str(python), '-I', '-B', '-c',
        'import json; from hermes_cli.profiles import list_profile_names,profile_exists,get_profile_dir; '
        'print(json.dumps([{ "name":name,"path":str(get_profile_dir(name))} '
        'for name in list_profile_names() if profile_exists(name) '
        'and (get_profile_dir(name)/"config.yaml").is_file()]))'],
        capture_output=True, text=True, timeout=10)
    try:
        rows = json.loads(probe.stdout.splitlines()[-1]) if probe.returncode == 0 else None
        if not isinstance(rows, list):
            raise ValueError()
        result = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'name', 'path'}:
                raise ValueError()
            if not all(isinstance(row[key], str) and row[key] and not any(ord(c) < 32 for c in row[key])
                       for key in ('name', 'path')) or not Path(row['path']).is_absolute():
                raise ValueError()
            path = Path(row['path']).resolve()
            if path not in [item['path'] for item in result]:
                result.append({'name': row['name'], 'path': path})
        return result
    except (ValueError, IndexError, TypeError):
        raise ValueError('Could not list native Hermes profiles; select one with --hermes-home') from None


def _select_home(args, ask):
    from colony_sidecar import setup
    explicit = getattr(args, 'hermes_home', None) or os.environ.get('HERMES_HOME')
    if explicit or getattr(args, 'non_interactive', False):
        return setup._resolve_hermes_home(explicit), None
    python = _interpreter(getattr(args, 'hermes_python', None))
    homes = _profile_homes(python)
    if homes:
        print('Existing Hermes profiles:')
        for row in homes:
            print(f"  {row['name']}: {row['path']}")
    default = str(homes[0]['path']) if homes else str(setup._resolve_hermes_home())
    selected = ask('Hermes home to attach (one profile)', default, True)
    return setup._resolve_hermes_home(selected), python


def _adapter_resources(wheel=None):
    packages = ('colony_hermes/', 'colony_memory/')
    if wheel:
        with zipfile.ZipFile(wheel) as archive:
            resources = {name: archive.read(name) for name in archive.namelist()
                if name.startswith(packages) and not name.endswith('/') and '__pycache__' not in name}
    else:
        try:
            distribution = importlib.metadata.distribution('colony-hermes')
        except importlib.metadata.PackageNotFoundError:
            raise ValueError('Install the canonical colony-hermes package alongside colonyai, or supply --adapter-wheel') from None
        resources = {str(path): distribution.locate_file(path).read_bytes() for path in distribution.files or []
                     if str(path).startswith(packages) and '__pycache__' not in str(path)}
    if any(Path(name).is_absolute() or '..' in Path(name).parts for name in resources):
        raise ValueError('Adapter resource escapes its package')
    for name in ('colony_hermes/__init__.py', 'colony_hermes/evidence.py', 'colony_hermes/client.py',
                 'colony_hermes/commitment_work.py', 'colony_memory/__init__.py', 'colony_memory/provider.py'):
        if name not in resources:
            raise ValueError('Canonical adapter artifact is incomplete')
    return resources


def _preflight_outbox(home, resources):
    """Check the selected runtime's existing path rules without creating state.

    The adapter may only be supplied as a wheel, so inspect that exact client
    resource rather than importing a possibly different installed adapter.
    Runtime descriptor checks still enforce the boundary when the file opens.
    """
    name = '_colony_setup_pathcheck'
    module = types.ModuleType(name)
    sys.modules[name] = module
    selected = home/'state'/'colony-turn-outbox.sqlite3'
    inspected = selected
    try:
        exec(compile(resources['colony_hermes/client.py'], '<canonical Colony client>', 'exec'), module.__dict__)
        boundary = module.PrivateSQLitePath(selected)
        for inspected in reversed(selected.parents):
            try:
                value = inspected.lstat()
            except FileNotFoundError:
                continue  # The installer/runtime creates missing directories privately.
            boundary._validate_directory(value, private_parent=inspected == selected.parent,
                                         label='ancestor')
        inspected = selected
        if selected.exists() or selected.is_symlink():
            boundary._validate_leaf(selected.lstat())
    except module.PrivateSQLitePathError as error:
        raise ValueError(f'Cannot use the Hermes conversation outbox at {inspected}: {error}. '
            'Choose --hermes-home beneath a directory you own without group/other write access; '
            'an existing state directory must be mode 0700. No permissions were changed.') from None
    finally:
        sys.modules.pop(name, None)


def _adapter_binding(python, resources):
    """Respect native entry-point precedence, verifying the selected code first."""
    expected = {name: hashlib.sha256(value).hexdigest() for name, value in resources.items()}
    probe = subprocess.run([str(python), '-I', '-c', r'''
import hashlib, importlib.metadata, importlib.util, json, sys
from pathlib import Path
expected = json.load(sys.stdin)
entries = importlib.metadata.entry_points()
selected = []
for group, name, module in [('hermes_agent.plugins', 'colony', 'colony_hermes'),
                            ('hermes_agent.memory_providers', 'colony-memory', 'colony_memory')]:
    matches = [ep for ep in entries.select(group=group) if ep.name == name]
    if len(matches) > 1 or (matches and matches[0].value != module):
        raise ValueError('Conflicting Colony entry point')
    selected.append(matches[0] if matches else None)
if not any(selected):
    print(json.dumps({'mode': 'private-directory'}))
    sys.exit(0)
if not all(selected):
    raise ValueError('Both canonical native Colony entry points are required')
sources, external_modules, versions = {}, {}, set()
for ep in selected:
    module = ep.value
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin:
        raise ValueError('Installed adapter package cannot be resolved')
    root = Path(spec.origin).resolve().parent
    sources[module] = str(root)
    versions.add(ep.dist.version)
    names = {name for name in expected if name.startswith(module+'/')}
    # Editable installs include source-only ops/examples and resolve the two
    # shared catalog modules through the existing hostworker namespace bridge.
    # Verify every packaged module at the path Python will actually import.
    for name in sorted(names):
        path = root/name.split('/', 1)[1]
        if name.endswith('.py'):
            qualified = name[:-3].replace('/', '.')
            if qualified.endswith('.__init__'):
                qualified = qualified[:-9]
            resolved = importlib.util.find_spec(qualified)
            if resolved is None or not resolved.origin:
                raise ValueError('Installed adapter module cannot be resolved')
            path = Path(resolved.origin).resolve()
            if not path.is_relative_to(root):
                external_modules[qualified] = str(path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
            raise ValueError('Installed adapter bytes differ from the selected artifact')
if len(versions) != 1:
    raise ValueError('Installed adapter entry-point versions disagree')
print(json.dumps({'mode': 'native-installed', 'version': versions.pop(),
    'sources': sources, 'external_modules': external_modules}))
'''], input=json.dumps(expected), capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise ValueError('Hermes has an incomplete or different installed Colony adapter; select its matching artifact, upgrade that package explicitly, or use a separate Hermes interpreter')
    return json.loads(probe.stdout.splitlines()[-1])


def _forwarder(site, module, memory=False):
    # Native Hermes memory discovery is import-free and looks for this symbol.
    hint = '# ColonyMemoryProvider native provider entry point.\n' if memory else ''
    return hint + ('import importlib as _importlib\nimport sys as _sys\n'
        f'_site = {str(site)!r}\n'
        'if _site not in _sys.path:\n    _sys.path.insert(0, _site)\n'
        f'_implementation = _importlib.import_module({module!r})\n'
        'for _name, _value in vars(_implementation).items():\n'
        '    if not _name.startswith("__") or _name in ("__all__", "__doc__"):\n'
        '        globals()[_name] = _value\n')


def _native_environment(original, values):
    # Preserve every unrelated line and refuse to replace an existing secret.
    text = original.decode() if original is not None else ''
    for name, value in values.items():
        if re.search(r'^\s*(?:export\s+)?' + re.escape(name) + r'\s*=', text, re.M):
            raise ValueError(f'{name} already exists; retain the existing instance or select a new Hermes home')
    return (text + ('\n' if text and not text.endswith('\n') else '') +
            '\n'.join(name + '=' + json.dumps(value) for name, value in values.items()) + '\n').encode()


def _resource_digest(resources):
    return hashlib.sha256(b''.join(name.encode()+resources[name] for name in sorted(resources))).hexdigest()


def _copied_resources(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Expected this instance\'s copied adapter directory')
    resources = {}
    for path in directory.rglob('*'):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            raise ValueError('Copied adapter contains a local symlink; reconcile it before refreshing')
        if path.is_file():
            resources[str(path.relative_to(directory))] = path.read_bytes()
    return resources


def refresh_adapter(state, args):
    """Refresh canonical code for one stopped attachment, preserving its data."""
    from .setup import _atomic_hermes_config_write, _read_hermes_config, _align_hermes_memory_spill
    path = state/'instance.json'; before = path.read_bytes(); manifest = json.loads(before)
    if (manifest.get('version') != 1 or manifest.get('profile') != 'local'
            or manifest.get('adapter_binding', {}).get('mode') not in {'native-installed', 'private-directory'}):
        raise ValueError('Adapter refresh requires a supported local attachment')
    home = Path(manifest['hermes_home'])
    python = _interpreter(getattr(args, 'hermes_python', None) or manifest['hermes_python'])
    resources = _adapter_resources(getattr(args, 'adapter_wheel', None))
    binding = _adapter_binding(python, resources)
    old_binding = manifest['adapter_binding']
    if old_binding['mode'] != binding['mode']:
        raise ValueError('Adapter loading mode changed; use the original interpreter topology before refreshing')
    updates = []
    adapter = state/'adapter'; staged = backup = None
    old_resources = None
    if binding['mode'] == 'private-directory':
        old_resources = _copied_resources(adapter)
        if _resource_digest(old_resources) != manifest['adapter_sha256']:
            raise ValueError('Copied adapter was edited locally; retain or reconcile those edits before refreshing')
        for directory, module in (('colony', 'colony_hermes'), ('colony-memory', 'colony_memory')):
            forwarder = home/'plugins'/directory/'__init__.py'
            if forwarder.is_symlink() or forwarder.read_text() != _forwarder(adapter, module, directory == 'colony-memory'):
                raise ValueError('The selected profile adapter forwarder changed; reconcile it before refreshing')
            target = home/'plugins'/directory/'plugin.yaml'
            if target.is_symlink() or target.read_bytes() != old_resources[module+'/plugin.yaml']:
                raise ValueError('The selected profile adapter manifest changed; reconcile it before refreshing')
            updates.append((target, target.read_bytes(), resources[module+'/plugin.yaml']))
        lane = manifest.get('local_work') or {}
        if lane.get('executor') == 'kanban':
            from .setup_local_work import native_root
            worker = native_root(home)/'profiles'/lane['worker_profile']
            config = yaml.safe_load((worker/'config.yaml').read_text())
            forwarder = worker/'plugins/colony/__init__.py'
            target = worker/'plugins/colony/plugin.yaml'
            if (config['plugins']['colony']['instance_dir'] != str(state)
                    or forwarder.is_symlink() or forwarder.read_text() != _forwarder(adapter, 'colony_hermes')
                    or target.is_symlink() or target.read_bytes() != old_resources['colony_hermes/plugin.yaml']):
                raise ValueError('The recorded draft worker binding changed; reconcile it before refreshing')
            updates.append((target, target.read_bytes(), resources['colony_hermes/plugin.yaml']))
    updated = {**manifest, 'hermes_python':str(python), 'sidecar_python':sys.executable,
        'sidecar_module_root':str(Path(__file__).resolve().parents[1]),
        'adapter_sha256':_resource_digest(resources), 'adapter_binding':binding}
    updates.append((path, before, _json(updated).encode()))
    config_paths = [home/'config.yaml']
    lane = manifest.get('local_work') or {}
    if lane.get('executor') == 'kanban':
        from .setup_local_work import native_root
        config_paths.append(native_root(home)/'profiles'/lane['worker_profile']/'config.yaml')
    for config_path in config_paths:
        config_before, config = _read_hermes_config(config_path)
        if config_path != config_paths[0] and config.get('plugins', {}).get('colony', {}).get('instance_dir') != str(state):
            raise ValueError('The recorded draft worker belongs to another instance')
        if _align_hermes_memory_spill(config):
            updates.append((config_path, config_before, yaml.safe_dump(config, sort_keys=False,
                                                                      allow_unicode=True).encode()))
            print('Hermes memory prefetch spill allowance: max_chars -> 65536 for '+str(config_path))
    copied_change = old_resources is not None and old_resources != resources
    if not copied_change and all(original == after for _, original, after in updates):
        print('Selected adapter already matches; private instance unchanged.')
        return
    completed = []
    try:
        if copied_change:
            staged = Path(tempfile.mkdtemp(prefix='.colony-adapter-', dir=state))
            for name, content in resources.items():
                _private_write(staged/name, content)
            # Hermes and Colony are stopped by the operator's existing lifecycle.
            # Keep the complete old directory for recovery, including caches.
            if _copied_resources(adapter) != old_resources or path.read_bytes() != before:
                raise ValueError('Attachment changed during refresh; retry after reconciling it')
            backup = Path(tempfile.mkdtemp(prefix='adapter-previous-', dir=state)); backup.rmdir()
            os.replace(adapter, backup)
            os.replace(staged, adapter)
        for target, original, after in updates:
            if original != after:
                _atomic_hermes_config_write(target, original, after)
                completed.append((target, original, after))
    except Exception:
        for target, original, after in reversed(completed):
            if not target.is_symlink() and target.read_bytes() == after:
                _atomic_hermes_config_write(target, after, original)
        if backup is not None and backup.exists():
            if not adapter.exists():
                os.replace(backup, adapter)
            elif _copied_resources(adapter) == resources:
                shutil.rmtree(adapter)
                os.replace(backup, adapter)
        raise
    finally:
        if staged is not None and staged.exists():
            shutil.rmtree(staged)
    print('Selected adapter refreshed ('+binding['mode']+'); identity and databases retained.')
    if backup is not None:
        print('Previous copied adapter retained at '+str(backup))
    print('Start this Colony instance and new Hermes processes through their existing lifecycle.')


def run(root_dir=None, args=None):
    from colony_sidecar import setup
    noninteractive = bool(getattr(args, 'non_interactive', False))
    def ask(label, value='', required=False):
        result = setup._prompt(label, str(value or ''), noninteractive).strip()
        if required and not result:
            raise ValueError(label + ' is required')
        if any(ord(char) < 32 for char in result):
            raise ValueError('Configuration values must fit on one line')
        return result
    try:
        receipt_choice = getattr(args, 'whatsapp_read_receipts', None)
        preferences_only = bool(getattr(args, 'preferences_only', False))
        preview = bool(getattr(args, 'preview', False))
        if preferences_only:
            home = setup._resolve_hermes_home(getattr(args, 'hermes_home', None))
            selected_python = None
        else:
            home, selected_python = _select_home(args, ask)
        if preview and not preferences_only:
            raise ValueError('--preview requires --preferences-only')
        if preferences_only:
            if receipt_choice is None:
                raise ValueError('--preferences-only requires --whatsapp-read-receipts on or off')
            if root_dir or any(getattr(args, name, None) for name in (
                    'start', 'refresh_adapter', 'replace_memory_provider', 'local_work',
                    'native_goals', 'model_url', 'model', 'adapter_wheel', 'agent_name',
                    'agent_values', 'timezone', 'quiet_hours', 'contact_name', 'encrypt',
                    'passphrase', 'claim_genesis', 'mcp_harnesses', 'no_harness')) or (
                    getattr(args, 'host_framework', None) not in (None, 'hermes')):
                raise ValueError('--preferences-only cannot be combined with instance or setup options')
            _write_receipt_preference(home, receipt_choice, preview)
            return 0
        state = Path(root_dir or os.environ.get('COLONY_STATE_DIR') or home/'colony').expanduser().resolve()
        if state == home or home.is_relative_to(state):
            raise ValueError('The private Colony directory must not contain the Hermes home')
        # State, credentials and private identity are never written into a checkout.
        for destination in (state, home):
            if any((parent/'.git').is_file() or (parent/'.git'/'HEAD').is_file()
                   for parent in (destination, *destination.parents)):
                raise ValueError('Choose a private Hermes home and instance directory outside Git checkouts')
        config_path = home/'config.yaml'
        original, config = setup._read_hermes_config(config_path)
        if receipt_choice is not None:
            _receipt_preference(config, receipt_choice)  # Validate before setup effects.
        for section in ('plugins', 'memory', 'compression'):
            if section in config and not isinstance(config[section], dict):
                raise ValueError('Hermes plugin, memory and compression settings must be mappings')
        if 'colony' in config.get('plugins', {}) and not isinstance(config['plugins']['colony'], dict):
            raise ValueError('Hermes Colony plugin settings must be a mapping')
        if 'colony' in config.get('plugins', {}).get('disabled', []):
            raise ValueError('Colony is explicitly disabled in this home; resolve that setting before attachment')
        if (home/'colony-memory.json').exists():
            raise ValueError('Existing native Colony settings need an explicit migration; select a new home')
        if (state/'instance.json').exists():
            manifest = json.loads((state/'instance.json').read_text())
            if manifest.get('hermes_home') != str(home):
                raise ValueError('This instance belongs to another Hermes home')
            if config.get('plugins', {}).get('colony', {}).get('instance_dir') != str(state):
                raise ValueError('The Hermes binding changed; restore its saved config or select another instance')
            if getattr(args, 'native_goals', False):
                from dotenv import dotenv_values
                from .setup_native_goals import prepare
                prepare(config, home, native_env=dotenv_values(home/'.env'),
                    observer_env=dotenv_values(state/'.env'), local_work=bool(getattr(args, 'local_work', False)
                        or manifest.get('local_work', {}).get('executor') == 'kanban'),
                    draft_board=manifest.get('local_work', {}).get('board'))
            if getattr(args, 'refresh_adapter', False):
                refresh_adapter(state, args)
            if getattr(args, 'local_work', False):
                # A native registration failure can follow successful attachment.
                # Retry that missing step with the retained role and interpreter.
                if not manifest.get('sidecar_python') or not manifest.get('sidecar_module_root'):
                    raise ValueError('This older instance requires an adapter upgrade before local drafts can be installed')
                from .router.native_policy import planning
                from .setup_local_work import install, verify_tools
                options, _ = asyncio.run(planning(json.loads((state/'.colony-llm-config.json').read_text())))
                verify_tools(options['base_url'], options['model'], options['api_key'])
                install(state)
                print('Accepted local drafts use native Kanban. Restart this Colony instance and Hermes gateway to load the binding.')
            if getattr(args, 'native_goals', False):
                from .setup_native_goals import enable
                enable(state)
            if receipt_choice is not None:
                _write_receipt_preference(home, receipt_choice)
            os.environ['COLONY_STATE_DIR'] = str(state)
            print(f'Existing private instance retained: {state}')
            print(f'Use colony --instance {str(state)!r} start, then status.')
            return 0
        if getattr(args, 'refresh_adapter', False):
            raise ValueError('Adapter refresh requires an existing private instance')
        if state.exists() and any(state.iterdir()):
            raise ValueError('The selected directory has existing state; use its existing configuration or a new private directory')
        if (home/'plugins').is_symlink():
            raise ValueError('Symlinked plugin directories require explicit migration')
        for name in ('colony', 'colony-memory'):
            if (home/'plugins'/name).exists():
                raise ValueError('An existing Colony directory adapter needs an explicit upgrade; choose another home for this installer')
        selected_provider = config.get('memory', {}).get('provider')
        replace_provider = bool(getattr(args, 'replace_memory_provider', False))
        if selected_provider not in (None, '', 'colony', 'colony-memory') and not replace_provider:
            if noninteractive or ask('Another memory provider is selected. Replace only its selection and retain its files? [y/N]', 'N').lower() not in {'y', 'yes'}:
                raise ValueError('Existing provider retained; choose another --hermes-home or explicitly request --replace-memory-provider')
            replace_provider = True
        python = selected_python or _interpreter(getattr(args, 'hermes_python', None))
        resources = _adapter_resources(getattr(args, 'adapter_wheel', None))
        _preflight_outbox(home, resources)
        binding = _adapter_binding(python, resources)
        owner_name = ask('Your name', getattr(args, 'contact_name', None) or os.environ.get('USER', 'Owner'), True)
        agent_name = ask('Agent name', getattr(args, 'agent_name', None) or 'Assistant', True)
        agent_preferences = _agent_preferences(ask, args, config)
        endpoint = _endpoint(ask('Local model API root', getattr(args, 'model_url', None), True))
        local_hosts = _verify_local_endpoint(endpoint)
        model_key = os.environ.get('COLONY_MODEL_API_KEY', '')
        if not noninteractive and not model_key:
            model_key = getpass.getpass('Model API key (blank if not required): ')
        model_key = model_key or 'local-no-key'
        if any(ord(char) < 32 or ord(char) > 126 for char in model_key):
            raise ValueError('Model API key must be a single printable ASCII value')
        model = getattr(args, 'model', None)
        if model:
            model = ask('Model identifier', model, True)
        if not model:
            response = httpx.get(endpoint+'/models', headers={'Authorization': 'Bearer '+model_key}, timeout=5, trust_env=False)
            response.raise_for_status()
            names = [row['id'] for row in response.json().get('data', []) if isinstance(row, dict) and isinstance(row.get('id'), str)]
            if names:
                print('Available models: ' + ', '.join(names[:20]))
            model = ask('Model identifier', names[0] if len(names) == 1 else '', True)
        probe = httpx.post(endpoint+'/chat/completions', headers={'Authorization': 'Bearer '+model_key},
            json={'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 8},
            timeout=30, trust_env=False)
        probe.raise_for_status()
        if not probe.json().get('choices'):
            raise ValueError('The selected endpoint did not return an OpenAI-compatible chat response')
        local_work = bool(getattr(args, 'local_work', False))
        if not noninteractive and not local_work:
            local_work = ask('Run explicitly accepted local summaries in the background? [Y/n]', 'Y').lower() in {'y','yes'}
        native_goals = bool(getattr(args, 'native_goals', False))
        if not noninteractive and not native_goals:
            native_goals = ask('Enable persistent tasks using this Hermes profile and its gateway? [y/N]', 'N').lower() in {'y','yes'}
        if local_work or native_goals:
            from .setup_local_work import verify_tools
            verify_tools(endpoint, model, model_key)
        port = int(getattr(args, 'port', 7777))
        if getattr(args, 'bind', '127.0.0.1') != '127.0.0.1':
            raise ValueError('The local Hermes profile listens on 127.0.0.1; configure remote access separately')
        if not 1 <= port <= 65535 or setup._check_port(port):
            raise ValueError('Choose a free sidecar port between 1 and 65535; no existing process will be stopped')
        url = f'http://127.0.0.1:{port}'
        original_env = (home/'.env').read_bytes() if (home/'.env').exists() else None
        key = secrets.token_urlsafe(32)
        env_updates = {'COLONY_NATIVE_API_KEY': key, 'COLONY_GENERAL_PLUGIN_ACTIVE': '1',
                       'COLONY_MEMORY_WORKER_TOOLS': '0', 'COLONY_MEMORY_TURN_WRITER': 'disabled',
                       'COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY': 'owner_system'}
        fresh_model = not config.get('model')
        if fresh_model:
            env_updates['OPENAI_API_KEY'] = model_key
            env_updates['OPENAI_BASE_URL'] = endpoint
        native_env = _native_environment(original_env, env_updates)
        goal_configuration, goal_details = None, None
        if native_goals:
            from dotenv import dotenv_values
            from .setup_native_goals import prepare
            selected = dict(config)
            if fresh_model:
                selected['model'] = {'provider': 'custom', 'default': model, 'base_url': endpoint}
            goal_configuration, goal_details = prepare(selected, home,
                native_env=dotenv_values(home/'.env'), observer_env={}, local_work=local_work)
        # Every runtime/resource/config preflight above occurs before state creation.
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = Path(tempfile.mkdtemp(prefix='.colony-init-', dir=state.parent))
        try:
            from colony_sidecar.contacts.store import SQLiteContactStore
            from colony_sidecar.contacts.config import ContactsConfig
            async def owner():
                store = SQLiteContactStore(ContactsConfig(sqlite_path=str(staged/'contacts.db')))
                await store.connect()
                try:
                    return await setup.build_owner_contact(store, owner_name)
                finally:
                    await store.close()
            owner_id = asyncio.run(owner())
            from colony_sidecar.util.autonomy_preset import PRESETS
            values = {**PRESETS['passive'], 'COLONY_INSTALL_PROFILE': 'local', 'COLONY_STATE_DIR': str(state),
                'COLONY_SIDECAR_HOST': '127.0.0.1', 'COLONY_SIDECAR_PORT': str(port),
                'COLONY_OWNER_CONTACT_ID': owner_id, 'COLONY_OWNER_NAME': owner_name,
                'COLONY_PERSONA_NAME': agent_name, 'COLONY_CONTACTS_DB': str(state/'contacts.db'),
                'COLONY_API_KEYRING_PATH': str(state/'api-keyring.json'), 'COLONY_API_KEY': '',
                'COLONY_CLIENT_API_KEY': key, 'COLONY_GRAPH_ENABLED': 'false',
                'COLONY_EMBED_PROVIDER': 'skip', 'WORLD_MODEL_BACKEND': 'sqlite',
                'COLONY_AUTONOMY_PRESET': 'passive', 'COLONY_EMBEDDED_WORKER_ENABLED': 'false',
                'COLONY_SOURCE_CLAIMS': 'on'}
            values.update({
                'COLONY_AGENT_VALUES': json.dumps(agent_preferences['values'], ensure_ascii=True),
                'COLONY_AGENT_TIMEZONE': agent_preferences['timezone'],
                'COLONY_AGENT_QUIET_HOURS': agent_preferences['quiet_hours'],
            })
            if goal_details is not None:
                values['COLONY_HERMES_WORK_BOARDS'] = json.dumps(goal_details['boards'], separators=(',', ':'))
            _private_write(staged/'.env', _native_environment(None, values))
            principal = {'principal': 'hermes-local', 'status': 'active', 'viewer_person_id': owner_id,
                'audiences': ['viewer'], 'allow_unscoped_api': False, 'turn_ingress_platforms': ['cli'],
                'scopes': ['context:read', 'memory:read', 'memory:search', 'memory:write', 'turns:write'],
                'credentials': [{'id': 'initial', 'secret': key, 'status': 'active'}]}
            _private_write(staged/'api-keyring.json', _json({'version': 1, 'principals': [principal]}))
            model_configuration = {'provider': 'local', 'baseUrl': endpoint,
                'apiKey': model_key, 'localHosts': local_hosts,
                'models': {name: model for name in ('small', 'medium', 'large')}}
            if local_work:
                from .setup_local_work import planning_configuration
                planning_configuration(model_configuration)
            _private_write(staged/'.colony-llm-config.json', _json(model_configuration))
            for name, content in resources.items():
                _private_write(staged/'adapter'/name, content)
            manifest = {'version': 1, 'hermes_home': str(home), 'hermes_python': str(python),
                'sidecar_python': sys.executable, 'sidecar_module_root': str(Path(__file__).resolve().parents[1]),
                'owner_id': owner_id, 'agent_name': agent_name, 'endpoint': endpoint, 'model': model,
                'agent_preferences': agent_preferences,
                'adapter_sha256': hashlib.sha256(b''.join(name.encode()+resources[name] for name in sorted(resources))).hexdigest(),
                'adapter_binding': binding,
                'profile': 'local', 'status': 'configured_not_behaviorally_verified'}
            _private_write(staged/'instance.json', _json(manifest))
            # Prepare the existing config path with the canonical provider helper.
            prepared_path = staged/'config.yaml'
            candidate = dict(goal_configuration if goal_configuration is not None else config)
            if replace_provider:
                candidate['memory'] = dict(candidate.get('memory') or {})
                candidate['memory']['provider'] = 'colony-memory'
                candidate['memory']['config'] = {}  # Saved original retains incumbent settings.
            _private_write(prepared_path, yaml.safe_dump(candidate, sort_keys=False))
            _, prepared = setup._prepare_hermes_config(prepared_path, url, owner_id)
            candidate = yaml.safe_load(prepared)
            plugin = candidate['plugins']['colony']
            plugin.update(instance_dir=str(state), owner_contact_id=owner_id,
                api_key='${COLONY_NATIVE_API_KEY}', execution_registry_enabled=True,
                attested_system_platforms=['cli'], enabled_action_tools=[], enabled_message_tools=[],
                turn_outbox_path=str(home/'state'/'colony-turn-outbox.sqlite3'))
            candidate['memory']['config'].update(api_key='${COLONY_NATIVE_API_KEY}', turn_writer='disabled')
            enabled = candidate['plugins'].setdefault('enabled', [])
            if not isinstance(enabled, list):
                raise ValueError('Hermes plugins.enabled must be a list')
            if 'colony' not in enabled:
                enabled.append('colony')
            candidate.setdefault('compression', {})['checkpoint_required'] = True
            if fresh_model:
                candidate['model'] = {'provider': 'custom', 'default': model, 'base_url': endpoint}
            if receipt_choice is not None:
                candidate, _ = _receipt_preference(candidate, receipt_choice)
            final_config = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode()
            prepared_path.unlink()
            backups = staged/'hermes-original'
            if original is not None: _private_write(backups/'config.yaml', original)
            if original_env is not None: _private_write(backups/'.env', original_env)
            if state.exists(): state.rmdir()  # Only an empty installer-selected directory is allowed.
            os.replace(staged, state)
        finally:
            if staged.exists(): shutil.rmtree(staged)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        created, replaced = [], []
        try:
            if (home/'.env').is_symlink() or ((home/'.env').read_bytes() if (home/'.env').exists() else None) != original_env:
                raise ValueError('Hermes private environment changed during installation')
            setup._atomic_hermes_config_write(home/'.env', original_env, native_env)
            os.chmod(home/'.env', 0o600)
            replaced.append((home/'.env', original_env, native_env))
            def create(path, value):
                raw = value if isinstance(value, bytes) else value.encode()
                _private_write(path, raw)
                created.append((path, raw))
            if binding['mode'] == 'private-directory':
                for directory, module in (('colony', 'colony_hermes'), ('colony-memory', 'colony_memory')):
                    create(home/'plugins'/directory/'__init__.py', _forwarder(state/'adapter', module, directory == 'colony-memory'))
                    create(home/'plugins'/directory/'plugin.yaml', resources[module+'/plugin.yaml'])
                create(home/'plugins'/'colony-memory'/'cli.py', _forwarder(state/'adapter', 'colony_memory.cli'))
            setup._atomic_hermes_config_write(config_path, original, final_config)
            replaced.append((config_path, original, final_config))
            if not (home/'SOUL.md').exists():
                guiding = ('Guiding values: ' + ', '.join(agent_preferences['values']) + '.\n'
                           if agent_preferences['values'] else '')
                create(home/'SOUL.md', f'# {agent_name}\n\nYou are {agent_name}, the personal assistant of {owner_name}.\n{guiding}Use retained evidence with its provenance; ask about uncertainty.\nYour opinions are revisable interpretations; distinguish them from facts and permissions.\nOwner consent is required for consequential external actions; an existing task-scoped consent covers only its stated scope.\n')
        except Exception:
            # Undo only this installation's exact bytes. Concurrent edits stay
            # intact, with original files still retained in the private state.
            for path, before, after in reversed(replaced):
                if not path.is_symlink() and path.is_file() and path.read_bytes() == after:
                    if before is None:
                        path.unlink()
                    else:
                        setup._atomic_hermes_config_write(path, after, before)
            for path, raw in reversed(created):
                if not path.is_symlink() and path.is_file() and path.read_bytes() == raw:
                    path.unlink()
            for name in ('colony', 'colony-memory'):
                directory = home/'plugins'/name
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            print(f'Attachment failed; prepared state and original files remain in {state}.')
            raise
        os.environ['COLONY_STATE_DIR'] = str(state)
        if local_work:
            from .setup_local_work import install
            install(state)
        print(f'Private agent configured in {home}; state in {state}.')
        print('Adapter loading: ' + binding['mode'] + ' (canonical artifact bytes verified).')
        print('Canonical memory capture and recollection are enabled for new Hermes sessions.')
        print('Hermes hook output spill allowance is at least 65536 characters or already disabled; retrieval budgets are unchanged.')
        print('Source memory, temporal claims, contacts, commitments and self state persist without a graph.')
        print('Graph/vector recall and consequential background work are optional and currently disabled.')
        if local_work:
            print('Accepted local drafts use the native Kanban board and dedicated worker profile.')
            print('Keep the selected Hermes gateway running. Its dispatch ticks refresh the planning role for future attempts.')
        if goal_details is not None:
            from .setup_native_goals import describe
            describe(goal_details)
        print(f'Start: colony --instance {str(state)!r} start --detach')
        print(f'Status: colony --instance {str(state)!r} status')
        print('No existing Hermes process was restarted. Begin a new session to load the adapter.')
        if getattr(args, 'start', False) or (not noninteractive and ask('Start this sidecar now? [Y/n]', 'Y').lower() in {'y','yes'}):
            result = subprocess.run([sys.executable, '-m', 'colony_sidecar', '--instance', str(state), 'start', '--detach'], timeout=60)
            return result.returncode
        return 0
    except (OSError, ValueError, KeyError, httpx.HTTPError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print('Hermes initialization failed: ' + detail)
        return 1

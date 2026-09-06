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
from urllib.parse import urlsplit
import zipfile

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
        'import importlib.metadata,json; import httpx,httpcore,typer,yaml; '
        'from agent.memory_manager import MemoryManager; '
        'from hermes_cli.plugins import get_plugin_manager; '
        'print(json.dumps({"version":importlib.metadata.version("hermes-agent")}))'],
        capture_output=True, text=True, timeout=30)
    if probe.returncode or json.loads(probe.stdout.splitlines()[-1]).get('version') != '0.21.0':
        raise ValueError('Select the Python interpreter of Hermes 0.21.0 with its native dependencies installed')
    return python


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
sources, versions = {}, set()
for ep in selected:
    module = ep.value
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin:
        raise ValueError('Installed adapter package cannot be resolved')
    root = Path(spec.origin).resolve().parent
    sources[module] = str(root)
    versions.add(ep.dist.version)
    names = {name for name in expected if name.startswith(module+'/')}
    actual_python = {module+'/'+str(path.relative_to(root)) for path in root.rglob('*.py')}
    if actual_python != {name for name in names if name.endswith('.py')}:
        raise ValueError('Installed adapter Python files differ from the selected artifact')
    for name in names:
        path = root/name.split('/', 1)[1]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
            raise ValueError('Installed adapter bytes differ from the selected artifact')
if len(versions) != 1:
    raise ValueError('Installed adapter entry-point versions disagree')
print(json.dumps({'mode': 'native-installed', 'version': versions.pop(), 'sources': sources}))
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
        home = setup._resolve_hermes_home(getattr(args, 'hermes_home', None))
        state = Path(root_dir or os.environ.get('COLONY_STATE_DIR') or home/'colony').expanduser().resolve()
        if state == home or home.is_relative_to(state):
            raise ValueError('The private Colony directory must not contain the Hermes home')
        # State, credentials and private identity are never written into a checkout.
        if any((parent/'.git').is_file() or (parent/'.git'/'HEAD').is_file()
               for parent in (state, *state.parents)):
            raise ValueError('Choose a private instance directory outside Git checkouts')
        config_path = home/'config.yaml'
        original, config = setup._read_hermes_config(config_path)
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
            os.environ['COLONY_STATE_DIR'] = str(state)
            print(f'Existing private instance retained: {state}')
            print(f'Use colony --instance {str(state)!r} start, then status.')
            return 0
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
        python = _interpreter(getattr(args, 'hermes_python', None))
        resources = _adapter_resources(getattr(args, 'adapter_wheel', None))
        binding = _adapter_binding(python, resources)
        owner_name = ask('Your name', getattr(args, 'contact_name', None) or os.environ.get('USER', 'Owner'), True)
        agent_name = ask('Agent name', getattr(args, 'agent_name', None) or 'Assistant', True)
        endpoint = _endpoint(ask('Local model API root', getattr(args, 'model_url', None), True))
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
            _private_write(staged/'.env', '\n'.join(k+'='+v for k,v in values.items())+'\n')
            principal = {'principal': 'hermes-local', 'status': 'active', 'viewer_person_id': owner_id,
                'audiences': ['viewer'], 'allow_unscoped_api': False, 'turn_ingress_platforms': ['cli'],
                'scopes': ['context:read', 'memory:read', 'memory:search', 'memory:write', 'turns:write'],
                'credentials': [{'id': 'initial', 'secret': key, 'status': 'active'}]}
            _private_write(staged/'api-keyring.json', _json({'version': 1, 'principals': [principal]}))
            _private_write(staged/'.colony-llm-config.json', _json({'provider': 'local', 'baseUrl': endpoint,
                'apiKey': model_key, 'models': {name: model for name in ('small', 'medium', 'large')}}))
            for name, content in resources.items():
                _private_write(staged/'adapter'/name, content)
            manifest = {'version': 1, 'hermes_home': str(home), 'hermes_python': str(python),
                'owner_id': owner_id, 'agent_name': agent_name, 'endpoint': endpoint, 'model': model,
                'adapter_sha256': hashlib.sha256(b''.join(name.encode()+resources[name] for name in sorted(resources))).hexdigest(),
                'adapter_binding': binding,
                'profile': 'local', 'status': 'configured_not_behaviorally_verified'}
            _private_write(staged/'instance.json', _json(manifest))
            # Prepare the existing config path with the canonical provider helper.
            prepared_path = staged/'config.yaml'
            candidate = dict(config)
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
                candidate['model'] = {'provider': 'openai', 'default': model, 'base_url': endpoint}
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
                create(home/'SOUL.md', f'# {agent_name}\n\nYou are {agent_name}, the personal assistant of {owner_name}.\nUse retained evidence with its provenance; ask about uncertainty.\nOwner consent is required for consequential external actions.\n')
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
        print(f'Private agent configured in {home}; state in {state}.')
        print('Adapter loading: ' + binding['mode'] + ' (canonical artifact bytes verified).')
        print('Canonical memory capture and recollection are enabled for new Hermes sessions.')
        print('Source memory, temporal claims, contacts, commitments and self state persist without a graph.')
        print('Graph/vector recall and consequential background work are optional and currently disabled.')
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

"""Opt in to native task tools on the existing selected Hermes profile."""
import copy
import json
import os
from pathlib import Path

import yaml

from .setup_local_work import board_name, native_root
from .turns.hermes_kanban import _BOARD, _board_path


def prepare(config, home, *, native_env, observer_env, local_work=False, draft_board=None):
    """Return configuration and observation choices without creating native work."""
    candidate = copy.deepcopy(config)
    for key in ('kanban', 'platform_toolsets', 'auxiliary'):
        if key in candidate and not isinstance(candidate[key], dict):
            raise ValueError(f'Hermes {key} must be a mapping before enabling native goals')
    # YAML aliases survive deepcopy; detach each branch this opt-in changes.
    kanban = candidate['kanban'] = dict(candidate.get('kanban', {}))
    disabled = {'0', 'false', 'no', 'off'}
    for environment in (os.environ, native_env):
        if str(environment.get('HERMES_KANBAN_DISPATCH_IN_GATEWAY', '')).strip().lower() in disabled:
            raise ValueError('HERMES_KANBAN_DISPATCH_IN_GATEWAY disables native dispatch; reconcile that existing setting before --native-goals')
    if 'dispatch_in_gateway' in kanban and kanban['dispatch_in_gateway'] is not True:
        raise ValueError('kanban.dispatch_in_gateway is explicitly disabled or invalid; enable it in the selected Hermes config before --native-goals')
    kanban['dispatch_in_gateway'] = True
    platforms = candidate['platform_toolsets'] = dict(candidate.get('platform_toolsets', {}))
    for parent, key, default in ((candidate, 'toolsets', ['hermes-cli']), (platforms, 'cli', ['hermes-cli'])):
        selected = parent.get(key, default)
        if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
            raise ValueError('Hermes toolsets and platform_toolsets.cli must be string lists')
        selected = parent[key] = list(selected)
        if 'kanban' not in selected:
            selected.append('kanban')

    auxiliary = candidate['auxiliary'] = dict(candidate.get('auxiliary', {}))
    judge = auxiliary.get('goal_judge', {})
    if not isinstance(judge, dict):
        raise ValueError('Hermes auxiliary.goal_judge must be a mapping')
    explicit = (str(judge.get('provider') or '').strip().lower() not in ('', 'auto')
                or str(judge.get('model') or '').strip().lower() not in ('', 'auto')
                or any(judge.get(key) for key in ('base_url', 'api_key', 'key_env', 'api_key_env', 'api_mode')))
    judge_mode = 'explicit judge retained' if explicit else 'automatic judge retained: main binding is not explicit'
    main = candidate.get('model')
    if (not explicit and isinstance(main, dict)
            and str(main.get('provider') or '').strip().lower() not in ('', 'auto')
            and str(main.get('default') or '').strip().lower() not in ('', 'auto')):
        judge = dict(judge, provider=main['provider'], model=main['default'])
        for key in ('base_url', 'api_key', 'api_mode'):
            if main.get(key):
                judge[key] = main[key]
        auxiliary['goal_judge'] = judge
        judge_mode = 'judge bound to the main provider and model selected at setup'

    root = native_root(home)
    for environment in (os.environ, native_env, observer_env):
        override = str(environment.get('HERMES_KANBAN_HOME') or '').strip()
        if override and Path(override).expanduser().resolve() != root:
            raise ValueError('HERMES_KANBAN_HOME conflicts with the selected native root; reconcile it before --native-goals')
    configured = observer_env.get('COLONY_HERMES_WORK_BOARDS')
    if 'COLONY_HERMES_WORK_BOARDS' in observer_env:
        try:
            boards = json.loads(configured)
        except (TypeError, ValueError):
            raise ValueError('Existing COLONY_HERMES_WORK_BOARDS must be a JSON board list') from None
        if (not isinstance(boards, list) or not 1 <= len(boards) <= 8
                or any(not isinstance(board, str) or not _BOARD.fullmatch(board) for board in boards)):
            raise ValueError('Existing COLONY_HERMES_WORK_BOARDS must select one to eight board slugs')
        boards = list(dict.fromkeys(boards))
        coverage = 'existing explicit board selection retained'
    else:
        candidates = [str(native_env.get('HERMES_KANBAN_BOARD') or os.environ.get('HERMES_KANBAN_BOARD', '')).strip().lower()]
        pointer = root/'kanban/current'
        if pointer.is_file() and pointer.stat().st_size <= 256:
            candidates.append(pointer.read_text().strip().lower())
        board = next((value for value in candidates if _BOARD.fullmatch(value) and
                      (value == 'default' or (root/'kanban/boards'/value/'board.json').is_file()
                       or (root/'kanban/boards'/value/'kanban.db').is_file())), 'default')
        boards = [board]
        if local_work:
            selected_draft = draft_board or board_name(home)
            if not isinstance(selected_draft, str) or not _BOARD.fullmatch(selected_draft):
                raise ValueError('The installed local-draft board binding is invalid')
            if selected_draft not in boards:
                boards.append(selected_draft)
        coverage = 'current board and optional accepted-draft board selected'
    for environment in (os.environ, native_env, observer_env):
        override = str(environment.get('HERMES_KANBAN_DB') or '').strip()
        if override and (len(boards) != 1 or Path(override).expanduser().resolve() != _board_path(root, boards[0])):
            raise ValueError('HERMES_KANBAN_DB conflicts with the selected observed board; reconcile it before --native-goals')
    details = {'profile': home.name if home.parent.name == 'profiles' else 'default',
               'boards': boards, 'coverage': coverage, 'goal_judge': judge_mode}
    return candidate, details


def describe(details):
    print('Native goals enabled for existing Hermes profile '+details['profile']+'.')
    print('Kanban availability is profile-wide in Hermes; saved channel tool lists and participant authority are retained.')
    print('Observed boards: '+', '.join(details['boards'])+' ('+details['coverage']+').')
    print('Goal completion: '+details['goal_judge']+'. Existing native fallback rules still apply.')
    print('The selected Hermes gateway is required. Colony does not start or restart it; use its existing lifecycle.')
    print('Native tasks retain this profile\'s tools and consent rules; this is not blanket consent for external effects.')


def enable(state):
    """Update an existing attachment through its existing atomic file writer."""
    from dotenv import dotenv_values
    from .setup import _atomic_hermes_config_write
    state = Path(state)
    manifest = json.loads((state/'instance.json').read_text())
    home = Path(manifest['hermes_home'])
    config_path, env_path = home/'config.yaml', state/'.env'
    config_before, env_before = config_path.read_bytes(), env_path.read_bytes()
    observer_env = dotenv_values(env_path)
    candidate, details = prepare(yaml.safe_load(config_before), home,
        native_env=dotenv_values(home/'.env'), observer_env=observer_env,
        local_work=manifest.get('local_work', {}).get('executor') == 'kanban',
        draft_board=manifest.get('local_work', {}).get('board'))
    config_after = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode()
    env_after = env_before
    if 'COLONY_HERMES_WORK_BOARDS' not in observer_env:
        suffix = 'COLONY_HERMES_WORK_BOARDS='+json.dumps(details['boards'], separators=(',', ':'))+'\n'
        env_after += (b'\n' if env_before and not env_before.endswith(b'\n') else b'')+suffix.encode()
    _atomic_hermes_config_write(config_path, config_before, config_after)
    try:
        _atomic_hermes_config_write(env_path, env_before, env_after)
    except Exception:
        if config_path.read_bytes() == config_after:
            _atomic_hermes_config_write(config_path, config_after, config_before)
        raise
    describe(details)

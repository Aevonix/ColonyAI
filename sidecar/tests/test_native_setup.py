"""Guided native init preserves profiles and creates one private scoped instance."""
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import httpx
import pytest
import yaml

from colony_sidecar import setup, setup_hermes
from colony_sidecar.util.instance import load_environment


def artifact(tmp_path):
    root = Path(__file__).resolve().parents[2]
    wheel = tmp_path/'adapter.whl'
    with zipfile.ZipFile(wheel, 'w') as output:
        for package, source in [('colony_hermes', root/'plugins/hermes-plugin'), ('colony_memory', root/'plugins/colony-memory')]:
            for path in source.rglob('*'):
                if path.is_file() and path.suffix in {'.py', '.yaml', '.md'} and '__pycache__' not in path.parts:
                    output.write(path, package+'/'+str(path.relative_to(source)))
    return wheel


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.delenv('COLONY_STATE_DIR', raising=False)
    monkeypatch.delenv('COLONY_SKIP_DOTENV', raising=False)
    monkeypatch.setattr(setup_hermes, '_interpreter', lambda value: Path('/fixture/python'))
    monkeypatch.setattr(setup_hermes, '_adapter_binding', lambda *args: {'mode': 'private-directory'})
    monkeypatch.setattr(setup, '_check_port', lambda port: False)
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: httpx.Response(200,
        request=httpx.Request('POST', 'http://test'), json={'choices': [{'message': {'content': 'OK'}}]}))
    return SimpleNamespace(non_interactive=True, hermes_home=str(tmp_path/'home'),
        contact_name='Existing Owner', agent_name='Orion', model_url='http://127.0.0.1:8123/v1',
        model='fixture-model', adapter_wheel=str(artifact(tmp_path)), port=8877, start=False)


def test_new_private_instance_uses_canonical_resources_and_scoped_authority(args, tmp_path, monkeypatch):
    home = Path(args.hermes_home)
    assert setup.run_init(None, args) == 0
    state = home/'colony'
    config = yaml.safe_load((home/'config.yaml').read_text())
    env = setup._load_existing_env(state/'.env')
    assert config['memory']['provider'] == 'colony-memory'
    assert config['plugins']['enabled'] == ['colony']
    assert config['plugins']['colony']['instance_dir'] == str(state)
    assert config['plugins']['colony']['enabled_action_tools'] == []
    assert 'toolsets' not in config and 'kanban' not in config
    assert 'COLONY_HERMES_WORK_BOARDS' not in env
    keyring = json.loads((state/'api-keyring.json').read_text())
    principal = keyring['principals'][0]
    assert principal['allow_unscoped_api'] is False
    assert principal['viewer_person_id'] == env['COLONY_OWNER_CONTACT_ID']
    assert principal['turn_ingress_platforms'] == ['cli']
    assert 'api:access' not in principal['scopes']
    assert env['COLONY_API_KEY'] == '' and env['COLONY_CLIENT_API_KEY'] == principal['credentials'][0]['secret']
    assert principal['credentials'][0]['secret'] not in (home/'config.yaml').read_text()
    assert (home/'SOUL.md').read_text().startswith('# Orion')
    assert (state/'adapter/colony_hermes/evidence.py').is_file()
    assert (state/'api-keyring.json').stat().st_mode & 0o777 == 0o600
    # Same selected home finds this instance without a separate global pointer.
    monkeypatch.delenv('COLONY_STATE_DIR', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('COLONY_API_KEY', 'foreign-inherited-key')
    load_environment()
    assert setup.os.environ['COLONY_STATE_DIR'] == str(state)
    assert setup.os.environ['COLONY_API_KEY'] == ''
    before = (home/'config.yaml').read_bytes(), (state/'api-keyring.json').read_bytes()
    assert setup.run_init(None, args) == 0
    assert before == ((home/'config.yaml').read_bytes(), (state/'api-keyring.json').read_bytes())


def test_attach_preserves_existing_identity_channels_model_and_unrelated_env(args):
    home = Path(args.hermes_home); home.mkdir()
    original = {'model': {'default': 'existing-model', 'provider': 'existing'},
                'platforms': {'terminal': {'enabled': True}}, 'plugins': {'enabled': ['other']}}
    (home/'config.yaml').write_text(yaml.safe_dump(original))
    (home/'SOUL.md').write_text('Existing private identity')
    (home/'.env').write_text('OTHER_PRIVATE_KEY=keep\n')
    assert setup.run_init(None, args) == 0
    updated = yaml.safe_load((home/'config.yaml').read_text())
    assert updated['model'] == original['model'] and updated['platforms'] == original['platforms']
    assert updated['plugins']['enabled'] == ['other', 'colony']
    assert (home/'SOUL.md').read_text() == 'Existing private identity'
    assert (home/'.env').read_text().startswith('OTHER_PRIVATE_KEY=keep\n')
    assert yaml.safe_load((home/'colony/hermes-original/config.yaml').read_text()) == original


def test_native_goals_opt_in_and_existing_instance_reentry_preserve_state(args, monkeypatch, capsys):
    from colony_sidecar import setup_local_work
    monkeypatch.setattr(setup_local_work, 'verify_tools', lambda *a: None)
    args.native_goals = True
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'colony'
    config = yaml.safe_load((home/'config.yaml').read_text())
    assert config['toolsets'] == ['hermes-cli', 'kanban']
    assert config['platform_toolsets']['cli'] == ['hermes-cli', 'kanban']
    assert config['kanban']['dispatch_in_gateway'] is True
    assert config['auxiliary']['goal_judge'] == {
        'provider':'openai', 'model':args.model, 'base_url':args.model_url}
    assert json.loads(setup._load_existing_env(state/'.env')['COLONY_HERMES_WORK_BOARDS']) == ['default']
    assert not (home/'kanban.db').exists() and not (home/'profiles').exists()
    assert 'Colony does not start or restart it' in capsys.readouterr().out
    paths = [home/'config.yaml', home/'SOUL.md', home/'.env', state/'.env', state/'contacts.db',
             state/'instance.json', state/'api-keyring.json']
    before = {path:path.read_bytes() for path in paths}
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Reentry made a model call'))
    assert setup.run_init(None, args) == 0
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize('conflict', ['yaml', 'home_env', 'process_env'])
def test_native_goals_dispatch_conflict_precedes_attachment(args, monkeypatch, conflict):
    from colony_sidecar import setup_local_work
    monkeypatch.setattr(setup_local_work, 'verify_tools', lambda *a: None)
    args.native_goals = True
    home = Path(args.hermes_home); home.mkdir()
    (home/'config.yaml').write_text('kanban: {dispatch_in_gateway: false}\n' if conflict == 'yaml' else '{}\n')
    if conflict == 'home_env':
        (home/'.env').write_text('HERMES_KANBAN_DISPATCH_IN_GATEWAY=false\n')
    if conflict == 'process_env':
        monkeypatch.setenv('HERMES_KANBAN_DISPATCH_IN_GATEWAY', 'off')
    before = {p:p.read_bytes() for p in home.iterdir()}
    assert setup.run_init(None, args) == 1
    assert before == {p:p.read_bytes() for p in home.iterdir()}


def test_native_goals_preserve_explicit_tools_judge_and_board_selection(args, monkeypatch):
    from colony_sidecar.setup_native_goals import enable
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'colony'
    config = yaml.safe_load((home/'config.yaml').read_text())
    judge = {'provider':'custom:deliberate', 'model':'judge-model', 'timeout':97, 'extra_body':{'mode':'retained'}}
    config.update(toolsets=['file'], platform_toolsets={'cli':['file'], 'telegram':['web']},
                  auxiliary={'goal_judge':judge, 'vision':{'provider':'existing'}})
    (home/'config.yaml').write_text(yaml.safe_dump(config))
    with (state/'.env').open('a') as stream:
        stream.write('COLONY_HERMES_WORK_BOARDS=["existing"]\n')
    paths = [home/'SOUL.md', home/'.env', state/'contacts.db', state/'instance.json', state/'api-keyring.json']
    before = {path:path.read_bytes() for path in paths}
    enable(state)
    after = yaml.safe_load((home/'config.yaml').read_text())
    assert after['toolsets'] == ['file', 'kanban']
    assert after['platform_toolsets'] == {'cli':['file', 'kanban'], 'telegram':['web']}
    assert after['auxiliary'] == config['auxiliary'] and after['model'] == config['model']
    assert json.loads(setup._load_existing_env(state/'.env')['COLONY_HERMES_WORK_BOARDS']) == ['existing']
    assert all(path.read_bytes() == data for path, data in before.items())


def test_native_goals_select_current_and_exact_draft_board_without_creating_boards(tmp_path):
    from colony_sidecar.setup_native_goals import prepare
    from colony_sidecar.setup_local_work import board_name
    home = tmp_path/'root/profiles/orion'
    current = tmp_path/'root/kanban/current'; current.parent.mkdir(parents=True)
    current.write_text('OPERATIONS\n')
    marker = current.parent/'boards/operations/board.json'; marker.parent.mkdir(parents=True); marker.write_text('{}')
    config = {'model':{'provider':'custom:local', 'default':'processor'},
              'auxiliary':{'goal_judge':{'provider':'auto', 'model':'auto', 'timeout':91}}}
    updated, details = prepare(config, home, native_env={}, observer_env={}, local_work=True)
    assert details['profile'] == 'orion' and details['boards'] == ['operations', board_name(home)]
    assert updated['auxiliary']['goal_judge'] == {'provider':'custom:local','model':'processor','timeout':91}
    assert not home.exists() and not (marker.parent.parent/board_name(home)).exists()
    _, details = prepare(config, home, native_env={}, observer_env={}, local_work=True,
                         draft_board='retained-draft-board')
    assert details['boards'] == ['operations', 'retained-draft-board']


def test_native_goals_environment_write_failure_restores_config(args, monkeypatch):
    from colony_sidecar.setup_native_goals import enable
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'colony'
    before = (home/'config.yaml').read_bytes(), (state/'.env').read_bytes()
    write = setup._atomic_hermes_config_write
    def fail_environment(path, previous, updated):
        if path == state/'.env':
            raise OSError('Disposable write failure')
        write(path, previous, updated)
    monkeypatch.setattr(setup, '_atomic_hermes_config_write', fail_environment)
    with pytest.raises(OSError):
        enable(state)
    assert before == ((home/'config.yaml').read_bytes(), (state/'.env').read_bytes())


def test_native_goals_detach_yaml_aliases_before_changing_selected_branches(tmp_path):
    from colony_sidecar.setup_native_goals import prepare
    config = yaml.safe_load('''
model: {provider: openai, default: selected-main}
toolsets: &tools [file]
platform_toolsets: &platforms {cli: *tools, telegram: *tools}
other_platforms: *platforms
kanban: &dispatch {}
other_dispatch: *dispatch
auxiliary: &aux {goal_judge: {provider: auto}}
other_aux: *aux
''')
    before = yaml.safe_dump(config)
    updated, _ = prepare(config, tmp_path/'home', native_env={}, observer_env={})
    assert updated['toolsets'] == updated['platform_toolsets']['cli'] == ['file', 'kanban']
    assert updated['platform_toolsets']['telegram'] == ['file']
    assert updated['other_platforms'] == {'cli':['file'], 'telegram':['file']}
    assert updated['other_dispatch'] == {}
    assert updated['other_aux'] == {'goal_judge':{'provider':'auto'}}
    assert updated['auxiliary']['goal_judge'] == {'provider':'openai','model':'selected-main'}
    assert yaml.safe_dump(config) == before


def _changed_adapter(args, monkeypatch):
    current = setup_hermes._adapter_resources(args.adapter_wheel)
    candidate = {**current, 'colony_hermes/qualified_update.py':b'VALUE = "new release"\n',
        'colony_hermes/plugin.yaml':current['colony_hermes/plugin.yaml']+b'\n# Selected new release\n'}
    monkeypatch.setattr(setup_hermes, '_adapter_resources', lambda wheel: candidate)
    args.refresh_adapter = True
    return current, candidate


def test_explicit_refresh_preserves_state_and_worker_and_is_idempotent(args, monkeypatch):
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'colony'
    current, candidate = _changed_adapter(args, monkeypatch)
    # Existing known worker, with independent config and state, must remain bound.
    worker = home/'profiles/colony-drafts'; plugin = worker/'plugins/colony'
    plugin.mkdir(parents=True)
    (plugin/'__init__.py').write_text(setup_hermes._forwarder(state/'adapter', 'colony_hermes'))
    (plugin/'plugin.yaml').write_bytes(current['colony_hermes/plugin.yaml'])
    (worker/'config.yaml').write_text(yaml.safe_dump({'plugins':{'colony':{'instance_dir':str(state)}},
        'model':{'default':'retain-model'},'unrelated':{'keep':[1,2]}}))
    manifest = json.loads((state/'instance.json').read_text())
    manifest['local_work'] = {'executor':'kanban','worker_profile':'colony-drafts','board':'colony-drafts'}
    (state/'instance.json').write_text(json.dumps(manifest))
    (state/'retained-memory.db').write_bytes(b'unchanged private fixture state')
    paths = [home/'SOUL.md', home/'config.yaml', home/'.env', state/'.env',
        state/'api-keyring.json', state/'retained-memory.db', worker/'config.yaml', plugin/'__init__.py']
    before = {path:path.read_bytes() for path in paths}
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Refresh made an inference call'))
    assert setup.run_init(None, args) == 0
    assert all(path.read_bytes()==raw for path,raw in before.items())
    assert setup_hermes._copied_resources(state/'adapter') == candidate
    assert (plugin/'plugin.yaml').read_bytes() == candidate['colony_hermes/plugin.yaml']
    assert json.loads((state/'instance.json').read_text())['local_work']==manifest['local_work']
    backups = list(state.glob('adapter-previous-*')); assert len(backups)==1
    assert setup_hermes._copied_resources(backups[0]) == current
    manifest_before=(state/'instance.json').read_bytes();mtime=(state/'instance.json').stat().st_mtime_ns
    assert setup.run_init(None, args)==0
    assert list(state.glob('adapter-previous-*'))==backups
    assert (state/'instance.json').read_bytes()==manifest_before
    assert (state/'instance.json').stat().st_mtime_ns==mtime


def test_refresh_rejects_local_edits_before_mutation(args, monkeypatch):
    assert setup.run_init(None, args)==0
    home=Path(args.hermes_home);state=home/'colony'
    _changed_adapter(args, monkeypatch)
    edited=state/'adapter/colony_hermes/evidence.py';edited.write_bytes(edited.read_bytes()+b'\n# Local change\n')
    before={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
    assert setup.run_init(None,args)==1
    assert before=={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}


def test_refresh_write_failure_restores_previous_adapter(args, monkeypatch):
    assert setup.run_init(None,args)==0
    home=Path(args.hermes_home);state=home/'colony'
    current,_=_changed_adapter(args,monkeypatch)
    before=(state/'instance.json').read_bytes(),(home/'plugins/colony/plugin.yaml').read_bytes()
    write=setup._atomic_hermes_config_write
    def fail_manifest(path,original,updated):
        if path==state/'instance.json':raise OSError('Disposable manifest write failure')
        write(path,original,updated)
    monkeypatch.setattr(setup,'_atomic_hermes_config_write',fail_manifest)
    assert setup.run_init(None,args)==1
    assert setup_hermes._copied_resources(state/'adapter')==current
    assert before==((state/'instance.json').read_bytes(),(home/'plugins/colony/plugin.yaml').read_bytes())


def test_refresh_installed_package_updates_binding_without_another_copy(args, monkeypatch):
    binding={'mode':'native-installed','version':'old','sources':{'colony_hermes':'/native/package'}}
    monkeypatch.setattr(setup_hermes,'_adapter_binding',lambda *a:binding)
    assert setup.run_init(None,args)==0
    home=Path(args.hermes_home);state=home/'colony'
    current,_=_changed_adapter(args,monkeypatch)
    binding={**binding,'version':'new'}
    assert setup.run_init(None,args)==0
    assert setup_hermes._copied_resources(state/'adapter')==current
    assert not list(state.glob('adapter-previous-*')) and not (home/'plugins').exists()
    assert json.loads((state/'instance.json').read_text())['adapter_binding']==binding


@pytest.mark.parametrize('original_mode', ['private-directory', 'native-installed'])
def test_refresh_rejects_changed_loading_topology_without_writing(args, monkeypatch, original_mode):
    binding={'mode':original_mode}
    monkeypatch.setattr(setup_hermes,'_adapter_binding',lambda *a:binding)
    assert setup.run_init(None,args)==0
    home=Path(args.hermes_home)
    _changed_adapter(args,monkeypatch)
    before={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
    binding={'mode':'native-installed' if original_mode=='private-directory' else 'private-directory'}
    assert setup.run_init(None,args)==1
    assert before=={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}


@pytest.mark.parametrize('address', ['127.0.0.1', '203.0.113.10'])
def test_selected_hostname_is_bound_for_runtime_routing(args, monkeypatch, address):
    from colony_sidecar.router.router import LLMRouter
    args.model_url = 'http://model.lan:8123/v1'
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 8123))])
    result = setup.run_init(None, args)
    if address != '127.0.0.1':
        assert result == 1 and not Path(args.hermes_home).exists()
        return
    assert result == 0
    config = json.loads((Path(args.hermes_home)/'colony/.colony-llm-config.json').read_text())
    assert config['localHosts'] == ['model.lan']
    router = LLMRouter(tiers={}, self_learner=object())
    router.configure(config)
    assert router.function_config(context={'function_role': 'extraction'}).base_url == args.model_url


@pytest.mark.parametrize('git_kind', ['directory', 'worktree_file'])
def test_private_home_cannot_enter_checkout_with_separate_state(args, tmp_path, git_kind):
    repository = tmp_path/'checkout'; repository.mkdir()
    if git_kind == 'directory':
        (repository/'.git').mkdir()
        (repository/'.git/HEAD').write_text('ref: refs/heads/main\n')
    else:
        (repository/'.git').write_text('gitdir: /unused-neutral-worktree\n')
    args.hermes_home = str(repository/'profile')
    state = tmp_path/'separate-private-state'
    assert setup.run_init(str(state), args) == 1
    assert not (repository/'profile').exists() and not state.exists()


@pytest.mark.parametrize('failure', ['endpoint', 'provider', 'artifact', 'malformed_config', 'installed_mismatch'])
def test_preflight_failure_leaves_selected_home_and_state_unchanged(args, monkeypatch, failure):
    home = Path(args.hermes_home); home.mkdir()
    (home/'SOUL.md').write_text('Keep me')
    (home/'config.yaml').write_text('plugins: {enabled: []}\n')
    if failure == 'endpoint': args.model_url = 'http://localhost:bad'
    if failure == 'provider': (home/'config.yaml').write_text('memory: {provider: other}\n')
    if failure == 'artifact': args.adapter_wheel = str(home/'missing.whl')
    if failure == 'malformed_config': (home/'config.yaml').write_text('model: 1\nmodel: 2\n')
    if failure == 'installed_mismatch':
        def mismatch(*a): raise ValueError('Installed adapter mismatch')
        monkeypatch.setattr(setup_hermes, '_adapter_binding', mismatch)
    before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    assert setup.run_init(None, args) == 1
    assert before == {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*') if p.is_file()}


def test_explicit_provider_replacement_retains_original(args):
    home = Path(args.hermes_home); home.mkdir()
    original = b'memory:\n  provider: other\n  config: {private_setting: retained}\n'
    (home/'config.yaml').write_bytes(original)
    args.replace_memory_provider = True
    assert setup.run_init(None, args) == 0
    assert (home/'colony/hermes-original/config.yaml').read_bytes() == original
    assert yaml.safe_load((home/'config.yaml').read_text())['memory']['provider'] == 'colony-memory'


def test_attachment_failure_restores_exact_existing_home(args, monkeypatch):
    home = Path(args.hermes_home); home.mkdir()
    original = b'plugins: {enabled: [other]}\n'
    (home/'config.yaml').write_bytes(original)
    (home/'.env').write_bytes(b'EXISTING_KEY=retained\n')
    (home/'SOUL.md').write_text('Existing identity')
    write = setup_hermes._private_write
    def failed_write(path, content):
        if path == home/'plugins'/'colony'/'plugin.yaml':
            raise OSError('fixture write failure')
        return write(path, content)
    monkeypatch.setattr(setup_hermes, '_private_write', failed_write)
    assert setup.run_init(None, args) == 1
    assert (home/'config.yaml').read_bytes() == original
    assert (home/'.env').read_bytes() == b'EXISTING_KEY=retained\n'
    assert (home/'SOUL.md').read_text() == 'Existing identity'
    assert not (home/'plugins'/'colony').exists()
    assert (home/'colony'/'hermes-original'/'config.yaml').read_bytes() == original


def test_instance_never_uses_another_homes_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.delenv('COLONY_SKIP_DOTENV', raising=False)
    monkeypatch.setenv('COLONY_INSTANCE_SELECTED', '1')
    selected = tmp_path/'missing-selected-instance'
    monkeypatch.setenv('COLONY_STATE_DIR', str(selected))
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path/'.colony').mkdir()
    (tmp_path/'.colony'/'.env').write_text('OTHER_AGENT_ONLY=private\n')
    monkeypatch.delenv('OTHER_AGENT_ONLY', raising=False)
    with pytest.raises(ValueError, match='incomplete'):
        load_environment()
    assert 'OTHER_AGENT_ONLY' not in os.environ


@pytest.mark.parametrize('command', [['start', '--detach'], ['stop']])
@pytest.mark.parametrize('skip_dotenv', ['', '1'])
def test_missing_explicit_instance_never_enters_legacy_process_control(tmp_path, monkeypatch, command, skip_dotenv):
    from colony_sidecar import cli
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.setenv('COLONY_SKIP_DOTENV', skip_dotenv)
    monkeypatch.setattr(cli.sys, 'argv', ['colony', '--instance', str(tmp_path/'typo'), *command])
    monkeypatch.setattr(cli, '_cleanup_orphans', lambda **kw: pytest.fail('Global cleanup invoked'))
    monkeypatch.setattr(cli, '_find_pid_on_port', lambda *a: pytest.fail('Unrelated port probed'))
    monkeypatch.setattr(cli.os, 'kill', lambda *a: pytest.fail('Process signalled'))
    with pytest.raises(ValueError, match='no legacy fallback'):
        cli.main()


def test_local_stop_refuses_reused_pid_and_other_instance_port(tmp_path, monkeypatch, capsys):
    from colony_sidecar import cli
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('COLONY_INSTALL_PROFILE', 'local')
    (tmp_path/'sidecar.pid').write_text('1234')
    (tmp_path/'sidecar-process.json').write_text(json.dumps({'pid':1234,'signature':'original process'}))
    monkeypatch.setattr(cli, '_process_signature', lambda pid: 'different process')
    monkeypatch.setattr(cli.os, 'kill', lambda *a: pytest.fail('Unrelated process signalled'))
    cli._cmd_stop()
    assert 'no process was stopped' in capsys.readouterr().out
    monkeypatch.setattr(setup, '_check_port', lambda port: True)
    monkeypatch.setattr(cli, '_cleanup_orphans', lambda **kw: pytest.fail('Global cleanup called'))
    with pytest.raises(SystemExit) as result:
        cli._cmd_start_daemon('127.0.0.1', 7777, True)
    assert result.value.code == 1


def test_local_status_only_uses_scoped_memory_status(tmp_path, monkeypatch, capsys):
    from colony_sidecar import cli
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('COLONY_INSTALL_PROFILE', 'local')
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'existing-owner')
    monkeypatch.setenv('COLONY_CLIENT_API_KEY', 'fixture-private-key')
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        data = {'status':'ok'} if url.endswith('/health') else {'sources': {'pending':0}}
        return httpx.Response(200, json=data, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    cli._cmd_status()
    assert len(calls) == 2 and calls[-1][0].endswith('/memory/sources/claims/status')
    assert calls[-1][1]['params'] == {'contact_id':'existing-owner'}
    assert 'fixture-private-key' not in capsys.readouterr().out


def test_legacy_state_directory_keeps_global_dotenv_and_launch_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.delenv('COLONY_SKIP_DOTENV', raising=False)
    monkeypatch.delenv('COLONY_INSTANCE_SELECTED', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    state = tmp_path/'.colony'/'data'; state.mkdir(parents=True)
    monkeypatch.setenv('COLONY_STATE_DIR', str(state))
    monkeypatch.setenv('LEGACY_LAUNCH_VALUE', 'launch-value')
    (state.parent/'.env').write_text('LEGACY_LAUNCH_VALUE=file-value\nLEGACY_FILE_ONLY=loaded\n')
    (state/'.env').write_text('WRONG_STATE_ENV=not-selected\n')
    load_environment()
    assert os.environ['COLONY_STATE_DIR'] == str(state)
    assert os.environ['LEGACY_LAUNCH_VALUE'] == 'launch-value'
    assert os.environ['LEGACY_FILE_ONLY'] == 'loaded'
    assert 'WRONG_STATE_ENV' not in os.environ

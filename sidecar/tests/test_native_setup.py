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

from apsimo import setup, setup_hermes
from apsimo.util.instance import load_environment
from apsimo.environment import apply_environment_aliases


@pytest.fixture(autouse=True)
def isolated_platform_environment(monkeypatch):
    monkeypatch.setattr(os, 'environ', {key:value for key,value in os.environ.items()
        if not key.startswith(('APSIMO_', 'COLONY_')) or key == 'COLONY_TEST_HOME'})


@pytest.mark.parametrize('version, supported', [
    ('0.21.0', True), ('0.21.1', True), ('0.20.0', False), ('0.22.0', False),
])
def test_native_interpreter_requires_a_supported_runtime(version, supported, monkeypatch):
    probe = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'version': version})))
    monkeypatch.setattr(setup_hermes.subprocess, 'run', probe)
    if supported:
        assert setup_hermes._interpreter('/selected/python') == Path('/selected/python')
    else:
        with pytest.raises(ValueError, match='Hermes 0.21.0 or 0.21.1'):
            setup_hermes._interpreter('/selected/python')
    assert probe.call_args.args[0][:3] == ['/selected/python', '-I', '-c']
    assert probe.call_args.kwargs['timeout'] == 30


def test_guided_setup_selects_one_native_profile_before_reading_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_HOME', raising=False)
    primary, selected = tmp_path/'default', tmp_path/'named'
    discovery = Mock(return_value=[{'name': 'default', 'path': primary}, {'name': 'work', 'path': selected}])
    monkeypatch.setattr(setup_hermes, '_interpreter', lambda value: Path('/selected/python'))
    monkeypatch.setattr(setup_hermes, '_profile_homes', discovery)
    ask = Mock(return_value=str(selected))
    home, python = setup_hermes._select_home(SimpleNamespace(), ask)
    assert home == selected and python == Path('/selected/python')
    assert ask.call_args.args[1] == str(primary)
    assert not primary.exists() and not selected.exists()


@pytest.mark.parametrize('selection', ['argument', 'environment', 'noninteractive'])
def test_explicit_or_noninteractive_home_does_not_discover_other_profiles(selection, tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_HOME', raising=False)
    args = SimpleNamespace(non_interactive=selection == 'noninteractive')
    home = tmp_path/'selected'
    if selection == 'argument': args.hermes_home = str(home)
    elif selection == 'environment': monkeypatch.setenv('HERMES_HOME', str(home))
    discovery = Mock(side_effect=AssertionError('Do not inspect unrelated profiles'))
    monkeypatch.setattr(setup_hermes, '_profile_homes', discovery)
    ask = Mock(side_effect=AssertionError('Selection is already explicit'))
    selected, interpreter = setup_hermes._select_home(args, ask)
    assert selected == (setup._resolve_hermes_home() if selection == 'noninteractive' else home)
    assert interpreter is None
    discovery.assert_not_called()


def test_profile_discovery_uses_native_live_listing_and_rejects_invalid_output(tmp_path, monkeypatch):
    probe = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps([
        {'name': 'default', 'path': str(tmp_path)}, {'name': 'alias', 'path': str(tmp_path)}])))
    monkeypatch.setattr(setup_hermes.subprocess, 'run', probe)
    assert setup_hermes._profile_homes('/native/python') == [{'name': 'default', 'path': tmp_path}]
    assert probe.call_args.args[0][:4] == ['/native/python', '-I', '-B', '-c']
    assert 'profile_exists(name)' in probe.call_args.args[0][-1]
    probe.return_value.stdout = json.dumps([{'name': 'work', 'path': 'relative/path'}])
    with pytest.raises(ValueError, match='--hermes-home'):
        setup_hermes._profile_homes('/native/python')


def artifact(tmp_path):
    root = Path(__file__).resolve().parents[2]
    wheel = tmp_path/'adapter.whl'
    with zipfile.ZipFile(wheel, 'w') as output:
        for package, source in [('apsimo_hermes', root/'plugins/hermes-plugin'), ('apsimo_memory', root/'plugins/apsimo-memory')]:
            for path in source.rglob('*'):
                if path.is_file() and path.suffix in {'.py', '.yaml', '.md'} and '__pycache__' not in path.parts:
                    output.write(path, package+'/'+str(path.relative_to(source)))
    return wheel


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.delenv('APSIMO_STATE_DIR', raising=False)
    monkeypatch.delenv('COLONY_STATE_DIR', raising=False)
    monkeypatch.delenv('APSIMO_SKIP_DOTENV', raising=False)
    monkeypatch.setattr(setup_hermes, '_interpreter', lambda value: Path('/fixture/python'))
    monkeypatch.setattr(setup_hermes, '_adapter_binding', lambda *args: {'mode': 'private-directory'})
    monkeypatch.setattr(setup, '_check_port', lambda port: False)
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: httpx.Response(200,
        request=httpx.Request('POST', 'http://test'), json={'choices': [{'message': {'content': 'OK'}}]}))
    return SimpleNamespace(non_interactive=True, hermes_home=str(tmp_path/'home'),
        contact_name='Existing Owner', agent_name='Orion', model_url='http://127.0.0.1:8123/v1',
        model='fixture-model', adapter_wheel=str(artifact(tmp_path)), port=8877, start=False)


def test_new_instance_can_keep_channel_disabled_with_receipts_preselected(args):
    home = Path(args.hermes_home)
    home.mkdir(mode=0o700)
    (home/'config.yaml').write_text('whatsapp: {enabled: false}\n')
    args.whatsapp_read_receipts = 'on'
    assert setup.run_init(None, args) == 0
    config = yaml.safe_load((home/'config.yaml').read_text())
    assert config['whatsapp'] == {'enabled': False, 'send_read_receipts': True}
    assert config['plugins']['apsimo']['enabled_message_tools'] == []


def test_new_private_instance_uses_canonical_resources_and_scoped_authority(args, tmp_path, monkeypatch):
    home = Path(args.hermes_home)
    assert setup.run_init(None, args) == 0
    state = home/'apsimo'
    config = yaml.safe_load((home/'config.yaml').read_text())
    env = setup._load_existing_env(state/'.env')
    assert config['memory']['provider'] == 'apsimo-memory'
    assert config['plugins']['enabled'] == ['apsimo']
    assert config['plugins']['apsimo']['instance_dir'] == str(state)
    assert config['plugins']['apsimo']['enabled_action_tools'] == []
    assert 'toolsets' not in config and 'kanban' not in config
    assert 'APSIMO_HERMES_WORK_BOARDS' not in env
    keyring = json.loads((state/'api-keyring.json').read_text())
    principal = keyring['principals'][0]
    assert principal['allow_unscoped_api'] is False
    assert principal['viewer_person_id'] == env['APSIMO_OWNER_CONTACT_ID']
    assert principal['turn_ingress_platforms'] == ['cli']
    assert 'api:access' not in principal['scopes']
    assert env['APSIMO_API_KEY'] == '' and env['APSIMO_CLIENT_API_KEY'] == principal['credentials'][0]['secret']
    assert principal['credentials'][0]['secret'] not in (home/'config.yaml').read_text()
    assert (home/'SOUL.md').read_text().startswith('# Orion')
    assert (state/'adapter/apsimo_hermes/evidence.py').is_file()
    assert (state/'api-keyring.json').stat().st_mode & 0o777 == 0o600
    # Same selected home finds this instance without a separate global pointer.
    monkeypatch.delenv('APSIMO_STATE_DIR', raising=False)
    monkeypatch.delenv('COLONY_STATE_DIR', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('APSIMO_API_KEY', 'foreign-inherited-key')
    load_environment()
    assert setup.os.environ['APSIMO_STATE_DIR'] == str(state)
    assert setup.os.environ['APSIMO_API_KEY'] == ''
    before = (home/'config.yaml').read_bytes(), (state/'api-keyring.json').read_bytes()
    assert setup.run_init(None, args) == 0
    assert before == ((home/'config.yaml').read_bytes(), (state/'api-keyring.json').read_bytes())


def test_writable_ancestor_stops_before_endpoint_probe_or_attachment(args, tmp_path, monkeypatch, capsys):
    shared = tmp_path/'shared-parent'
    shared.mkdir(mode=0o775)
    shared.chmod(0o775)
    private = shared/'private-child'
    private.mkdir(mode=0o700)
    args.hermes_home = str(private/'profile')
    probe = Mock(side_effect=AssertionError('Model endpoint must not be probed'))
    binding = Mock(side_effect=AssertionError('Adapter must not be attached'))
    monkeypatch.setattr(setup_hermes, '_verify_local_endpoint', probe)
    monkeypatch.setattr(setup_hermes, '_adapter_binding', binding)
    assert setup.run_init(None, args) == 1
    output = capsys.readouterr().out
    assert 'private SQLite ancestor is writable by another principal' in output
    assert str(shared) in output and '--hermes-home' in output
    assert 'No permissions were changed' in output
    probe.assert_not_called()
    binding.assert_not_called()
    assert not list(private.iterdir())
    assert shared.stat().st_mode & 0o777 == 0o775
    assert private.stat().st_mode & 0o777 == 0o700


def test_private_path_preflight_does_not_create_outbox_before_safe_install(args, monkeypatch):
    home = Path(args.hermes_home)
    resources = setup_hermes._adapter_resources(args.adapter_wheel)
    setup_hermes._preflight_outbox(home, resources)
    assert not home.exists()
    original_probe = setup_hermes._verify_local_endpoint
    def probe(endpoint):
        assert not home.exists(), 'Preflight must not partially attach before a model probe'
        return original_probe(endpoint)
    monkeypatch.setattr(setup_hermes, '_verify_local_endpoint', probe)
    assert setup.run_init(None, args) == 0
    assert (home/'apsimo'/'instance.json').is_file()
    assert not (home/'state'/'colony-turn-outbox.sqlite3').exists()
    setup_hermes._preflight_outbox(home, resources)
    from test_hermes_turn_outbox import _load_client
    client = _load_client('colony_setup_runtime_path_test')
    path = home/'state'/'colony-turn-outbox.sqlite3'
    client.TurnOutbox(path).prepare()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_attach_preserves_existing_identity_channels_model_and_unrelated_env(args):
    home = Path(args.hermes_home); home.mkdir(mode=0o700)
    original = {'model': {'default': 'existing-model', 'provider': 'existing'},
                'platforms': {'terminal': {'enabled': True}}, 'plugins': {'enabled': ['other']}}
    (home/'config.yaml').write_text(yaml.safe_dump(original))
    (home/'SOUL.md').write_text('Existing private identity')
    (home/'.env').write_text('OTHER_PRIVATE_KEY=keep\n')
    assert setup.run_init(None, args) == 0
    updated = yaml.safe_load((home/'config.yaml').read_text())
    assert updated['model'] == original['model'] and updated['platforms'] == original['platforms']
    assert updated['plugins']['enabled'] == ['other', 'apsimo']
    assert (home/'SOUL.md').read_text() == 'Existing private identity'
    assert (home/'.env').read_text().startswith('OTHER_PRIVATE_KEY=keep\n')
    assert yaml.safe_load((home/'apsimo/hermes-original/config.yaml').read_text()) == original


def test_guiding_values_and_time_preferences_roundtrip_privately(args, monkeypatch):
    args.agent_values = 'Be candid, Respect # evidence, Read "carefully", Literal ${TOKEN}'
    args.timezone = 'Europe/Paris'
    args.quiet_hours = '22:30-07:15'
    monkeypatch.setenv('TOKEN', 'must-not-substitute')
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'apsimo'
    env = setup._load_existing_env(state/'.env')
    expected = ['Be candid', 'Respect # evidence', 'Read "carefully"', 'Literal ${TOKEN}']
    assert json.loads(env['APSIMO_AGENT_VALUES']) == expected
    assert env['APSIMO_AGENT_TIMEZONE'] == 'Europe/Paris'
    assert env['APSIMO_AGENT_QUIET_HOURS'] == '22:30-07:15'
    assert json.loads((state/'instance.json').read_text())['agent_preferences']['values'] == expected
    monkeypatch.setenv('HERMES_HOME', str(home))
    load_environment()
    assert json.loads(os.environ['APSIMO_AGENT_VALUES']) == expected
    before = (state/'.env').read_bytes(), (home/'SOUL.md').read_bytes()
    args.agent_values = 'Replacement must not overwrite an existing identity'
    assert setup.run_init(None, args) == 0
    assert before == ((state/'.env').read_bytes(), (home/'SOUL.md').read_bytes())


@pytest.mark.parametrize('field,value', [('timezone', 'No/Such_Zone'),
    ('quiet_hours', '25:00-07:00'), ('quiet_hours', '08:00-08:00')])
def test_invalid_time_preferences_do_not_partially_attach(args, field, value):
    setattr(args, field, value)
    assert setup.run_init(None, args) == 1
    assert not (Path(args.hermes_home)/'apsimo'/'instance.json').exists()


def test_native_goals_opt_in_and_existing_instance_reentry_preserve_state(args, monkeypatch, capsys):
    from apsimo import setup_local_work
    monkeypatch.setattr(setup_local_work, 'verify_tools', lambda *a: None)
    args.native_goals = True
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'apsimo'
    config = yaml.safe_load((home/'config.yaml').read_text())
    assert config['toolsets'] == ['hermes-cli', 'kanban']
    assert config['platform_toolsets']['cli'] == ['hermes-cli', 'kanban']
    assert config['kanban']['dispatch_in_gateway'] is True
    assert config['auxiliary']['goal_judge'] == {
        'provider':'custom', 'model':args.model, 'base_url':args.model_url}
    assert json.loads(setup._load_existing_env(state/'.env')['APSIMO_HERMES_WORK_BOARDS']) == ['default']
    assert not (home/'kanban.db').exists() and not (home/'profiles').exists()
    assert 'Apsimo does not start or restart it' in capsys.readouterr().out
    paths = [home/'config.yaml', home/'SOUL.md', home/'.env', state/'.env', state/'contacts.db',
             state/'instance.json', state/'api-keyring.json']
    before = {path:path.read_bytes() for path in paths}
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Reentry made a model call'))
    assert setup.run_init(None, args) == 0
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize('conflict', ['yaml', 'home_env', 'process_env'])
def test_native_goals_dispatch_conflict_precedes_attachment(args, monkeypatch, conflict):
    from apsimo import setup_local_work
    monkeypatch.setattr(setup_local_work, 'verify_tools', lambda *a: None)
    args.native_goals = True
    home = Path(args.hermes_home); home.mkdir(mode=0o700)
    (home/'config.yaml').write_text('kanban: {dispatch_in_gateway: false}\n' if conflict == 'yaml' else '{}\n')
    if conflict == 'home_env':
        (home/'.env').write_text('HERMES_KANBAN_DISPATCH_IN_GATEWAY=false\n')
    if conflict == 'process_env':
        monkeypatch.setenv('HERMES_KANBAN_DISPATCH_IN_GATEWAY', 'off')
    before = {p:p.read_bytes() for p in home.iterdir()}
    assert setup.run_init(None, args) == 1
    assert before == {p:p.read_bytes() for p in home.iterdir()}


@pytest.mark.parametrize('location', ['home_env', 'process_env'])
@pytest.mark.parametrize('variable', ['HERMES_KANBAN_HOME', 'HERMES_KANBAN_DB'])
@pytest.mark.parametrize('matching', [False, True])
def test_native_goals_native_path_overrides_match_observation_before_writes(args, monkeypatch, location, variable, matching):
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home)
    args.native_goals = True
    selected = home if variable.endswith('_HOME') else home/'kanban.db'
    override = str(selected if matching else home.parent/'other-native-ledger')
    if location == 'home_env':
        with (home/'.env').open('a') as stream:
            stream.write(variable+'='+override+'\n')
    else:
        monkeypatch.setenv(variable, override)
    before = {p:p.read_bytes() for p in home.rglob('*') if p.is_file()}
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Existing opt-in made a model call'))
    assert setup.run_init(None, args) == (0 if matching else 1)
    if not matching:
        assert before == {p:p.read_bytes() for p in home.rglob('*') if p.is_file()}
    else:
        assert yaml.safe_load((home/'config.yaml').read_text())['kanban']['dispatch_in_gateway'] is True
        assert not (home/'kanban.db').exists()


def test_native_goals_single_database_override_cannot_claim_multiple_boards(tmp_path):
    from apsimo.setup_native_goals import prepare
    home = tmp_path/'home'
    with pytest.raises(ValueError, match='HERMES_KANBAN_DB conflicts'):
        prepare({}, home, native_env={'HERMES_KANBAN_DB':str(home/'kanban.db')},
                observer_env={}, local_work=True)
    _, details = prepare({}, home, native_env={'HERMES_KANBAN_DB':str(home/'kanban.db')},
        observer_env={'APSIMO_HERMES_WORK_BOARDS':'["default","default"]'})
    assert details['boards'] == ['default']
    assert not home.exists()


def test_native_goals_preserve_explicit_tools_judge_and_board_selection(args, monkeypatch):
    from apsimo.setup_native_goals import enable
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'apsimo'
    config = yaml.safe_load((home/'config.yaml').read_text())
    judge = {'provider':'custom:deliberate', 'model':'judge-model', 'timeout':97, 'extra_body':{'mode':'retained'}}
    config.update(toolsets=['file'], platform_toolsets={'cli':['file'], 'telegram':['web']},
                  auxiliary={'goal_judge':judge, 'vision':{'provider':'existing'}})
    (home/'config.yaml').write_text(yaml.safe_dump(config))
    with (state/'.env').open('a') as stream:
        stream.write('APSIMO_HERMES_WORK_BOARDS=["existing"]\n')
    paths = [home/'SOUL.md', home/'.env', state/'contacts.db', state/'instance.json', state/'api-keyring.json']
    before = {path:path.read_bytes() for path in paths}
    enable(state)
    after = yaml.safe_load((home/'config.yaml').read_text())
    assert after['toolsets'] == ['file', 'kanban']
    assert after['platform_toolsets'] == {'cli':['file', 'kanban'], 'telegram':['web']}
    assert after['auxiliary'] == config['auxiliary'] and after['model'] == config['model']
    assert json.loads(setup._load_existing_env(state/'.env')['APSIMO_HERMES_WORK_BOARDS']) == ['existing']
    assert all(path.read_bytes() == data for path, data in before.items())


def test_native_goals_select_current_and_exact_draft_board_without_creating_boards(tmp_path):
    from apsimo.setup_native_goals import prepare
    from apsimo.setup_local_work import board_name
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
    from apsimo.setup_native_goals import enable
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'apsimo'
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
    from apsimo.setup_native_goals import prepare
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
    candidate = {**current, 'apsimo_hermes/qualified_update.py':b'VALUE = "new release"\n',
        'apsimo_hermes/plugin.yaml':current['apsimo_hermes/plugin.yaml']+b'\n# Selected new release\n'}
    monkeypatch.setattr(setup_hermes, '_adapter_resources', lambda wheel: candidate)
    args.refresh_adapter = True
    return current, candidate


def test_explicit_refresh_preserves_state_and_worker_and_is_idempotent(args, monkeypatch):
    assert setup.run_init(None, args) == 0
    home = Path(args.hermes_home); state = home/'apsimo'
    current, candidate = _changed_adapter(args, monkeypatch)
    # Existing known worker, with independent config and state, must remain bound.
    worker = home/'profiles/colony-drafts'; plugin = worker/'plugins/apsimo'
    plugin.mkdir(parents=True)
    (plugin/'__init__.py').write_text(setup_hermes._forwarder(state/'adapter', 'apsimo_hermes'))
    (plugin/'plugin.yaml').write_bytes(current['apsimo_hermes/plugin.yaml'])
    (worker/'config.yaml').write_text(yaml.safe_dump({'plugins':{'apsimo':{'instance_dir':str(state)}},
        'model':{'default':'retain-model'},'unrelated':{'keep':[1,2]},
        'hooks':{'output_spill':{'max_chars':65536}}}))
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
    assert (plugin/'plugin.yaml').read_bytes() == candidate['apsimo_hermes/plugin.yaml']
    assert json.loads((state/'instance.json').read_text())['local_work']==manifest['local_work']
    backups = list(state.glob('adapter-previous-*')); assert len(backups)==1
    assert setup_hermes._copied_resources(backups[0]) == current
    manifest_before=(state/'instance.json').read_bytes();mtime=(state/'instance.json').stat().st_mtime_ns
    assert setup.run_init(None, args)==0
    assert list(state.glob('adapter-previous-*'))==backups
    assert (state/'instance.json').read_bytes()==manifest_before
    assert (state/'instance.json').stat().st_mtime_ns==mtime


def test_explicit_refresh_aligns_old_spill_allowance_without_changing_adapter(args, monkeypatch, capsys):
    assert setup.run_init(None, args) == 0
    home=Path(args.hermes_home);state=home/'apsimo';path=home/'config.yaml'
    config=yaml.safe_load(path.read_text());config['hooks']={'output_spill':{'max_chars':10000,'preview_tail':200},'retained':{'x':1}}
    path.write_text(yaml.safe_dump(config));original=path.read_bytes()
    adapter=setup_hermes._copied_resources(state/'adapter')
    worker=home/'profiles/colony-drafts';(worker/'plugins/apsimo').mkdir(parents=True)
    (worker/'plugins/apsimo/__init__.py').write_text(setup_hermes._forwarder(state/'adapter','apsimo_hermes'))
    (worker/'plugins/apsimo/plugin.yaml').write_bytes(adapter['apsimo_hermes/plugin.yaml'])
    worker_config={'plugins':{'apsimo':{'instance_dir':str(state)}},'hooks':{'output_spill':{'preview_head':123}}}
    (worker/'config.yaml').write_text(yaml.safe_dump(worker_config))
    manifest=json.loads((state/'instance.json').read_text());manifest['local_work']={'executor':'kanban','worker_profile':'colony-drafts'}
    (state/'instance.json').write_text(json.dumps(manifest))
    identity=(home/'SOUL.md').read_bytes();args.refresh_adapter=True
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Refresh made an inference call'))
    assert setup.run_init(None,args)==0
    changed=yaml.safe_load(path.read_text());config['hooks']['output_spill']['max_chars']=65536
    assert changed==config and setup_hermes._copied_resources(state/'adapter')==adapter
    assert (home/'SOUL.md').read_bytes()==identity
    worker_config['hooks']['output_spill']['max_chars']=65536
    assert yaml.safe_load((worker/'config.yaml').read_text())==worker_config
    assert any(p.read_bytes()==original for p in home.glob('.config.yaml.colony-backup-*'))
    assert 'max_chars -> 65536' in capsys.readouterr().out
    before=path.read_bytes();assert setup.run_init(None,args)==0 and path.read_bytes()==before


def test_worker_profile_creation_and_role_refresh_align_memory_spill(tmp_path, monkeypatch):
    from apsimo import setup_local_work as local
    state=tmp_path/'apsimo';state.mkdir();home=tmp_path/'hermes'
    worker=home/'profiles/colony-drafts';worker.mkdir(parents=True)
    config=local.worker_configuration({}, {}, {'instance_dir':str(state)}, {})
    assert config['hooks']['output_spill']['max_chars']==65536
    config['hooks']={'output_spill':{'max_chars':10000,'preview_head':321}}
    path=worker/'config.yaml';path.write_text(yaml.safe_dump(config))
    (state/'instance.json').write_text(json.dumps({'hermes_home':str(home),'local_work':{'executor':'kanban','worker_profile':'colony-drafts'}}))
    monkeypatch.setattr(local,'model_configuration',lambda *a,**k: ({'model':{'default':'changed'}},{'role':'planning'}))
    local.refresh_role(state)
    after=yaml.safe_load(path.read_text())
    assert after['hooks']['output_spill']=={'max_chars':65536,'preview_head':321}
    assert after['model']=={'default':'changed'}
    after['hooks']['output_spill']={'enabled':False,'max_chars':10000}
    path.write_text(yaml.safe_dump(after));local.refresh_role(state)
    assert yaml.safe_load(path.read_text())==after


def test_review_setup_is_opt_in_and_upgrade_preserves_selection(args, monkeypatch):
    from apsimo import setup_native_reviews, setup_local_work
    calls = []
    monkeypatch.setattr(setup_native_reviews, 'configure', lambda state, **kw: calls.append((state, kw)))
    monkeypatch.setattr(setup_local_work, 'verify_tools', lambda *a: None)
    assert setup.run_init(None, args) == 0
    assert calls == []
    home = Path(args.hermes_home); state = home/'apsimo'
    args.native_reviews = True
    assert setup.run_init(None, args) == 0
    assert calls == [(state, {'install': True})]
    path = home/'config.yaml'; config = yaml.safe_load(path.read_text())
    config['plugins']['apsimo']['native_reviews'] = {'enabled': True, 'instance_dir': str(state)}
    path.write_text(yaml.safe_dump(config))
    args.native_reviews = False; args.refresh_adapter = True
    monkeypatch.setattr(setup_hermes, 'refresh_adapter', lambda *a: None)
    assert setup.run_init(None, args) == 0
    assert len(calls) == 2
    config['plugins']['apsimo']['native_reviews']['enabled'] = False
    path.write_text(yaml.safe_dump(config))
    assert setup.run_init(None, args) == 0
    assert len(calls) == 2


def test_refresh_rejects_local_edits_before_mutation(args, monkeypatch):
    assert setup.run_init(None, args)==0
    home=Path(args.hermes_home);state=home/'apsimo'
    _changed_adapter(args, monkeypatch)
    edited=state/'adapter/apsimo_hermes/evidence.py';edited.write_bytes(edited.read_bytes()+b'\n# Local change\n')
    before={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
    assert setup.run_init(None,args)==1
    assert before=={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}


def test_refresh_write_failure_restores_previous_adapter(args, monkeypatch):
    assert setup.run_init(None,args)==0
    home=Path(args.hermes_home);state=home/'apsimo'
    current,_=_changed_adapter(args,monkeypatch)
    before=(state/'instance.json').read_bytes(),(home/'plugins/apsimo/plugin.yaml').read_bytes()
    write=setup._atomic_hermes_config_write
    def fail_manifest(path,original,updated):
        if path==state/'instance.json':raise OSError('Disposable manifest write failure')
        write(path,original,updated)
    monkeypatch.setattr(setup,'_atomic_hermes_config_write',fail_manifest)
    assert setup.run_init(None,args)==1
    assert setup_hermes._copied_resources(state/'adapter')==current
    assert before==((state/'instance.json').read_bytes(),(home/'plugins/apsimo/plugin.yaml').read_bytes())


def test_refresh_installed_package_updates_binding_without_another_copy(args, monkeypatch):
    binding={'mode':'native-installed','version':'old','sources':{'apsimo_hermes':'/native/package'}}
    monkeypatch.setattr(setup_hermes,'_adapter_binding',lambda *a:binding)
    assert setup.run_init(None,args)==0
    home=Path(args.hermes_home);state=home/'apsimo'
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
    from apsimo.router.router import LLMRouter
    args.model_url = 'http://model.lan:8123/v1'
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 8123))])
    result = setup.run_init(None, args)
    if address != '127.0.0.1':
        assert result == 1 and not Path(args.hermes_home).exists()
        return
    assert result == 0
    config = json.loads((Path(args.hermes_home)/'apsimo/.colony-llm-config.json').read_text())
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
    home = Path(args.hermes_home); home.mkdir(mode=0o700)
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
    home = Path(args.hermes_home); home.mkdir(mode=0o700)
    original = b'memory:\n  provider: other\n  config: {private_setting: retained}\n'
    (home/'config.yaml').write_bytes(original)
    args.replace_memory_provider = True
    assert setup.run_init(None, args) == 0
    assert (home/'apsimo/hermes-original/config.yaml').read_bytes() == original
    assert yaml.safe_load((home/'config.yaml').read_text())['memory']['provider'] == 'apsimo-memory'


def test_attachment_failure_restores_exact_existing_home(args, monkeypatch):
    home = Path(args.hermes_home); home.mkdir(mode=0o700)
    original = b'plugins: {enabled: [other]}\n'
    (home/'config.yaml').write_bytes(original)
    (home/'.env').write_bytes(b'EXISTING_KEY=retained\n')
    (home/'SOUL.md').write_text('Existing identity')
    write = setup_hermes._private_write
    def failed_write(path, content):
        if path == home/'plugins'/'apsimo'/'plugin.yaml':
            raise OSError('fixture write failure')
        return write(path, content)
    monkeypatch.setattr(setup_hermes, '_private_write', failed_write)
    assert setup.run_init(None, args) == 1
    assert (home/'config.yaml').read_bytes() == original
    assert (home/'.env').read_bytes() == b'EXISTING_KEY=retained\n'
    assert (home/'SOUL.md').read_text() == 'Existing identity'
    assert not (home/'plugins'/'apsimo').exists()
    assert (home/'apsimo'/'hermes-original'/'config.yaml').read_bytes() == original


def test_instance_never_uses_another_homes_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.delenv('APSIMO_SKIP_DOTENV', raising=False)
    monkeypatch.setenv('APSIMO_INSTANCE_SELECTED', '1')
    selected = tmp_path/'missing-selected-instance'
    monkeypatch.setenv('APSIMO_STATE_DIR', str(selected))
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
    from apsimo import cli
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.setenv('APSIMO_SKIP_DOTENV', skip_dotenv)
    monkeypatch.setattr(cli.sys, 'argv', ['apsimo', '--instance', str(tmp_path/'typo'), *command])
    monkeypatch.setattr(cli, '_cleanup_orphans', lambda **kw: pytest.fail('Global cleanup invoked'))
    monkeypatch.setattr(cli, '_find_pid_on_port', lambda *a: pytest.fail('Unrelated port probed'))
    monkeypatch.setattr(cli.os, 'kill', lambda *a: pytest.fail('Process signalled'))
    with pytest.raises(ValueError, match='no legacy fallback'):
        cli.main()


def test_local_stop_refuses_reused_pid_and_other_instance_port(tmp_path, monkeypatch, capsys):
    from apsimo import cli
    monkeypatch.setenv('APSIMO_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('APSIMO_INSTALL_PROFILE', 'local')
    (tmp_path/'sidecar.pid').write_text('1234')
    (tmp_path/'sidecar-process.json').write_text(json.dumps({'pid':1234,'signature':'original process'}))
    monkeypatch.setattr(cli, '_process_signature', lambda pid: 'different process')
    monkeypatch.setattr(cli.os, 'kill', lambda *a: pytest.fail('Unrelated process signalled'))
    apply_environment_aliases()
    cli._cmd_stop()
    assert 'no process was stopped' in capsys.readouterr().out
    monkeypatch.setattr(setup, '_check_port', lambda port: True)
    monkeypatch.setattr(cli, '_cleanup_orphans', lambda **kw: pytest.fail('Global cleanup called'))
    with pytest.raises(SystemExit) as result:
        cli._cmd_start_daemon('127.0.0.1', 7777, True)
    assert result.value.code == 1


@pytest.mark.parametrize('interrupted', [False, True])
def test_local_start_records_process_after_python_launcher_exec(tmp_path, monkeypatch, interrupted):
    from apsimo import cli
    monkeypatch.setenv('APSIMO_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('APSIMO_INSTALL_PROFILE', 'local')
    monkeypatch.setattr(setup, '_check_port', lambda port: False)
    monkeypatch.setattr(cli, '_find_pids_on_port', lambda port: [])
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    proc = Mock(pid=1234)
    proc.poll.return_value = None
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda *a, **k: proc)
    signature = ['same-start-time venv/python -m uvicorn']
    monkeypatch.setattr(cli, '_process_signature', lambda pid: signature[0])
    def ready(*a, **k):
        signature[0] = 'same-start-time framework/Python -m uvicorn'
        if interrupted:
            raise KeyboardInterrupt()
        return True
    monkeypatch.setattr(cli, '_wait_for_sidecar', ready)
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: Mock(json=lambda:{'capabilities':[]}))
    apply_environment_aliases()
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            cli._cmd_start_daemon('127.0.0.1', 7777, False)
        proc.terminate.assert_called_once_with()
    else:
        cli._cmd_start_daemon('127.0.0.1', 7777, False)
        proc.terminate.assert_not_called()
    record = json.loads((tmp_path/'sidecar-process.json').read_text())
    assert record['signature'] == signature[0]
    def stop(pid, sig):
        assert (pid, sig) == (1234, 15)
        signature[0] = ''
    monkeypatch.setattr(cli.os, 'kill', stop)
    apply_environment_aliases()
    cli._cmd_stop()
    assert not (tmp_path/'sidecar.pid').exists()


def test_local_status_only_uses_scoped_memory_status(tmp_path, monkeypatch, capsys):
    from apsimo import cli
    monkeypatch.setenv('APSIMO_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('APSIMO_INSTALL_PROFILE', 'local')
    monkeypatch.setenv('APSIMO_OWNER_CONTACT_ID', 'existing-owner')
    monkeypatch.setenv('APSIMO_CLIENT_API_KEY', 'fixture-private-key')
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        data = {'status':'ok'} if url.endswith('/health') else {'sources': {'pending':0}}
        return httpx.Response(200, json=data, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    apply_environment_aliases()
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


def test_explicit_rename_keeps_old_state_forwarders_credentials_and_work(args, monkeypatch):
    """Exercise the mixed old/new layout, including a retained worker and retry."""
    home = Path(args.hermes_home)
    state = home/'colony'
    adapter = state/'adapter'
    old_resources = {}
    for module, name in [('colony_hermes', 'colony'), ('colony_memory', 'colony-memory')]:
        old_resources[module+'/__init__.py'] = b'# retained historical implementation\n'
        old_resources[module+'/plugin.yaml'] = ('name: '+name+'\n').encode()
    for name, content in old_resources.items():
        path = adapter/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (state/'.env').write_text(f'COLONY_STATE_DIR={state}\nCOLONY_INSTALL_PROFILE=local\n'
                             'COLONY_API_KEY=fixture-private-key\n')
    (home/'.env').write_text('COLONY_NATIVE_API_KEY=fixture-native-key\nPRIVATE_CHANNEL=retained\n')
    (home/'SOUL.md').write_text('Private agent identity retained.')
    (state/'retained-memory.db').write_bytes(b'original source ids, erasures and corrections')
    manifest = {'version':1, 'profile':'local', 'hermes_home':str(home),
                'hermes_python':'/fixture/python', 'owner_id':'owner-original',
                'adapter_binding':{'mode':'private-directory'},
                'adapter_sha256':setup_hermes._resource_digest(old_resources),
                'local_work':{'executor':'kanban', 'worker_profile':'colony-drafts', 'board':'colony-drafts'}}
    (state/'instance.json').write_text(json.dumps(manifest))
    root_config = {'plugins':{'enabled':['other', 'colony'], 'colony':{'instance_dir':str(state)},
                             'entries':{'colony':{'allow_tool_override':True}}},
                   'memory':{'provider':'colony-memory', 'config':{'api_key':'${COLONY_NATIVE_API_KEY}'}},
                   'model':{'default':'keep-model'}, 'platform_toolsets':{'cli':['colony','kanban']}}
    (home/'config.yaml').write_text(yaml.safe_dump(root_config))
    worker = home/'profiles/colony-drafts'
    worker.mkdir(parents=True)
    (worker/'config.yaml').write_text(yaml.safe_dump({'plugins':{'enabled':['colony'],
        'colony':{'instance_dir':str(state)}}, 'toolsets':['colony','kanban'], 'model':{'default':'keep-worker'}}))
    forwarders = []
    for profile, directories in [(home, [('colony','colony_hermes'),('colony-memory','colony_memory')]),
                                 (worker, [('colony','colony_hermes')])]:
        for directory, module in directories:
            target = profile/'plugins'/directory
            target.mkdir(parents=True)
            forwarder = target/'__init__.py'
            forwarder.write_text(setup_hermes._forwarder(adapter, module, module=='colony_memory'))
            forwarders.append(forwarder)
            (target/'plugin.yaml').write_bytes(old_resources[module+'/plugin.yaml'])
    candidate = setup_hermes._adapter_resources(args.adapter_wheel)
    for old, new, name in [('colony_hermes','apsimo_hermes','apsimo'),
                            ('colony_memory','apsimo_memory','apsimo-memory')]:
        candidate[new+'/plugin.yaml'] = ('name: '+name+'\n').encode()
        candidate[old+'/__init__.py'] = ('# compatibility import resource for '+new+'\n').encode()
        candidate[old+'/plugin.yaml'] = candidate[new+'/plugin.yaml']
    monkeypatch.setattr(setup_hermes, '_adapter_resources', lambda *a: candidate)
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('Rename must not call a model'))
    retained = [home/'.env', home/'SOUL.md', state/'.env', state/'retained-memory.db', *forwarders]
    before = {path:path.read_bytes() for path in retained}
    args.refresh_adapter = True
    assert setup.run_init(None, args) == 0
    assert all(path.read_bytes() == raw for path,raw in before.items())
    assert not (home/'apsimo').exists()
    assert not (home/'plugins/apsimo').exists()
    after = yaml.safe_load((home/'config.yaml').read_text())
    assert after['plugins']['enabled'] == ['other','apsimo']
    assert 'colony' not in after['plugins']
    assert after['plugins']['apsimo']['instance_dir'] == str(state)
    assert after['plugins']['entries'] == {'apsimo':{'allow_tool_override':True}}
    assert after['memory'] == {'provider':'colony-memory','config':{'api_key':'${COLONY_NATIVE_API_KEY}'}}
    assert after['model'] == root_config['model']
    assert after['platform_toolsets']['cli'] == ['apsimo','kanban']
    assert yaml.safe_load((home/'plugins/colony/plugin.yaml').read_text())['name'] == 'apsimo'
    assert json.loads((state/'instance.json').read_text())['local_work'] == manifest['local_work']
    assert setup_hermes._copied_resources(next(state.glob('adapter-previous-*'))) == old_resources
    stable = {str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
    assert setup.run_init(None, args) == 0
    assert stable == {str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
    # The normal loader also accepts the retained old private environment.
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('APSIMO_API_KEY', 'foreign-inherited-value')
    load_environment()
    assert os.environ['COLONY_STATE_DIR'] == str(state)
    assert os.environ['COLONY_API_KEY'] == 'fixture-private-key'
    assert 'APSIMO_API_KEY' not in os.environ


def test_installed_probe_checks_actual_metadata_aliases_and_module_bytes(tmp_path):
    import subprocess
    import sys
    import venv
    root = tmp_path/'interpreter'
    venv.EnvBuilder(with_pip=False).create(root)
    python = root/'bin/python'
    site = Path(subprocess.check_output([str(python), '-I', '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    resources = {}
    for package in ('apsimo_hermes', 'apsimo_memory'):
        for name in ('__init__.py', 'client.py', 'plugin.yaml'):
            content = b'# Selected fixture module\n' if name.endswith('.py') else b'name: fixture\n'
            path = site/package/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            resources[package+'/'+name] = content
    dist = site/'apsimo_hermes-1.3.0.dist-info'
    dist.mkdir()
    (dist/'METADATA').write_text('Metadata-Version: 2.1\nName: apsimo-hermes\nVersion: 1.3.0\n')
    (dist/'entry_points.txt').write_text(
        '[hermes_agent.plugins]\napsimo = apsimo_hermes\ncolony = apsimo_hermes\n'
        '[hermes_agent.memory_providers]\napsimo-memory = apsimo_memory\ncolony-memory = apsimo_memory\n')
    result = setup_hermes._adapter_binding(python, resources)
    assert result['mode'] == 'native-installed' and result['version'] == '1.3.0'
    assert set(result['sources']) == {'apsimo_hermes','apsimo_memory'}
    # A second old distribution must not be hidden by a directory fallback.
    old = site/'colony_hermes-1.2.1.dist-info'
    old.mkdir()
    (old/'METADATA').write_text('Metadata-Version: 2.1\nName: colony-hermes\nVersion: 1.2.1\n')
    (old/'entry_points.txt').write_text('[hermes_agent.plugins]\ncolony = colony_hermes\n')
    with pytest.raises(ValueError, match='incomplete or different'):
        setup_hermes._adapter_binding(python, resources)
    (old/'entry_points.txt').unlink()
    (site/'apsimo_hermes/client.py').write_bytes(b'# Different installed implementation\n')
    with pytest.raises(ValueError, match='incomplete or different'):
        setup_hermes._adapter_binding(python, resources)

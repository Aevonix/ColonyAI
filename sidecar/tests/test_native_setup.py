"""Guided native init preserves profiles and creates one private scoped instance."""
import json
import os
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


@pytest.mark.parametrize('failure', ['endpoint', 'provider', 'artifact', 'malformed_config'])
def test_preflight_failure_leaves_selected_home_and_state_unchanged(args, monkeypatch, failure):
    home = Path(args.hermes_home); home.mkdir()
    (home/'SOUL.md').write_text('Keep me')
    (home/'config.yaml').write_text('plugins: {enabled: []}\n')
    if failure == 'endpoint': args.model_url = 'http://localhost:bad'
    if failure == 'provider': (home/'config.yaml').write_text('memory: {provider: other}\n')
    if failure == 'artifact': args.adapter_wheel = str(home/'missing.whl')
    if failure == 'malformed_config': (home/'config.yaml').write_text('model: 1\nmodel: 2\n')
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
    load_environment()
    assert 'OTHER_AGENT_ONLY' not in os.environ


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

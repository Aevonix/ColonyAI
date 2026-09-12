"""An explicit receipt choice changes one selected profile, never transport authority."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest
import yaml

from apsimo import setup_hermes


def attached(tmp_path, config):
    home, state = tmp_path / 'selected', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    config = deepcopy(config)
    config['plugins'] = {'colony': {'instance_dir': str(state)}}
    (home / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
    (home / 'SOUL.md').write_text('Retained identity.\n')
    (state / 'instance.json').write_text(json.dumps({'hermes_home': str(home)}))
    (state / 'retained.db').write_bytes(b'untouched fixture state')
    return home, state, config


@pytest.mark.parametrize('choice, expected', [('on', True), ('off', False)])
def test_existing_profile_explicit_preference_preserves_scope_and_state(tmp_path, monkeypatch, choice, expected):
    home, state, before = attached(tmp_path, {'platforms': {'whatsapp': {
        'enabled': False, 'extra': {'send_read_receipts': not expected,
        'allow_from': ['fixture-sender'], 'group_policy': 'disabled',
        'bridge_script': '/selected/bridge.js', 'custom_presence': False}}},
        'model': {'provider': 'fixture', 'api_key': '${UNCHANGED_KEY}'}})
    def forbidden(*args, **kwargs):
        pytest.fail('A profile preference must not invoke a process or service')
    monkeypatch.setattr(setup_hermes.subprocess, 'run', forbidden)
    args = SimpleNamespace(non_interactive=True, hermes_home=str(home), whatsapp_read_receipts=choice)
    assert setup_hermes.run(state, args) == 0
    current = yaml.safe_load((home / 'config.yaml').read_text())
    before['platforms']['whatsapp']['extra']['send_read_receipts'] = expected
    assert current == before
    assert (home / 'SOUL.md').read_text() == 'Retained identity.\n'
    assert (state / 'retained.db').read_bytes() == b'untouched fixture state'
    assert len(list(home.glob('.config.yaml.colony-backup-*'))) == 1
    # Repeating the same choice is byte-idempotent and adds no backup or state.
    snapshot = (home / 'config.yaml').read_bytes()
    assert setup_hermes.run(state, args) == 0
    assert (home / 'config.yaml').read_bytes() == snapshot
    assert len(list(home.glob('.config.yaml.colony-backup-*'))) == 1


def test_omitted_receipt_preference_retains_config_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("APSIMO_STATE_DIR", raising=False)
    home, state, _ = attached(tmp_path, {'whatsapp': {'send_read_receipts': True}})
    path = home / 'config.yaml'
    path.write_text('# A retained comment\n' + path.read_text())
    before = path.read_bytes()
    assert setup_hermes.run(state, SimpleNamespace(non_interactive=True, hermes_home=str(home))) == 0
    assert path.read_bytes() == before
    assert not list(home.glob('.config.yaml.colony-backup-*'))
    assert "APSIMO_STATE_DIR" not in os.environ
    # A legacy caller can select its next state without a stale canonical input
    # taking precedence over that selection.
    from apsimo.environment import normalize_environment
    from apsimo import get_state_dir
    following = tmp_path / 'following-state'
    monkeypatch.setenv('COLONY_STATE_DIR', str(following))
    assert normalize_environment()['COLONY_STATE_DIR'] == str(following)
    assert get_state_dir() == following


def test_receipt_cli_flag_uses_existing_init_parser(tmp_path, monkeypatch):
    import sys
    from apsimo import cli, setup
    observed = []
    monkeypatch.setattr(sys, 'argv', ['colony', 'init', '--whatsapp-read-receipts', 'on',
                                     '--hermes-home', str(tmp_path), '--non-interactive'])
    monkeypatch.setattr(setup, 'run_init', lambda root_dir, args: observed.append(args) or 1)
    with pytest.raises(SystemExit) as exit_code:
        cli.main()
    assert exit_code.value.code == 1
    assert observed[0].whatsapp_read_receipts == 'on'


@pytest.mark.parametrize('preview', [True, False])
def test_preference_only_cli_never_sets_up_identity_models_or_instance(tmp_path, monkeypatch, preview):
    import sys
    from apsimo import cli
    home = tmp_path/'home'
    home.mkdir()
    config = home/'config.yaml'
    config.write_text('# Preserve this on preview\nwhatsapp:\n  enabled: false\n  send_read_receipts: false\n')
    before = config.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Preference-only mode must not initialize anything')
    for module, name in [(cli, '_cmd_init'), (cli, '_load_dotenv'),
                         (setup_hermes, '_select_home'), (setup_hermes.subprocess, 'run'),
                         (setup_hermes.httpx, 'get'), (setup_hermes.httpx, 'post')]:
        monkeypatch.setattr(module, name, forbidden)
    argv = ['colony', 'init', '--hermes-home', str(home), '--preferences-only',
            '--whatsapp-read-receipts', 'on']
    monkeypatch.setattr(sys, 'argv', argv + (['--preview'] if preview else []))
    cli.main()
    assert not (home/'colony').exists()
    assert not (home/'SOUL.md').exists()
    if preview:
        assert config.read_bytes() == before
        assert list(home.iterdir()) == [config]
    else:
        assert yaml.safe_load(config.read_bytes()) == {'whatsapp': {
            'enabled': False, 'send_read_receipts': True}}
        backups = list(home.glob('.config.yaml.colony-backup-*'))
        assert len(backups) == 1 and backups[0].read_bytes() == before


@pytest.mark.parametrize('options', [
    {'start': True}, {'refresh_adapter': True}, {'model': 'fixture'},
    {'native_goals': True}, {'local_work': True},
])
def test_preference_only_rejects_setup_combinations_before_writes(tmp_path, options):
    from apsimo import setup
    (tmp_path/'config.yaml').write_text('whatsapp: {enabled: false}\n')
    args = SimpleNamespace(hermes_home=str(tmp_path), preferences_only=True,
                           whatsapp_read_receipts='on', **options)
    assert setup.run_init(None, args) == 1
    assert list(tmp_path.iterdir()) == [tmp_path/'config.yaml']


@pytest.mark.parametrize('config', [{}, {'whatsapp': {}}, {'platforms': {'whatsapp': {}}}])
def test_missing_channel_is_rejected_without_enabling_it(tmp_path, config):
    original = yaml.safe_dump(config)
    path = tmp_path/'config.yaml'
    path.write_text(original)
    args = SimpleNamespace(hermes_home=str(tmp_path), preferences_only=True,
                           whatsapp_read_receipts='on')
    assert setup_hermes.run(None, args) == 1
    assert path.read_text() == original and list(tmp_path.iterdir()) == [path]


def test_projection_does_not_mutate_a_shared_yaml_alias():
    config = yaml.safe_load('platforms:\n  whatsapp: &channel\n    enabled: false\n    extra: &extras\n      send_read_receipts: false\nretained: *channel\nother: *extras\n')
    before = deepcopy(config)
    candidate, _ = setup_hermes._receipt_preference(config, 'on')
    assert config == before
    assert candidate['retained'] == before['retained']
    assert candidate['other'] == before['other']
    assert candidate['platforms']['whatsapp']['extra']['send_read_receipts'] is True


def test_config_race_is_rejected_and_concurrent_bytes_remain(tmp_path, monkeypatch):
    from apsimo import setup
    path = tmp_path/'config.yaml'
    path.write_text('whatsapp: {enabled: false}\n')
    actual_writer = setup._atomic_hermes_config_write
    def concurrent_edit(path, original, updated):
        path.write_text('whatsapp: {enabled: false}\nretained: concurrent\n')
        return actual_writer(path, original, updated)
    monkeypatch.setattr(setup, '_atomic_hermes_config_write', concurrent_edit)
    assert setup_hermes.run(None, SimpleNamespace(hermes_home=str(tmp_path),
        preferences_only=True, whatsapp_read_receipts='on')) == 1
    assert yaml.safe_load(path.read_text())['retained'] == 'concurrent'
    assert 'send_read_receipts' not in path.read_text()

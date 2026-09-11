"""Actual legacy setup reports the host work it does not perform."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apsimo.persona.engine import PersonaEngine
from apsimo.persona.manifest import load_manifest


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('COLONY_STATE_DIR', str(state))
    repo = tmp_path / 'persona'
    repo.mkdir()
    for name, value in {'identity.md': '# Retained identity\n',
                        'overlay.yaml': 'display_name: fixture\n',
                        'plugin.py': '# fixture plugin\n',
                        'channels.json': '{"channels": []}\n'}.items():
        (repo / name).write_text(value)
    (repo / 'persona.yaml').write_text('''name: retained
host:
  type: hermes
  identity: identity.md
  config_overlay: overlay.yaml
  plugins:
    - name: fixture
      source: plugin.py
colony:
  channels_config: channels.json
''')
    host = tmp_path / '.hermes'
    host.mkdir()
    (host / 'SOUL.md').write_text('Existing host identity\n')
    return repo, state, host


def test_setup_saves_manifest_and_channels_without_claiming_host_install(deployment):
    repo, state, host = deployment
    engine = PersonaEngine(load_manifest(repo), repo, state_dir=state)
    before = {p.name: p.read_bytes() for p in host.iterdir()}
    result = engine.setup(interactive=False)
    assert 'host_config' not in result['steps']
    assert len(result['warnings']) == 1 and 'Host settings were not applied' in result['warnings'][0]
    assert result['steps'] == ['colony_config', 'services', 'channels', 'state_saved']
    assert (state / 'channels.json').read_bytes() == (repo / 'channels.json').read_bytes()
    saved = json.loads((engine.persona_dir / 'manifest.json').read_text())
    assert saved['host']['identity'] == 'identity.md'
    assert saved['host']['plugins'] == [{'name': 'fixture', 'source': 'plugin.py'}]
    assert {p.name: p.read_bytes() for p in host.iterdir()} == before


def test_cli_reports_unapplied_host_settings_from_actual_setup(deployment, monkeypatch, capsys):
    from apsimo import cli
    repo, state, host = deployment
    config = repo / 'variables.yaml'
    config.write_text('variables: {}\nsecrets: {}\n')
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    cli._cmd_persona(SimpleNamespace(persona_command='setup', repo=str(repo), config=str(config)))
    output = capsys.readouterr()
    assert "setup finished with unapplied settings" in output.out
    assert 'Host settings were not applied' in output.out
    assert 'host_config' not in output.out
    assert "setup complete" not in output.out
    assert (host / 'SOUL.md').read_text() == 'Existing host identity\n'
    assert (state / 'channels.json').is_file()

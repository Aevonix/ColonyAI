"""Current guided setup configuration preserves the selected profile."""

from pathlib import Path
from types import SimpleNamespace
import os
import stat
import subprocess

import pytest
import yaml

from apsimo import setup


URL = "http://127.0.0.1:7777"


def _write_config(path, contact_id="", url=URL):
    original, updated = setup._prepare_hermes_config(path, url, contact_id)
    setup._atomic_hermes_config_write(path, original, updated)


def snapshot(path):
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in path.rglob("*") if item.is_file()
    }


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # Attachment must never invoke either legacy supervision or live checks.
    def forbidden(*args, **kwargs):
        pytest.fail("Staging invoked a live or global operation")
    monkeypatch.setattr(subprocess, "run", forbidden)
    return home


def test_preserve_nested_values_identity_and_existing_secret(tmp_path, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    before = {
        "model": {"name": "private-model", "extra": {"sampling": [1, 2]}},
        "channels": {"voice": {"enabled": True}},
        "context": {"engine": "default", "limit": 9000},
        "context_engine": "legacy-setting-preserved",
        "memory": {"provider": "apsimo", "limit": 20, "config": {
            "url": URL, "contact_id": "existing-owner", "api_key": "existing-secret",
            "custom": {"retain": ["nested", "values"]},
        }},
        "plugins": {"enabled": ["unrelated", "apsimo"], "unrelated": {
            "routes": {"private": ["one", "two"]}, "secret": "${PRIVATE_KEY}",
        }, "apsimo": {"url": URL, "contact_id": "existing-owner", "api_key": "${OLD_KEY}"}},
    }
    raw = "# Preserve this recovery copy\n" + yaml.safe_dump(before, sort_keys=False)
    config_path.write_text(raw)
    config_path.chmod(0o640)
    (home / "SOUL.md").write_text("Existing private identity")
    _write_config(config_path)
    after = yaml.safe_load(config_path.read_text())
    before["memory"]["provider"] = "apsimo-memory"
    before['hooks'] = {'output_spill': {'max_chars': 65536}}
    assert after == before
    assert (home / "SOUL.md").read_text() == "Existing private identity"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    backups = list(home.glob(".config.yaml.colony-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == raw
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "existing-secret" not in capsys.readouterr().out
    assert "do-not-copy-key" not in config_path.read_text()


@pytest.mark.parametrize('spill,expected', [
    ({}, {'max_chars':65536}),
    ({'max_chars':10000,'preview_head':200}, {'max_chars':65536,'preview_head':200}),
    ({'max_chars':131072}, {'max_chars':131072}),
    ({'enabled':False,'max_chars':1000}, {'enabled':False,'max_chars':1000}),
])
def test_native_memory_spill_alignment_preserves_operator_options(spill, expected):
    config={'hooks':{'output_spill':spill,'other':{'keep':1}},'alias':spill}
    original=dict(spill)
    setup._align_hermes_memory_spill(config)
    assert config['hooks']=={'output_spill':expected,'other':{'keep':1}}
    assert config['alias']==original
    assert setup._align_hermes_memory_spill(config) is False


def test_idempotent_staging_does_not_rewrite_or_backup_again(tmp_path):
    home = tmp_path / "hermes"
    _write_config(home / "config.yaml", contact_id="owner")
    before = snapshot(home)
    mtime = (home / "config.yaml").stat().st_mtime_ns
    _write_config(home / "config.yaml", contact_id="owner")
    assert snapshot(home) == before
    assert (home / "config.yaml").stat().st_mtime_ns == mtime


def test_canonical_owner_binding_is_preserved_or_conflict_rejected(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("plugins:\n  colony:\n    owner_contact_id: existing-owner\n")
    before = snapshot(home)
    with pytest.raises(ValueError, match="contact bindings disagree"):
        _write_config(config_path, contact_id="other-owner")
    assert snapshot(home) == before
    _write_config(config_path)
    config = yaml.safe_load(config_path.read_text())
    assert config["plugins"]["apsimo"]["owner_contact_id"] == "existing-owner"
    assert config["memory"]["config"]["contact_id"] == "existing-owner"


def test_native_memory_binding_is_not_silently_redirected(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "colony-memory.json").write_text('{"url":"http://other-instance.test","contact_id":"other-owner"}')
    before = snapshot(home)
    with pytest.raises(ValueError, match="endpoint differs"):
        _write_config(home / "config.yaml", contact_id="owner")
    assert snapshot(home) == before


@pytest.mark.parametrize("blank", [None, ""])
def test_blank_key_uses_private_environment_reference(blank, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump({"memory": {"config": {"api_key": blank}},
                                    "plugins": {"apsimo": {"api_key": blank}}}))
    _write_config(path, contact_id="owner")
    config = yaml.safe_load(path.read_text())
    assert config["memory"]["config"]["api_key"] == "${APSIMO_API_KEY}"
    assert config["plugins"]["apsimo"]["api_key"] == "${APSIMO_API_KEY}"


@pytest.mark.parametrize("raw", [
    "memory:\n  provider: honcho\n", "memory:\n  provider: mem0\n",
    "[one, two]\n", "memory: scalar\n", "plugins: []\n",
    "memory:\n  config: []\n", "plugins:\n  colony: false\n",
    "memory: [unterminated\n", "memory: {}\nmemory: {}\n",
    "plugins:\n  private: secret-value\n  private: duplicate\n",
    "memory:\n  provider: colony-memory\n  config:\n    contact_id: another-owner\n",
    "memory:\n  provider: colony-memory\n  config:\n    url: https://existing.example\n",
])
def test_preflight_failure_leaves_every_file_unchanged(raw, tmp_path, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(raw)
    (home / "SOUL.md").write_text("Private identity")
    before = snapshot(home)
    with pytest.raises(ValueError):
        _write_config(home / "config.yaml", contact_id="owner")
    assert snapshot(home) == before
    assert not (home / "plugins").exists()
    output = capsys.readouterr().out
    assert "do-not-print" not in output and "secret-value" not in output


@pytest.mark.parametrize("url", ["http://owner:private@localhost/v1", "http://localhost?token=private", "file:///tmp/private",
    "http://localhost:bad", "http://localhost:70000"])
def test_credential_url_failure_creates_nothing(url, tmp_path, capsys):
    home = tmp_path / "new-hermes"
    with pytest.raises(ValueError, match="Sidecar URL"):
        _write_config(home / "config.yaml", contact_id="owner", url=url)
    assert not home.exists()
    assert "private" not in capsys.readouterr().out


def test_failed_atomic_replace_retains_original_and_private_backup(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = b"# original\nmodel: old\n"
    config_path.write_bytes(original)
    def fail_replace(*args):
        raise OSError("simulated rename failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        _write_config(config_path, contact_id="owner")
    assert config_path.read_bytes() == original
    assert not list(tmp_path.glob(".config.yaml.colony-stage-*"))
    backup, = tmp_path.glob(".config.yaml.colony-backup-*")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_changed_config_precondition_preserves_newer_writer(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: original\n")
    original, updated = setup._prepare_hermes_config(path, URL, "owner")
    path.write_text("model: changed-by-owner\n")
    with pytest.raises(ValueError, match="changed during staging"):
        setup._atomic_hermes_config_write(path, original, updated)
    assert path.read_text() == "model: changed-by-owner\n"
    assert not list(tmp_path.glob(".config.yaml.colony-*"))


def test_yaml_alias_does_not_mutate_unrelated_configuration(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("shared: &settings\n  custom: retained\nmemory:\n  config: *settings\n")
    _write_config(path, contact_id="owner")
    config = yaml.safe_load(path.read_text())
    assert config["shared"] == {"custom": "retained"}
    assert config["memory"]["config"]["custom"] == "retained"


def test_run_init_returns_failure_and_passes_selected_home(tmp_path, monkeypatch):
    from apsimo import setup_hermes
    seen = []
    def fail(root_dir, args):
        seen.append((root_dir, args.hermes_home))
        return 1
    monkeypatch.setattr(setup_hermes, "run", fail)
    args = SimpleNamespace(hermes_home=str(tmp_path / "chosen"))
    assert setup.run_init(str(tmp_path / "private"), args) == 1
    assert seen == [(str(tmp_path / "private"), args.hermes_home)]


def test_cli_threads_home_and_preserves_failure_exit(monkeypatch, tmp_path):
    from apsimo import cli
    selected = str(tmp_path / "chosen")
    def fail_init(root_dir, args):
        assert args.hermes_home == selected
        return 1
    monkeypatch.setattr(setup, "run_init", fail_init)
    monkeypatch.setattr("sys.argv", ["apsimo", "init", "--agent-harness", "hermes", "--hermes-home", selected])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("command", [
    ["persona", "setup", "/unused"],
    ["init", "--no-harness"],
    ["init", "--mcp-harnesses", "codex"],
    ["init", "--host-framework", "standalone"],
    ["init", "--tier", "1"],
    ["init", "--neo4j-password", "unused"],
    ["init", "--skip-model-download"],
])
def test_removed_deployment_commands_exit_before_setup(command, monkeypatch, tmp_path):
    from apsimo import cli

    def forbidden(*args, **kwargs):
        pytest.fail("An unsupported command entered setup")

    monkeypatch.setattr(setup, "run_init", forbidden)
    monkeypatch.setattr("sys.argv", ["apsimo", *command])
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert snapshot(tmp_path) == before


def test_start_retains_graph_config_without_managing_its_container(monkeypatch):
    from apsimo import cli, runtime_logging
    import uvicorn

    monkeypatch.setenv("COLONY_GRAPH_ENABLED", "true")
    monkeypatch.setenv("NEO4J_URI", "bolt://configured-graph.invalid:7687")
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "_is_service_loaded", lambda: False)
    monkeypatch.setattr(cli, "_find_pid_on_port", lambda port: None)
    monkeypatch.setattr(runtime_logging, "configure_runtime_logging", lambda **kwargs: None)
    calls = []

    def serve(app, **kwargs):
        assert os.environ["COLONY_GRAPH_ENABLED"] == "true"
        assert os.environ["NEO4J_URI"] == "bolt://configured-graph.invalid:7687"
        calls.append((app, kwargs["host"], kwargs["port"]))

    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setattr("sys.argv", ["apsimo", "start", "--host", "127.0.0.1", "--port", "8877"])
    # The autouse fixture rejects any subprocess, including Docker management.
    cli.main()
    assert calls == [("apsimo.server:app", "127.0.0.1", 8877)]

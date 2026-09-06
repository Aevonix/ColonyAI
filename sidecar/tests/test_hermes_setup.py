"""Source-checkout staging must not corrupt or activate another Hermes home."""

from pathlib import Path
from types import SimpleNamespace
import os
import stat
import subprocess

import pytest
import yaml

from colony_sidecar import setup


URL = "http://127.0.0.1:7777"


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
    monkeypatch.setattr(setup.subprocess, "run", forbidden)
    return home


@pytest.mark.parametrize("selection", ["explicit", "environment", "default"])
def test_stage_targets_only_selected_home(selection, tmp_path, monkeypatch, isolated_host, capsys):
    default = isolated_host / ".hermes"
    other = tmp_path / "other profile"
    explicit = tmp_path / "chosen profile"
    monkeypatch.setenv("HERMES_HOME", str(other))
    kwargs = {}
    if selection == "explicit":
        kwargs["hermes_home"] = explicit
        target = explicit
    elif selection == "environment":
        target = other
    else:
        monkeypatch.delenv("HERMES_HOME")
        target = default
    assert setup._setup_hermes_plugin("new-private-key", URL, contact_id="owner", **kwargs)
    config = yaml.safe_load((target / "config.yaml").read_text())
    assert config["memory"]["provider"] == "colony-memory"
    assert config["memory"]["config"]["api_key"] == "${COLONY_API_KEY}"
    assert "enabled" not in config["plugins"]
    assert "context_engine" not in config and "context" not in config
    for relative in (
        "colony-memory/provider.py", "colony-memory/plugin.yaml", "colony/__init__.py",
        "colony/evidence.py",
        "colony/colony_hostworker/catalog.py", "colony/colony_hostworker/contract.py",
    ):
        assert (target / "plugins" / relative).is_file()
    assert not (target / "plugins/context_engine").exists()
    assert not (target / "scripts").exists()
    assert not (isolated_host / "Library").exists()
    for unselected in (default, other, explicit):
        if unselected != target:
            assert not unselected.exists()
    assert "new-private-key" not in (target / "config.yaml").read_text()
    output = capsys.readouterr().out
    assert "new-private-key" not in output
    assert "Staging only" in output
    assert "integration complete" not in output.lower()
    assert stat.S_IMODE((target / "config.yaml").stat().st_mode) == 0o600


def test_preserve_nested_values_identity_and_existing_secret(tmp_path, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    before = {
        "model": {"name": "private-model", "extra": {"sampling": [1, 2]}},
        "channels": {"voice": {"enabled": True}},
        "context": {"engine": "default", "limit": 9000},
        "context_engine": "legacy-setting-preserved",
        "memory": {"provider": "colony", "limit": 20, "config": {
            "url": URL, "contact_id": "existing-owner", "api_key": "existing-secret",
            "custom": {"retain": ["nested", "values"]},
        }},
        "plugins": {"enabled": ["unrelated", "colony"], "unrelated": {
            "routes": {"private": ["one", "two"]}, "secret": "${PRIVATE_KEY}",
        }, "colony": {"url": URL, "contact_id": "existing-owner", "api_key": "${OLD_KEY}"}},
    }
    raw = "# Preserve this recovery copy\n" + yaml.safe_dump(before, sort_keys=False)
    config_path.write_text(raw)
    config_path.chmod(0o640)
    (home / "SOUL.md").write_text("Existing private identity")
    assert setup._setup_hermes_plugin("do-not-copy-key", URL, hermes_home=home)
    after = yaml.safe_load(config_path.read_text())
    before["memory"]["provider"] = "colony-memory"
    assert after == before
    assert (home / "SOUL.md").read_text() == "Existing private identity"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    backups = list(home.glob(".config.yaml.colony-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == raw
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "existing-secret" not in capsys.readouterr().out
    assert "do-not-copy-key" not in config_path.read_text()


def test_idempotent_staging_does_not_rewrite_or_backup_again(tmp_path):
    home = tmp_path / "hermes"
    assert setup._setup_hermes_plugin("key", URL, contact_id="owner", hermes_home=home)
    before = snapshot(home)
    mtime = (home / "config.yaml").stat().st_mtime_ns
    assert setup._setup_hermes_plugin("different-key", URL, contact_id="owner", hermes_home=home)
    assert snapshot(home) == before
    assert (home / "config.yaml").stat().st_mtime_ns == mtime


def test_canonical_owner_binding_is_preserved_or_conflict_rejected(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("plugins:\n  colony:\n    owner_contact_id: existing-owner\n")
    before = snapshot(home)
    assert not setup._setup_hermes_plugin("key", URL, contact_id="other-owner", hermes_home=home)
    assert snapshot(home) == before
    assert setup._setup_hermes_plugin("key", URL, hermes_home=home)
    config = yaml.safe_load(config_path.read_text())
    assert config["plugins"]["colony"]["owner_contact_id"] == "existing-owner"
    assert config["memory"]["config"]["contact_id"] == "existing-owner"


def test_native_memory_binding_is_not_silently_redirected(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "colony-memory.json").write_text('{"url":"http://other-instance.test","contact_id":"other-owner"}')
    before = snapshot(home)
    assert not setup._setup_hermes_plugin("key", URL, contact_id="owner", hermes_home=home)
    assert snapshot(home) == before


@pytest.mark.parametrize("blank", [None, ""])
def test_blank_key_uses_private_environment_reference(blank, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump({"memory": {"config": {"api_key": blank}},
                                    "plugins": {"colony": {"api_key": blank}}}))
    assert setup._setup_hermes_plugin("never-persist", URL, contact_id="owner", hermes_home=home)
    config = yaml.safe_load(path.read_text())
    assert config["memory"]["config"]["api_key"] == "${COLONY_API_KEY}"
    assert config["plugins"]["colony"]["api_key"] == "${COLONY_API_KEY}"


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
    assert not setup._setup_hermes_plugin("do-not-print", URL, contact_id="owner", hermes_home=home)
    assert snapshot(home) == before
    assert not (home / "plugins").exists()
    output = capsys.readouterr().out
    assert "do-not-print" not in output and "secret-value" not in output


@pytest.mark.parametrize("url", ["http://owner:private@localhost/v1", "http://localhost?token=private", "file:///tmp/private",
    "http://localhost:bad", "http://localhost:70000"])
def test_credential_url_failure_creates_nothing(url, tmp_path, capsys):
    home = tmp_path / "new-hermes"
    assert not setup._setup_hermes_plugin("key", url, contact_id="owner", hermes_home=home)
    assert not home.exists()
    assert "private" not in capsys.readouterr().out


def test_missing_resources_are_detected_before_any_copy(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: keep-me\n")
    before = snapshot(home)
    monkeypatch.setattr(setup, "__file__", str(tmp_path / "missing/sidecar/colony_sidecar/setup.py"))
    assert not setup._setup_hermes_plugin("key", URL, contact_id="owner", hermes_home=home)
    assert snapshot(home) == before
    assert not (home / "plugins").exists()


def test_existing_different_plugin_blocks_all_writes(tmp_path):
    home = tmp_path / "hermes"
    plugin = home / "plugins/colony-memory/provider.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("# currently installed revision\n")
    before = snapshot(home)
    assert not setup._setup_hermes_plugin("key", URL, contact_id="owner", hermes_home=home)
    assert snapshot(home) == before


@pytest.mark.parametrize("kind", ["config", "plugins"])
def test_symlink_destination_is_not_followed(kind, tmp_path):
    home = tmp_path / "hermes"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    if kind == "config":
        target = outside / "config.yaml"
        target.write_text("model: private\n")
        (home / "config.yaml").symlink_to(target)
    else:
        (home / "plugins").symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    assert not setup._setup_hermes_plugin("key", URL, contact_id="owner", hermes_home=home)
    assert snapshot(outside) == before


def test_failed_atomic_replace_retains_original_and_private_backup(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = b"# original\nmodel: old\n"
    config_path.write_bytes(original)
    def fail_replace(*args):
        raise OSError("simulated rename failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        setup._write_hermes_config(config_path, "key", URL, "owner")
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
    setup._write_hermes_config(path, "key", URL, "owner")
    config = yaml.safe_load(path.read_text())
    assert config["shared"] == {"custom": "retained"}
    assert config["memory"]["config"]["custom"] == "retained"


def test_run_init_returns_failure_and_passes_selected_home(tmp_path, monkeypatch):
    from colony_sidecar import setup_hermes
    seen = []
    def fail(root_dir, args):
        seen.append((root_dir, args.hermes_home))
        return 1
    monkeypatch.setattr(setup_hermes, "run", fail)
    args = SimpleNamespace(hermes_home=str(tmp_path / "chosen"))
    assert setup.run_init(str(tmp_path / "private"), args) == 1
    assert seen == [(str(tmp_path / "private"), args.hermes_home)]


def test_cli_threads_home_and_preserves_failure_exit(monkeypatch, tmp_path):
    from colony_sidecar import cli
    selected = str(tmp_path / "chosen")
    def fail_init(root_dir, args):
        assert args.hermes_home == selected
        return 1
    monkeypatch.setattr(setup, "run_init", fail_init)
    monkeypatch.setattr("sys.argv", ["colony", "init", "--agent-harness", "hermes", "--hermes-home", selected])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1

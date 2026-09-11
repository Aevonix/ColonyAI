"""Unit tests for Apsimo MCP harness configuration."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Skip if mcp package is not installed (config imports from __init__ which imports server)
pytest.importorskip("mcp")

from apsimo.mcp.config import (
    HARNESS_DEFS,
    add_to_harness,
    detect_harnesses,
    remove_from_harness,
)


@pytest.mark.parametrize('harness', ['claude-code', 'codex', 'hermes'])
def test_legacy_entry_migration_preserves_credentials_and_other_settings(tmp_path, monkeypatch, harness):
    import tomllib
    import yaml
    path = tmp_path / 'config'
    existing = {'command': 'colony', 'args': ['mcp'], 'env': {
        'COLONY_API_KEY': 'fixture-credential', 'COLONY_URL': 'http://selected:8222'},
        'startup_timeout_sec': 91}
    other = {'command': 'unrelated'}
    if harness == 'claude-code':
        path.write_text(json.dumps({'model': 'chosen', 'mcpServers': {'colony': existing, 'other': other}}))
        read = lambda: json.loads(path.read_text())
        key = 'mcpServers'
    elif harness == 'hermes':
        path.write_text(yaml.safe_dump({'model': 'chosen', 'mcp_servers': {'colony': existing, 'other': other}}))
        read = lambda: yaml.safe_load(path.read_text())
        key = 'mcp_servers'
    else:
        path.write_text('model="chosen"\n[mcp_servers.colony]\ncommand="colony"\nargs=["mcp"]\n'
            'startup_timeout_sec=91\n[mcp_servers.colony.env]\nCOLONY_API_KEY="fixture-credential"\n'
            'COLONY_URL="http://selected:8222"\n[mcp_servers.other]\ncommand="unrelated"\n'
            '[profiles.work]\nmodel="preserved"\n')
        read = lambda: tomllib.loads(path.read_text())
        key = 'mcp_servers'
    monkeypatch.setitem(HARNESS_DEFS[harness], 'config_path', str(path))
    before_env = dict(os.environ)
    result = add_to_harness(harness, 'owner')
    assert 'fixture-credential' not in result
    data = read()
    assert data['model'] == 'chosen'
    assert set(data[key]) == {'apsimo', 'other'}
    assert data[key]['other'] == other
    selected = data[key]['apsimo']
    assert selected['command'] == 'apsimo'
    assert selected['startup_timeout_sec'] == 91
    assert selected['env']['APSIMO_API_KEY'] == 'fixture-credential'
    assert selected['env']['APSIMO_URL'] == 'http://selected:8222'
    assert add_to_harness(harness, 'owner') is None
    add_to_harness(harness, 'owner', sidecar_url='http://explicit:9000')
    assert read()[key]['apsimo']['env']['APSIMO_URL'] == 'http://explicit:9000'
    assert dict(os.environ) == before_env
    remove_from_harness(harness)
    assert read()[key] == {'other': other}
    if harness == 'codex':
        assert read()['profiles']['work']['model'] == 'preserved'


def test_conflicting_duplicate_integrations_are_not_written(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'mcpServers': {
        'colony': {'command': 'old-custom'}, 'apsimo': {'command': 'new-custom'}}}))
    monkeypatch.setitem(HARNESS_DEFS['claude-code'], 'config_path', str(path))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='different settings'):
        add_to_harness('claude-code', 'owner')
    assert path.read_bytes() == before


def test_actual_cli_print_config_preserves_subcommand_and_custom_launch(tmp_path):
    import subprocess
    import sys
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COLONY_', 'APSIMO_'))}
    env['HOME'] = str(tmp_path)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    args = [sys.executable, '-m', 'apsimo', 'mcp', 'setup', '--print-config',
            '--harness', 'claude-code', '--contact-id', 'owner',
            '--sidecar-url', 'http://selected:9999']
    for extra, command, expected_args in [([], 'apsimo', ['mcp']),
            (['--mcp-command', '/custom/python', '--mcp-args', '-m custom_bridge'],
             '/custom/python', ['-m', 'custom_bridge'])]:
        result = subprocess.run(args + extra, env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        data, _ = json.JSONDecoder().raw_decode(result.stdout)
        config = data['mcpServers']['apsimo']
        assert config['command'] == command
        assert config['args'] == expected_args
        assert config['env']['APSIMO_URL'] == 'http://selected:9999'
        assert not (tmp_path / '.claude.json').exists()


# ---------------------------------------------------------------------------
# Harness definitions tests
# ---------------------------------------------------------------------------

class TestHarnessDefs:
    def test_all_harnesses_have_required_fields(self):
        for hid, hdef in HARNESS_DEFS.items():
            assert "display" in hdef, f"{hid} missing display"
            assert "detect_cmds" in hdef, f"{hid} missing detect_cmds"
            assert "config_path" in hdef, f"{hid} missing config_path"
            assert "config_format" in hdef, f"{hid} missing config_format"
            assert "source_tag" in hdef, f"{hid} missing source_tag"

    def test_harnesses_defined(self):
        assert len(HARNESS_DEFS) == 5
        assert "claude-code" in HARNESS_DEFS
        assert "codex" in HARNESS_DEFS
        assert "crush" in HARNESS_DEFS
        assert "opencode" in HARNESS_DEFS
        assert "hermes" in HARNESS_DEFS

    def test_source_tags_are_unique(self):
        tags = [hdef["source_tag"] for hdef in HARNESS_DEFS.values()]
        assert len(tags) == len(set(tags)), "Source tags must be unique"

    def test_config_formats_are_valid(self):
        valid = {"json", "toml", "yaml"}
        for hid, hdef in HARNESS_DEFS.items():
            assert hdef["config_format"] in valid, f"{hid} has invalid format"


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

class TestDetection:
    def test_detect_returns_all_harnesses(self):
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/claude"
            result = detect_harnesses()
            assert len(result) == 5
            # All should be True since mock returns a path for any command
            assert all(result.values())

    def test_detect_nothing_installed(self):
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            result = detect_harnesses()
            assert not any(result.values())

    def test_detect_mixed(self):
        with patch("shutil.which") as mock_which:
            def side_effect(cmd):
                return "/usr/bin/claude" if cmd == "claude" else None
            mock_which.side_effect = side_effect
            result = detect_harnesses()
            assert result["claude-code"] is True
            assert result["codex"] is False
            assert result["crush"] is False


# ---------------------------------------------------------------------------
# JSON config tests
# ---------------------------------------------------------------------------

class TestJsonConfig:
    def test_add_to_empty_config(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            diff = add_to_harness("claude-code", "owner")
            assert diff is not None

            data = json.loads(config_path.read_text())
            assert "mcpServers" in data
            assert "apsimo" in data["mcpServers"]
            colony = data["mcpServers"]["apsimo"]
            assert colony["command"] == "apsimo"
            assert colony["args"] == ["mcp"]
            assert colony["env"]["APSIMO_MCP_CONTACT_ID"] == "owner"
            assert colony["env"]["APSIMO_MCP_SOURCE"] == "claude-code"

    def test_add_preserves_existing_servers(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "other": {"command": "other-server"}
            }
        }))

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")

            data = json.loads(config_path.read_text())
            assert "other" in data["mcpServers"]
            assert "apsimo" in data["mcpServers"]

    def test_add_returns_none_if_already_configured(self, tmp_path):
        config_path = tmp_path / "claude.json"
        # Write a config that matches what add_to_harness would produce
        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")
            # Second call should return None
            diff = add_to_harness("claude-code", "owner")
            assert diff is None

    def test_add_dry_run_does_not_write(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text("{}")
        original = config_path.read_text()

        with patch.object(Path, "expanduser", return_value=config_path):
            diff = add_to_harness("claude-code", "owner", dry_run=True)
            assert diff is not None
            assert config_path.read_text() == original

    def test_remove_from_config(self, tmp_path):
        config_path = tmp_path / "claude.json"
        # First add
        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")
            # Verify it's there
            data = json.loads(config_path.read_text())
            assert "apsimo" in data["mcpServers"]
            # Remove
            diff = remove_from_harness("claude-code")
            assert diff is not None
            # Verify it's gone
            data = json.loads(config_path.read_text())
            assert "apsimo" not in data.get("mcpServers", {})

    def test_remove_preserves_other_servers(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "other": {"command": "other-server"}
            }
        }))

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")
            remove_from_harness("claude-code")

            data = json.loads(config_path.read_text())
            assert "other" in data["mcpServers"]
            assert "apsimo" not in data.get("mcpServers", {})

    def test_remove_returns_none_if_not_present(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text('{"mcpServers": {}}')

        with patch.object(Path, "expanduser", return_value=config_path):
            diff = remove_from_harness("claude-code")
            assert diff is None

    def test_remove_dry_run_does_not_write(self, tmp_path):
        config_path = tmp_path / "claude.json"

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")
            content_before = config_path.read_text()
            remove_from_harness("claude-code", dry_run=True)
            # Config should not have changed
            assert config_path.read_text() == content_before


# ---------------------------------------------------------------------------
# TOML config tests
# ---------------------------------------------------------------------------

class TestTomlConfig:
    def test_add_to_toml(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("[settings]\nkey = \"value\"\n")

        with patch.object(Path, "expanduser", return_value=config_path):
            diff = add_to_harness("codex", "owner")
            assert diff is not None

            content = config_path.read_text()
            assert "[mcp_servers.apsimo]" in content
            assert 'command = "apsimo"' in content
            assert "APSIMO_MCP_SOURCE = \"codex\"" in content
            assert "APSIMO_MCP_CONTACT_ID = \"owner\"" in content

    def test_add_to_toml_preserves_existing(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("[settings]\nkey = \"value\"\n")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("codex", "owner")
            content = config_path.read_text()
            assert "[settings]" in content
            assert 'key = "value"' in content

    def test_add_toml_returns_none_if_already_present(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("codex", "owner")
            diff = add_to_harness("codex", "owner")
            assert diff is None

    def test_remove_from_toml(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("[settings]\nkey = \"value\"\n")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("codex", "owner")
            # Verify it's there
            assert "[mcp_servers.apsimo]" in config_path.read_text()
            # Remove
            diff = remove_from_harness("codex")
            assert diff is not None
            # Verify it's gone
            assert "[mcp_servers.apsimo]" not in config_path.read_text()
            # Settings preserved
            assert "[settings]" in config_path.read_text()

    def test_remove_toml_dry_run(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("codex", "owner")
            content_before = config_path.read_text()
            remove_from_harness("codex", dry_run=True)
            assert config_path.read_text() == content_before


# ---------------------------------------------------------------------------
# Unknown harness tests
# ---------------------------------------------------------------------------

class TestUnknownHarness:
    def test_add_unknown_returns_error(self):
        diff = add_to_harness("unknown-harness", "owner")
        assert "Unknown harness" in diff

    def test_remove_unknown_returns_error(self):
        diff = remove_from_harness("unknown-harness")
        assert "Unknown harness" in diff


# ---------------------------------------------------------------------------
# OpenCode-specific tests (uses "mcp" key + "type": "stdio")
# ---------------------------------------------------------------------------

class TestOpenCodeConfig:
    def test_add_to_opencode_uses_mcp_key(self, tmp_path):
        config_path = tmp_path / "opencode.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            diff = add_to_harness("opencode", "owner")
            assert diff is not None

            data = json.loads(config_path.read_text())
            # OpenCode uses "mcp" not "mcpServers"
            assert "mcp" in data
            assert "mcpServers" not in data
            colony = data["mcp"]["apsimo"]
            assert colony["command"] == "apsimo"
            assert colony["type"] == "stdio"
            assert colony["env"]["APSIMO_MCP_SOURCE"] == "opencode"

    def test_claude_code_uses_mcpServers_key(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")

            data = json.loads(config_path.read_text())
            assert "mcpServers" in data
            assert "mcp" not in data
            # Claude Code does NOT include "type" field
            assert "type" not in data["mcpServers"]["apsimo"]

    def test_remove_from_opencode(self, tmp_path):
        config_path = tmp_path / "opencode.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("opencode", "owner")
            assert "apsimo" in json.loads(config_path.read_text())["mcp"]
            remove_from_harness("opencode")
            assert "apsimo" not in json.loads(config_path.read_text()).get("mcp", {})

    def test_detect_opencode(self):
        with patch("shutil.which") as mock_which:
            def side_effect(cmd):
                return "/usr/local/bin/opencode" if cmd == "opencode" else None
            mock_which.side_effect = side_effect
            result = detect_harnesses()
            assert result["opencode"] is True
            assert result["claude-code"] is False

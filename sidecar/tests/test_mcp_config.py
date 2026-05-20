"""Unit tests for Colony MCP harness configuration."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Skip if mcp package is not installed (config imports from __init__ which imports server)
pytest.importorskip("mcp")

from colony_sidecar.mcp.config import (
    HARNESS_DEFS,
    add_to_harness,
    detect_harnesses,
    remove_from_harness,
)


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
            assert "colony" in data["mcpServers"]
            colony = data["mcpServers"]["colony"]
            assert colony["command"] == "colony"
            assert colony["args"] == ["mcp"]
            assert colony["env"]["COLONY_MCP_CONTACT_ID"] == "owner"
            assert colony["env"]["COLONY_MCP_SOURCE"] == "claude-code"

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
            assert "colony" in data["mcpServers"]

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
            assert "colony" in data["mcpServers"]
            # Remove
            diff = remove_from_harness("claude-code")
            assert diff is not None
            # Verify it's gone
            data = json.loads(config_path.read_text())
            assert "colony" not in data.get("mcpServers", {})

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
            assert "colony" not in data.get("mcpServers", {})

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
            assert "[mcp_servers.colony]" in content
            assert 'command = "colony"' in content
            assert "COLONY_MCP_SOURCE = \"codex\"" in content
            assert "COLONY_MCP_CONTACT_ID = \"owner\"" in content

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
            assert "[mcp_servers.colony]" in config_path.read_text()
            # Remove
            diff = remove_from_harness("codex")
            assert diff is not None
            # Verify it's gone
            assert "[mcp_servers.colony]" not in config_path.read_text()
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
            colony = data["mcp"]["colony"]
            assert colony["command"] == "colony"
            assert colony["type"] == "stdio"
            assert colony["env"]["COLONY_MCP_SOURCE"] == "opencode"

    def test_claude_code_uses_mcpServers_key(self, tmp_path):
        config_path = tmp_path / "claude.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("claude-code", "owner")

            data = json.loads(config_path.read_text())
            assert "mcpServers" in data
            assert "mcp" not in data
            # Claude Code does NOT include "type" field
            assert "type" not in data["mcpServers"]["colony"]

    def test_remove_from_opencode(self, tmp_path):
        config_path = tmp_path / "opencode.json"
        config_path.write_text("{}")

        with patch.object(Path, "expanduser", return_value=config_path):
            add_to_harness("opencode", "owner")
            assert "colony" in json.loads(config_path.read_text())["mcp"]
            remove_from_harness("opencode")
            assert "colony" not in json.loads(config_path.read_text()).get("mcp", {})

    def test_detect_opencode(self):
        with patch("shutil.which") as mock_which:
            def side_effect(cmd):
                return "/usr/local/bin/opencode" if cmd == "opencode" else None
            mock_which.side_effect = side_effect
            result = detect_harnesses()
            assert result["opencode"] is True
            assert result["claude-code"] is False

"""Colony MCP harness configuration.

Handles detecting installed harnesses and configuring them to use Colony's MCP server.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Harness definitions
# ---------------------------------------------------------------------------

HARNESS_DEFS = {
    "claude-code": {
        "display": "Claude Code",
        "detect_cmds": ["claude"],
        "config_path": "~/.claude.json",
        "config_format": "json",
        "mcp_key": "mcpServers",  # Key for MCP servers in config
        "source_tag": "claude-code",
    },
    "codex": {
        "display": "Codex",
        "detect_cmds": ["codex"],
        "config_path": "~/.codex/config.toml",
        "config_format": "toml",
        "source_tag": "codex",
    },
    "crush": {
        "display": "Crush",
        "detect_cmds": ["crush"],
        "config_path": "~/.config/crush/crush.json",
        "config_format": "json",
        "mcp_key": "mcpServers",
        "source_tag": "crush",
    },
    "opencode": {
        "display": "OpenCode",
        "detect_cmds": ["opencode"],
        "config_path": "~/.config/opencode/opencode.json",
        "config_format": "json",
        "mcp_key": "mcp",  # OpenCode uses "mcp" not "mcpServers"
        "mcp_type": "stdio",  # OpenCode requires type field
        "source_tag": "opencode",
    },
    "hermes": {
        "display": "Hermes",
        "detect_cmds": ["hermes"],
        "config_path": "~/.hermes/config.yaml",
        "config_format": "yaml",
        "mcp_key": "mcp_servers",
        "source_tag": "hermes",
    },
}


def detect_harnesses() -> dict[str, bool]:
    """Return {harness_id: is_installed} for all known harnesses."""
    result = {}
    for hid, hdef in HARNESS_DEFS.items():
        installed = any(shutil.which(cmd) for cmd in hdef["detect_cmds"])
        result[hid] = installed
    return result


# ---------------------------------------------------------------------------
# Config writers
# ---------------------------------------------------------------------------

def _mcp_config(contact_id: str, source: str, include_type: bool = False) -> dict[str, Any]:
    """Return the MCP server config block for Colony."""
    config = {
        "command": "colony",
        "args": ["mcp"],
        "env": {
            "COLONY_API_KEY": "${COLONY_API_KEY}",
            "COLONY_URL": f"http://127.0.0.1:{os.environ.get('COLONY_SIDECAR_PORT', '7777')}",
            "COLONY_MCP_CONTACT_ID": contact_id,
            "COLONY_MCP_SOURCE": source,
        },
    }
    if include_type:
        config["type"] = "stdio"
    return config


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _add_to_json_config(hdef: dict, contact_id: str, source: str, dry_run: bool = False) -> str | None:
    """Add Colony to a JSON-format harness config. Returns diff description or None if already present."""
    config_path = hdef["config_path"]
    mcp_key = hdef.get("mcp_key", "mcpServers")
    needs_type = hdef.get("mcp_type") == "stdio"
    
    path = Path(config_path).expanduser()
    data = _read_json(path)

    if mcp_key not in data:
        data[mcp_key] = {}

    existing = data[mcp_key].get("colony")
    new_config = _mcp_config(contact_id, source, include_type=needs_type)

    if existing == new_config:
        return None  # Already configured identically

    old_desc = json.dumps(existing, indent=2) if existing else "(not present)"
    new_desc = json.dumps(new_config, indent=2)

    if not dry_run:
        data[mcp_key]["colony"] = new_config
        _write_json(path, data)

    return f"  Old: {old_desc[:100]}\n  New: {new_desc[:100]}"


def _add_to_toml_config(config_path: str, contact_id: str, source: str, dry_run: bool = False) -> str | None:
    """Add Colony to a TOML-format harness config."""
    path = Path(config_path).expanduser()
    sidecar_port = os.environ.get("COLONY_SIDECAR_PORT", "7777")

    toml_block = f'''
[mcp_servers.colony]
command = "colony"
args = ["mcp"]
env = {{ COLONY_API_KEY = "${{COLONY_API_KEY}}", COLONY_URL = "http://127.0.0.1:{sidecar_port}", COLONY_MCP_CONTACT_ID = "{contact_id}", COLONY_MCP_SOURCE = "{source}" }}
'''

    if path.exists():
        content = path.read_text()
        if "[mcp_servers.colony]" in content:
            return None  # Already present
    else:
        content = ""

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append Colony config
        with open(path, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(toml_block)

    return f"  Adding:\n{toml_block.strip()}"


def _add_to_yaml_config(config_path: str, contact_id: str, source: str, dry_run: bool = False) -> str | None:
    """Add Colony to a YAML-format harness config."""
    if yaml is None:
        return "  PyYAML not installed — run: pip install pyyaml"

    path = Path(config_path).expanduser()
    sidecar_port = os.environ.get('COLONY_SIDECAR_PORT', '7777')

    data = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            data = {}

    if not isinstance(data, dict):
        data = {}

    mcp_key = "mcp_servers"
    if mcp_key not in data:
        data[mcp_key] = {}

    new_config = {
        "command": "colony",
        "args": ["mcp"],
        "env": {
            "COLONY_API_KEY": "${COLONY_API_KEY}",
            "COLONY_URL": f"http://127.0.0.1:{sidecar_port}",
            "COLONY_MCP_CONTACT_ID": contact_id,
            "COLONY_MCP_SOURCE": source,
        },
    }

    existing = data[mcp_key].get("colony")
    if existing == new_config:
        return None  # Already configured identically

    old_desc = yaml.dump(existing, default_flow_style=False) if existing else "(not present)"
    new_desc = yaml.dump(new_config, default_flow_style=False)

    if not dry_run:
        data[mcp_key]["colony"] = new_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    return f"  Old: {old_desc[:100]}\n  New: {new_desc[:100]}"


def _remove_from_yaml_config(config_path: str, dry_run: bool = False) -> str | None:
    """Remove Colony from a YAML-format harness config."""
    if yaml is None:
        return "  PyYAML not installed"

    path = Path(config_path).expanduser()
    if not path.exists():
        return None

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    mcp_key = "mcp_servers"
    if mcp_key in data and isinstance(data[mcp_key], dict) and "colony" in data[mcp_key]:
        if not dry_run:
            del data[mcp_key]["colony"]
            if not data[mcp_key]:
                del data[mcp_key]
            path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        return "  Removed 'colony' from Hermes config"
    return None


def add_to_harness(harness_id: str, contact_id: str, dry_run: bool = False) -> str | None:
    """Add Colony MCP config to a specific harness. Returns diff or None if already configured."""
    hdef = HARNESS_DEFS.get(harness_id)
    if not hdef:
        return f"  Unknown harness: {harness_id}"

    source = hdef["source_tag"]

    if hdef["config_format"] == "json":
        return _add_to_json_config(hdef, contact_id, source, dry_run)
    elif hdef["config_format"] == "toml":
        return _add_to_toml_config(hdef["config_path"], contact_id, source, dry_run)
    elif hdef["config_format"] == "yaml":
        return _add_to_yaml_config(hdef["config_path"], contact_id, source, dry_run)

    return None


def remove_from_harness(harness_id: str, dry_run: bool = False) -> str | None:
    """Remove Colony MCP config from a harness. Returns description or None if not present."""
    hdef = HARNESS_DEFS.get(harness_id)
    if not hdef:
        return f"  Unknown harness: {harness_id}"

    path = Path(hdef["config_path"]).expanduser()
    mcp_key = hdef.get("mcp_key", "mcpServers")

    if hdef["config_format"] == "json":
        data = _read_json(path)
        if mcp_key in data and "colony" in data[mcp_key]:
            if not dry_run:
                del data[mcp_key]["colony"]
                _write_json(path, data)
            return f"  Removed 'colony' from {hdef['display']} config"
        return None

    elif hdef["config_format"] == "toml":
        if not path.exists():
            return None
        content = path.read_text()
        if "[mcp_servers.colony]" not in content:
            return None
        if not dry_run:
            # Remove the [mcp_servers.colony] section
            lines = content.split("\n")
            output = []
            in_section = False
            for line in lines:
                if line.strip() == "[mcp_servers.colony]":
                    in_section = True
                    continue
                if in_section and (line.startswith("[") and not line.startswith("[[")):
                    in_section = False
                if not in_section:
                    output.append(line)
            path.write_text("\n".join(output))
        return f"  Removed 'colony' from {hdef['display']} config"

    elif hdef["config_format"] == "yaml":
        return _remove_from_yaml_config(hdef["config_path"], dry_run)

    return None

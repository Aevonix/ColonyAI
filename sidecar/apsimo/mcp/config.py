"""Apsimo MCP harness configuration.

Handles detecting installed harnesses and configuring them to use Apsimo's MCP server.
"""

import json
import os
import shutil
import subprocess
import shlex
import tomllib
import re
from pathlib import Path
from typing import Any, Optional

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
        "config_path": "~/.crush.json",
        "config_format": "json",
        "mcp_key": "mcp",
        "mcp_type": "stdio",
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

def _mcp_config(contact_id: str, source: str, include_type: bool = False,
                sidecar_url: Optional[str] = None) -> dict[str, Any]:
    """New Apsimo config. Authentication is inherited, never a literal placeholder."""
    from apsimo.environment import normalize_environment
    env = normalize_environment()
    url = sidecar_url or env.get("COLONY_SIDECAR_URL") or (
        "http://127.0.0.1:" + env.get("COLONY_SIDECAR_PORT", "7777"))
    custom = env.get("COLONY_MCP_COMMAND")
    result = {
        "command": custom or "apsimo",
        "args": shlex.split(env.get("COLONY_MCP_ARGS", "")) if custom else ["mcp"],
        "env": {"APSIMO_URL": url, "APSIMO_MCP_CONTACT_ID": contact_id,
                "APSIMO_MCP_SOURCE": source},
    }
    if include_type:
        result["type"] = "stdio"
    return result


def _read_json(path: Path) -> dict:
    # Do not replace unreadable configuration with an empty document.
    return json.loads(path.read_text()) if path.exists() else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _selected(servers: dict) -> dict:
    old, new = servers.get("colony"), servers.get("apsimo")
    if old is not None and new is not None and old != new:
        raise ValueError("Both colony and apsimo MCP entries exist with different settings")
    selected = new if new is not None else old
    if selected is not None and not isinstance(selected, dict):
        raise ValueError("The Apsimo MCP entry must be a configuration table")
    return selected or {}


def _merged(existing: dict, desired: dict, sidecar_url: Optional[str]) -> dict:
    """Retain custom launch settings and literal credentials during the rename."""
    from apsimo.environment import normalize_environment
    current_env = existing.get("env", {})
    normalized = normalize_environment(current_env)
    env = {("APSIMO_" + k[7:] if k.startswith("COLONY_") else k): v
           for k, v in normalized.items()}
    # Old generated placeholders are not portable across harnesses. Inherit the
    # real variable from the launching environment instead of passing a literal.
    if env.get("APSIMO_API_KEY") in {"${COLONY_API_KEY}", "${APSIMO_API_KEY}"}:
        env.pop("APSIMO_API_KEY")
    selected_env = dict(desired["env"])
    if (sidecar_url is None and not any(os.environ.get(k) for k in (
            "APSIMO_SIDECAR_URL", "COLONY_SIDECAR_URL",
            "APSIMO_SIDECAR_PORT", "COLONY_SIDECAR_PORT"))
            and "APSIMO_URL" in env):
        selected_env["APSIMO_URL"] = env["APSIMO_URL"]
    env.update(selected_env)
    result = {**existing, **desired, "env": env}
    if (existing.get("command") not in {None, "colony", "apsimo"}
            and not (os.environ.get("APSIMO_MCP_COMMAND") or os.environ.get("COLONY_MCP_COMMAND"))):
        result["command"] = existing["command"]
        result["args"] = existing.get("args", [])
    return result


def _update_servers(servers: dict, desired: dict, sidecar_url: Optional[str]) -> bool:
    new = _merged(_selected(servers), desired, sidecar_url)
    if servers.get("apsimo") == new and "colony" not in servers:
        return False
    servers.pop("colony", None)
    servers["apsimo"] = new
    return True


def _add_to_json_config(hdef: dict, contact_id: str, source: str,
                        dry_run: bool = False, sidecar_url: Optional[str] = None) -> Optional[str]:
    path = Path(hdef["config_path"]).expanduser()
    data = _read_json(path)
    servers = data.setdefault(hdef.get("mcp_key", "mcpServers"), {})
    desired = _mcp_config(contact_id, source, hdef.get("mcp_type") == "stdio", sidecar_url)
    if not _update_servers(servers, desired, sidecar_url):
        return None
    if not dry_run:
        _write_json(path, data)
    return "  Configure one Apsimo MCP entry; preserve other servers and credentials"


def _without_toml_entries(content: str) -> str:
    """Remove only our two server tables, preserving following tables verbatim."""
    output, skipping = [], False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            match = re.match(r"^\[\s*mcp_servers\s*\.\s*(?:colony|apsimo)(?:\s*\.[^]]*)?\s*\]", stripped)
            skipping = bool(match)
        if not skipping:
            output.append(line)
    return "".join(output)


def _toml_block(config: dict) -> str:
    # JSON string escaping is TOML basic-string escaping for our scalar values.
    def key(name):
        return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)

    def value(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, dict):
            return "{ " + ", ".join(key(k) + " = " + value(v) for k, v in item.items()) + " }"
        if isinstance(item, list):
            return "[" + ", ".join(value(v) for v in item) + "]"
        return json.dumps(item, ensure_ascii=False)
    return "\n[mcp_servers.apsimo]\n" + "".join(
        key(k) + " = " + value(v) + "\n" for k, v in config.items())


def _add_to_toml_config(config_path: str, contact_id: str, source: str,
                        dry_run: bool = False, sidecar_url: Optional[str] = None) -> Optional[str]:
    path = Path(config_path).expanduser()
    content = path.read_text() if path.exists() else ""
    servers = tomllib.loads(content).get("mcp_servers", {})
    desired = _mcp_config(contact_id, source, sidecar_url=sidecar_url)
    if not _update_servers(servers, desired, sidecar_url):
        return None
    updated = _without_toml_entries(content).rstrip("\n") + _toml_block(servers["apsimo"])
    parsed = tomllib.loads(updated)
    if parsed.get("mcp_servers") != servers:
        raise ValueError("Unsupported MCP TOML table layout; configuration was not modified")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
    return "  Configure one Apsimo MCP entry; preserve other TOML tables and credentials"


def _add_to_yaml_config(config_path: str, contact_id: str, source: str,
                        dry_run: bool = False, sidecar_url: Optional[str] = None) -> Optional[str]:
    if yaml is None:
        return "  PyYAML not installed; run: pip install pyyaml"
    path = Path(config_path).expanduser()
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    desired = _mcp_config(contact_id, source, sidecar_url=sidecar_url)
    if not _update_servers(data.setdefault("mcp_servers", {}), desired, sidecar_url):
        return None
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return "  Configure one Apsimo MCP entry; preserve other settings and credentials"


def add_to_harness(harness_id: str, contact_id: str, dry_run: bool = False,
                   sidecar_url: Optional[str] = None) -> Optional[str]:
    """Configure a single Apsimo MCP server without mutating process settings."""
    hdef = HARNESS_DEFS.get(harness_id)
    if not hdef:
        return f"  Unknown harness: {harness_id}"
    args = (contact_id, hdef["source_tag"], dry_run, sidecar_url)
    if hdef["config_format"] == "json":
        return _add_to_json_config(hdef, *args)
    if hdef["config_format"] == "toml":
        return _add_to_toml_config(hdef["config_path"], *args)
    if hdef["config_format"] == "yaml":
        return _add_to_yaml_config(hdef["config_path"], *args)
    return None


def remove_from_harness(harness_id: str, dry_run: bool = False) -> Optional[str]:
    """Remove either recognized MCP name, leaving unrelated settings intact."""
    hdef = HARNESS_DEFS.get(harness_id)
    if not hdef:
        return f"  Unknown harness: {harness_id}"
    path = Path(hdef["config_path"]).expanduser()
    if not path.exists():
        return None
    fmt = hdef["config_format"]
    if fmt == "toml":
        content = path.read_text()
        data = tomllib.loads(content)
        key = "mcp_servers"
    elif fmt == "yaml":
        if yaml is None:
            return "  PyYAML not installed"
        data = yaml.safe_load(path.read_text()) or {}
        key = "mcp_servers"
    else:
        data = _read_json(path)
        key = hdef.get("mcp_key", "mcpServers")
    servers = data.get(key, {})
    if not any(name in servers for name in ("colony", "apsimo")):
        return None
    for name in ("colony", "apsimo"):
        servers.pop(name, None)
    if fmt == "toml":
        updated = _without_toml_entries(content)
        if tomllib.loads(updated).get(key, {}) != servers:
            raise ValueError("Unsupported MCP TOML table layout; configuration was not modified")
    if not dry_run:
        if fmt == "toml":
            path.write_text(updated)
        elif fmt == "yaml":
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        else:
            _write_json(path, data)
    return f"  Removed Apsimo MCP integration from {hdef['display']} config"

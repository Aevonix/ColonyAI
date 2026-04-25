"""Detect harness installations and workspaces."""

from __future__ import annotations

import json
from pathlib import Path


def detect_openclaw_workspace() -> Path | None:
    """Detect OpenClaw workspace directory.
    
    Checks:
    1. agents.defaults.workspace in ~/.openclaw/openclaw.json
    2. Default: ~/.openclaw/workspace
    
    Returns:
        Path to workspace, or None if not found
    """
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    
    # Try to read from config
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            workspace = config.get("agents", {}).get("defaults", {}).get("workspace")
            if workspace:
                path = Path(workspace).expanduser()
                if path.exists():
                    return path
        except (json.JSONDecodeError, KeyError):
            pass
    
    # Fall back to default
    default = Path.home() / ".openclaw" / "workspace"
    if default.exists():
        return default
    
    return None

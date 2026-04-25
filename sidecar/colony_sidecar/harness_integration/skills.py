"""Write Colony diagnostic skill to harness skills directories."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


# Skill directory paths for each harness
SKILL_PATHS = {
    "openclaw": None,  # Special case - uses workspace_dir
    "crush": "~/.config/crush/skills/colony-diagnose",
    "codex": "~/.codex/skills/colony-diagnose",
    "claude-code": "~/.codex/skills/colony-diagnose",  # Shares with Codex
    "opencode": "~/.config/opencode/skills/colony-diagnose",
    "hermes": None,  # Plugin-based, no skill directory
}

# Config paths for skills_paths updates (Crush only)
CONFIG_PATHS = {
    "crush": ["~/.crush.json", "~/.config/crush/crush.json"],
}


COLONY_DIAGNOSTIC_SKILL = """---
name: colony-diagnose
description: Diagnose Colony connection issues. Use when Colony MCP tools return errors, context is not loading, or user reports "colony not working".
---

# Colony Diagnostic Skill

## Trigger

- User reports Colony issues
- MCP tools return connection errors
- Context not loading from Colony

## Diagnostic Steps

### 1. Check Sidecar Status

```bash
colony status
```

**Expected:** `Sidecar is healthy`

**Fix if not running:** `colony start -d`

### 2. Check API Connectivity

```bash
curl -s http://127.0.0.1:7777/v1/host/capabilities \\
  -H "Authorization: Bearer colony"
```

**Expected:** JSON with `capabilities` array

**Fix if 401:** Check `COLONY_API_KEY` in `~/.colony/.env` matches harness config

### 3. Check MCP Configuration

**Crush:**
```bash
cat ~/.config/crush/crush.json | jq '.mcp.colony'
```

**Claude Code:**
```bash
cat ~/.claude.json | jq '.mcpServers.colony'
```

**Codex:**
```bash
cat ~/.codex/config.toml | grep -A10 'mcp_servers.colony'
```

**Expected:** `command`, `args`, `env.COLONY_URL`, `env.COLONY_API_KEY`

**Fix if missing:** Run `colony mcp setup --harness <name>`

### 4. Check Plugin Status (OpenClaw only)

```bash
openclaw plugins list --json | jq '.plugins[] | select(.id=="colony")'
```

**Expected:** `status: "loaded"` or `"enabled"`

**Fix if not loaded:**
```bash
openclaw gateway restart
```

### 5. Test Context Flow

Store a test fact:
```bash
curl -X POST http://127.0.0.1:7777/v1/host/mind/facts \\
  -H "Authorization: Bearer colony" \\
  -H "Content-Type: application/json" \\
  -d '{"contact_id": "test", "fact": "Diagnostic test", "source": "shared_context"}'
```

Verify via MCP tool `colony_lookup_facts` with query "Diagnostic test".

## Common Issues

| Issue | Fix |
|-------|-----|
| Sidecar not running | `colony start -d` |
| 401 Unauthorized | Check `COLONY_API_KEY` matches in `.env` and harness config |
| MCP not loading | Restart harness after config change |
| Plugin not loading | `openclaw gateway restart` |
| Neo4j errors | `docker start neo4j` or re-run `colony init` |
| Connection refused | Check firewall allows port 7777 |

## Architecture

```
Colony Sidecar (:7777)
    │
    ├── Plugin API ──────► OpenClaw, Hermes
    │
    └── MCP Server ───────► Crush, Codex, Claude Code, OpenCode
```

Both paths read/write to the same cognitive stores (facts, commitments, etc.).
"""


def get_skill_path(harness_id: str) -> Path | None:
    """Get the skill directory path for a harness.
    
    Args:
        harness_id: Harness identifier (e.g., 'crush', 'openclaw')
    
    Returns:
        Path to skill directory, or None if harness doesn't support skills
    """
    path_str = SKILL_PATHS.get(harness_id)
    if path_str:
        return Path(path_str).expanduser()
    return None


def get_skill_config_path(harness_id: str) -> Path | None:
    """Get the harness config path for skills_paths update.
    
    Args:
        harness_id: Harness identifier
    
    Returns:
        Path to config file, or None if not applicable
    """
    config_paths = CONFIG_PATHS.get(harness_id, [])
    for path_str in config_paths:
        path = Path(path_str).expanduser()
        if path.exists():
            return path
    return None


def write_colony_skill(harness_id: str, workspace_dir: Path | None = None) -> bool:
    """Write Colony diagnostic skill to harness skills directory.
    
    For Crush: also updates options.skills_paths in config.
    
    Args:
        harness_id: Harness identifier (e.g., 'crush', 'openclaw')
        workspace_dir: Optional workspace dir (used for OpenClaw to override)
    
    Returns:
        True if written successfully, False otherwise (including if harness doesn't support skills)
    """
    # Special handling for OpenClaw - use workspace skills dir
    if harness_id == "openclaw" and workspace_dir:
        skill_dir = workspace_dir / "skills" / "colony-diagnose"
    else:
        skill_dir = get_skill_path(harness_id)
    
    # Harness doesn't support skills (e.g., Hermes)
    if not skill_dir:
        return False
    
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(COLONY_DIAGNOSTIC_SKILL)
        
        # For Crush, update skills_paths in config
        if harness_id == "crush":
            _update_crush_skills_paths()
        
        return True
    except Exception:
        return False


def remove_colony_skill(harness_id: str) -> bool:
    """Remove Colony diagnostic skill from harness skills directory.
    
    Args:
        harness_id: Harness identifier
    
    Returns:
        True if removed successfully, False if not found or error
    """
    skill_dir = get_skill_path(harness_id)
    
    if not skill_dir or not skill_dir.exists():
        return False
    
    try:
        shutil.rmtree(skill_dir)
        return True
    except Exception:
        return False


def _update_crush_skills_paths() -> bool:
    """Internal: Add ~/.config/crush/skills to Crush's skills_paths if not present.
    
    Crush config can be at:
    1. ~/.crush.json (checked first by Crush)
    2. ~/.config/crush/crush.json (checked second)
    
    We check both and update whichever exists, preferring ~/.crush.json.
    If neither exists, we don't create one just for skills_paths.
    """
    skill_path_to_add = "~/.config/crush/skills"
    
    # Find existing config
    config_path = get_skill_config_path("crush")
    if not config_path:
        return False  # No config exists, don't create one
    
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return False
    
    # Check if skills_paths already includes our path
    options = data.get("options", {})
    skills_paths = options.get("skills_paths", [])
    
    # Normalize paths for comparison
    normalized_new = str(Path(skill_path_to_add).expanduser())
    normalized_existing = [str(Path(p).expanduser()) for p in skills_paths]
    
    if normalized_new in normalized_existing:
        return True  # Already present
    
    # Add the path
    skills_paths.append(skill_path_to_add)
    options["skills_paths"] = skills_paths
    data["options"] = options
    
    try:
        config_path.write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False

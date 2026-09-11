"""Write Apsimo diagnostic skill to harness skills directories."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


# Skill directory paths for each harness
SKILL_PATHS = {
    "openclaw": None,  # Special case - uses workspace_dir
    "crush": "~/.config/crush/skills/apsimo-diagnose",
    "codex": "~/.codex/skills/apsimo-diagnose",
    "claude-code": "~/.codex/skills/apsimo-diagnose",  # Shares with Codex
    "opencode": "~/.config/opencode/skills/apsimo-diagnose",
    "hermes": None,  # Plugin-based, no skill directory
}

# Config paths for skills_paths updates (Crush only)
CONFIG_PATHS = {
    "crush": ["~/.crush.json", "~/.config/crush/crush.json"],
}


APSIMO_DIAGNOSTIC_SKILL = """---
name: apsimo-diagnose
description: Diagnose Apsimo or legacy ColonyAI connection failures and missing recalled context using the selected instance.
---

# Apsimo diagnostics

1. Run `apsimo status` and `apsimo doctor`. Read the reported endpoint and instance
   instead of assuming a directory, machine, port, or database backend.
2. For coding harnesses, run `apsimo mcp detect`. Confirm one `apsimo` MCP server
   is configured. Legacy `colony` entries are migrated by `apsimo mcp setup --harness <name>`.
3. Call `apsimo_health`. If it fails, report the actual transport or authentication
   error. Use the selected instance's credential reference; never guess a key,
   print its value, or dump a whole configuration into a transcript.
4. If health passes, use `apsimo_get_context` for the authenticated contact and
   `apsimo_lookup_facts` for a known source-backed fact. Report what was actually
   retrieved. Do not insert test facts into ordinary memory.
5. For Hermes, inspect its selected profile's plugin and memory-provider status.
   Correct the indicated connection or configuration, then repeat the failed
   probe once. A successful HTTP request alone does not prove useful recollection.

Apsimo is the platform's new name; Aeva or another private agent keeps its own
identity. Existing state paths and COLONY_* variables remain supported. Conflicting
explicit APSIMO_* and COLONY_* settings must be resolved at configuration load.
"""
COLONY_DIAGNOSTIC_SKILL = APSIMO_DIAGNOSTIC_SKILL


def get_skill_path(harness_id: str) -> Path | None:
    """Get the skill directory path for a harness.
    
    Args:
        harness_id: Harness identifier (e.g., 'crush', 'openclaw')
    
    Returns:
        Path to skill directory, or None if harness doesn't support skills
    """
    path_str = SKILL_PATHS.get(harness_id)
    if path_str:
        canonical = Path(path_str).expanduser()
        legacy = canonical.with_name("colony-diagnose")
        # Reuse an installed legacy directory, so setup does not discover two
        # copies of one skill. New installations use the canonical directory.
        return legacy if legacy.exists() and not canonical.exists() else canonical
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


def write_apsimo_skill(harness_id: str, workspace_dir: Path | None = None) -> bool:
    """Write Apsimo diagnostic skill to harness skills directory.
    
    For Crush: also updates options.skills_paths in config.
    
    Args:
        harness_id: Harness identifier (e.g., 'crush', 'openclaw')
        workspace_dir: Optional workspace dir (used for OpenClaw to override)
    
    Returns:
        True if written successfully, False otherwise (including if harness doesn't support skills)
    """
    # Special handling for OpenClaw - use workspace skills dir
    if harness_id == "openclaw" and workspace_dir:
        skill_dir = workspace_dir / "skills" / "apsimo-diagnose"
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


def remove_apsimo_skill(harness_id: str) -> bool:
    """Remove Apsimo diagnostic skill from harness skills directory.
    
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


# ---------------------------------------------------------------------------
# Apsimo Check Skill (for on-demand initiative checking)
# ---------------------------------------------------------------------------

APSIMO_CHECK_SKILL = """---
name: apsimo-check
description: Review current commitments and useful work through the selected Apsimo instance.
---

Use `apsimo_get_context` and `apsimo_check_commitments` for the authenticated
contact. Summarize overdue work, current commitments, and source-backed surprises.
Distinguish observations from proposed actions. Respect existing task ownership
and execution limits; do not create duplicate work or claim that a task ran
without its execution evidence. Do not write a memory merely because this check
was performed. If a tool fails, report its actual blocker and change strategy.
"""
COLONY_CHECK_SKILL = APSIMO_CHECK_SKILL


def write_apsimo_check_skill(workspace_dir: Path) -> bool:
    """Write apsimo-check skill to OpenClaw workspace skills directory.

    Args:
        workspace_dir: OpenClaw workspace directory

    Returns:
        True if written successfully, False otherwise
    """
    skill_dir = workspace_dir / "skills" / "apsimo-check"
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(COLONY_CHECK_SKILL)
        return True
    except Exception:
        return False


write_colony_skill = write_apsimo_skill
write_colony_check_skill = write_apsimo_check_skill
remove_colony_skill = remove_apsimo_skill

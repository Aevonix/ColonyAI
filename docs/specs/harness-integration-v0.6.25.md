# Colony Harness Integration Spec v0.6.25

## Overview

Fully automated integration system that writes context files and skills to each harness's native locations during `colony init` and `colony mcp setup`.

---

## Part 1: Remove `colony spawn`

### Rationale

`colony spawn` was a misplacement — Colony is a cognitive substrate, not an orchestration layer. The command hardcodes a specific workflow pattern that doesn't generalize to different user setups.

### Files to Modify

**`sidecar/colony_sidecar/cli.py`**

1. Remove `spawn_p` subparser definition (~lines 138-150)
2. Remove `elif args.command == "spawn": _cmd_spawn(args)` handler (~line 495)
3. Remove `def _cmd_spawn(args)` function (~lines 655-775)

### Environment Variables to Remove

The following `COLONY_SPAWN_*` environment variables are no longer needed:

- `COLONY_SPAWN_HOST` — Coding server hostname
- `COLONY_SPAWN_USER` — SSH user
- `COLONY_SPAWN_CONTACT` — Contact ID for context
- `COLONY_SPAWN_MODEL` — Model for the agent
- `COLONY_SPAWN_AGENT` — Default agent (crush, codex, claude-code)
- `COLONY_SPAWN_CRUSH_PATH` — Path to crush binary
- `COLONY_SPAWN_CODEX_PATH` — Path to codex binary
- `COLONY_SPAWN_CLAUDE_PATH` — Path to claude binary

### Code Locations (for reference)

```
cli.py:136      # --- spawn ---
cli.py:141-149  spawn_p.add_argument() definitions
cli.py:494      elif args.command == "spawn": _cmd_spawn(args)
cli.py:655-775  def _cmd_spawn(args) function
```

### Cleanup Verification

After removal, verify no orphaned references:
```bash
grep -rn "COLONY_SPAWN\|colony spawn\|_cmd_spawn" sidecar/colony_sidecar/
```

Expected result: No matches (except possibly in comments/strings unrelated to the command)

---

## Part 2: Create `docs/HARNESS_INTEGRATION.md`

### Purpose

Reference documentation for all integration patterns. Ships with Colony and answers "how do I connect X to Colony?"

### Structure

```
HARNESS_INTEGRATION.md
├── Architecture Overview
│   ├── Diagram: Plugin path vs MCP path
│   └── Context flow between harnesses
├── Agent Harnesses (Plugin)
│   ├── OpenClaw
│   └── Hermes
├── Coding Harnesses (MCP)
│   ├── Crush
│   ├── Codex
│   ├── Claude Code
│   └── OpenCode
├── MCP Tools Reference (14 tools)
├── API Endpoints Reference
├── Distributed Setups
├── Troubleshooting
└── Files Written by Colony
```

---

## Part 3: Create `harness_integration/` Module

### Directory Structure

```
sidecar/colony_sidecar/harness_integration/
├── __init__.py
├── context.py      # COLONY.md template and writer
├── skills.py       # Diagnostic skill template and writer
└── detect.py       # Harness detection utilities
```

### File: `__init__.py`

Exports:
- `write_colony_context(workspace_dir: Path) -> bool`
- `write_colony_skill(harness_id: str, workspace_dir: Path | None = None) -> bool`
- `remove_colony_skill(harness_id: str) -> bool`
- `get_skill_path(harness_id: str) -> Path | None`
- `get_skill_config_path(harness_id: str) -> Path | None`
- `detect_openclaw_workspace() -> Path | None`
- `COLONY_CONTEXT_TEMPLATE`
- `COLONY_DIAGNOSTIC_SKILL`

### File: `context.py`

Contains `COLONY_CONTEXT_TEMPLATE` — markdown content for `COLONY.md`:

- Quick reference table of MCP tools
- API endpoints table
- Context flow diagram
- Configuration reference
- Troubleshooting pointer

Function:
```python
def write_colony_context(workspace_dir: Path) -> bool:
    """Write COLONY.md to the harness workspace.
    
    Args:
        workspace_dir: Path to the harness workspace (e.g., ~/.openclaw/workspace)
    
    Returns:
        True if written successfully, False otherwise
    """
```

### File: `skills.py`

Contains `COLONY_DIAGNOSTIC_SKILL` — SKILL.md content for diagnostics:

- Triggers: "colony not working", connection errors
- 5 diagnostic steps with expected outputs
- Common issues table with fixes
- Architecture diagram

**Important:** Keep `SKILL_PATHS` dict in this file (do NOT import from mcp/config.py to avoid circular imports)

Skill directory paths:

| Harness | Path |
|---------|------|
| OpenClaw | `~/.openclaw/workspace/skills/colony-diagnose/` |
| Crush | `~/.config/crush/skills/colony-diagnose/` |
| Codex | `~/.codex/skills/colony-diagnose/` |
| Claude Code | `~/.codex/skills/colony-diagnose/` (shared) |
| OpenCode | `~/.config/opencode/skills/colony-diagnose/` |
| Hermes | None (has MCP config in `~/.hermes/config.yaml`, but no skill directory) |

**Crush `skills_paths` handling:**

Crush requires `options.skills_paths` in config to discover skills. When writing a skill for Crush:

1. Write skill to `~/.config/crush/skills/colony-diagnose/SKILL.md`
2. Read Crush config from `~/.crush.json` (or `~/.config/crush/crush.json`)
3. If `options.skills_paths` doesn't include `~/.config/crush/skills`, add it
4. Write updated config

This ensures Crush will discover the skill automatically.

Functions:
```python
def get_skill_path(harness_id: str) -> Path | None:
    """Get the skill directory path for a harness."""

def get_skill_config_path(harness_id: str) -> Path | None:
    """Get the harness config path for skills_paths update (Crush only)."""

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

def remove_colony_skill(harness_id: str) -> bool:
    """Remove Colony diagnostic skill from harness skills directory.
    
    Args:
        harness_id: Harness identifier
    
    Returns:
        True if removed successfully, False if not found or error
    """

def _update_crush_skills_paths() -> bool:
    """Internal: Add ~/.config/crush/skills to Crush's skills_paths if not present.
    
    Crush config can be at:
    1. ~/.crush.json (checked first by Crush)
    2. ~/.config/crush/crush.json (checked second)
    
    We check both and update whichever exists, preferring ~/.crush.json.
    If neither exists, we don't create one just for skills_paths.
    """
```

### File: `detect.py`

**Note:** Do NOT create `detect_all_harnesses()` — use existing `detect_harnesses()` from `mcp/config.py` for coding harnesses.

Only OpenClaw-specific detection is needed here:

```python
def detect_openclaw_workspace() -> Path | None:
    """Detect OpenClaw workspace directory.
    
    Checks:
    1. agents.defaults.workspace in ~/.openclaw/openclaw.json
    2. Default: ~/.openclaw/workspace
    
    Returns:
        Path to workspace, or None if not found
    """
```

---

## Part 4: Modify `setup.py` - OpenClaw Integration

### Location

`_configure_openclaw_plugin()` after config is written successfully

### Changes

After successful plugin configuration (after the `print("  ✅ Plugin configuration written")` line):

```python
# Write Colony context to OpenClaw workspace
from colony_sidecar.harness_integration import write_colony_context, write_colony_skill
from colony_sidecar.harness_integration.detect import detect_openclaw_workspace

workspace = detect_openclaw_workspace()
if workspace:
    if write_colony_context(workspace):
        print("  ✅ Colony context written to OpenClaw workspace")
    
    if write_colony_skill("openclaw", workspace):
        print("  ✅ Colony diagnostic skill installed")
else:
    print("  ⚠️ Could not detect OpenClaw workspace — context file not written")
    print("     Manually create: ~/.openclaw/workspace/COLONY.md")
```

---

## Part 5: Modify `cli.py` - MCP Setup Output

### Location

`_cmd_mcp()` function, in the `args.mcp_command == "setup"` branch, inside the `for hid in selected:` loop

### Current Code (around the add_to_harness call)

```python
diff = add_to_harness(hid, contact_id, dry_run=args.dry_run)
if diff is None:
    print(f"  Already configured — skipping")
elif args.dry_run:
    print(f"  Would add (dry run):")
    print(diff)
else:
    print(f"  Added Colony MCP (source: {hdef['source_tag']})")
```

### Changes

After the existing `else:` block, add skill writing:

```python
else:
    print(f"  Added Colony MCP (source: {hdef['source_tag']})")
    
    # Write skill
    from colony_sidecar.harness_integration import write_colony_skill
    if write_colony_skill(hid):
        print(f"  ✅ Diagnostic skill installed")
```

**Important:** Use lazy import (`from colony_sidecar.harness_integration import write_colony_skill`) inside the function to avoid circular imports.

---

## Part 6: Modify `cli.py` - MCP Remove

### Location

`_cmd_mcp()` function, in the `args.mcp_command == "remove"` branch

### Changes

After removing MCP config, also remove skill:

```python
elif args.mcp_command == "remove":
    # ... existing harness selection code ...
    
    for hid in selected:
        diff = remove_from_harness(hid, dry_run=args.dry_run)
        if diff:
            print(diff)
            if not args.dry_run:
                print(f"  Removed Colony MCP from {HARNESS_DEFS[hid]['display']}")
                
                # Remove skill
                from colony_sidecar.harness_integration import remove_colony_skill
                if remove_colony_skill(hid):
                    print(f"  ✅ Diagnostic skill removed")
        else:
            print(f"  Colony not configured in {HARNESS_DEFS[hid]['display']}")
```

---

## Part 7: Update Interactive `colony init` - MCP Harness Setup

### Location

`setup.py` - Step 10 (or wherever `_setup_mcp_harnesses()` is called)

### Current Behavior (BUG)

`_setup_mcp_harnesses()` calls `_setup_mcp_harness()` for each harness, but the current code tries to import `configure_harness` which doesn't exist. It should call `add_to_harness()` instead.

### Changes

Replace the entire `_setup_mcp_harness()` function:

```python
def _setup_mcp_harness(harness: str, api_key: str, sidecar_url: str, non_interactive: bool = False) -> bool:
    """Configure MCP for a single coding harness."""
    try:
        from colony_sidecar.mcp.config import add_to_harness
        
        # Get contact_id from environment or default
        contact_id = os.environ.get("COLONY_MCP_CONTACT_ID", os.environ.get("USER", "user"))
        
        result = add_to_harness(harness, contact_id, dry_run=False, sidecar_url=sidecar_url)
        
        if result is not None:
            print(f"  ✅ {harness} MCP configured")
            
            # Write skill
            from colony_sidecar.harness_integration import write_colony_skill
            if write_colony_skill(harness):
                print(f"  ✅ {harness} diagnostic skill installed")
            
            return True
        else:
            print(f"  ⚪ {harness} already configured")
            return True
    except Exception as exc:
        print(f"  ⚠️ MCP config failed for {harness}: {exc}")
        return False
```

---

## Part 8: Version Bump

All version files → `0.6.25`:
- `package.json`
- `sidecar/pyproject.toml`
- `openclaw.plugin.json`

---

## Files Summary

| File | Action |
|------|--------|
| `cli.py` | Remove spawn (~130 lines), update mcp setup (~5 lines), update mcp remove (~5 lines) |
| `setup.py` | Add context/skill writing in OpenClaw plugin (~15 lines), update MCP setup (~5 lines) |
| `docs/HARNESS_INTEGRATION.md` | Create (~300 lines) |
| `harness_integration/__init__.py` | Create (~30 lines) |
| `harness_integration/context.py` | Create (~80 lines) |
| `harness_integration/skills.py` | Create (~130 lines) |
| `harness_integration/detect.py` | Create (~50 lines) |

**Note:** `mcp/config.py` is NOT modified — skill writing happens at the CLI/setup layer, not in the config layer.

---

## Dependency Graph

```
harness_integration/
├── skills.py      (no imports from mcp/config.py)
├── context.py     (no imports from mcp/config.py)
└── detect.py      (no imports from mcp/config.py)

cli.py
├── imports from mcp/config.py (existing)
└── lazy imports from harness_integration (new)

setup.py
└── imports from harness_integration (new)
```

No circular imports because:
1. `harness_integration/` does not import from `mcp/config.py`
2. Imports from `harness_integration` are lazy (inside functions)

---

## Testing Checklist

| Test | Command | Expected |
|------|---------|----------|
| Spawn removed | `colony spawn --help` | Error: unknown command |
| No spawn refs | `grep -rn "COLONY_SPAWN" sidecar/` | No results |
| No _cmd_spawn | `grep -rn "_cmd_spawn" sidecar/` | No results |
| OpenClaw context | `colony init --agent-harness openclaw` | `COLONY.md` created |
| OpenClaw skill | Same | Skill in `workspace/skills/` |
| Crush MCP + skill | `colony mcp setup --harness crush` | Both installed |
| Crush skills_paths | Same | `~/.config/crush/skills` in config |
| Remove cleans up | `colony mcp remove --harness crush` | MCP config + skill removed |
| Auto-detect | `colony mcp setup` | All detected harnesses |
| Context visible | OpenClaw session | `COLONY.md` in context |
| Skill triggers | "colony not working" | Diagnostic runs |

---

## Commit Message

```
feat(integration): automated harness context + skill installation

BREAKING: Remove 'colony spawn' command (orchestration doesn't belong in Colony)

Add:
- docs/HARNESS_INTEGRATION.md: comprehensive integration reference
- harness_integration/ module: context + skill writing utilities
- Auto-write COLONY.md to OpenClaw workspace during init
- Auto-write diagnostic skill to all harness directories
- Clean up skills on `colony mcp remove`

Every harness now gets:
- MCP/plugin config (existing)
- COLONY.md context (OpenClaw only, always-loaded)
- colony-diagnose skill (all harnesses, on-demand)
```
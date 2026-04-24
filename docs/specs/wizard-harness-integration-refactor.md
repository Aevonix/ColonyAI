# Wizard Harness Integration Refactor

**Date:** 2026-04-24
**Status:** Draft
**Target Version:** 0.7.0

## Problem Statement

The current `colony init` wizard forces users to choose a "host framework" (OpenClaw, Hermes, etc.) even though Colony is a standalone sidecar that doesn't require any harness. This creates confusion and blocks users who want to:

1. Run Colony standalone (API-only mode)
2. Connect only coding harnesses (Claude Code, Codex, Crush) via MCP
3. Set up a harness later after the sidecar is running

## Design Principles

1. **Standalone is valid** — Colony runs as a sidecar exposing an HTTP/WebSocket API. No harness required.
2. **Detect, don't force** — Scan for existing harnesses and offer to connect them.
3. **Separate concerns** — Coding harnesses (MCP) are different from agent harnesses (OpenClaw/Hermes).
4. **Graceful guidance** — If user wants a harness that isn't installed, show instructions, don't fail.
5. **Non-interactive support** — All choices available via CLI flags.

## Harness Categories

### Coding Harnesses (MCP)

These are CLI coding agents that connect via MCP protocol:

| Harness | Config Path | Detection |
|---------|-------------|-----------|
| Claude Code | `~/.claude/settings.json` | Check for `~/.claude/` directory |
| Codex | `~/.codex/config.json` | Check for `~/.codex/` directory |
| Crush | `~/.crush/mcp.json` | Check for `~/.crush/` directory |
| OpenCode | `~/.opencode/` | Check for `~/.opencode/` directory |

**Setup:** Write MCP server config to harness's config file.

### Agent Harnesses (Plugin/API)

These are full agent frameworks that Colony integrates with as a plugin:

| Harness | Integration Type | Detection |
|---------|------------------|-----------|
| OpenClaw | Plugin (`openclaw plugins install`) | `which openclaw` |
| Hermes | MemoryProvider plugin | `which hermes` or config file |

**Setup:** Install plugin and configure API endpoint + key.

## Proposed Wizard Flow

### Step 3: Harness Integration (Interactive)

```
Step 3: Harness integration

Colony integrates with coding agents and agent frameworks via MCP and plugins.

Detected coding harnesses:
  [1] Claude Code

Connect Claude Code via MCP? [Y/n] y
  ✅ MCP config written to ~/.claude/settings.json

Detected agent harnesses:
  (none)

Configure an agent harness?
  [1] OpenClaw (recommended for production deployments)
  [2] Hermes (experimental)
  [3] Skip — run standalone

Choice [3]: 1

OpenClaw CLI found at /usr/bin/openclaw
Installing Colony plugin...
  ✅ Colony plugin installed
  ✅ Colony set as active context engine

Colony plugin configuration:
  sidecarUrl: http://127.0.0.1:7777
  apiKey: xK9mN...

Restart OpenClaw gateway to load the plugin? [Y/n] y
  ✅ Gateway restarted
```

### Step 3: No Harnesses Detected (Interactive)

```
Step 3: Harness integration

No coding or agent harnesses detected.

Colony will run as a standalone sidecar at http://localhost:7777
You can integrate with a harness later or use the API directly.

Set up a harness now?
  [1] OpenClaw (recommended for production)
  [2] Hermes (experimental)
  [3] Claude Code (via MCP)
  [4] Skip — run standalone

Choice [4]: 4

Running standalone. API docs: http://localhost:7777/docs
To connect a harness later: colony mcp setup --harness <name>
```

### Step 3: Harness Not Installed (Interactive)

```
Step 3: Harness integration

Configure an agent harness?
  [1] OpenClaw (recommended for production)
  [2] Hermes (experimental)
  [3] Skip — run standalone

Choice [3]: 1

OpenClaw CLI not found in PATH.

To install OpenClaw:

  Linux:  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
          sudo apt install nodejs
          sudo npm install -g openclaw

  macOS:  brew install node@22
          npm install -g openclaw

After installing, run: openclaw gateway install && openclaw gateway start

Then re-run: colony init

Continue without OpenClaw? [Y/n] y

Running standalone. Colony API at http://localhost:7777
```

### Step 3: Non-Interactive Mode

```bash
# Standalone (default)
colony init --non-interactive

# Connect coding harnesses only
colony init --non-interactive --mcp-harnesses claude-code,codex

# Connect OpenClaw plugin
colony init --non-interactive --agent-harness openclaw

# Connect Hermes plugin
colony init --non-interactive --agent-harness hermes

# Connect multiple
colony init --non-interactive --mcp-harnesses claude-code --agent-harness openclaw

# Skip all harness setup (explicit)
colony init --non-interactive --no-harness
```

## CLI Flag Reference

| Flag | Argument | Description |
|------|----------|-------------|
| `--non-interactive` | — | Run without prompts, use defaults |
| `--mcp-harnesses` | `claude-code,codex,crush,opencode` | Connect coding harnesses via MCP |
| `--agent-harness` | `openclaw,hermes` | Connect agent harness via plugin |
| `--no-harness` | — | Skip all harness setup (standalone) |

**Backward compatibility:** `--host-framework openclaw` maps to `--agent-harness openclaw`.

## Detection Logic

```python
def detect_coding_harnesses() -> list[str]:
    """Detect installed coding harnesses that support MCP."""
    harnesses = []
    if Path.home().joinpath(".claude").exists():
        harnesses.append("claude-code")
    if Path.home().joinpath(".codex").exists():
        harnesses.append("codex")
    if Path.home().joinpath(".crush").exists():
        harnesses.append("crush")
    if Path.home().joinpath(".opencode").exists():
        harnesses.append("opencode")
    return harnesses

def detect_agent_harnesses() -> list[str]:
    """Detect installed agent harnesses."""
    harnesses = []
    if shutil.which("openclaw"):
        harnesses.append("openclaw")
    if shutil.which("hermes") or Path.home().joinpath(".hermes").exists():
        harnesses.append("hermes")
    return harnesses
```

## Setup Functions

### MCP Harness Setup (Existing)

Already implemented in `colony mcp setup`. Wizard should call:

```python
def _setup_mcp_harness(harness: str, api_key: str, sidecar_url: str) -> bool:
    """Configure MCP for a coding harness."""
    # Call existing mcp config logic
    from colony_sidecar.mcp.config import configure_harness
    return configure_harness(harness, api_key, sidecar_url)
```

### Agent Harness Setup

```python
def _setup_agent_harness(harness: str, api_key: str, sidecar_url: str, non_interactive: bool) -> bool:
    """Configure agent harness plugin."""
    if harness == "openclaw":
        return _setup_openclaw_plugin(api_key, sidecar_url, non_interactive)
    elif harness == "hermes":
        return _setup_hermes_plugin(api_key, sidecar_url, non_interactive)
    return False
```

## OpenClaw Setup Improvements

Current OpenClaw setup should be improved to:

1. **Check Node.js stability:**
   ```python
   def _check_nodejs_stability() -> tuple[bool, str]:
       """Check if Node.js is installed system-wide (stable) or via version manager (unstable)."""
       node_path = shutil.which("node")
       if not node_path:
           return False, "Node.js not found"
       
       unstable_paths = ["/.nvm/", "/.nvm/versions/", "/.volta/", "/.asdf/", "/.local/share/nvm/"]
       for unstable in unstable_paths:
           if unstable in node_path:
               return False, f"Node.js from version manager ({node_path})"
       
       return True, node_path
   ```

2. **Warn but continue if unstable:**
   ```
   ⚠️ Node.js is installed via nvm (version manager).
   This works for development but may break in production.
   Consider installing Node.js system-wide for production deployments.
   
   Continue with plugin setup? [Y/n]
   ```

3. **Better error handling:**
   - Network errors → suggest checking internet connection
   - Permission errors → suggest `sudo` or fixing permissions
   - Missing manifest → suggest updating Colony

## Hermes Setup (Future)

Hermes integration via MemoryProvider plugin already exists. Wizard should:

1. Check if Hermes config exists
2. Add Colony as a MemoryProvider in Hermes config
3. Configure API endpoint and key

Implementation deferred pending Hermes testing.

## Migration from Current Wizard

Current wizard has these concepts that need migration:

| Current | New |
|---------|-----|
| `--host-framework openclaw` | `--agent-harness openclaw` |
| `--host-framework hermes` | `--agent-harness hermes` |
| `--host-framework claude-code` | `--mcp-harnesses claude-code` |
| Interactive host framework prompt | Separate coding/agent harness prompts |

Backward compatibility: Accept old flags, map to new internally.

## Files to Modify

1. `sidecar/colony_sidecar/setup.py`
   - Refactor `_detect_host_framework()` into separate detection functions
   - Add `_setup_mcp_harness()` and `_setup_agent_harness()`
   - Update `run_setup()` flow for new Step 3

2. `sidecar/colony_sidecar/cli.py`
   - Add new CLI flags (`--mcp-harnesses`, `--agent-harness`, `--no-harness`)
   - Keep `--host-framework` for backward compatibility

3. `sidecar/colony_sidecar/mcp/config.py`
   - Already handles MCP harness setup — minor updates if needed

## Testing Plan

1. **Interactive tests:**
   - No harnesses detected → standalone mode
   - Coding harness detected → MCP setup offered
   - Agent harness detected → plugin setup offered
   - Both detected → both offered
   - Harness not installed → instructions shown

2. **Non-interactive tests:**
   - `--non-interactive` (standalone)
   - `--mcp-harnesses claude-code`
   - `--agent-harness openclaw`
   - `--mcp-harnesses claude-code --agent-harness openclaw`
   - `--no-harness` (explicit standalone)

3. **Backward compatibility:**
   - `--host-framework openclaw` still works
   - Old interactive flow still works

## Success Criteria

1. User can run `colony init` without any harness installed
2. User can connect only coding harnesses without an agent harness
3. User can connect only an agent harness without coding harnesses
4. User can run standalone with no harness setup
5. Non-interactive mode supports all configurations
6. Backward compatible with existing `--host-framework` flag

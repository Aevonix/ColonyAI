# Colony + Hermes Integration Spec

## Overview

Colony provides persistent cognitive infrastructure (commitments, affect, facts, patterns, world model) that any agent or coding tool can share. Hermes Agent is a full agent runtime with its own memory, tools, gateway, and context engine. This spec defines how Colony integrates with Hermes as a first-class host framework, alongside OpenClaw.

**Source code:** https://github.com/NousResearch/hermes-agent

**Guiding principle:** Colony is harness-agnostic. The sidecar, API, stores, and MCP server don't change for Hermes. Only the adapter layer between Hermes and Colony's sidecar is new code.

---

## What We Build Now (v0.6.3)

### 1. Hermes MCP Harness Config

Add Hermes to Colony's MCP harness configuration so `colony mcp setup` configures Hermes automatically.

**Hermes MCP config** lives in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  colony:
    command: "colony"
    args: ["mcp"]
    env:
      COLONY_API_KEY: "${COLONY_API_KEY}"
      COLONY_URL: "http://127.0.0.1:7777"
      COLONY_MCP_CONTACT_ID: "<user-chosen>"
      COLONY_MCP_SOURCE: "hermes"
```

**Implementation in Colony:**

- Add `hermes` to `HARNESS_DEFS` in `colony_sidecar/mcp/config.py`
  - `display`: "Hermes"
  - `detect_cmds`: `["hermes"]`
  - `config_path`: `"~/.hermes/config.yaml"`
  - `config_format`: `"yaml"` (new format, needs a `_add_to_yaml_config` writer)
  - `source_tag`: `"hermes"`
- Implement YAML config writer (similar to JSON and TOML writers already in config.py)
  - Read existing YAML, add `mcp_servers.colony` block
  - Preserve all other config (providers, models, toolsets, etc.)
  - Skip if already present
- Add `colony mcp setup --harness hermes` to CLI
- Add Hermes to `colony mcp detect`
- Add Hermes as a host framework choice in `colony init` setup wizard

**Result:** After `colony init` + `colony mcp setup`, Hermes auto-discovers all 14 Colony MCP tools as `mcp_colony_*`. Hermes can check commitments, record affect, search world model, create commitments, remember facts, etc. Zero code changes on Hermes's side.

**Status:** Done.

### 2. Colony Memory Provider Plugin for Hermes

Hermes has a pluggable memory provider system. The ABC is in `agent/memory_provider.py`. The default provider uses MEMORY.md/USER.md for persistent recall. Colony implements the MemoryProvider ABC to inject cognitive context and sync turns back for extraction.

**How Hermes's memory system works (from source):**

The MemoryManager (`agent/memory_manager.py`) orchestrates one built-in provider (always active) and at most one external provider. The lifecycle is:

1. `initialize(session_id, **kwargs)` — called at agent startup
2. `system_prompt_block()` — static text added to system prompt
3. `prefetch(query, session_id)` — called before each API call, returns context text
4. `sync_turn(user_msg, assistant_response)` — called after each turn
5. `shutdown()` — clean exit

Optional hooks include `on_session_end(messages)`, `on_pre_compress(messages)`, and `on_memory_write()`.

Only ONE external memory provider runs at a time (alongside the built-in). Colony should be configured as `memory.provider: colony` in config.yaml.

**Plugin structure:**

```
~/.hermes/plugins/memory/colony/
  __init__.py
  provider.py     # ColonyMemoryProvider
  SKILL.md        # Plugin metadata
```

**provider.py implementation:**

- `name` property returns `"colony"`
- `is_available()` checks sidecar health endpoint
- `initialize(session_id)` stores session and platform info
- `system_prompt_block()` returns brief note that Colony is active
- `prefetch(query)` calls `/v1/host/context/assemble`, formats sections as `<memory-context>` block
- `sync_turn(user_msg, assistant_response)` calls `/v1/host/turns/sync` for extraction
- `shutdown()` cleans up cached context

**Hermes config for the plugin:**

```yaml
# In ~/.hermes/config.yaml
memory:
  provider: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"
```

**Status:** Done.

### 3. Colony CLI: Hermes Setup

Update `colony init` setup wizard to offer Hermes as a host framework choice (alongside OpenClaw, Claude Code, Codex, Crush, OpenCode, Standalone).

When Hermes is selected:
- Verify `hermes` CLI is installed
- Configure MCP servers in `~/.hermes/config.yaml`
- Optionally install Colony memory provider plugin to `~/.hermes/plugins/memory/colony/`
- Set COLONY_MCP_CONTACT_ID in Hermes config
- Verify sidecar is reachable
- Offer to start Hermes gateway if not running

**Status:** Done (MCP config + setup wizard choice).

---

## What We Defer

### Cognition Channel Adapter

**Goal:** When Colony's autonomy loop fires a cognition trigger, the LLM call should go through Hermes's agent system instead of OpenClaw's `sessions_spawn`.

**Why deferred:** This requires a host-specific adapter. Colony's cognition channel currently routes through OpenClaw's `sessions_spawn` API. For Hermes, it would need to call Hermes's delegate/subagent system (`tools/delegate_tool.py`). The adapter interface needs to be designed so Colony can work with any host.

**Implementation sketch:**
1. Define a `CognitionChannel` ABC with a `spawn(prompt)` method
2. OpenClaw implementation: calls `sessions_spawn`
3. Hermes implementation: calls Hermes's delegate tool to spawn a subagent
4. Colony selects the implementation based on `COLONY_HOST_FRAMEWORK` env var

**Source references:**
- `tools/delegate_tool.py` — Hermes subagent delegation
- `colony_sidecar/cognition/trigger.py` — Current cognition trigger implementation

### Memory Bridge

**Goal:** Sync between Hermes's MEMORY.md/USER.md and Colony's cognitive stores.

**Why deferred:** Both systems have memory but with different philosophies. Hermes: small, curated, frozen per session (~3.5K chars). Colony: large, structured, live-updating graph stores. The mapping isn't straightforward.

Options to evaluate:
1. **Colony replaces Hermes memory.** Hermes's memory tool writes to Colony instead of MEMORY.md. Colony's context engine returns the relevant memory. Pros: single source of truth. Cons: breaks Hermes's prompt caching assumptions.
2. **Colony supplements Hermes memory.** Hermes keeps its own memory, Colony adds cognitive layers on top. Pros: no breaking changes. Cons: two memory systems to manage.
3. **Unidirectional sync.** Colony's stores feed INTO Hermes's MEMORY.md on session start. Hermes writes stay local. Pros: simple. Cons: Colony never learns from Hermes's own memory writes.

**Source references:**
- `tools/memory_tool.py` — Hermes memory tool implementation
- `agent/memory_manager.py` — Hermes memory orchestration
- `agent/memory_provider.py` — Memory provider ABC

### Honcho vs Colony Theory of Mind

**Goal:** Decide how Colony's ToM (affect, shared facts, pattern extraction) coexists with Hermes's Honcho integration.

**Why deferred:** Honcho provides dialectic user modeling. Colony provides affect tracking + shared facts + pattern extraction + surprise detection. They overlap in function but differ in approach. Need real-world usage to determine whether to replace, supplement, or run both.

**Source references:**
- `agent/prompt_builder.py` — Honcho static block and recall injection
- `colony_sidecar/tom/` — Colony Theory of Mind implementation

### Contact ID Mapping

**Goal:** Map Hermes users (from messaging platform pairing) to Colony contact IDs automatically.

**Why deferred:** Colony requires a contact_id for all cognitive operations. In CLI mode, there's one user. In gateway mode, there may be multiple paired users. The mapping from Hermes platform user to Colony contact needs configuration.

**Implementation sketch:**
- In CLI mode: contact_id from config or env var
- In gateway mode: mapping table in Colony config or sidecar, keyed by platform + user_id
- Auto-create contacts on first message from unknown user

**Source references:**
- `gateway/pairing.py` — DM pairing authorization
- `gateway/session.py` — SessionStore with platform user info
- `colony_sidecar/contacts/` — Colony contacts store

---

## Dependency Order

```
Phase 1 (v0.6.3):  MCP harness config + memory provider plugin
                     ↓
Phase 2:            Cognition channel adapter + memory bridge
                     ↓
Phase 3:            Contact ID mapping + Honcho coordination
```

Each phase is independently valuable. Phase 1 gives Hermes users Colony tools + Colony context + turn sync. Phase 2 enables full bidirectional intelligence. Phase 3 polishes the multi-user experience.

Note: Turn sync is already handled by `sync_turn()` in the memory provider. The originally planned "Phase 2: Turn sync hook" is no longer needed as a separate phase since the MemoryProvider lifecycle covers it.

---

## Test Plan

### Phase 1 Tests

- [ ] Hermes appears in `colony mcp detect` output
- [ ] `colony mcp setup --harness hermes` writes correct YAML to `~/.hermes/config.yaml`
- [ ] `colony mcp setup --harness hermes --dry-run` doesn't write
- [ ] `colony mcp remove --harness hermes` removes Colony from config
- [ ] Existing Hermes config (providers, models, toolsets) is preserved
- [ ] Hermes discovers all 14 Colony MCP tools after setup
- [ ] Colony memory provider plugin returns assembled sections
- [ ] Colony memory provider plugin handles sidecar unreachable gracefully
- [ ] `colony init` offers Hermes as a host framework choice

---

## File Changes Summary

### Colony repo

| File | Change |
|---|---|
| `colony_sidecar/mcp/config.py` | Add `hermes` to HARNESS_DEFS with YAML format |
| `colony_sidecar/mcp/config.py` | Add `_add_to_yaml_config()` and YAML removal support |
| `colony_sidecar/setup.py` | Add Hermes to host framework choices |

### New: Colony memory provider plugin for Hermes

| File | Purpose |
|---|---|
| `plugins/hermes-memory/__init__.py` | Plugin registration |
| `plugins/hermes-memory/provider.py` | ColonyMemoryProvider implementation |
| `plugins/hermes-memory/SKILL.md` | Plugin metadata |
| `plugins/hermes-memory/install.sh` | Installation script (copies to ~/.hermes/plugins/memory/colony/) |

### Not modified

- Colony sidecar (no changes needed)
- Colony MCP server (no changes needed)
- Colony API (no changes needed)
- Hermes source code (we write plugins, not patches)

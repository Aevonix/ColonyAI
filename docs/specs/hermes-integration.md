# Colony + Hermes Integration Spec

## Overview

Colony provides persistent cognitive infrastructure (commitments, affect, facts, patterns, world model) that any agent or coding tool can share. Hermes Agent is a full agent runtime with its own memory, tools, gateway, and context engine. This spec defines how Colony integrates with Hermes as a first-class host framework, alongside OpenClaw.

**Source code:** https://github.com/NousResearch/hermes-agent

**Guiding principle:** Colony is harness-agnostic. The sidecar, API, stores, and MCP server don't change for Hermes. Only the adapter layer between Hermes and Colony's sidecar is new code.

---

## What We Build Now (v0.7.0)

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

### 2. Colony Context Engine Plugin for Hermes

Hermes has a pluggable context engine system. The ABC is in `agent/context_engine.py`. The default engine (`context_compressor.py`) does lossy summarization of middle conversation turns when context exceeds token thresholds. Colony can replace or supplement this with an engine that calls Colony's `/v1/host/context/assemble` endpoint.

**How Hermes's prompt assembly works (from docs):**

The system prompt is built in layers by `agent/prompt_builder.py`:

1. Agent identity (SOUL.md)
2. Tool-aware behavior guidance
3. Honcho static block (when active)
4. Optional system message
5. Frozen MEMORY snapshot
6. Frozen USER profile snapshot
7. Skills index
8. Context files (AGENTS.md, .hermes.md, etc.)
9. Timestamp + session ID
10. Platform hint

Additionally, there are "API-call-time-only layers" that are NOT cached:
- `ephemeral_system_prompt`
- Prefill messages
- Gateway-derived session context overlays
- Later-turn Honcho recall

Colony's context should inject as an API-call-time-only layer so it stays fresh on every call and doesn't break prompt caching for the stable prefix.

**Plugin structure:**

```
~/.hermes/plugins/context_engine/colony/
  __init__.py
  engine.py       # ColonyContextEngine(ContextEngine)
  SKILL.md        # Plugin metadata
```

**engine.py pseudocode:**

```python
from agent.context_engine import ContextEngine

class ColonyContextEngine(ContextEngine):
    """Colony context engine for Hermes.
    
    Calls Colony sidecar's /v1/host/context/assemble endpoint
    to get cognitive context (commitments, affect, facts, patterns, surprises)
    and injects it as an ephemeral system prompt layer.
    """

    def __init__(self, config):
        self.sidecar_url = config.get("url", "http://127.0.0.1:7777")
        self.api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
        self.contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))

    async def compress(self, messages, system_prompt, **kwargs):
        """Called by Hermes's agent loop to get context.
        
        Instead of compressing, we call Colony's context assembly
        and return the assembled sections as additional context.
        """
        # Call Colony sidecar
        context = httpx.post(
            f"{self.sidecar_url}/v1/host/context/assemble",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "identity": {"host_id": "hermes"},
                "context": {
                    "session_id": kwargs.get("session_id", ""),
                    "contact_id": self.contact_id,
                },
                "incoming_message": self._extract_last_user_message(messages),
            },
            timeout=10,
        )
        
        # Return assembled sections as ephemeral context
        sections = context.json().get("sections", [])
        if sections:
            return self._format_sections(sections)
        return None

    def _extract_last_user_message(self, messages):
        """Extract the last user message for query-aware context."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return {"role": "user", "content": msg.get("content", "")}
        return {"role": "user", "content": ""}

    def _format_sections(self, sections):
        """Format Colony sections into a prompt block."""
        parts = []
        for section in sections:
            header = section.get("id", "colony-context")
            content = section.get("content", "")
            priority = section.get("priority", 50)
            parts.append(f"## {header} [priority {priority}]\n{content}")
        return "# Colony Cognitive Context\n\n" + "\n\n".join(parts)
```

**Important detail:** The ContextEngine ABC interface needs to be verified against the actual source code at `agent/context_engine.py`. The pseudocode above assumes a `compress(messages, system_prompt, **kwargs)` signature. The real interface might be different. Check:
- What arguments does the ABC require?
- Is it sync or async?
- Does it return a string to inject, or modify messages in place?
- Can it add to the ephemeral layer specifically?

**Hermes config for the plugin:**

```yaml
# In ~/.hermes/config.yaml
context_engine:
  plugin: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "marc"
```

### 3. Colony CLI: Hermes Setup

Update `colony init` setup wizard to offer Hermes as a host framework choice (alongside OpenClaw, Claude Code, Codex, Crush, OpenCode, Standalone).

When Hermes is selected:
- Verify `hermes` CLI is installed
- Configure MCP servers in `~/.hermes/config.yaml`
- Optionally install Colony context engine plugin to `~/.hermes/plugins/context_engine/colony/`
- Set COLONY_MCP_CONTACT_ID in Hermes config
- Verify sidecar is reachable
- Offer to start Hermes gateway if not running

---

## What We Defer

### Turn Sync Hook

**Goal:** When Hermes processes a conversation turn, automatically fire `POST /v1/host/turns/sync` to Colony's sidecar. This triggers LLM extraction of commitments, affect, and facts from the conversation.

**Why deferred:** Hermes's hook/plugin system needs deeper source analysis. The entry points are:
- `hermes_cli/plugins.py` — PluginManager with discovery and hooks
- `gateway/hooks.py` — Hook discovery and lifecycle events
- `gateway/builtin_hooks/` — Always-registered hooks

Need to determine:
1. What hook points exist (pre-turn, post-turn, post-response, etc.)
2. Whether hooks can access the full message content (user message + agent response)
3. Whether hooks run sync or async
4. Whether hooks can make HTTP calls to the sidecar

**Implementation sketch:** A Hermes hook that fires after each agent response, POSTing the user message + agent response to Colony's turn sync endpoint. The turn sync endpoint handles extraction, pattern detection, and cognition triggers internally.

**Source references:**
- `hermes_cli/plugins.py` — PluginManager
- `gateway/hooks.py` — Hook lifecycle
- `run_agent.py` — AIAgent loop (where hooks fire)

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

**Why deferred:** Both systems have memory but with different philosophies. Hermes: small, curated, frozen per session (2,200 + 1,375 chars). Colony: large, structured, live-updating graph stores. The mapping isn't straightforward.

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
Phase 1 (v0.7.0):  MCP harness config + context engine plugin
                     ↓
Phase 2:            Turn sync hook (needs Hermes plugin/hook analysis)
                     ↓
Phase 3:            Cognition channel adapter + memory bridge
                     ↓
Phase 4:            Contact ID mapping + Honcho coordination
```

Each phase is independently valuable. Phase 1 gives Hermes users Colony tools + Colony context. Phase 2 enables automatic extraction. Phase 3 enables full bidirectional intelligence. Phase 4 polishes the multi-user experience.

---

## Test Plan

### Phase 1 Tests

- [ ] Hermes appears in `colony mcp detect` output
- [ ] `colony mcp setup --harness hermes` writes correct YAML to `~/.hermes/config.yaml`
- [ ] `colony mcp setup --harness hermes --dry-run` doesn't write
- [ ] `colony mcp remove --harness hermes` removes Colony from config
- [ ] Existing Hermes config (providers, models, toolsets) is preserved
- [ ] Hermes discovers all 14 Colony MCP tools after setup
- [ ] Colony context engine plugin returns assembled sections
- [ ] Colony context engine plugin handles sidecar unreachable gracefully
- [ ] `colony init` offers Hermes as a host framework choice

---

## File Changes Summary

### Colony repo

| File | Change |
|---|---|
| `colony_sidecar/mcp/config.py` | Add `hermes` to HARNESS_DEFS with YAML format |
| `colony_sidecar/mcp/config.py` | Add `_add_to_yaml_config()` and YAML removal support |
| `colony_sidecar/setup.py` | Add Hermes to host framework choices |
| `colony_sidecar/doctor.py` | Add Hermes detection check |
| `tests/test_mcp_config.py` | Add Hermes YAML config tests |

### New: Colony context engine plugin for Hermes

| File | Purpose |
|---|---|
| `plugins/hermes-context-engine/__init__.py` | Plugin registration |
| `plugins/hermes-context-engine/engine.py` | ColonyContextEngine implementation |
| `plugins/hermes-context-engine/SKILL.md` | Plugin metadata |
| `plugins/hermes-context-engine/install.sh` | Installation script (copies to ~/.hermes/plugins/) |

### Not modified

- Colony sidecar (no changes needed)
- Colony MCP server (no changes needed)
- Colony API (no changes needed)
- Hermes source code (we write plugins, not patches)

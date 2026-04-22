# Colony MCP Server Specification

## Overview

Colony exposes its cognitive substrate as an MCP (Model Context Protocol) server, enabling any MCP-compatible CLI harness to use Colony's memory, commitments, affect tracking, and context assembly as native tools during reasoning.

## Supported Harnesses

| Harness | MCP Transport | Config Location | Config Format |
|---------|--------------|-----------------|---------------|
| Claude Code | stdio | `~/.claude.json` → `mcpServers` | JSON |
| Codex | stdio or streamable HTTP | `~/.codex/config.toml` → `[mcp_servers.colony]` | TOML |
| Crush (formerly OpenCode) | stdio, HTTP, or SSE | `~/.config/crush/crush.json` → `mcpServers` | JSON |

All three support stdio transport (Colony launches as a subprocess). Codex and Crush also support HTTP/SSE transport (Colony runs as a separate process).

## Architecture

```
         WhatsApp ──→ OpenClaw ──→ Colony ──→ LLM
                         │              ↑
                         │              │
                   sessions_spawn       │  (same stores:
                         │              │   commitments, facts,
                         ↓              │   affect, world model)
                   Claude Code ──→ MCP ─┘
                   (as a tool)

         Or standalone:

         Claude Code ──→ MCP ──→ Colony ──→ LLM
         Codex       ──→ MCP ──→ Colony ──→ LLM
         Crush       ──→ MCP ──→ Colony ──→ LLM
```

The MCP server is a thin adapter. It translates MCP tool calls into Colony sidecar HTTP requests. This means:
- The sidecar must be running for MCP tools to work
- All cognitive state lives in one place (the sidecar's stores)
- The same Colony instance serves OpenClaw AND any MCP client simultaneously
- Data from all sources is unified with source tracking for provenance

## Setup Wizard: Host Framework Selection

`colony init` adapts its flow based on the user's host framework. This is the first branching point:

```
Step 3: Host Framework

  [1] OpenClaw (messaging agent platform)
  [2] Claude Code (coding agent)
  [3] Codex (coding agent)
  [4] Crush (coding agent)
  [5] Standalone (no host — Colony API only)

  Choose [1-5]:
```

### What changes per framework:

| Step | OpenClaw | Claude Code / Codex / Crush | Standalone |
|------|----------|---------------------------|------------|
| Plugin config | Writes OpenClaw plugin config | Skips | Skips |
| MCP setup | Offers MCP for detected coding harnesses | Runs `colony mcp setup` for selected harness | Skips |
| Contact ID | Not asked (OpenClaw provides it) | Asks "What should Colony call you?" → `COLONY_MCP_CONTACT_ID` | Skips |
| Gateway restart | Offers `openclaw gateway restart` | Skips | Skips |
| Validation | `colony validate` (uses OpenClaw) | `colony validate` (uses MCP) | Manual API test |

### OpenClaw flow (existing + MCP extension):
```
colony init → configure plugin → start sidecar → restart gateway →
  detect coding harnesses → offer selective MCP setup → validate
```

When OpenClaw is selected as the primary framework, the wizard then checks
for coding harnesses and offers to connect them:

```
Step 3b: Additional Harnesses

  OpenClaw is your primary agent. Colony can also connect
  coding harnesses so they share the same intelligence layer.
  Data from each source is tagged so you can trace where it came from.

  Detected coding harnesses:
    [1] Claude Code  ✅ installed
    [2] Codex        ✅ installed
    [3] Crush        ❌ not found

  Which should Colony connect? (comma-separated, e.g. 1,2 or 'all') [all]: 1

  What should Colony call you? [marc]

  Configuring Claude Code... ✅ (source: claude-code)
  Skipped Codex (not selected)
```

This creates a unified intelligence layer: OpenClaw conversations,
Claude Code coding sessions, and any other connected harnesses all
read and write to the same Colony instance, with source tracking
on every piece of data.

### CLI harness flow (new):
```
colony init → choose framework → configure sidecar → start sidecar →
  colony mcp setup --harness <choice> → set contact ID → validate
```

### Multiple CLI harnesses selected:
If the user selects multiple CLI harnesses (without OpenClaw), each gets
its own MCP config with a unique `COLONY_MCP_SOURCE`:

```
Step 3b: Additional Harnesses

  Detected coding harnesses:
    [1] Claude Code  ✅
    [2] Codex        ✅

  Which should Colony connect? (comma-separated, or 'all') [all]: 1,2

  What should Colony call you? [marc]

  Configuring Claude Code... ✅ (source: claude-code)
  Configuring Codex... ✅ (source: codex)
```

### Standalone flow:
```
colony init → choose standalone → configure sidecar → start sidecar →
  API key + URL shown for custom integration → validate manually
```

The sidecar is identical in all cases. The framework choice only affects
how things connect to it.

## MCP Tools

### 1. `colony_get_context`
Get assembled context for a contact/project. Returns the same sections the OpenClaw plugin gets.

**Input:**
```json
{
  "contact_id": "marc",       // optional — defaults to COLONY_MCP_CONTACT_ID from env
  "message": "What should I work on next?"  // optional — mapped to incoming_message.content in the API
}
```

The `message` field is mapped to `incoming_message.content` in the sidecar's `/v1/host/context/assemble` API. If omitted, context is assembled without a query-specific focus.

**Output:**
```json
{
  "sections": [
    {"title": "Pending Commitments", "priority": 72, "body": "- Ship v0.6.0 (pending, due 2026-04-25)"},
    {"title": "Contact Affect", "priority": 80, "body": "Mood: positive (valence: 0.80)"},
    {"title": "Known Facts", "priority": 70, "body": "Prefers async comms"}
  ],
  "summary": "3 active commitments (1 overdue), positive mood, 5 known facts"
}
```

**When to call:** At the start of a task, or when the user asks "what should I work on?" or mentions a person/project.

**Performance note:** Context assembly involves multiple store queries. The MCP server may cache results for up to 30 seconds per contact_id to avoid redundant lookups during rapid tool calls within the same session.

---

### 2. `colony_check_commitments`
List or search commitments.

**Input:**
```json
{
  "status": "pending",        // pending | fulfilled | cancelled | overdue (optional)
  "person_id": "marc",        // optional — defaults to COLONY_MCP_CONTACT_ID
  "limit": 10                 // max results (optional, default 10)
}
```

**Output:**
```json
{
  "commitments": [
    {"id": "abc-123", "person_id": "marc", "description": "Ship v0.6.0", "status": "pending", "due_at": "2026-04-25T00:00:00Z", "priority": 3}
  ],
  "total": 1
}
```

**When to call:** Before starting work, to check what's owed. When a deadline is mentioned. When planning a sprint.

---

### 3. `colony_create_commitment`
Create a new commitment.

**Input:**
```json
{
  "person_id": "marc",        // optional — defaults to COLONY_MCP_CONTACT_ID
  "description": "Fix the auth bug by Friday",
  "due_at": "2026-04-25T00:00:00Z",  // ISO 8601 (optional)
  "priority": 2                        // 1-5 (optional, default 2)
}
```

**Output:**
```json
{
  "id": "def-456",
  "status": "pending"
}
```

**When to call:** When the user promises something or agrees to a deadline. When a task has a clear due date.

---

### 4. `colony_fulfill_commitment`
Mark a commitment as fulfilled.

**Input:**
```json
{
  "commitment_id": "def-456"
}
```

**Output:**
```json
{
  "status": "fulfilled"
}
```

**When to call:** When a task is completed. When a promise is kept.

---

### 5. `colony_remember_fact`
Store a fact about a person, project, or concept.

**Input:**
```json
{
  "contact_id": "marc",       // optional — defaults to COLONY_MCP_CONTACT_ID
  "fact": "Prefers dark mode and async communication",
  "category": "preference",   // preference | role | context | decision | constraint (optional)
  "confidence": 0.9           // 0-1 (optional, default 0.8)
}
```

**Output:**
```json
{
  "id": "fact-789",
  "stored": true
}
```

**When to call:** When the user states a preference, makes a decision, or reveals context worth remembering. When you learn something about the codebase or team.

---

### 6. `colony_lookup_facts`
Retrieve facts about a contact or topic.

**Input:**
```json
{
  "contact_id": "marc",       // optional — defaults to COLONY_MCP_CONTACT_ID
  "category": "preference",   // optional filter
  "limit": 10
}
```

**Output:**
```json
{
  "facts": [
    {"fact": "Prefers dark mode and async communication", "category": "preference", "confidence": 0.9}
  ]
}
```

**When to call:** When starting a conversation with someone. When making design decisions. When personalizing output.

---

### 7. `colony_record_affect`
Record an emotional state or mood observation.

**Input:**
```json
{
  "contact_id": "marc",       // optional — defaults to COLONY_MCP_CONTACT_ID
  "valence": 0.7,             // -1 (negative) to 1 (positive)
  "arousal": 0.5,             // 0 (calm) to 1 (energetic)
  "trigger": "Feature shipped successfully"
}
```

**Output:**
```json
{
  "recorded": true,
  "current_state": {"valence": 0.7, "arousal": 0.5, "trend": "improving"}
}
```

**When to call:** When the user expresses frustration or satisfaction. After successes or failures. When mood seems to shift.

---

### 8. `colony_check_affect`
Get current affect state for a contact.

**Input:**
```json
{
  "contact_id": "marc"        // optional — defaults to COLONY_MCP_CONTACT_ID
}
```

**Output:**
```json
{
  "current_valence": 0.7,
  "current_arousal": 0.5,
  "trend": "improving",
  "last_event": "Feature shipped successfully",
  "last_updated": "2026-04-22T15:00:00Z"
}
```

**When to call:** Before delivering bad news. When deciding how to frame feedback. When checking if someone's been frustrated.

---

### 9. `colony_search_world`
Search the world model for entities or relationships.

**Input:**
```json
{
  "query": "auth system",
  "entity_type": "component",  // optional filter
  "limit": 5
}
```

**Output:**
```json
{
  "entities": [
    {"name": "AuthService", "entity_type": "component", "properties": {"status": "broken", "owner": "marc"}}
  ],
  "relationships": [
    {"from": "AuthService", "type": "depends_on", "to": "UserDB"}
  ]
}
```

**When to call:** When exploring a codebase. When understanding dependencies. When planning changes.

---

### 10. `colony_record_surprise`
Record something unexpected.

**Input:**
```json
{
  "observation": "Auth tests passing despite broken config",
  "expected": "Tests should fail",
  "actual": "Tests pass due to cached credentials",
  "surprise_score": 0.8       // 0-1, how surprising (optional, auto-estimated if omitted)
}
```

**Output:**
```json
{
  "id": "surprise-012",
  "recorded": true
}
```

**When to call:** When something doesn't behave as expected. When a bug is weirder than anticipated. When assumptions are violated.

---

### 11. `colony_get_patterns`
Retrieve learned patterns.

**Input:**
```json
{
  "category": "behavior",     // behavior | workflow | preference (optional)
  "limit": 5
}
```

**Output:**
```json
{
  "patterns": [
    {"pattern": "User tests manually before committing", "frequency": 8, "confidence": 0.85}
  ]
}
```

**When to call:** When suggesting workflows. When onboarding to a project. When planning work.

---

### 12. `colony_health`
Check if Colony sidecar is running and healthy.

**Input:** `{}` (no parameters)

**Output:**
```json
{
  "status": "ok",
  "capabilities": 34,
  "uptime_seconds": 86400,
  "e2e_validated": true
}
```

**When to call:** At session start. When tools return errors. As a sanity check.

---

### 13. `colony_cancel_commitment`
Cancel a commitment that's no longer relevant.

**Input:**
```json
{
  "commitment_id": "abc-123",
  "reason": "Scope changed — auth refactor deferred to v0.7"  // optional
}
```

**Output:**
```json
{
  "status": "cancelled"
}
```

**When to call:** When a commitment is abandoned, deferred, or no longer applicable. Not the same as fulfilled — cancelled means it won't be done.

---

### 14. `colony_forget_fact`
Remove an outdated or incorrect fact.

**Input:**
```json
{
  "fact_id": "fact-789"  // ID from colony_lookup_facts
}
```

**Output:**
```json
{
  "deleted": true
}
```

**When to call:** When you learn a fact was wrong. When a preference changes. When context is stale and misleading.

## Tool Annotations

MCP supports tool annotations that help harnesses decide when and how to call tools. Colony tools are annotated as:

| Tool | Read-only | Mutating | Idempotent | Safe for auto-call |
|------|-----------|----------|------------|-------------------|
| `colony_get_context` | ✅ | ❌ | ✅ | ✅ |
| `colony_check_commitments` | ✅ | ❌ | ✅ | ✅ |
| `colony_create_commitment` | ❌ | ✅ | ❌ | ❌ |
| `colony_fulfill_commitment` | ❌ | ✅ | ✅ | ❌ |
| `colony_cancel_commitment` | ❌ | ✅ | ✅ | ❌ |
| `colony_remember_fact` | ❌ | ✅ | ❌ | ❌ |
| `colony_lookup_facts` | ✅ | ❌ | ✅ | ✅ |
| `colony_forget_fact` | ❌ | ✅ | ✅ | ❌ |
| `colony_record_affect` | ❌ | ✅ | ❌ | ❌ |
| `colony_check_affect` | ✅ | ❌ | ✅ | ✅ |
| `colony_search_world` | ✅ | ❌ | ✅ | ✅ |
| `colony_record_surprise` | ❌ | ✅ | ❌ | ❌ |
| `colony_get_patterns` | ✅ | ❌ | ✅ | ✅ |
| `colony_health` | ✅ | ❌ | ✅ | ✅ |

Read-only tools can be called without side effects. Harnesses may auto-call these at session start.
Mutating tools should only be called when the LLM decides the user's intent warrants a write.

## Error Handling

All MCP tools return consistent error responses:

```json
{
  "error": "commitment_not_found",
  "message": "Commitment abc-123 does not exist",
  "suggestion": "Check commitment IDs with colony_check_commitments"
}
```

**Error types:**
| Error | When |
|-------|------|
| `sidecar_unreachable` | Colony sidecar is not running or not reachable at COLONY_URL |
| `auth_failed` | COLONY_API_KEY is missing or invalid |
| `not_found` | Requested resource (commitment, fact) doesn't exist |
| `validation_error` | Input fails validation (e.g. due_at in the past) |
| `contact_id_required` | No contact_id provided and COLONY_MCP_CONTACT_ID not set |
| `rate_limited` | Too many requests (if throttling is enabled) |

**Sidecar unreachable** is the most important case. When the MCP server can't reach the sidecar, it returns:
```json
{
  "error": "sidecar_unreachable",
  "message": "Colony sidecar not reachable at http://127.0.0.1:7777",
  "suggestion": "Start with: colony start"
}
```

## Contact ID Handling

Tools that accept a `contact_id` or `person_id` parameter follow this resolution order:

1. **Explicit parameter** — if provided in the tool call, use it
2. **Environment variable** — `COLONY_MCP_CONTACT_ID` (set during `colony mcp setup`)
3. **Error** — if neither is available, return `contact_id_required` error

This avoids the "default" junk drawer problem. Every fact, commitment, and affect event is associated with a real identity from day one. For solo developers, they set their name once during setup and never think about it again.

## Source Field Handling

The `source` field is **not included in tool input schemas**. It is automatically injected by the MCP server from the `COLONY_MCP_SOURCE` environment variable. Tool callers should never set it manually — the MCP server always overrides it.

When calling the sidecar API, the MCP server adds `source` as a query parameter or request body field. This ensures provenance is always accurate and can't be spoofed by the harness.

## MCP Resources

MCP resources are read-only data the harness can browse proactively. Colony exposes:

### `colony://status`
Current system status, capabilities, and validation state.

### `colony://commitments`
All active commitments (pending + overdue).

### `colony://affect/{contact_id}`
Current affect state for a contact.

### `colony://facts/{contact_id}`
Known facts for a contact.

### `colony://world/entities`
Top entities in the world model (most referenced, recent).

### `colony://surprises/unresolved`
Current unresolved surprises.

## MCP Prompts

MCP prompts are reusable templates the harness can invoke:

### `colony://prompts/daily_briefing`
"Review my commitments, current affect state, and any surprises. Prioritize what I should work on today."

### `colony://prompts/pre_task`
"Before I start this task, check my commitments and facts about the relevant people and components."

### `colony://prompts/post_task`
"I just completed a task. Record what happened, check off any commitments, and note any surprises."

## Transport Modes

### stdio (primary, recommended for local use)
The MCP server runs as a subprocess of the CLI harness. Colony sidecar must already be running on localhost.

```json
// Claude Code: ~/.claude.json
{
  "mcpServers": {
    "colony": {
      "command": "colony",
      "args": ["mcp"],
      "env": {
        "COLONY_API_KEY": "${COLONY_API_KEY}",
        "COLONY_URL": "http://127.0.0.1:7777",
        "COLONY_MCP_CONTACT_ID": "marc",
        "COLONY_MCP_SOURCE": "claude-code"
      }
    }
  }
}
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.colony]
command = "colony"
args = ["mcp"]
env = { COLONY_API_KEY = "${COLONY_API_KEY}", COLONY_URL = "http://127.0.0.1:7777", COLONY_MCP_CONTACT_ID = "marc", COLONY_MCP_SOURCE = "codex" }
```

### streamable HTTP (for remote/CI use — Phase 2)
The MCP server runs as part of the Colony sidecar itself, exposed on a dedicated endpoint.

```toml
# Codex: ~/.codex/config.toml (HTTP mode)
[mcp_servers.colony]
url = "http://localhost:7777/mcp"
bearer_token_env_var = "COLONY_API_KEY"
```

## `colony mcp` Command

The MCP server is accessed via the `colony mcp` subcommand, not a separate binary.

```
colony mcp                        # Start MCP server (stdio transport)
colony mcp --transport http       # Start MCP server (HTTP transport)
colony mcp setup --harness <name> # Configure a harness to use Colony
colony mcp setup --harness all    # Configure all detected harnesses
colony mcp remove --harness <name># Remove Colony from a harness config
colony mcp remove --harness all   # Remove Colony from all harness configs
```

### `colony mcp setup`

Auto-discovers installed harnesses and lets the user choose which to configure.

**Flow:**
1. Detect installed harnesses (`which claude`, `which codex`, `which crush`)
2. Ask "What should Colony call you?" → sets `COLONY_MCP_CONTACT_ID`
3. Show detected harnesses and let user select which to configure:
   ```
   Detected coding harnesses:
     [1] Claude Code ✅ installed
     [2] Codex       ✅ installed
     [3] Crush       ❌ not found
   
   Which should Colony connect? (comma-separated, e.g. 1,2 or 'all') [all]: 1,2
   ```
4. For each selected harness:
   - Read existing config
   - Show diff: "I'll add Colony to your Claude Code config. Here's what changes:"
   - Merge Colony into `mcpServers` section (preserve existing servers)
   - Each harness gets its own `COLONY_MCP_SOURCE` env var for provenance tracking
   - Write updated config
5. Verify: start MCP server briefly and confirm it responds to `initialize`

**Flags:**
- `--harness <name>` — specific harness (claude-code, codex, crush), skip discovery
- `--harness all` — configure all detected harnesses without prompting
- `--contact-id <id>` — skip the "What should Colony call you?" prompt
- `--dry-run` — show what would change without writing

**Edge case: no harnesses detected.** If `colony mcp setup` is run and no harnesses are found:
```
  No coding harnesses detected.
  Install one of: Claude Code, Codex, or Crush
  Then run: colony mcp setup
```

**Edge case: no harnesses selected.** If the user doesn't select any harnesses:
```
  No harnesses selected. Run 'colony mcp setup' again when ready.
```

### `colony mcp remove`

Remove Colony from one or more harness configs. The inverse of `colony mcp setup`.

**Flow:**
1. Read existing config for the specified harness
2. Show what will be removed (the Colony entry in mcpServers)
3. Remove Colony entry, preserve all other MCP servers
4. Write updated config

**Flags:**
- `--harness <name>` — specific harness
- `--harness all` — remove from all harnesses
- `--dry-run` — show what would change without writing

### Re-running `colony init`

If a user runs `colony init` again with an existing configuration:
- Detect existing .env and sidecar config → offer to update or start fresh
- For MCP harnesses: detect existing Colony entries in harness configs → skip if already configured, update if config format has changed
- Never duplicate Colony entries in mcpServers

### Source Tracking (Provenance)

Every write to Colony includes a `source` field indicating which harness or interface created it. This enables:
- Tracing where information came from (conversation vs coding session vs autonomous work)
- Filtering context by source ("show me only facts from Codex")
- Debugging which harness is actually using Colony
- Multi-harness setups where different tools contribute different insights

**Source values:**
| Source | Meaning |
|--------|----------|
| `openclaw` | Created during an OpenClaw conversation (plugin) |
| `claude-code` | Created by Claude Code via MCP |
| `codex` | Created by Codex via MCP |
| `crush` | Created by Crush via MCP |
| `api` | Created via direct API call (no source specified) |
| `autonomy` | Created by Colony's autonomy loop |
| `setup` | Created during `colony init` (seed data) |

**How sources are set:**
- OpenClaw plugin: `source: "openclaw"` (set in plugin config)
- MCP tools: `source` read from `COLONY_MCP_SOURCE` env var (set per harness during `colony mcp setup`)
- Direct API: `source` is optional, defaults to `null` (backwards compatible)
- Autonomy loop: `source: "autonomy"` (automatic)
- Setup wizard: `source: "setup"` (seed data)

**Schema changes:** Add optional `source` TEXT column to commitments, shared_facts, affect_events, surprises, and patterns tables. Nullable for backwards compatibility.

**Context assembly:** Source info is included in section bodies so the LLM can weigh information by origin:
```
Pending Commitments:
  - Fix auth bug (from conversation, April 22)
  - Review PR #42 (from Claude Code, April 22)

Known Facts:
  - Prefers dark mode (from Claude Code)
  - Auth system uses JWT (from Codex)
```

## Implementation Plan

### Phase 1: Core MCP Server (Week 1)
- Add `mcp` dependency to pyproject.toml (`mcp[cli] >= 1.0`)
- Create `colony_sidecar/mcp/` module:
  - `server.py` — MCP server with all 12 tools
  - `transport.py` — stdio + HTTP transport adapters
  - `config.py` — MCP-specific config
- Add `colony mcp` CLI subcommand (not a separate binary)
- All tools proxy to sidecar HTTP API (no direct DB access)
- Auth: reads `COLONY_API_KEY` from env, passes as Bearer token
- Clear error messages when sidecar is unreachable: "Colony sidecar not reachable. Start with: colony start"

### Phase 2: HTTP Transport (Week 1-2)
- Add `/mcp` endpoint to Colony sidecar for streamable HTTP transport
- Enables remote MCP connections (e.g., CI, cloud-hosted Codex)
- Same auth as sidecar API (Bearer token)

### Phase 3: Harness Integration (Week 2)
- Build `colony mcp setup` command:
  - Auto-detect installed harnesses
  - Handle no-harnesses-detected and no-harnesses-selected edge cases
  - Ask for contact ID
  - Read existing config → show diff → merge → write
  - Verify MCP server responds
- Build `colony mcp remove` command (inverse of setup)
- Per-harness config writers:
  - `~/.claude.json` for Claude Code
  - `~/.codex/config.toml` for Codex
  - `~/.config/crush/crush.json` for Crush
- Update `colony validate` for MCP path:
  - When no OpenClaw: validate via MCP tools instead
  - Step 1: `colony_health` tool call
  - Step 2: Seed data via MCP tools (create_commitment, remember_fact, record_affect)
  - Step 3: `colony_get_context` tool call → verify sections returned
  - Step 4: Cleanup via MCP (fulfill_commitment, forget_fact)
  - Write E2E validation stamp

### Phase 4: Setup Wizard Integration (Week 2-3)
- Add host framework selection to `colony init` (Step 3)
- Branch wizard flow based on framework choice:
  - OpenClaw → existing plugin flow + offer MCP for detected coding harnesses
  - CLI harness → `colony mcp setup --harness <choice>` + contact ID
  - Standalone → API key + URL shown
- Add contact ID prompt for CLI harness users
- Handle re-init: detect existing config, offer update vs fresh start, never duplicate MCP entries

### Phase 5: Harness-Specific Optimizations (Week 3)
- Claude Code: Add `.claude/commands/` templates for colony workflows
  - `/colony:budget` — check commitments
  - `/colony:briefing` — daily briefing
  - `/colony:done` — mark commitment fulfilled
- Codex: Add project-scoped `.codex/config.toml` generation
- Crush: Add MCP prompt templates

## Dependencies

- `mcp[cli] >= 1.0` — Official Python MCP SDK
- `httpx` — Already a dependency, used for sidecar API calls
- No new database or infrastructure required

## Security

- MCP server authenticates to sidecar using `COLONY_API_KEY` (Bearer token)
- No direct database access — all reads/writes go through sidecar API
- MCP server has same permissions as the API key allows
- For HTTP transport: same CORS and auth as sidecar
- `colony_health` tool is the only unauthenticated call (matches health endpoint exemption)
- `colony mcp setup` preserves existing harness configs — merges, never overwrites

## Key Design Decisions

1. **MCP server proxies to sidecar API, not direct DB access.** One source of truth, one auth layer, and the MCP server can run on a different machine than the sidecar. Clear error messages when sidecar is down.

2. **stdio is the primary transport.** Most widely supported, requires no network configuration, sidecar is already running locally. HTTP transport comes later for remote/CI scenarios.

3. **`colony mcp` subcommand, not a separate binary.** Same `colony` install, same PATH. The harness config just references `colony mcp` as the command. One less thing to install and maintain.

4. **Auto-discovery with selective setup and diff preview.** `colony mcp setup` detects what's installed, lets the user choose which harnesses to configure (not all-or-nothing), shows what will change before writing, and merges into existing configs without clobbering other MCP servers.

5. **Contact ID set during setup, required at runtime.** No "default" junk drawer — every piece of data is associated with a real identity from day one. For solo devs, set it once during `colony mcp setup` and never think about it again. For multi-person use, override per tool call.

6. **Host framework is a setup choice, not an architecture.** The sidecar is identical regardless of whether it's accessed via OpenClaw plugin or MCP. The framework choice only affects how things connect to it.

7. **Source tracking on every write.** Every commitment, fact, affect event, surprise, and pattern records which harness or interface created it (`COLONY_MCP_SOURCE` env var). This enables provenance tracing, source-filtered context, and debugging which harnesses are actively using Colony. OpenClaw uses `source: "openclaw"`, each CLI harness uses its own source tag.

8. **Multi-harness setups are first-class.** A user with OpenClaw + Claude Code + Codex gets a unified intelligence layer where all three read and write to the same Colony instance. The setup wizard detects all installed harnesses and lets the user choose which to connect — no all-or-nothing requirement.

## What This Enables

**Before MCP:** Claude Code is a stateless coder. Every session starts fresh. No memory of commitments, no awareness of mood, no project context beyond files.

**After MCP:** Claude Code can:
- Check what you promised to deliver before suggesting what to work on
- Remember your preferences (dark mode, async comms, late-night coding)
- Notice when you're frustrated and adjust its approach
- Track patterns in how you work
- Record surprises and anomalies for future reference
- Build a world model of your codebase over time

**Same Colony, multiple interfaces.** Whether you're chatting via OpenClaw on WhatsApp or coding via Claude Code in the terminal, your commitments, facts, and affect are consistent and shared across every interface you use.

**No OpenClaw required.** A developer who only wants Colony's cognitive substrate for their CLI coding agent can use it standalone. They don't need to install or configure OpenClaw.

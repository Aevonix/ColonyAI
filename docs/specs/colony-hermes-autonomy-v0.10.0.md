# Colony-Hermes Autonomy Architecture Spec v0.10.0

**Author:** Aeva  
**Date:** 2026-05-16  
**Version:** 0.10.0  
**Status:** Draft — pending review  
**Scope:** End-to-end specification for Colony-generated initiatives that trigger autonomous agent action in Hermes. Covers generation, delivery, channel routing, execution, and response delivery.

**Supersedes:**
- `docs/autonomy-architecture-spec-v2.2.md` (2026-05-14)
- `docs/autonomy-pipeline-spec.md` (2026-05-14)
- `docs/specs/hermes-integration.md` (v0.6.3)

---

## 1. Executive Summary

Colony v0.10.0 implements a complete autonomy pipeline where the Colony sidecar generates initiatives, delivers them to Hermes via webhook, and the Hermes agent (Aeva) acts on them using available tools. The pipeline is fully operational with the following subsystems:

| Subsystem | Status | PR |
|-----------|--------|-----|
| Initiative generation (autonomy loop) | ✅ Operational | #39 |
| Direct webhook push (Colony → Hermes) | ✅ Operational | #33 |
| Per-person channel routing (ChannelRegistry) | ✅ Operational | #40 |
| Owner contact ID alias + async store fix | ✅ Operational | #40 |
| Local LLM support (Ollama, LM Studio, vLLM) | ✅ Operational | #34 |
| Internal state separation | ✅ Operational | #32 |
| Neo4j stale-fallback / schema drift fix | ✅ Operational | #37, #41 |
| Silence-triggered owner check-in | ✅ Operational | — |
| Conversation synthesis for goal extraction | ✅ Operational | #39 |
| MCP harness config + memory provider | ✅ Operational | v0.6.3 |

**Constraint:** Zero modifications to Hermes source code under `~/.hermes/hermes-agent/`. All integration is via Colony code, Hermes config, plugins, hooks, and webhooks.

---

## 2. Design Principles

1. **Agent acts, never reminds** — Initiatives trigger autonomous tool use (terminal, web, file, browser, code execution). The agent does the work and reports results. It never tells the user "you should do X."
2. **Guaranteed delivery context** — Every initiative payload includes `delivery_context` with resolved `user_chat` (DM) and `home_chat` (group) channels. The agent uses these to route responses correctly.
3. **No hardcoded identity** — Agent name, platform, chat IDs, and auth secrets are configurable via environment variables. Nothing in the code is user-specific.
4. **Single push path** — Colony POSTs directly to the Hermes webhook. All legacy polling paths (plugin poller, hook poller) are deprecated. The WebSocket subscriber is preserved as a read-only observer.
5. **Home channel for system, DM for personal** — `channel_hint` indicates routing intent: `dm` for owner check-ins and personal goals, `home` for system-level initiatives.

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  COLONY SIDECAR (port 7777)                                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ AutonomyLoop                                                        │    │
│  │  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │    │
│  │  │ GoalEngine   │  │ InitiativeStore  │  │ ConversationSynthesis │  │    │
│  │  │ (checks goals│  │ (dedup, persist, │  │ (extracts goals from  │  │    │
│  │  │  generates   │  │  reactivates)    │  │  conversation memory) │  │    │
│  │  │  initiatives)│  │                  │  │                       │  │    │
│  │  └──────┬───────┘  └──────────────────┘  └──────────────────────┘  │    │
│  │         │                                                           │    │
│  │         ▼ 1. Build structured payload with delivery_context          │    │
│  │  ┌──────────────────────────────────────────────────────────────┐  │    │
│  │  │ ProactiveDeliveryBridge.push_initiative()                    │  │    │
│  │  │  • Resolves channels via ChannelRegistry                     │  │    │
│  │  │  • POSTs to Hermes webhook                                   │  │    │
│  │  └────────────────────────┬─────────────────────────────────────┘  │    │
│  └───────────────────────────┼────────────────────────────────────────┘    │
└─────────────────────────────┼───────────────────────────────────────────────┘
                              │ POST /webhooks/colony-initiatives
                              │ Body: {"type": "initiative",
                              │        "payload": {...},
                              │        "delivery_context": {...},
                              │        "channel_hint": "dm" | "home"}
┌─────────────────────────────▼───────────────────────────────────────────────┐
│  HERMES WEBHOOK ADAPTER (port 8644)                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ _handle_webhook()                                                   │    │
│  │  1. Parse payload (nested under "payload")                          │    │
│  │  2. Render prompt via {__raw__} (full JSON dump)                   │    │
│  │  3. Invoke agent (handle_message → agent loop)                      │    │
│  │  4. Agent response queued for delivery                              │    │
│  └────────────────────────┬────────────────────────────────────────────┘    │
│                           │ agent response                                  │
│  ┌────────────────────────▼────────────────────────────────────────────┐    │
│  │ Agent Decision Layer                                                │    │
│  │  • Reads delivery_context.user_chat / .home_chat from prompt        │    │
│  │  • Chooses channel based on channel_hint                            │    │
│  │  • Uses send_message to deliver concise result                      │    │
│  │  • Full reasoning stays in logs (deliver: log)                      │    │
│  └────────────────────────┬────────────────────────────────────────────┘    │
│                           │ send_message(platform:chat_id)                  │
└───────────────────────────┼─────────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  USER CHANNELS (WhatsApp, Telegram, Discord, etc.)                          │
│  • DM channel: personal initiatives (owner check-in, goal follow-ups)       │
│  • Home channel: system initiatives + tool-progress notifications            │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Initiative Generation

### 4.1 Autonomy Loop

The `AutonomyLoop` runs on a configurable tick interval (default: 60s). Each tick executes:

1. **Phase: Evaluate** — Check goals, commitments, patterns, and world model for triggers
2. **Phase: Generate** — Create initiatives with rich context (not just titles)
3. **Phase: Execute** — Push initiatives to Hermes via `ProactiveDeliveryBridge.push_initiative()`

**Key files:**
- `colony_sidecar/autonomy/loop.py` — main loop
- `colony_sidecar/autonomy/checkin.py` — `OwnerCheckInTask`
- `colony_sidecar/autonomy/synthesis.py` — `ConversationSynthesisTask`

### 4.2 Initiative Types

| Type | Description | channel_hint | Example |
|------|-------------|--------------|---------|
| `proactive_message` | Direct agent action trigger | `dm` / `home` | "Check disk space and report" |
| `follow_up` | Pending task reminder | `home` | "Follow up on: Write spec" |
| `relationship` | Contact maintenance | `dm` | (Disabled per user preference) |
| `research` | Web/code research task | `home` | "Research calendar AI options" |
| `insight` | Pattern or anomaly detected | `home` | "Recurring build failure pattern" |

> **Note:** Relationship initiatives are disabled in the owner's configuration per explicit preference. The initiative engine focuses on project state, system health, task queue, and research — NOT contact/relationship reminders.

### 4.3 Payload Schema

```json
{
  "type": "initiative",
  "occurred_at": "2026-05-16T23:11:42.100569+00:00",
  "payload": {
    "initiative_type": "follow_up",
    "title": "Short, actionable title",
    "description": "Full description of what to do",
    "priority": 72,
    "status": "pending",
    "id": "followup-94381c5e9525",
    "dedup_key": "follow_up:32931c84-f435-4091-8640-7cfb4c03182e",
    "agent_name": "Aeva",
    "context": {
      "trigger": "Task has been pending for 0 day(s)",
      "suggested_actions": ["Review status of '...'"],
      "constraints": {},
      "metadata": {
        "source": "autonomy_loop",
        "entity_id": "32931c84-f435-4091-8640-7cfb4c03182e",
        "entity_type": "follow_up"
      }
    },
    "created_at": "2026-05-16T23:11:42.100552+00:00",
    "expires_at": null
  },
  "delivery_context": {
    "user_chat": "whatsapp:+1555XXXXXXX",
    "home_chat": "whatsapp:GROUP_ID@g.us"
  },
  "channel_hint": "home"
}
```

### 4.4 Context Building

The loop builds per-initiative context rather than dumping full engine state. This is implemented in `_build_initiative_context()` (PR #32):

- `trigger` — Why this initiative was generated
- `suggested_actions` — Concrete next steps the agent can take
- `constraints` — Guardrails (e.g., "do not message people the owner hasn't talked to recently")
- `metadata` — Source subsystem, entity IDs for tracing

---

## 5. Initiative Delivery Pipeline

### 5.1 ProactiveDeliveryBridge

**File:** `colony_sidecar/delivery/bridge.py`

The bridge has three delivery paths:

1. **Push initiative (Hermes webhook)** — `push_initiative()` — primary path for autonomy
2. **Push to gateway** — `push_to_gateway()` — legacy path for direct platform adapter calls
3. **Poll path** — `get_pending()` — for gateway polling (deprecated in Hermes integration)

### 5.2 ChannelRegistry

**File:** `colony_sidecar/delivery/channels.py`

Resolves per-person delivery channels from multiple sources with priority ordering:

1. **Environment variables** (`COLONY_CHANNEL_DM_owner`, etc.)
2. **JSON config file** (`{COLONY_STATE_DIR}/data/channels.json`)
3. **Contact handle inference** (phone → chat platform DM, configurable gateway map)
4. **Home channel fallback** (`WHATSAPP_HOME_CHANNEL`, `TELEGRAM_HOME_CHANNEL`, etc.)

**Owner alias:** When `person_id` matches `COLONY_OWNER_CONTACT_ID`, the registry also checks the `"owner"` key. This fixes the UUID mismatch bug where the autonomy loop passed the owner's contact UUID but the env var was keyed as `COLONY_CHANNEL_DM_owner`.

**Async store guard:** `_infer_from_handles()` inspects `contacts_store.get_handles` and skips if it's a coroutine function, preventing `'coroutine' object is not iterable` crashes in sync `resolve()` contexts.

**Key methods:**
```python
ChannelRegistry.load(contacts_store=contacts_store)  # singleton, called in server.py lifespan
registry.resolve(person_id, channel_type="home") -> Optional[Channel]
```

### 5.3 Webhook Push Flow

```python
# In ProactiveDeliveryBridge.push_initiative()
person_id = initiative.get("entity_id", "")
channel_hint = initiative.get("channel_hint", "home")

if not person_id:
    user_channel = None
    home_channel = self._channel_registry.resolve("__system__", "home")
else:
    user_channel = self._channel_registry.resolve(person_id, "dm")
    home_channel = self._channel_registry.resolve(person_id, "home")

delivery_context = {}
if user_channel:
    delivery_context["user_chat"] = f"{user_channel.platform}:{user_channel.chat_id}"
if home_channel:
    delivery_context["home_chat"] = f"{home_channel.platform}:{home_channel.chat_id}"

payload["delivery_context"] = delivery_context
payload["channel_hint"] = channel_hint

# POST to Hermes webhook
# URL: COLONY_HERMES_WEBHOOK_URL (default: http://127.0.0.1:8644/webhooks/colony-initiatives)
```

### 5.4 Server Startup Integration

**File:** `colony_sidecar/server.py` (lifespan)

```python
channel_registry = ChannelRegistry.load(contacts_store=contacts_store)
delivery = ProactiveDeliveryBridge(channel_registry=channel_registry)
# delivery is passed to AutonomyLoop and other consumers
```

---

## 6. Hermes Execution

### 6.1 Webhook Route Configuration

**File:** `~/.hermes/config.yaml`

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8644
      secret: INSECURE_NO_AUTH
      routes:
        colony-initiatives:
          deliver: log
          prompt: "{__raw__}\n\nYou are Aeva, an autonomous agent. Colony generated this\ninitiative. Act on it immediately using your available tools.\n\nDELIVERY\nRULES:\n- Your FULL response (detailed reasoning, tool outputs, findings)\ngoes to LOGS only.\n- Colony has resolved the best channel(s) for this initiative:\n\u2022 {delivery_context.user_chat} (DM channel)\n\u2022 {delivery_context.home_chat}\n(home channel)\n- Choose the channel matching the initiative's channel_hint\n(dm \u2192 user_chat, home \u2192 home_chat).\n- If the matching channel is missing,\nfall back to the other.\n- If BOTH channels are missing, log the result and do\nNOT attempt to send a message.\n- Send AT MOST ONE message to the user per\ninitiative. Make it concise \u2014 one or two sentences max.\n- Do NOT send\nmultiple follow-up messages. Do NOT send \"still working\" updates.\n- If\nthe initiative requires user input or is blocked, send ONE message asking\nwhat they want to do.\n- If you can complete the initiative autonomously,\nsend ONE message summarizing what you did.\n"
          secret: INSECURE_NO_AUTH
```

**Why `deliver: log`:** The webhook's own response (the agent's full reasoning) goes to logs. The agent uses `send_message` to choose the appropriate user channel per-initiative. This is generic — works for any platform without config changes.

**Why `{__raw__}`:** Dumps the entire JSON payload. The agent can read and understand any initiative structure without template maintenance. Prevents template fragility when payload fields change.

### 6.2 Agent Behavior

When the agent receives an initiative:

1. **Parse** — Read the full JSON from `{__raw__}`
2. **Route** — Determine target channel from `channel_hint` + `delivery_context`
3. **Act** — Use available tools to complete the initiative
4. **Deliver** — Send ONE concise message to the resolved channel
5. **Log** — Full reasoning and tool outputs go to logs only

**Channel selection rules:**
- `channel_hint=dm` → `send_message` to `delivery_context.user_chat`
- `channel_hint=home` → `send_message` to `delivery_context.home_chat`
- Missing preferred channel → fall back to the other
- Both missing → log only (CLI-only deployment)

---

## 7. Configuration Reference

### 7.1 Colony Environment Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `COLONY_HERMES_WEBHOOK_URL` | `http://127.0.0.1:8644/webhooks/colony-initiatives` | Hermes webhook endpoint |
| `COLONY_HERMES_WEBHOOK_SECRET` | `...` | Optional HMAC secret |
| `COLONY_AGENT_NAME` | `Aeva` | Agent name in initiative payload |
| `COLONY_CHANNEL_DM_owner` | `whatsapp:+1555XXXXXXX` | Owner DM channel |
| `COLONY_CHANNEL_HOME` | `discord:#general` | Global home channel override |
| `COLONY_CHANNEL_GATEWAY_MAP` | `{"imessage":"whatsapp"}` | Handle-to-platform inference mapping |
| `COLONY_CHANNEL_INFER_FROM_HANDLES` | `true` | Enable contact handle inference |
| `COLONY_OWNER_CONTACT_ID` | `cid-...` | Owner's Colony contact UUID |
| `WHATSAPP_HOME_CHANNEL` | `GROUP_ID@g.us` | WhatsApp home (read by registry) |
| `TELEGRAM_HOME_CHANNEL` | `@groupname` | Telegram home (read by registry) |
| `DISCORD_HOME_CHANNEL` | `#general` | Discord home (read by registry) |
| `COLONY_LLM_PROVIDER` | `local` | LLM provider for Colony general tasks |
| `COLONY_LLM_BASE_URL` | `http://localhost:11434` | Base URL for local LLM |

### 7.2 JSON Config File

**Path:** `{COLONY_STATE_DIR}/data/channels.json`

```json
{
  "contacts": {
    "owner": {
      "dm": {"platform": "whatsapp", "chat_id": "+1555XXXXXXX"},
      "home": {"platform": "whatsapp", "chat_id": "GROUP_ID@g.us"}
    }
  },
  "fallback": {
    "home": {"platform": "whatsapp", "chat_id": "GROUP_ID@g.us"}
  }
}
```

### 7.3 Hermes Config

**Path:** `~/.hermes/config.yaml`

Key sections:
- `platforms.webhook.routes.colony-initiatives` — webhook route (see §6.1)
- `plugins.colony` — Colony plugin config
- `memory.provider: colony` — Colony memory provider

---

## 8. Local Model Support

**PR:** #34  
**File:** `colony_sidecar/router/tiers.py`

Colony general LLM tasks are routed through local models (Ollama, LM Studio, vLLM) instead of Anthropic API.

**Auto-discovery:**
- Ollama: `GET http://localhost:11434/api/tags`
- LM Studio: `GET http://localhost:1234/v1/models`
- vLLM: `GET http://localhost:8000/v1/models`

**Generic zero-cost tiers:** Unknown providers get a default tier with no cost constraints.

**Endpoint:** `GET /v1/host/models` returns available models.

---

## 9. Testing Strategy

### 9.1 Unit Tests

**File:** `tests/test_channel_registry.py`

| Test | Description |
|------|-------------|
| `test_owner_contact_id_alias` | UUID → `owner` aliasing |
| `test_async_get_handles_skipped_in_sync_resolve` | Async store graceful skip |
| `test_resolve_priority_env_json_handles_fallback` | Resolution priority ordering |
| `test_case_insensitive_person_id` | Normalized matching |
| `test_home_channel_from_env_vars` | `WHATSAPP_HOME_CHANNEL` fallback |

### 9.2 Integration Tests

1. Configure `WHATSAPP_HOME_CHANNEL` and `COLONY_CHANNEL_DM_owner`
2. Trigger `OwnerCheckInTask` with `channel_hint="dm"`
3. Verify webhook payload contains:
   - `delivery_context.user_chat`
   - `delivery_context.home_chat`
   - `channel_hint`
4. Verify agent receives prompt with resolved channels
5. Verify agent sends message to correct channel

### 9.3 E2E Verification

```bash
# 1. Verify sidecar health
curl -s http://127.0.0.1:7777/v1/host/health | jq .

# 2. Check autonomy status
curl -s http://127.0.0.1:7777/v1/host/autonomy/status | jq .

# 3. Trigger test initiative
curl -X POST http://127.0.0.1:8644/webhooks/colony-initiatives \
  -H "Content-Type: application/json" \
  -d '{"type":"initiative","payload":{"initiative_type":"test","title":"Test","description":"Test initiative","priority":50,"status":"pending","id":"test-1","dedup_key":"test:1","agent_name":"Aeva","context":{}},"delivery_context":{"home_chat":"whatsapp:GROUP_ID@g.us"},"channel_hint":"home"}'
```

---

## 10. What's Implemented vs. Deferred

### 10.1 Implemented in v0.10.0

- [x] Direct Colony → Hermes webhook push
- [x] Per-person channel routing via ChannelRegistry
- [x] Owner contact ID alias resolution
- [x] Async store crash guard
- [x] Delivery context population in initiative payload
- [x] Agent channel selection based on `channel_hint`
- [x] Local LLM support (Ollama, LM Studio, vLLM)
- [x] Conversation synthesis for goal extraction
- [x] Silence-triggered owner check-in
- [x] Internal state separation (no engine state dumps)
- [x] Neo4j schema drift fix (ABOUT not BELONGS_TO)
- [x] Stale initiative / graph fallback fix
- [x] MCP harness config for Hermes
- [x] Colony memory provider plugin

### 10.2 Deferred to Future Versions

- [ ] **Cognition channel adapter** — Route Colony cognition triggers through Hermes's delegate/subagent system instead of OpenClaw's `sessions_spawn`
- [ ] **Memory bridge** — Bidirectional sync between Hermes MEMORY.md/USER.md and Colony cognitive stores
- [ ] **Contact ID mapping** — Auto-map Hermes platform users to Colony contact IDs in gateway mode
- [ ] **Honcho coordination** — Decide how Colony ToM coexists with Hermes Honcho integration
- [ ] **API endpoints for channel CRUD** — `GET /v1/channels/{person_id}`, `PUT /v1/channels/{person_id}`
- [ ] **Multi-DM ranked preferences** — Support multiple DM channels per person with priority ranking
- [ ] **Cross-platform routing** — Full support for DM on one platform + home on another (e.g., Telegram DM + Discord home)
- [ ] **Digest channel** — Bundled morning briefing (partially implemented in bridge, not wired to scheduler)

---

## 11. References

- `colony_sidecar/delivery/bridge.py` — `ProactiveDeliveryBridge.push_initiative()`
- `colony_sidecar/delivery/channels.py` — `ChannelRegistry`
- `colony_sidecar/autonomy/loop.py` — `AutonomyLoop`
- `colony_sidecar/autonomy/checkin.py` — `OwnerCheckInTask`
- `colony_sidecar/autonomy/synthesis.py` — `ConversationSynthesisTask`
- `colony_sidecar/server.py` — Server lifespan, registry singleton wiring
- `~/.hermes/config.yaml` — Hermes webhook route config
- `plugins/hermes-plugin/` — Hermes plugin (poller, events, memory provider)
- `docs/specs/2026-05-16-channel-registry-initiative-routing.md` — ChannelRegistry v0.2.0 spec

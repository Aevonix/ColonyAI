# Colony-Hermes Real Autonomy Pipeline — Spec v1.0

**Date:** 2026-05-14
**Constraint:** Zero Hermes source code changes. All fixes via config, Colony code, Hermes plugins/hooks, and scripts.

---

## 1. Current State (What I Found)

### 1.1 The Webhook Pipeline IS Working

When a POST hits `http://127.0.0.1:8644/webhooks/colony-initiatives`:
- ✅ Webhook receives it, returns 202 Accepted
- ✅ Prompt template renders (with correct nested payload)
- ✅ Agent run triggers immediately (`handle_message(event)`)
- ✅ Agent processes the initiative and generates a response
- ❌ Response goes to **logs only** — `deliver_type=log`

**Log evidence:**
```
[webhook] POST event=unknown route=colony-initiatives prompt_len=1767
[gateway.run] inbound message: platform=webhook user=colony-initiatives
[webhook] Response for webhook:colony-initiatives:...: Test initiative acknowledged...
```

### 1.2 The Template Bug (Confirmed)

The template in `~/.hermes/config.yaml` expects nested `payload.*` keys:
```
Event type: {type}
Initiative type: {payload.initiative_type}
Title: {payload.title}
```

But the Hermes hook handler (`~/.hermes/hooks/colony-initiatives/handler.py`) sends a **flat** payload:
```json
{"initiative_type": "...", "title": "...", "description": "..."}
```

**Result:** All `{payload.*}` placeholders render as literal text. The agent receives broken prompts.

**Fix tested:** Sending a properly nested payload renders correctly.

### 1.3 The Delivery Gap (Critical)

The webhook route has **no `deliver` config**. After the agent generates a response, the webhook's `send()` method checks delivery info and falls back to `log`. The response never reaches the owner.

```yaml
colony-initiatives:
  secret: INSECURE_NO_AUTH
  prompt: "..."
  # MISSING: deliver: whatsapp
  # MISSING: deliver_extra
```

### 1.4 Colony Plugin Connection Failure

The Colony plugin (`~/.hermes/plugins/colony/events.py`) has been logging since May 13:
```
Poll cycle error: All connection attempts failed
```

It cannot connect to Colony. This means **zero initiatives are flowing** through the plugin path.

### 1.5 Colony's Internal Endpoints Don't Exist in Hermes

Colony's `ProactiveDeliveryBridge` tries to POST to:
- `POST /internal/deliver` — does not exist
- `POST /internal/initiative` — does not exist

These are Colony-native concepts that Hermes never implemented. The fallback is WebSocket broadcast, which also requires a subscriber.

### 1.6 Redundant Polling

There are **three** mechanisms trying to do the same job:
1. Colony plugin (`events.py`) — broken, can't connect
2. Hermes hook (`handler.py`) — works but sends flat payload
3. Colony delivery bridge (`push_initiative()`) — posts to non-existent endpoint

### 1.7 Initiative Quality

Current initiatives are shallow:
- 6x "relationship" — "No contact with X for 14 days"
- 15x "follow_up" — stale goal reminders

None include actionable context. An agent receiving "Follow up on: Build Colony integration" has no idea what to actually do.

---

## 2. Target Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Colony        │     │   Hermes         │     │   User          │
│   Sidecar       │────▶│   Webhook        │────▶│   (Primary      │
│                 │     │   /webhooks/     │     │    channel)     │
│ - Generates     │     │   colony-        │     │                 │
│   substantive   │     │   initiatives    │     │ - Receives      │
│   initiatives   │     │                  │     │   agent reports │
│ - Includes      │     │ - Renders prompt │     │                 │
│   context/docs  │     │ - Triggers agent │     │ - Can reply     │
│   for agent     │     │ - Watcher ack    │     │   to redirect   │
│                 │     │   to log         │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌─────────────────┐
                        │   Watcher       │
                        │   (log or       │
                        │    configured   │
                        │    channel)     │
                        │                 │
                        │ - Verbose       │
                        │   reasoning     │
                        │ - Tool outputs  │
                        │ - Full logs     │
                        └─────────────────┘
```

**Agent autonomy loop:**
1. Colony generates initiative with rich context
2. Colony POSTs to Hermes webhook
3. Webhook renders prompt → triggers agent run
4. **Watcher channel** receives webhook ack + verbose agent reasoning (logs by default)
5. Agent uses tools (terminal, web, files, git, etc.) to act
6. **Agent decides** which channel to communicate to the user based on initiative type, priority, and configured available channels
7. User sees concise results and can reply to redirect

**Channel philosophy:**
- **Watcher:** Internal thinking, verbose output, tool logs. Default: `log` (file). Configurable to any platform channel (WhatsApp group, Discord thread, Telegram channel, etc.).
- **User channel:** Concise, actionable results. Chosen by the agent per-initiative. Never hardcoded.

---

## 3. Implementation Plan (No Hermes Source Changes)

### Phase 1: Fix the Delivery Pipeline (1-2 hours)

#### 3.1.1 Fix `~/.hermes/config.yaml`

Update the `colony-initiatives` route:

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
          secret: INSECURE_NO_AUTH
          # Use __raw__ to dump full payload — avoids template fragility
          prompt: |
            {__raw__}

            You are an autonomous agent (Aeva). Colony generated this initiative.
            ACT on it immediately using your available tools:
            - terminal: run commands, git, build, deploy
            - web: research, check APIs, read docs
            - file: read/write code, configs, specs
            - browser: interact with web apps
            - send_message: communicate with the user on any configured platform

            Do not ask for permission. Do not say "I will research this." DO the work.
            Report what you accomplished and what remains blocked.

            CHANNEL SELECTION RULES:
            - Deliver CONCISE results to the user's primary channel
            - Use the watcher channel only for verbose reasoning if needed
            - For urgent/high-priority items: notify user immediately
            - For low-priority/internal tasks: log to watcher, batch for digest
            - For relationship/messaging initiatives: draft message, ask for approval before sending if unsure
            - The available channels are determined by the platforms configured in this instance

          # Webhook responses go to watcher (log) by default
          # The agent decides where to send user-facing results via send_message
          deliver: log

          # Optional: configure a watcher channel for verbose output
          # watcher: whatsapp
          # watcher_extra:
          #   chat_id: "..."

          # Rate limit: max 5 initiatives per minute
          rate_limit: 5
```

**Why `deliver: log`:** The webhook's own response (delivery receipt) goes to logs. The agent uses `send_message` to choose the appropriate user channel per-initiative. This is generic — works for WhatsApp, Telegram, Discord, or any future platform without config changes.

**Why `__raw__`:** Dumps the entire JSON payload. The agent can read and understand any initiative structure without template maintenance.

#### 3.1.2 Fix `~/.hermes/hooks/colony-initiatives/handler.py`

The hook currently sends a flat payload. Change it to wrap under `payload`:

```python
payload = {
    "type": "initiative",
    "payload": {
        "initiative_type": initiative.get("initiative_type", ""),
        "title": initiative.get("title", ""),
        "description": initiative.get("description", ""),
        "priority": initiative.get("priority", 0),
        "status": initiative.get("status", ""),
        "id": iid,
        "dedup_key": initiative.get("dedup_key", ""),
        "context": initiative.get("context", {}),
        "created_at": initiative.get("created_at", ""),
        "expires_at": initiative.get("expires_at", ""),
    },
    "occurred_at": initiative.get("created_at", ""),
}
```

Also fix the connection URL — it should use `COLONY_SIDECAR_HOST` and `COLONY_SIDECAR_PORT` from `~/.colony/.env` instead of hardcoded `http://127.0.0.1:7777`.

#### 3.1.3 Remove/Fix Redundant Colony Plugin

Option A: **Disable the plugin's polling** (simplest)
- In `~/.hermes/plugins/colony/events.py`, comment out the `_poll_initiatives` task creation
- Keep the WebSocket event subscriber for cache injection (useful for context)

Option B: **Fix the plugin's connection**
- Debug why `urllib.request` can't connect (likely URL mismatch or auth issue)
- Make it use the same payload structure as the hook

**Recommendation:** Option A. The hook handler is already working. One polling mechanism is enough.

### Phase 2: Fix Colony-to-Hermes Delivery (2-3 hours)

#### 3.2.1 Make Colony POST Directly to Webhook

Instead of relying on Hermes to poll Colony, have Colony push initiatives to the webhook. This is more reliable and lower latency.

In Colony's autonomy loop (`~/colony-work/sidecar/colony_sidecar/autonomy/loop.py`), after generating an initiative, add:

```python
# Push to Hermes webhook for immediate agent action
await self._push_to_hermes_webhook(initiative)
```

Implement `_push_to_hermes_webhook`:
- POST to `http://127.0.0.1:8644/webhooks/colony-initiatives`
- Payload: `{"type": "initiative", "payload": initiative_dict}`
- No auth needed (secret is `INSECURE_NO_AUTH`)
- Fire-and-forget (don't block autonomy loop)

#### 3.2.2 Disable Colony's Broken Internal Endpoint Calls

In `ProactiveDeliveryBridge.push_initiative()` and `push_to_gateway()`, add early-return if the gateway URL is the default localhost (Hermes doesn't have these endpoints):

```python
if self._gateway_url == "http://localhost:7779":
    # Hermes doesn't have /internal/deliver — skip
    return False
```

### Phase 3: Improve Initiative Quality (4-6 hours)

#### 3.3.1 Add Context to Initiatives

Current initiatives are just titles. Rich initiatives include:

```json
{
  "initiative_type": "research",
  "title": "Research calendar AI integration options",
  "description": "The owner's goal 'Research calendar AI integration options' has been pending for 11 days. He previously asked about Google Calendar API, Notion calendar, and Calendly.",
  "context": {
    "goal_id": "af47b77e-...",
    "goal_created_at": "2026-05-10T02:41:34Z",
    "days_pending": 11,
    "related_memories": [
      "The owner mentioned wanting calendar integration on May 10",
      "He already has Google Workspace skills loaded"
    ],
    "suggested_actions": [
      "Search web for 'Google Calendar API Python quickstart 2026'",
      "Check Notion API calendar capabilities",
      "Compare Calendly API vs native scheduling"
    ]
  }
}
```

#### 3.3.2 Add Initiative Types That Trigger Real Work

New initiative types Colony should generate:

| Type | Trigger | Agent Action |
|------|---------|-------------|
| `research` | Goal pending > 7 days | Web search, summarize findings |
| `code_review` | New PR opened | Fetch diff, review, comment |
| `dependency_check` | Project has open Dependabot alerts | Check severity, suggest fixes |
| `meeting_prep` | Calendar event in 30 min | Research attendees, prep notes |
| `relationship` | No contact > 14 days | Draft message, send if appropriate |
| `follow_up` | Task blocked > 3 days | Investigate blocker, report |
| `insight` | Pattern detected | Summarize pattern, suggest action |

### Phase 4: Testing & Validation (1-2 hours)

#### 3.4.1 Manual Test Flow

```bash
# 1. Verify webhook delivery works
curl -X POST http://127.0.0.1:8644/webhooks/colony-initiatives \
  -H "Content-Type: application/json" \
  -d '{
    "type": "initiative",
    "payload": {
      "initiative_type": "research",
      "title": "Test: Research MLX inference optimization",
      "description": "Research ways to speed up MLX model inference on Apple Silicon",
      "priority": 80,
      "context": {"suggested_actions": ["Search web for MLX inference tips"]}
    }
  }'

# 2. Check watcher (logs) for agent reasoning
# 3. Check user's primary channel for concise result
# 4. Check logs for errors
```

#### 3.4.2 Automated Health Check

Add a cronjob that polls the pipeline every 5 minutes:

```python
# ~/.hermes/scripts/colony-autonomy-health.py
# Checks: sidecar up, webhook up, initiatives flowing, delivery working
```

---

## 4. What We CANNOT Do (Hermes Source Required)

These require Hermes source changes and are **out of scope** for this plan:

1. **Agent self-triggering without webhook** — Hermes agents only start from platform events (messages, webhooks). There's no API to spawn an agent run programmatically.

2. **Silent agent runs** — All agent runs produce a response that must go somewhere (log, chat, etc.). There's no "background mode" where the agent works silently.

3. **Agent-to-agent communication** — No native mechanism for one agent instance to message another.

4. **Custom delivery endpoints** — Colony's `/internal/deliver` and `/internal/initiative` don't exist and would need to be added to Hermes gateway.

5. **Persistent agent state across runs** — Each webhook triggers a fresh agent run with fresh context. No shared memory between runs (except Colony's memory, which we can inject).

---

## 5. Recommended Immediate Actions

### Today (30 min)
1. Fix `~/.hermes/config.yaml` — set `deliver: log`, switch template to `{__raw__}`, add channel selection rules to prompt
2. Fix hook handler payload structure (wrap in `payload`)
3. Test with curl → verify agent runs and decides channel

### This Week (2-3 hours)
4. Add Colony direct POST to webhook
5. Disable redundant plugin polling
6. Add rich context to 2-3 initiative types

### Next Sprint (4-6 hours)
7. Expand initiative types (research, code_review, dependency_check)
8. Add health monitoring cronjob
9. Tune rate limits based on noise level

---

## 6. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| User spam from too many initiatives | Medium | Rate limit (5/min), digest mode for low-priority |
| Agent runs fail silently | Low | Log everything, health check cronjob |
| Colony generates bad initiatives | Medium | Agent validates before acting, reports uncertainty |
| User wants to approve before send | Medium | Add `requires_approval` flag to initiatives |
| Webhook delivery fails | Low | Fallback to `deliver: log` + cronjob alerts |
| Wrong channel chosen by agent | Low | Agent prompt includes channel selection rules; user can redirect |

---

## 7. Success Criteria

- [ ] User receives a message from the agent that was triggered by a Colony initiative
- [ ] The agent performed real work (not just "I will research this")
- [ ] The work was relevant and useful
- [ ] User did not have to prompt the agent to start
- [ ] Pipeline operates for 24h without manual intervention
- [ ] Channel selection is generic — works regardless of user's primary platform

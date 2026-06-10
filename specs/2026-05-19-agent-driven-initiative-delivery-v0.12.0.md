# Spec: Agent-Driven Initiative Delivery (Colony-Hermes Integration v0.12.0)

**Date:** 2026-05-19  
**Author:** ColonyAI agent
**Status:** Draft — awaiting owner review  
**Version:** Targets ColonyAI v0.12.0, Hermes Agent any  
**Prerequisites:** Temporal awareness spec (2026-05-15) — Phase 1 (sidecar telemetry) should be deployed first  

---

## 1. Executive Summary

Colony currently bypasses the agent's judgment by pushing "send this message now" directives directly to the user via webhooks. The agent's prompt instructs it to "Act on it immediately" and "Send AT MOST ONE message to the user per initiative." This causes:

1. **Orphan messages** — initiatives appear as isolated messages with no conversation context
2. **Spam** — the same initiative fires repeatedly because deduplication is broken at two levels
3. **No agent judgment** — Colony decides WHEN and WHETHER to message, not the agent
4. **No temporal awareness** — the agent has no concept of "how long since the last user message" or "was this already mentioned?"

This spec redesigns the pipeline so Colony **proposes** and the **agent decides**. Colony stores initiatives; the agent queries them, evaluates urgency against temporal context, and either appends to an ongoing conversation, starts a new one, or logs silently.

---

## 2. Root Cause Analysis

### 2.1 Deduplication is completely broken (Colony)

**Location:** `colony_sidecar/intelligence/components/initiative_engine.py:1069-1092`

```python
# Bug: goal_store has NO list_recent method. Only BriefingStore does.
if self._goal_store and entity_id and type_val == "follow_up":
    try:
        recent = self._goal_store.list_recent(
            entity_type="initiative",
            entity_id=entity_id,
            hours=cooldown_tasks,
        )
        if recent:
            continue  # Still in cooldown
    except Exception:
        pass  # Swallows AttributeError every tick
```

The `except Exception: pass` swallows the `AttributeError` silently. Every tick generates the same initiatives with no cooldown.

**Location:** `colony_sidecar/autonomy/loop.py:520-604` (`_phase_execute`)

The autonomy loop builds a payload and calls `delivery.push_initiative(payload)` — it **never persists the initiative to the initiative_store**. The store's SQLite dedup logic (`store.py:206-243`) works correctly but is never hit. Initiatives exist only in memory for one tick, then vanish.

**Result:** The same initiatives are generated and pushed endlessly.

### 2.2 Colony sends directives, not tasks (Architecture)

**Location:** `colony_sidecar/delivery/bridge.py:243-369` (`push_initiative`)

The bridge POSTs to the Hermes webhook with a payload that includes:
- `initiative_type`, `title`, `description`, `priority`
- `delivery_context` (user_chat, home_chat)
- `channel_hint` ("home" or "dm")

**Location:** `~/.hermes/config.yaml` — `colony-initiatives` webhook route prompt

The prompt instructs: *"Act on it immediately"*, *"Send AT MOST ONE message to the user per initiative"*, *"If you can complete the initiative autonomously, send ONE message summarizing what you did."*

There is no evaluation step. The agent is told to message the user, so it messages the user.

### 2.3 Webhooks create isolated sessions (Hermes)

**Location:** `gateway/platforms/webhook.py:527-543`

```python
session_chat_id = f"webhook:{route_name}:{delivery_id}"
# ...
source = self.build_source(
    chat_id=session_chat_id,
    chat_name=f"webhook/{route_name}",
    chat_type="webhook",
    user_id=f"webhook:{route_name}",
    user_name=route_name,
)
```

Each webhook POST creates a session like `webhook:colony-initiatives:123456`. This is completely isolated from the user's DM session (`agent:main:whatsapp:dm:...`). The agent running in the webhook session has no access to conversation history, no knowledge of whether the user was just messaged, and no way to append to an ongoing conversation.

### 2.4 Startup re-push amplifies spam (Colony)

**Location:** `colony_sidecar/autonomy/loop.py:1045-1119` (`_phase_startup_repush`)

On tick 1, the loop takes ALL pending initiatives from the store and re-pushes them to delivery. Combined with broken dedup, restarts blast the same messages again.

### 2.5 No temporal state in agent context (Both)

The webhook payload has `occurred_at` but the prompt does not surface it prominently. The agent has no access to:
- When the user was last messaged
- When this initiative was last pushed
- How many times it has been pushed
- Whether the user is in an active conversation

---

## 3. Proposed Architecture

### 3.1 Design Principles

1. **Colony proposes, agent decides** — Colony stores initiatives and makes them queryable. The agent decides whether, when, and how to act.
2. **No orphan messages** — Routine initiatives appear naturally in conversation context via in-session injection. Only urgent items trigger proactive messages.
3. **Temporal awareness is mandatory** — Every initiative carries creation time, last push time, and push count. The agent evaluates staleness before acting.
4. **Deduplication at the source** — Initiatives are persisted before any push happens. The store's existing SQLite dedup logic becomes the source of truth.

### 3.2 New Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         COLONY SIDECAR                               │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │   Autonomy   │───▶│  Initiative  │───▶│   Initiative Store   │  │
│  │    Loop      │    │   Engine     │    │   (SQLite dedup)     │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
│                                                  │                   │
│                          ┌───────────────────────┘                   │
│                          ▼                                           │
│                   ┌──────────────┐                                   │
│                   │   Delivery   │                                   │
│                   │   Bridge     │                                   │
│                   └──────┬───────┘                                   │
│                          │                                           │
│            ┌─────────────┼─────────────┐                             │
│            │             │             │                             │
│            ▼             ▼             ▼                             │
│     ┌──────────┐  ┌──────────┐  ┌──────────┐                       │
│     │ IN_SESSION│  │  PUSH    │  │  DIGEST  │                       │
│     │ (routine) │  │ (urgent) │  │ (bundle) │                       │
│     └─────┬─────┘  └─────┬────┘  └─────┬────┘                       │
│           │              │              │                            │
└───────────┼──────────────┼──────────────┼────────────────────────────┘
            │              │              │
            │              ▼              │
            │      ┌──────────────┐       │
            │      │ Hermes Webhook│       │
            │      │  (agent run)  │       │
            │      └──────┬───────┘       │
            │             │               │
            ▼             ▼               ▼
     ┌──────────────┐  ┌──────────────────────┐  ┌──────────────┐
     │ Colony-Memory│  │    send_message      │  │  Morning     │
     │ Provider     │  │    (agent decides)   │  │  Briefing    │
     │ pre_llm_call │  │                      │  │              │
     │  hook        │  │                      │  │              │
     └──────────────┘  └──────────────────────┘  └──────────────┘
```

### 3.3 Channel Routing Rules

| Initiative Priority | Colony Action | Agent Action |
|---------------------|---------------|--------------|
| `priority < 0.6` | Store as `IN_SESSION` only | Sees it in context on next user message; acts naturally |
| `0.6 <= priority < 0.85` | Store as `IN_SESSION`; also push webhook with `requires_approval: true` | Evaluates on webhook; may send message or log silently |
| `priority >= 0.85` | Store as `IN_SESSION`; push webhook with `requires_approval: false` | Evaluates on webhook; sends message unless quiet hours or recent contact |
| `priority >= 0.95` | Store as `IN_SESSION`; push webhook with `urgent: true` | Strong bias to message; only skips if in quiet hours |

---

## 4. Colony Changes (v0.12.0)

### 4.1 Fix Initiative Engine Deduplication

**File:** `colony_sidecar/intelligence/components/initiative_engine.py`

**Replace** the broken `goal_store.list_recent()` calls (lines 1069-1092) with initiative_store lookups:

```python
# Before: broken goal_store.list_recent() calls
# After: check initiative_store for existing pending initiatives

if self._initiative_store and entity_id:
    existing = self._initiative_store.get_by_dedup_key(
        f"{type_val}:{entity_id}"
    )
    if existing and existing.status == "pending":
        # Already pending — don't regenerate
        continue
    if existing and existing.status in ("assigned", "acknowledged"):
        # In progress — don't regenerate
        continue
```

**Add** `_initiative_store` reference to the engine (injected via registry).

### 4.2 Persist Initiatives Before Push

**File:** `colony_sidecar/autonomy/loop.py:520-604` (`_phase_execute`)

**Before pushing to delivery**, persist the initiative to the store:

```python
# In _phase_execute, before delivery.push_initiative(payload):
initiative_store = self._registry.initiative_store
if initiative_store:
    stored = initiative_store.create(
        type=type_value,
        description=initiative.description,
        priority=initiative.priority,
        rationale=initiative.rationale,
        action_hint=initiative.action_hint,
        entity_id=initiative.entity_id,
        dedup_key=f"{type_value}:{initiative.entity_id}",
        source_type="autonomy_loop",
        source_id=initiative.id,
    )
    # Use the stored ID for the payload
    payload["id"] = stored.id
```

### 4.3 Add Temporal Metadata to Initiatives

**File:** `colony_sidecar/initiatives/store.py`

**Add columns** to the initiatives table:

```python
# In _init_db schema:
last_pushed_at TEXT,       -- ISO-8601 timestamp
push_count INTEGER DEFAULT 0,
first_pushed_at TEXT,      -- ISO-8601 timestamp
```

**Add methods**:

```python
def record_push(self, initiative_id: str) -> None:
    """Increment push_count and update last_pushed_at."""
    now = datetime.now(timezone.utc).isoformat()
    self._db.execute(
        """
        UPDATE initiatives
        SET push_count = push_count + 1,
            last_pushed_at = ?,
            first_pushed_at = COALESCE(first_pushed_at, ?)
        WHERE id = ?
        """,
        [now, now, initiative_id],
    )
    self._db.commit()

def get_push_history(self, initiative_id: str) -> dict:
    """Return push metadata for an initiative."""
    row = self._db.execute(
        "SELECT first_pushed_at, last_pushed_at, push_count FROM initiatives WHERE id = ?",
        [initiative_id],
    ).fetchone()
    if not row:
        return {}
    return {
        "first_pushed_at": row[0],
        "last_pushed_at": row[1],
        "push_count": row[2],
    }
```

### 4.4 Modify Delivery Bridge for Tiered Routing

**File:** `colony_sidecar/delivery/bridge.py:243-369` (`push_initiative`)

**Replace** the current "always push webhook" logic with tiered routing:

```python
async def push_initiative(self, initiative: Dict[str, Any]) -> bool:
    """Push a structured initiative to Hermes via webhook.
    
    Only pushes if the initiative priority warrants proactive messaging.
    Lower-priority initiatives are left in IN_SESSION for the agent
    to discover on the next user message.
    """
    priority = initiative.get("priority", 0.5)
    
    # Always record the push attempt in the store
    initiative_store = getattr(self, "_initiative_store", None)
    if initiative_store and initiative.get("id"):
        initiative_store.record_push(initiative["id"])
    
    # Priority-based routing
    if priority < 0.6:
        # Low priority: IN_SESSION only, no webhook
        logger.debug("Initiative %s (priority=%.2f) queued as IN_SESSION", 
                     initiative.get("id"), priority)
        return True  # Considered "delivered" to in-session queue
    
    # Build and push webhook payload
    payload = { ... }  # existing payload building
    
    # Add temporal metadata
    if initiative_store and initiative.get("id"):
        push_history = initiative_store.get_push_history(initiative["id"])
        payload["push_history"] = push_history
    
    # Add requires_approval flag for mid-tier
    payload["requires_approval"] = priority < 0.85
    payload["urgent"] = priority >= 0.95
    
    # ... rest of existing push logic ...
```

### 4.5 Disable Startup Re-Push

**File:** `colony_sidecar/autonomy/loop.py:1045-1119` (`_phase_startup_repush`)

**Remove or gate** the re-push logic. Initiatives should not be re-pushed on startup — they are already in the store and will be discovered by the agent via tools or in-session injection.

```python
async def _phase_startup_repush(self) -> None:
    """On first tick: prune orphaned initiatives only. Do NOT re-push."""
    if self.stats.ticks != 1:
        return
    
    initiative_store = self._registry.initiative_store
    graph = self._registry.graph
    
    if initiative_store is None:
        return
    
    # Only prune orphaned initiatives (existing logic)
    # Remove the re-push block entirely
```

### 4.6 Add IN_SESSION API Endpoint

**File:** `colony_sidecar/api/routers/host.py` (new endpoint)

**Add** `GET /v1/host/delivery/in-session` to allow the Hermes provider to fetch pending in-session items:

```python
@router.get("/v1/host/delivery/in-session")
async def get_in_session_deliveries(
    person_id: str = "owner",
    limit: int = 20,
    bridge: ProactiveDeliveryBridge = Depends(get_delivery_bridge),
):
    """Return pending IN_SESSION deliveries for a person.
    
    Called by the Hermes colony-memory provider before each LLM call
    to inject Colony context into the conversation.
    """
    context = bridge.get_in_session_context(person_id)
    if context is None:
        return {"items": [], "count": 0}
    
    # Return structured items instead of formatted text
    items = [
        {
            "delivery_id": d.delivery_id,
            "content": d.content,
            "source": d.source,
            "urgency": d.urgency,
            "queued_at": d.queued_at.isoformat(),
            "initiative_id": d.initiative_id,
        }
        for d in bridge._pending
        if d.person_id == person_id 
        and d.channel == "in_session" 
        and not d.sent
    ][:limit]
    
    return {"items": items, "count": len(items)}
```

---

## 5. Hermes Changes

### 5.1 Extend Colony-Memory Provider with In-Session Injection

**File:** `~/.hermes/plugins/colony-memory/provider.py`

**Add** a `pre_llm_call` hook that fetches pending in-session items from Colony and injects them into the system prompt.

```python
# In the provider's __init__ or register method:
ctx.register_hook("pre_llm_call", self._inject_colony_context)

async def _inject_colony_context(self, messages: list, **kwargs) -> list:
    """Inject pending Colony initiatives into the system prompt."""
    # Only inject if there's a user message in this turn (not for cron/webhook runs)
    has_user_message = any(m.get("role") == "user" for m in messages)
    if not has_user_message:
        return messages
    
    # Fetch in-session items from Colony
    try:
        resp = self.client.get("/v1/host/delivery/in-session", timeout=3)
        if resp.status_code != 200:
            return messages
        data = resp.json()
        items = data.get("items", [])
    except Exception:
        return messages
    
    if not items:
        return messages
    
    # Format items for prompt injection
    lines = ["[Colony Context — Pending Items]"]
    for item in items:
        age_hours = self._hours_since(item["queued_at"])
        lines.append(
            f"• [{item['source']}] {item['content'][:120]}"
            f" (queued {age_hours:.1f}h ago, urgency={item['urgency']:.2f})"
        )
    
    context_text = "\n".join(lines)
    
    # Inject into system prompt (replace existing Colony context or append)
    injected = False
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            # Remove old Colony context block
            import re
            content = re.sub(
                r"\[Colony Context — Pending Items\].*?(?=\n\[|\Z)",
                "",
                content,
                flags=re.DOTALL,
            )
            content = content.strip()
            if content:
                msg["content"] = f"{content}\n\n{context_text}"
            else:
                msg["content"] = context_text
            injected = True
            break
    
    if not injected:
        messages.insert(0, {"role": "system", "content": context_text})
    
    # Mark items as consumed so they're not injected again
    for item in items:
        try:
            self.client.post(
                f"/v1/host/delivery/in-session/{item['delivery_id']}/consume",
                timeout=3,
            )
        except Exception:
            pass
    
    return messages
```

### 5.2 Update Webhook Prompt for Agent-Driven Evaluation

**File:** `~/.hermes/config.yaml` — `colony-initiatives` route

**Replace** the directive-style prompt with an evaluation-style prompt:

```yaml
colony-initiatives:
  deliver: log
  prompt: |
    {__raw__}

    Current time: {now}
    Your last conversation with the owner: {hours_since_last_user_message}h ago
    Colony sidecar status: {colony_status}
    Last successful turn sync: {last_sync_at} ({sync_silence_hours}h ago)

    Colony has generated an initiative. BEFORE ACTING, evaluate:

    1. TEMPORAL CHECK:
       - This initiative was created at {payload.created_at}
       - It has been pushed {payload.push_history.push_count} times (last: {payload.push_history.last_pushed_at})
       - If older than 24 hours, evaluate whether it is still relevant
       - If older than 72 hours, summarize briefly in logs and do NOT act on it

    2. CONVERSATION CONTEXT CHECK:
       - If you spoke with the owner within the last 30 minutes, this initiative
         may be a natural continuation. Consider appending to that conversation.
       - If no recent conversation, evaluate whether this initiative warrants
         interrupting the owner

    3. DECISION RULES:
       - URGENT (priority >= 0.95): Strong bias to message. Only skip if owner
         is in quiet hours or you messaged them within the last 15 minutes.
       - HIGH (priority 0.85-0.95): Message if no recent contact and not quiet hours.
       - MEDIUM (priority 0.6-0.85): Message only if genuinely time-sensitive.
         Otherwise log silently and let in-session injection handle it.
       - LOW (priority < 0.6): Never message from webhook. These are handled
         via in-session injection on the next user message.

    4. IF YOU DECIDE TO MESSAGE:
       - Use send_message to the appropriate channel (DM for personal, home for system)
       - Keep it to ONE message. No follow-ups.
       - Include enough context so the owner understands why you're messaging
       - Mark the initiative as acknowledged via colony_initiative_feedback

    5. IF YOU DECIDE NOT TO MESSAGE:
       - Log your reasoning
       - If the initiative requires_approval=true, you may still need to
         action it autonomously (complete tasks, send messages to contacts, etc.)
       - Report what you did in logs

    DELIVERY CHANNELS:
    - DM: {delivery_context.user_chat}
    - Home: {delivery_context.home_chat}
    - Route self-initiatives (subsystem_health, data_quality, etc.) to home channel
    - Route personal initiatives (follow_up, relationship) to DM
    - If the matching channel is missing, fall back to the other
    - If BOTH are missing, log only
```

**Note:** The `{now}`, `{hours_since_last_user_message}`, `{colony_status}`, etc. variables need to be resolved by the webhook adapter or the poller. If Hermes webhook routes don't support global template variables, we enrich the payload in the Colony bridge instead (see 4.4).

### 5.3 Add `colony_initiative_acknowledge` Tool

**File:** `~/.hermes/plugins/colony/__init__.py`

**Add** a tool for the agent to mark initiatives as seen without full feedback:

```python
{
    "name": "colony_initiative_acknowledge",
    "description": "Mark a Colony initiative as acknowledged (seen by agent).",
    "parameters": {
        "type": "object",
        "properties": {
            "initiative_id": {"type": "string"},
            "action": {
                "type": "string",
                "enum": ["acknowledged", "actioned", "dismissed", "snoozed"],
            },
            "reason": {"type": "string"},
        },
        "required": ["initiative_id", "action"],
    },
}
```

**Handler:**

```python
def _handle_colony_initiative_acknowledge(self, args: dict) -> str:
    try:
        resp = self._client.post(
            "/v1/host/initiatives/feedback",
            json={
                "initiative_id": args["initiative_id"],
                "action": args["action"],
                "details": {"reason": args.get("reason", "")},
            },
            timeout=5,
        )
        resp.raise_for_status()
        return json.dumps({"success": True})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
```

### 5.4 Add `session_search` Temporal Queries

The existing `session_search` tool can be used by the agent to check recent conversation history. The agent should query:
- "last conversation with owner" → to check recency
- "initiative about X" → to check if this was already discussed

No code changes needed — the tool already exists.

---

## 6. Data Flow Examples

### 6.1 Low-Priority Follow-Up (Routine)

1. Colony autonomy loop generates: *"Follow up on: Review Q2 budget"* (priority=0.55)
2. Initiative engine checks store — no existing pending `follow_up:budget-review`
3. Loop persists to initiative_store with `channel_hint="in_session"`
4. Delivery bridge sees priority < 0.6 — **no webhook push**
5. Initiative sits in store as `IN_SESSION`
6. User messages: "What's on my plate today?"
7. Hermes colony-memory provider's `pre_llm_call` hook fetches in-session items
8. Injects into system prompt: *"[Colony Context] • [follow_up] Follow up on: Review Q2 budget (queued 2.5h ago, urgency=0.55)"*
9. Agent sees context in prompt, responds naturally: *"You also have a pending follow-up on the Q2 budget review from Colony. Want me to help with that?"*
10. User replies: "Yes, pull up the spreadsheet"
11. Agent acts on it; conversation flows naturally

### 6.2 High-Priority Health Alert (Proactive)

1. Colony generates: *"Disk usage is 94% on spark1"* (priority=0.92)
2. Initiative engine checks store — no existing pending `subsystem_health:spark1-disk`
3. Loop persists to store
4. Delivery bridge sees priority >= 0.85 — **pushes webhook**
5. Payload includes: `urgent=true`, `requires_approval=false`, temporal metadata
6. Hermes webhook adapter creates isolated session, runs agent
7. Agent evaluates:
   - Initiative created 30 seconds ago
   - No recent user conversation (last contact 3 hours ago)
   - Not quiet hours
   - Priority 0.92 warrants proactive message
8. Agent sends message to home channel: *"Alert: Disk usage on spark1 is at 94%. I can clean up old logs if you'd like."*
9. Agent marks initiative as `actioned` via `colony_initiative_feedback`

### 6.3 Stale Initiative (Spam Prevention)

1. Colony generates: *"Follow up on: Call vendor"* (priority=0.7)
2. Loop persists and pushes webhook (priority >= 0.6)
3. Webhook fires; agent evaluates
4. Agent checks `push_history`: first_pushed_at=48h ago, push_count=5
5. Agent checks via `session_search`: owner was already reminded about this yesterday
6. Agent decides: stale, already discussed → logs *"Skipping stale initiative 'Call vendor' — already pushed 5 times over 48h and discussed yesterday"*
7. Agent marks initiative as `dismissed` via `colony_initiative_feedback`
8. No message sent to user

---

## 7. Implementation Order

| Phase | Scope | Files | Complexity |
|-------|-------|-------|------------|
| 1 | Fix initiative engine dedup (use initiative_store) | `initiative_engine.py` | Low |
| 2 | Persist initiatives before push | `autonomy/loop.py` | Low |
| 3 | Add temporal columns to initiative store | `initiatives/store.py`, `models.py` | Low |
| 4 | Add push tracking to delivery bridge | `delivery/bridge.py` | Low |
| 5 | Disable startup re-push | `autonomy/loop.py` | Low |
| 6 | Add IN_SESSION API endpoint | `api/routers/host.py` | Low |
| 7 | Extend colony-memory provider with pre_llm_call hook | `provider.py` | Medium |
| 8 | Update webhook prompt for evaluation | `config.yaml` | Low |
| 9 | Add colony_initiative_acknowledge tool | `plugins/colony/__init__.py` | Low |

**Dependencies:**
- Phase 1-6 are Colony-only and can be reviewed/merged together
- Phase 7-9 are Hermes-side and depend on Phase 6 (IN_SESSION API)
- The temporal awareness spec (2026-05-15) Phase 1 (sidecar telemetry) should be deployed first to provide `colony_status` and `last_sync_at` for the webhook prompt

---

## 8. Acceptance Criteria

- [ ] `initiative_engine.generate()` does not create duplicate initiatives within cooldown period
- [ ] Every generated initiative is persisted to `initiative_store` before any push
- [ ] `last_pushed_at`, `push_count`, `first_pushed_at` are tracked per initiative
- [ ] Initiatives with priority < 0.6 do NOT trigger webhook pushes
- [ ] Initiatives with priority >= 0.85 trigger webhook pushes
- [ ] Startup re-push is disabled (no blast on restart)
- [ ] `GET /v1/host/delivery/in-session` returns pending in-session items
- [ ] Hermes colony-memory provider injects in-session items into system prompt
- [ ] Agent evaluates temporal state before messaging (checks initiative age, push count, last user contact)
- [ ] Stale initiatives (>72h or push_count > 3) are logged, not messaged
- [ ] Agent uses `colony_initiative_feedback` to mark initiatives as acknowledged/actioned/dismissed
- [ ] No orphan messages: routine initiatives appear naturally in conversation context

---

## 9. Backward Compatibility

- Existing `IN_SESSION` deliveries in Colony's memory queue are preserved
- Webhook payloads gain new fields (`push_history`, `requires_approval`, `urgent`) but existing fields are unchanged
- The `initiative_store` schema change is additive (new columns) — existing rows get NULL defaults
- The autonomy bridge cron job can be left running; it will now evaluate initiatives rather than blindly messaging
- If the Hermes provider hook fails to fetch in-session items, the agent degrades gracefully (no injection, no error)

---

## 10. Open Questions

1. **Should the agent have a tool to query "when was I last in conversation with the owner?"**
   - Currently the agent can use `session_search` to find recent sessions
   - We could add a lightweight `colony_get_temporal_state` tool that returns last_user_contact, last_sync, etc.
   - **Recommendation:** Start with `session_search` + webhook prompt context. Add dedicated tool if needed.

2. **How do we handle timezone-aware quiet hours in the agent?**
   - Colony already has quiet hours config in `AutonomyConfig`
   - The webhook payload should include `quiet_hours_start` and `quiet_hours_end` in the owner's timezone
   - **Recommendation:** Add these to the payload in bridge.py.

3. **What about the existing `colony_autonomy_enable` tool that creates a cron job?**
   - The cron job should continue to run but its behavior changes: it now queries initiatives and evaluates them
   - The prompt in `_AUTONOMY_PROMPT` needs to be updated to match the new evaluation rules
   - **Recommendation:** Update `_AUTONOMY_PROMPT` in `plugins/colony/__init__.py` as part of this spec.

4. **Should IN_SESSION items be consumed immediately on injection or after the agent acts on them?**
   - Current proposal: consume on injection (mark as sent in Colony)
   - Risk: Agent might ignore the injected context, and the initiative is lost
   - **Alternative:** Only consume after agent calls `colony_initiative_feedback`
   - **Recommendation:** Consume on injection but require agent to call feedback. If no feedback within 24h, initiative becomes eligible for webhook push (escalation).

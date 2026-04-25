# Initiative Handoff to OpenClaw (v0.6.27)

## Problem

Current v0.6.26 design has Colony pushing **raw text** directly to user's phone via channel adapters. This is wrong because:

1. **No LLM** — Colony sends strings like "Commitment due: X" without context or personality
2. **No decision-making** — Every initiative becomes a message, even if inappropriate
3. **Home channel required** — Colony needs to know WHERE to send, but shouldn't decide HOW
4. **Wastes LLM capability** — OpenClaw has SOUL.md, MEMORY.md, context about the user

## Solution

Colony generates **structured initiatives** and pushes them to OpenClaw. OpenClaw's LLM decides:
- Compose a thoughtful message?
- Spawn an agent to act?
- Update a goal silently?
- Wait for better timing?
- Do nothing?

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         COLONY SIDECAR                          │
├─────────────────────────────────────────────────────────────────┤
│  AutonomyLoop._phase_initiative()                               │
│       ↓                                                         │
│  InitiativeEngine.generate()                                    │
│       ↓                                                         │
│  Initiative { type, priority, context, suggested_action }       │
│       ↓                                                         │
│  push_initiative(initiative)                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    POST /internal/initiative
                    Authorization: Bearer {api_key}
                    Content-Type: application/json
                    { initiative: {...} }
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      OPENCLAW PLUGIN                            │
├─────────────────────────────────────────────────────────────────┤
│  /internal/initiative handler                                   │
│       ↓                                                         │
│  api.runtime.system.enqueueSystemEvent(text, {                  │
│    sessionKey: "main",                                          │
│    contextKey: "colony:initiative:{id}"                         │
│  })                                                             │
│       ↓                                                         │
│  Main session receives as system message                        │
│       ↓                                                         │
│  LLM decides action                                             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LLM DECISION TREE                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Is priority >= 0.8 AND NOT quiet hours?                        │
│    → Compose contextual message to user                         │
│                                                                 │
│  Is action_hint "spawn_agent"?                                  │
│    → Spawn subagent with task                                   │
│                                                                 │
│  Is action_hint "update_goal"?                                  │
│    → Call colony_update_goal tool                               │
│                                                                 │
│  Is priority < 0.5?                                             │
│    → Defer, add to next briefing                                │
│                                                                 │
│  Is user in focus mode / DND?                                   │
│    → Queue for later                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Initiative Schema

```typescript
interface ColonyInitiative {
  id: string;                      // Unique initiative ID
  type: InitiativeType;            // "follow_up" | "relationship" | "scheduling" | "reminder"
  priority: number;                // 0.0 - 1.0
  title: string;                   // Short description
  description: string;             // Full context
  rationale: string;               // Why this was generated
  suggested_action: string;        // "notify_user" | "spawn_agent" | "update_goal" | "remind"
  entity_id?: string;              // Related contact/goal/commitment ID
  context: {                       // Rich context for LLM
    blocked_goal?: {
      goal_id: string;
      title: string;
      days_pending: number;
    };
    neglected_contact?: {
      contact_id: string;
      days_since_contact: number;
      valence_trend: string;
    };
    upcoming_commitment?: {
      commitment_id: string;
      description: string;
      hours_until_due: number;
    };
  };
  generated_at: string;            // ISO timestamp
}
```

## Files to Change

### 1. Colony Sidecar: `delivery/bridge.py`

**Change**: Add `push_initiative(initiative)` method (keep `push_to_gateway` for BriefingEngine)

```python
async def push_initiative(self, initiative: Dict[str, Any]) -> bool:
    """Push a structured initiative to OpenClaw for LLM decision-making.
    
    Returns True if gateway accepted, False otherwise.
    """
    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp not available — cannot push initiative")
        return False

    url = f"{self._gateway_url.rstrip('/')}/internal/initiative"
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if self._gateway_api_key:
        headers["Authorization"] = f"Bearer {self._gateway_api_key}"

    payload = {
        "initiative": initiative,
        "source": "autonomy_loop",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                if resp.status == 200:
                    logger.info(
                        "Initiative pushed to gateway: %s (type=%s, priority=%.2f)",
                        initiative.get("id"),
                        initiative.get("type"),
                        initiative.get("priority", 0),
                    )
                    return True
                body = await resp.text()
                logger.warning(
                    "Gateway /internal/initiative returned %d: %s",
                    resp.status, body[:200]
                )
                return False
    except Exception as exc:
        logger.warning("push_initiative failed: %s", exc)
        return False
```

**Note**: The `_home_channels`, `_load_home_channels()`, and `resolve_home_channel()` methods are **NOT removed** — they're still used by the BriefingEngine. The `push_to_gateway()` method remains for briefings; `push_initiative()` is added for autonomy loop use.

### 2. Colony Sidecar: `autonomy/loop.py`

**Change**: `_phase_execute()` pushes structured initiative, not text

```python
async def _phase_execute(self) -> None:
    """Push initiatives to OpenClaw for LLM decision-making."""
    delivery = self._registry.delivery
    if delivery is None:
        return

    for initiative in list(self._pending_initiatives):
        try:
            # Build structured initiative payload
            # Note: Initiative dataclass has: id, type, description, priority, rationale, action_hint, entity_id
            # We add title (derived from description) and context (from engine state)
            initiative_type = getattr(initiative, "type", "unknown")
            type_value = initiative_type.value if hasattr(initiative_type, "value") else str(initiative_type)
            
            payload = {
                "id": getattr(initiative, "id", str(uuid.uuid4())),
                "type": type_value,
                "priority": getattr(initiative, "priority", 0.5),
                "title": getattr(initiative, "description", "").split(".")[0][:80] if getattr(initiative, "description", "") else "(no title)",
                "description": getattr(initiative, "description", ""),
                "rationale": getattr(initiative, "rationale", ""),
                "suggested_action": getattr(initiative, "action_hint", "notify_user") or "notify_user",
                "entity_id": getattr(initiative, "entity_id", None),
                "context": self._last_initiative_context if hasattr(self, "_last_initiative_context") else {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

            ok = await delivery.push_initiative(payload)
            if ok:
                self.stats.actions_executed += 1
                self.stats.actions_this_hour += 1
                logger.info("Pushed initiative: %s", payload["id"])
        except Exception as exc:
            logger.error("Failed to push initiative: %s", exc)

    self._pending_initiatives = []
```

**Also add** to `_phase_initiative()` to capture context from engine:

```python
# After engine.generate(), capture context for payload building
engine = self._registry.initiative_engine
if engine:
    # InitiativeEngine stores context in self._context
    self._last_initiative_context = dict(getattr(engine, "_context", {}))
```

### 3. Colony Plugin: `src/plugin.ts`

**Change**: Replace `/internal/deliver` with `/internal/initiative` that enqueues a system event

```typescript
api.registerHttpRoute({
  path: "/internal/initiative",
  auth: "plugin",
  match: "exact",
  handler: async (req, res) => {
    const bodyResult = await readJsonBodyWithLimit(req, { maxBytes: 64 * 1024 });
    if (!bodyResult.ok) {
      res.statusCode = 400;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Invalid request body" }));
      return true;
    }

    const { initiative, source, timestamp } = bodyResult.value as Record<string, unknown>;

    // Auth check
    const colonyApiKey = api.pluginConfig?.apiKey as string | undefined;
    const authHeader = req.headers["authorization"] as string | undefined;
    const presentedKey = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : null;
    
    if (presentedKey !== colonyApiKey) {
      res.statusCode = 401;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Unauthorized" }));
      return true;
    }

    // Validate initiative structure
    if (!initiative || typeof initiative !== "object") {
      res.statusCode = 400;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Missing initiative object" }));
      return true;
    }

    const init = initiative as Record<string, unknown>;

    // Format as readable text for LLM
    const text = formatInitiativeText(init);

    // Enqueue as system event in main session
    try {
      const enqueued = api.runtime.system.enqueueSystemEvent(text, {
        sessionKey: "main",
        contextKey: `colony:initiative:${init.id}`,
        trusted: true,
      });

      if (!enqueued) {
        api.logger.warn?.(`[colony] Duplicate initiative blocked: ${init.id}`);
      }

      res.statusCode = 200;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, enqueued, initiativeId: init.id }));
      
      api.logger.info?.(`[colony] Initiative enqueued: ${init.id} (priority=${init.priority})`);
    } catch (err) {
      api.logger.error?.(`[colony] Failed to enqueue initiative: ${err}`);
      res.statusCode = 500;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: String(err) }));
    }
    return true;
  }
});

// Helper to format initiative as readable text for LLM
function formatInitiativeText(init: Record<string, unknown>): string {
  const lines = [
    `[colony_initiative]`,
    `ID: ${init.id ?? "unknown"}`,  // Include ID for deduplication (text-based)
    `Type: ${init.type ?? "unknown"}`,
    `Priority: ${init.priority ?? 0}`,
    `Title: ${init.title ?? "(no title)"}`,
    `Description: ${init.description ?? "(no description)"}`,
    `Rationale: ${init.rationale ?? "(no rationale)"}`,
    `Suggested action: ${init.suggested_action ?? "notify_user"}`,
  ];

  // Add context if present
  const ctx = init.context as Record<string, unknown> | undefined;
  if (ctx) {
    if (ctx.blocked_goal) {
      const g = ctx.blocked_goal as Record<string, unknown>;
      lines.push(`Context - Blocked goal: ${g.title} (pending ${g.days_pending} days)`);
    }
    if (ctx.neglected_contact) {
      const c = ctx.neglected_contact as Record<string, unknown>;
      lines.push(`Context - Neglected contact: ${c.contact_id} (${c.days_since_contact} days since contact)`);
    }
    if (ctx.upcoming_commitment) {
      const c = ctx.upcoming_commitment as Record<string, unknown>;
      lines.push(`Context - Upcoming commitment: ${c.description} (due in ${c.hours_until_due}h)`);
    }
  }

  return lines.join("\n");
}
```

### 4. OpenClaw System Event Appearance

The initiative appears in the main session as a "System:" message:

```
System: [2026-04-25 14:30:00] [colony_initiative]
ID: abc123
Type: follow_up
Priority: 0.85
Title: Blocked goal pending 3 days
Description: Your "Deploy API to production" goal has been blocked for 3 days.
Rationale: Goal priority=80, days_pending=3
Suggested action: notify_user
Context - Blocked goal: Deploy API to production (pending 3 days)
```

The LLM sees this in its message history and decides what to do.

**Note**: The ID line is included so that identical initiatives are deduplicated by text content. If the same initiative is sent twice, the second one is ignored because the text matches `entry.lastText`.

### 5. Deduplication Behavior

`enqueueSystemEvent` deduplicates by **text content**, not `contextKey`:

```javascript
// From OpenClaw's system-events.ts
if (entry.lastText === cleaned) return false;  // Dedupe by text
entry.lastText = cleaned;
```

This means:
- Same initiative text twice → second one blocked ✅
- Different text (even same `contextKey`) → both enqueued

The `contextKey` is stored for `isSystemEventContextChanged()` checks but NOT for deduplication. Since we include the initiative `id` in the text, identical initiatives are still deduplicated correctly.

### 6. LLM Guidance Skill

Create `~/.openclaw/workspace/skills/colony-initiatives/SKILL.md`:

```markdown
# Colony Initiatives

When you receive a `[colony_initiative]` system event:

## Decision Tree

1. **Check priority**:
   - ≥0.8: Consider acting soon
   - 0.5-0.8: Add to next briefing or handle opportunistically
   - <0.5: Defer silently

2. **Check timing**:
   - Quiet hours (22:00-07:00)? Queue for morning unless urgent
   - User in focus mode? Wait unless priority ≥0.9

3. **Choose action based on `suggested_action`**:
   - `notify_user`: Compose a brief, contextual message. Use your personality.
   - `spawn_agent`: Create a subagent to investigate or fix
   - `update_goal`: Call colony_update_goal silently
   - `remind`: Schedule a reminder, don't message now

## Composing Messages

- Don't just forward the raw description
- Add context from what you know about the user
- Be helpful, not nagging
- One clear message, not a wall of text
```

## Config Changes

**Before (v0.6.26)**:
```bash
COLONY_GATEWAY_INTERNAL_URL="http://127.0.0.1:18789"
COLONY_API_KEY="<key>"
WHATSAPP_HOME_CHANNEL="+1234567890"  # ← REQUIRED
```

**After (v0.6.27)**:
```bash
COLONY_GATEWAY_INTERNAL_URL="http://127.0.0.1:18789"
COLONY_API_KEY="<key>"
# No home channel needed — OpenClaw decides delivery
```

## LLM Prompt Guidance

Add to OpenClaw's system prompt (or a skill):

```markdown
## Handling Colony Initiatives

When you receive a `colony_initiative` system event:

1. **Check priority**: 
   - ≥0.8: Consider notifying user soon
   - 0.5-0.8: Add to next briefing or handle opportunistically
   - <0.5: Defer silently

2. **Check timing**:
   - Quiet hours (22:00-07:00)? Queue for morning unless urgent
   - User in focus mode? Wait unless priority ≥0.9

3. **Choose action**:
   - `notify_user`: Compose a brief, contextual message. Use your personality.
   - `spawn_agent`: Create a subagent to investigate or fix
   - `update_goal`: Call colony_update_goal silently
   - `remind`: Schedule a reminder, don't message now

4. **Compose thoughtfully**:
   - Don't just forward the raw description
   - Add context from what you know about the user
   - Be helpful, not nagging
```

---

## Concerns & Limitations

### Single-User Assumption

Current design assumes one user per Colony instance. For multi-user:
- Colony would need to track `user_id` per goal/commitment
- Initiative payload would include `target_user` field
- Plugin would route to correct session

**Future work**: Add `entity_id` → `sessionKey` mapping in plugin.

### Rate Limiting

| Layer | Limit |
|-------|-------|
| Colony (`max_actions_per_hour`) | 20/hour default |
| OpenClaw (text dedupe) | Prevents identical initiatives |
| LLM decision | Can always choose to do nothing |

### No Guaranteed Delivery

System events are in-memory. If OpenClaw restarts before the LLM processes the initiative:
- Initiative is lost
- Colony doesn't retry

**Future work**: Colony could persist initiatives with `status: pending` and retry on gateway failure.

## Benefits

| Before (v0.6.26) | After (v0.6.27) |
|------------------|-----------------|
| Colony decides WHAT to send | Colony proposes, LLM decides |
| Raw text strings | Structured context |
| Always sends message | Can defer, act silently, spawn agents |
| Home channel required | Only gateway URL needed |
| No user context | Full SOUL.md, MEMORY.md, conversation context |

## Files Changed

| File | Change |
|------|--------|
| `delivery/bridge.py` | Add `push_initiative()` method (keep `push_to_gateway` and home channel logic for BriefingEngine) |
| `autonomy/loop.py` | Update `_phase_execute()` to use `push_initiative()`, add context capture in `_phase_initiative()` |
| `src/plugin.ts` | Replace `/internal/deliver` with `/internal/initiative`, add `formatInitiativeText()` helper |
| `skills/colony-initiatives/SKILL.md` | Add LLM guidance skill (optional, but recommended) |
| `pyproject.toml` | Bump to 0.6.27 |

## Testing

```bash
# 1. Create a blocked goal
curl -X POST http://127.0.0.1:7777/v1/host/goals \
  -H "Authorization: Bearer colony" \
  -H "Content-Type: application/json" \
  -d '{"title": "Test blocked goal", "status": "blocked"}'

# 2. Wait for autonomy tick (5 min) or trigger manually

# 3. Check OpenClaw main session logs for system event
# Should see: "System: [timestamp] [colony_initiative] ..."

# 4. Verify LLM response — should compose contextual message or take action
```

## Automatic Skill Installation

The `colony-initiatives` skill should be auto-written during:
- `colony init --openclaw` (harness integration)
- `colony mcp setup --harness openclaw`

This is handled by the existing harness integration system (v0.6.25).

## Version

- **Spec Version**: v0.6.27
- **Created**: 2026-04-25
- **Status**: Ready for implementation

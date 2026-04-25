# Initiative Engine Wiring + Direct Delivery (v0.6.26)

## Problem Statement

Colony's autonomy loop runs but is **passive** — it ticks every 5 minutes but generates zero initiatives and delivers zero messages.

### Root Causes

1. **Loop calls non-existent method**: `_phase_initiative()` checks for `cognition.generate_initiatives()` but MetaLearner doesn't have this method
2. **InitiativeEngine exists but isn't wired**: The engine is at `intelligence/components/initiative_engine.py` but never instantiated
3. **Delivery path broken**: Loop emits `action_executed` events, but plugin handles `proactive_message` events
4. **Token-burning workaround**: Current delivery uses subagent which burns LLM tokens per message

---

## Solution Overview

Two parts:
1. **Wire InitiativeEngine** to autonomy loop (no LLM needed — rule-based)
2. **Add `/internal/deliver` endpoint** to Colony plugin for direct message delivery

---

## Part 1: Wire InitiativeEngine

### 1.1 Add Properties to SubsystemRegistry

**File**: `sidecar/colony_sidecar/autonomy/registry.py`

```python
# ADD these properties (note: singular names match actual store names)

@property
def initiative_engine(self) -> Any:
    """Get or create the InitiativeEngine (NOT MetaLearner)."""
    if not hasattr(self, '_initiative_engine'):
        try:
            from colony_sidecar.intelligence.components.initiative_engine import InitiativeEngine
            from colony_sidecar.api.routers.host import _graph

            self._initiative_engine = InitiativeEngine(
                graph_client=_graph.driver if _graph and hasattr(_graph, 'driver') else None,
                event_bus=None,  # Not needed for rule-based generation
                mind_model=None,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to create InitiativeEngine: %s", e)
            self._initiative_engine = None
    return self._initiative_engine

@property
def commitment_store(self) -> Any:
    """Get the CommitmentStore."""
    from colony_sidecar.api.routers.host import _commitment_store  # singular
    return _commitment_store

@property
def affect_store(self) -> Any:
    """Get the AffectStore."""
    from colony_sidecar.api.routers.host import _affect_store
    return _affect_store

@property
def pattern_store(self) -> Any:
    """Get the PatternStore."""
    from colony_sidecar.api.routers.host import _pattern_store
    return _pattern_store
```

### 1.2 Rewrite `_phase_initiative` in AutonomyLoop

**File**: `sidecar/colony_sidecar/autonomy/loop.py`

```python
async def _phase_initiative(self) -> None:
    """Run initiative engine to generate autonomous action proposals."""
    engine = self._registry.initiative_engine
    if engine is None:
        return

    try:
        engine.clear_context()
        await self._feed_pending_tasks(engine)
        await self._feed_neglected_contacts(engine)
        await self._feed_commitment_reminders(engine)

        initiatives = await engine.generate(
            min_priority=self.config.initiative_confidence_threshold,
        )

        if self._in_quiet_hours():
            initiatives = [i for i in initiatives if i.priority >= 0.9]

        if initiatives:
            logger.info("Phase initiative: %d new proposals", len(initiatives))
        self._pending_initiatives = initiatives
        self.stats.initiatives_generated += len(initiatives)
    except Exception as exc:
        self.stats.errors += 1
        logger.error("Phase initiative error: %s", exc, exc_info=True)
        self._pending_initiatives = []
```

### 1.3 Add Context Feeding Helpers

```python
async def _feed_pending_tasks(self, engine: Any) -> None:
    """Feed blocked goals as pending tasks."""
    goals = self._registry.goals
    if goals is None:
        return

    try:
        blocked = goals.list_goals(status="blocked", limit=20) if hasattr(goals, "list_goals") else []
        pending_tasks = []

        for goal in blocked:
            # Goal is a dataclass with attribute access
            created = goal.created_at
            days_pending = 0
            if created:
                from datetime import datetime, timezone
                days_pending = (datetime.now(timezone.utc) - created).total_seconds() / 86400

            pending_tasks.append({
                "description": goal.title or "blocked goal",
                "days_pending": days_pending,
                "entity_id": goal.context.get("contact_id") if goal.context else None,
            })

        if pending_tasks:
            engine.add_context("pending_tasks", pending_tasks)
    except Exception as e:
        logger.warning("Failed to feed pending tasks: %s", e)

async def _feed_neglected_contacts(self, engine: Any) -> None:
    """Feed contacts with declining affect."""
    affect = self._registry.affect_store
    if affect is None:
        return

    try:
        # AffectStore.get_all_states() returns list of contact state dicts
        states = affect.get_all_states() if hasattr(affect, "get_all_states") else []
        neglected = []

        for state in states[:20]:
            contact_id = state.get("contact_id")
            if not contact_id:
                continue

            # Note: field is "current_valence", not "valence"
            valence = state.get("current_valence", 0)
            if valence < -0.3:
                neglected.append({
                    "name": contact_id,
                    "entity_id": contact_id,
                    "days_since_contact": 0,
                })

        if neglected:
            engine.add_context("neglected_contacts", neglected)
    except Exception as e:
        logger.warning("Failed to feed neglected contacts: %s", e)

async def _feed_commitment_reminders(self, engine: Any) -> None:
    """Feed upcoming commitments as scheduling opportunities."""
    commitments = self._registry.commitment_store
    if commitments is None:
        return

    try:
        # CommitmentStore.list() returns {"commitments": [...], "total": N}
        result = commitments.list(status=["pending"], limit=20) if hasattr(commitments, "list") else {"commitments": []}
        active = result.get("commitments", [])
        
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        opportunities = []

        for c in active:
            due = c.get("due_at")
            if not due:
                continue

            if isinstance(due, str):
                due = datetime.fromisoformat(due.replace("Z", "+00:00"))

            hours_until = (due - now).total_seconds() / 3600

            if 0 < hours_until < 48:
                opportunities.append({
                    "description": f"Commitment due: {c.get('description', 'untitled')}",
                    "priority": 0.9 if hours_until < 4 else 0.6,
                    "rationale": f"Due in {int(hours_until)}h",
                    "action_hint": "remind_user",
                })

        if opportunities:
            engine.add_context("scheduling_opportunities", opportunities)
    except Exception as e:
        logger.warning("Failed to feed commitment reminders: %s", e)
```

---

## Part 2: Fix Delivery Path

### 2.1 Add `/internal/deliver` Endpoint to Colony Plugin

**File**: `src/plugin.ts`

```typescript
// At top of file, add import
import { readJsonBodyWithLimit } from "openclaw/plugin-sdk/webhook-request-guards";

// In register() function

api.registerHttpRoute({
  path: "/internal/deliver",
  auth: "plugin",  // No gateway auth — we check our own
  match: "exact",
  handler: async (req, res) => {
    // Parse JSON body
    const bodyResult = await readJsonBodyWithLimit(req, { maxBytes: 64 * 1024 });
    if (!bodyResult.ok) {
      res.statusCode = 400;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Invalid request body" }));
      return true;
    }
    
    const { platform, chat_id, message, source } = bodyResult.value as Record<string, unknown>;

    // Auth check - Authorization header or X-Colony-Api-Key
    const colonyApiKey = api.pluginConfig?.apiKey as string | undefined;
    const authHeader = req.headers["authorization"] as string | undefined;
    const customHeader = req.headers["x-colony-api-key"] as string | undefined;
    
    const presentedKey = authHeader?.startsWith("Bearer ") 
      ? authHeader.slice(7) 
      : customHeader;
    
    if (presentedKey !== colonyApiKey) {
      res.statusCode = 401;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Unauthorized" }));
      return true;
    }

    // Validate required fields
    if (typeof platform !== "string" || typeof chat_id !== "string" || typeof message !== "string") {
      res.statusCode = 400;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "Missing required fields: platform, chat_id, message" }));
      return true;
    }

    // Load channel adapter
    const adapter = await api.runtime.channel.outbound.loadAdapter(platform);
    if (!adapter?.sendText) {
      res.statusCode = 400;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: `Platform ${platform} not available` }));
      return true;
    }

    // Send message
    try {
      const result = await adapter.sendText({
        cfg: api.config,
        to: chat_id,
        text: message,
      });
      res.statusCode = 200;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, messageId: (result as { messageId?: string })?.messageId }));
    } catch (err) {
      api.logger.error?.(`Colony delivery failed: ${err}`);
      res.statusCode = 500;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: String(err) }));
    }
    return true;
  }
});
```

### 2.2 Update `_phase_execute` to Use Direct Delivery

**File**: `sidecar/colony_sidecar/autonomy/loop.py`

```python
async def _phase_execute(self) -> None:
    """Execute approved actions via direct delivery."""
    for initiative in list(self._pending_initiatives):
        if self.stats.actions_this_hour >= self.config.max_actions_per_hour:
            logger.warning("Hourly action limit reached")
            break

        delivery = self._registry.delivery
        if delivery is None:
            continue

        home = delivery.resolve_home_channel()
        if home is None:
            logger.warning("No home channel configured — cannot deliver")
            continue

        try:
            ok = await delivery.push_to_gateway(
                platform=home["platform"],
                chat_id=home["chat_id"],
                message=getattr(initiative, "description", ""),
                source="initiative",
            )
            if ok:
                self.stats.actions_executed += 1
                self.stats.actions_this_hour += 1
                logger.info("Delivered initiative: %s", getattr(initiative, "id", "?"))
        except Exception as exc:
            logger.error("push_to_gateway failed: %s", exc)

    self._pending_initiatives = []
```

---

## Files Changed

| File | Change |
|------|--------|
| `autonomy/registry.py` | ADD 4 properties: `initiative_engine`, `commitment_store`, `affect_store`, `pattern_store` |
| `autonomy/loop.py` | REPLACE `_phase_initiative()`, ADD 3 context helpers, UPDATE `_phase_execute()` |
| `src/plugin.ts` | ADD `/internal/deliver` HTTP route |
| `pyproject.toml` | BUMP version to 0.6.26 |

---

## Config Required (User Sets Their Own)

```bash
# In Colony .env
COLONY_GATEWAY_INTERNAL_URL="http://127.0.0.1:18789"
COLONY_API_KEY="<user-api-key>"

# At least one home channel required:
WHATSAPP_HOME_CHANNEL="<user-phone-or-chat-id>"
# TELEGRAM_HOME_CHANNEL="<chat-id>"
# DISCORD_HOME_CHANNEL="<channel-id>"
# SLACK_HOME_CHANNEL="<channel-id>"
```

---

## Why This Approach

| Concern | Solution |
|---------|----------|
| No LLM needed | InitiativeEngine is rule-based (priority = `0.4 + days * 0.1`) |
| No token burn | HTTP route bypasses subagent entirely |
| No OpenClaw core changes | Plugin adds the route, not core |
| Works on any setup | Uses existing channel adapter system |
| Fast | ~10ms localhost HTTP latency |
| Reliable | HTTP status codes, can retry on failure |

---

## Testing

```bash
# 1. Create a blocked goal
curl -X POST http://127.0.0.1:7777/v1/host/goals \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"title": "Test blocked goal", "status": "blocked"}'

# 2. Wait for next autonomy tick (~5 min) or restart sidecar

# 3. Check autonomy status
curl http://127.0.0.1:7777/v1/host/autonomy/status \
  -H "Authorization: Bearer <api-key>"

# Should see: "initiatives_generated": 1, "actions_executed": 1
```

---

## Version

- **Spec Version**: v0.6.26 (consolidated)
- **Created**: 2026-04-25
- **Status**: Ready for implementation

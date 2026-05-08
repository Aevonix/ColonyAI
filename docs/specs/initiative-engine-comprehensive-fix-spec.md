# Comprehensive Initiative Engine Fix Specification

## Overview

This specification documents fixes for 60 bugs identified in the ColonyAI initiative generation and delivery pipeline during deep code audits. The bugs span the InitiativeEngine, AutonomyLoop, SubsystemRegistry, ProactiveDeliveryBridge, and InitiativeStore components.

**Target Branch:** `fix/initiative-engine-generation` → `main`
**Version:** v0.7.14
**Author:** Aeva
**Date:** 2026-05-07

---

## Critical Bugs (Must Fix Before Merge)

### Bug 11: `mark_initiative_generated` Only Called for Last Initiative

**Severity:** HIGH
**Location:** `initiative_engine.py`, `generate()` method, lines 541-548

**Problem:** The `mark_initiative_generated()` call is outside the `for initiative in result:` loop but uses the loop variable `initiative`. In Python, loop variables leak into enclosing scope, so only the last initiative gets marked.

**Current Code:**
```python
for initiative in result:
    try:
        self._store.create(...)
    except Exception as e:
        logger.warning(...)

# Mark initiative as generated on the goal (v0.7.10)
if self._goal_store and initiative.entity_id:
    try:
        self._goal_store.mark_initiative_generated(initiative.entity_id)
```

**Fix:** Move `mark_initiative_generated()` inside the loop:
```python
for initiative in result:
    try:
        self._store.create(...)
        
        # Mark initiative as generated on the goal (v0.7.10)
        if self._goal_store and initiative.entity_id:
            try:
                self._goal_store.mark_initiative_generated(initiative.entity_id)
            except Exception as e:
                logger.debug("Failed to mark initiative generated for %s: %s", initiative.entity_id, e)
    except Exception as e:
        logger.warning("Failed to persist initiative %s: %s", initiative.id, e)
```

---

### Bug 12: Research Tasks Use Wrong `days_pending`

**Severity:** HIGH
**Location:** `initiative_engine.py`, `_load_pending_research_tasks()`, line 432

**Problem:** Sets `days_pending` to the threshold config value instead of calculating actual age from `created_at`.

**Current Code:**
```python
existing_tasks.append({
    "entity_id": record["id"],
    "description": f"Research: {record.get('title', 'Unknown')}",
    "days_pending": self._config.research_task_age_days,  # BUG
    "priority": record.get("priority", 0.5),
})
```

**Fix:**
```python
created_at = record.get("created_at")
if created_at:
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    elif hasattr(created_at, 'to_native'):
        created_at = created_at.to_native()
    days_pending = (datetime.now(timezone.utc) - created_at).days
else:
    days_pending = self._config.research_task_age_days

existing_tasks.append({
    "entity_id": record["id"],
    "description": f"Research: {record.get('title', 'Unknown')}",
    "days_pending": max(0, days_pending),
    "priority": record.get("priority", 0.5),
})
```

---

### Bug 20: Graph Priority Discarded in Follow-Up Generation

**Severity:** HIGH
**Location:** `initiative_engine.py`, `_generate_follow_ups()`, line 705

**Problem:** The context item includes a `"priority"` field from the graph (e.g., goal priority), but `_generate_follow_ups()` ignores it and calculates priority solely from days pending. A high-priority goal blocked for 1 day gets lower priority than a low-priority goal blocked for 5 days.

**Current Code:**
```python
priority = min(1.0, 0.4 + days * 0.1)
```

**Fix:** Blend graph priority with time-based priority:
```python
graph_priority = item.get("priority", 0.5)
days_priority = min(1.0, 0.4 + days * 0.1)
# Weight: 60% time-based, 40% graph priority
priority = min(1.0, days_priority * 0.6 + graph_priority * 0.4)
```

---

### Bug 37: `_last_graph_load` Not Reset on `clear_context()`

**Severity:** CRITICAL
**Location:** `initiative_engine.py`, `clear_context()`, line 156

**Problem:** When `clear_context()` is called at the start of each tick, `_last_graph_load` is not reset. If the next `generate()` call happens within 10 seconds, `_load_graph_context()` skips loading entirely, leaving the context empty.

**Current Code:**
```python
def clear_context(self, context_type: Optional[str] = None) -> None:
    if context_type:
        self._context.pop(context_type, None)
    else:
        self._context.clear()
```

**Fix:**
```python
def clear_context(self, context_type: Optional[str] = None) -> None:
    if context_type:
        self._context.pop(context_type, None)
    else:
        self._context.clear()
    # Reset graph load cache so next generate() reloads from graph
    self._last_graph_load = None
```

---

### Bug 38: `created_at` Uses Naive Datetime

**Severity:** HIGH
**Location:** `initiative_engine.py`, `Initiative` dataclass, line 95

**Problem:** `datetime.now()` returns a naive datetime. Mixed timezone-aware and naive datetimes cause `TypeError` on comparison.

**Current Code:**
```python
created_at: datetime = field(default_factory=datetime.now)
```

**Fix:**
```python
created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

---

### Bug 44/45: Health/Scheduling Initiatives Bypass Cooldown Checks

**Severity:** HIGH
**Location:** `initiative_engine.py`, `_generate_health_suggestions()` and `_generate_scheduling_suggestions()`

**Problem:** Health and scheduling initiatives don't have `entity_id` or `dedup_key`, so the cooldown check in `generate()` skips them entirely.

**Current Code (health):**
```python
initiatives.append(
    Initiative(
        id=f"health-{_uuid_module.uuid4().hex[:12]}",
        type=InitiativeType.HEALTH,
        description=f"Review {metric}: current={value}, target={target}",
        priority=priority,
        rationale=f"{metric} is outside target range",
        action_hint=f"Check and adjust {metric}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
)
```

**Fix (health):**
```python
initiatives.append(
    Initiative(
        id=f"health-{_uuid_module.uuid4().hex[:12]}",
        type=InitiativeType.HEALTH,
        description=f"Review {metric}: current={value}, target={target}",
        priority=priority,
        rationale=f"{metric} is outside target range",
        action_hint=f"Check and adjust {metric}",
        entity_id=metric,
        dedup_key=f"health:{metric}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
)
```

**Fix (scheduling):**
```python
# Use description hash for dedup
dedup_key = f"schedule:{hash(desc) % 10000000}"
initiatives.append(
    Initiative(
        id=f"schedule-{_uuid_module.uuid4().hex[:12]}",
        type=InitiativeType.SCHEDULING,
        description=desc,
        priority=min(1.0, priority),
        rationale=slot.get("rationale", "Based on observed patterns"),
        action_hint=slot.get("action_hint"),
        entity_id=dedup_key.split(":", 1)[1] if ":" in dedup_key else None,
        dedup_key=dedup_key,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
)
```

---

### Bug 47: `complete()` Calls Goal Store with Wrong ID

**Severity:** HIGH
**Location:** `initiative_engine.py`, `complete()`, line 577

**Problem:** `complete()` passes the initiative ID (e.g., `"followup-abc123"`) to `goal_store.complete_task()` instead of the goal's entity_id.

**Current Code:**
```python
self._goal_store.complete_task(initiative_id, result=result)
```

**Fix:** Look up entity_id from store or in-memory list:
```python
# Find the initiative to get its entity_id
entity_id = None
if self._store:
    try:
        stored = self._store.get(initiative_id)
        if stored:
            entity_id = stored.entity_id
    except Exception:
        pass

# Fallback to in-memory list
if not entity_id:
    for init in self._initiatives:
        if init.id == initiative_id:
            entity_id = init.entity_id
            break

if self._goal_store and entity_id:
    try:
        self._goal_store.complete_task(entity_id, result=result)
    except Exception as e:
        logger.debug("Failed to complete goal %s: %s", entity_id, e)
```

---

### Bug 50/51: Neo4j DateTime Objects Not Handled

**Severity:** HIGH
**Location:** `initiative_engine.py`, `_load_blocked_goals()` and `_load_neglected_contacts()`

**Problem:** Neo4j's Python driver returns `neo4j.time.DateTime` objects, not Python `datetime`. Subtraction fails.

**Current Code:**
```python
if isinstance(blocked_at, str):
    blocked_at = datetime.fromisoformat(blocked_at.replace('Z', '+00:00'))
days_pending = (datetime.now(timezone.utc) - blocked_at).days
```

**Fix:** Add Neo4j DateTime conversion helper:
```python
def _parse_neo4j_datetime(self, value: Any) -> Optional[datetime]:
    """Convert Neo4j datetime or string to Python datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    if hasattr(value, 'to_native'):
        # neo4j.time.DateTime
        return value.to_native()
    if isinstance(value, datetime):
        return value
    return None
```

Then use it in both loaders:
```python
blocked_at = self._parse_neo4j_datetime(record.get("blocked_at"))
if blocked_at:
    days_pending = max(0, (datetime.now(timezone.utc) - blocked_at).days)
```

---

## Medium Priority Bugs

### Bug 13: Negative Days from Future-Dated Blocked Goals

**Fix:** Use `max(0, ...)`:
```python
days_pending = max(0, (datetime.now(timezone.utc) - blocked_at).days)
```

### Bug 14: NULL `last_interaction` Gets Threshold Days

**Fix:** Use a higher default:
```python
days_since = self._config.contact_neglect_days * 2  # or a fixed value like 30
```

### Bug 22: `acknowledge()` Doesn't Remove from In-Memory List

**Fix:**
```python
async def acknowledge(self, initiative_id: str) -> None:
    self._initiatives = [i for i in self._initiatives if i.id != initiative_id]
    # ... rest of method
```

### Bug 33: Generators Run Sequentially Instead of Parallel

**Fix:** Use `asyncio.gather()`:
```python
generators = []
if not types or InitiativeType.FOLLOW_UP in types:
    generators.append(self._generate_follow_ups())
    generators.append(self._generate_task_completion_follow_ups())
if not types or InitiativeType.RELATIONSHIP in types:
    generators.append(self._generate_relationship_suggestions())
if not types or InitiativeType.HEALTH in types:
    generators.append(self._generate_health_suggestions())
if not types or InitiativeType.SCHEDULING in types:
    generators.append(self._generate_scheduling_suggestions())

results = await asyncio.gather(*generators, return_exceptions=True)
for result in results:
    if isinstance(result, Exception):
        logger.warning("Generator failed: %s", result)
    else:
        initiatives.extend(result)
```

### Bug 36: Generated Initiatives Not Added to In-Memory List

**Fix:** Add to `self._initiatives` after generation:
```python
result = sorted(deduped, key=lambda i: i.priority, reverse=True)
self._initiatives.extend(result)  # Add to in-memory list
```

### Bug 43: No Limit on Total Initiatives Generated

**Fix:** Add `max_initiatives` parameter with default 20:
```python
async def generate(
    self,
    types: Optional[List[InitiativeType]] = None,
    min_priority: float = 0.5,
    cooldown_tasks: float = 12.0,
    cooldown_contacts: float = 72.0,
    max_initiatives: int = 20,
) -> List[Initiative]:
    # ... after sorting ...
    result = sorted(deduped, key=lambda i: i.priority, reverse=True)
    result = result[:max_initiatives]  # Limit total
```

### Bug 54: `get_active()` Falls Back to In-Memory on Empty Store Result

**Fix:** Only fall back on exception:
```python
if self._store:
    try:
        stored = self._store.list(status=["pending", "assigned", "acknowledged"], limit=100)
        if stored:  # Only use store result if not empty
            return self._convert_stored_to_initiatives(stored)
    except Exception as e:
        logger.warning("Failed to load from store, using in-memory: %s", e)
```

---

## Low Priority Bugs

### Bug 15: Import Shadowing in `get_active()`

**Fix:** Remove redundant import or use alias consistently.

### Bug 19: `dismiss()` Doesn't Check if Initiative Exists in Memory

**Fix:** This is harmless, skip or add check for consistency.

### Bug 26: Store Doesn't Validate Priority Range

**Fix:** Add validation in `InitiativeStore.create()`:
```python
priority = max(0.0, min(1.0, priority))
```

### Bug 40: Signal Loading Skipped When Scheduling Opportunities Manually Fed

**Fix:** Separate checks:
```python
if not self._context.get("scheduling_opportunities"):
    loaders.append(self._load_scheduling_opportunities())
# Always check signals unless explicitly disabled
if not self._context.get("pending_signals"):
    loaders.append(self._load_pending_signals())
```

### Bug 41: `SubsystemRegistry.anomalies` Creates New EventBus

**Fix:** Use shared event bus:
```python
from colony_sidecar.api.routers.host import _event_bus
# or pass event_bus to registry
```

### Bug 57/58: Parameter Validation

**Fix:** Add validation in `generate()`:
```python
min_priority = max(0.0, min(1.0, min_priority))
cooldown_tasks = max(0.0, cooldown_tasks)
cooldown_contacts = max(0.0, cooldown_contacts)
```

### Bug 59: Env Var Parsing Without Validation

**Fix:** Add try/except in `InitiativeConfig.from_env()`:
```python
@classmethod
def from_env(cls) -> "InitiativeConfig":
    def _int(env_var: str, default: int) -> int:
        try:
            return int(os.getenv(env_var, str(default)))
        except ValueError:
            logger.warning("Invalid %s, using default %d", env_var, default)
            return default
    
    def _float(env_var: str, default: float) -> float:
        try:
            return float(os.getenv(env_var, str(default)))
        except ValueError:
            logger.warning("Invalid %s, using default %.1f", env_var, default)
            return default
    
    return cls(
        contact_neglect_days=_int("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", 7),
        goal_block_threshold_days=_int("COLONY_INITIATIVE_GOAL_BLOCK_DAYS", 1),
        health_score_threshold=_float("COLONY_INITIATIVE_HEALTH_THRESHOLD", 70.0),
        calendar_gap_threshold_hours=_float("COLONY_INITIATIVE_GAP_THRESHOLD", 2.0),
        research_task_age_days=_int("COLONY_INITIATIVE_RESEARCH_AGE_DAYS", 1),
        signal_accumulation_threshold=_int("COLONY_INITIATIVE_SIGNAL_THRESHOLD", 10),
    )
```

---

## Test Updates Required

1. **Test Bug 11:** Verify `mark_initiative_generated` called for all initiatives
2. **Test Bug 12:** Verify research tasks use actual age, not threshold
3. **Test Bug 20:** Verify graph priority influences initiative priority
4. **Test Bug 37:** Verify `clear_context()` resets `_last_graph_load`
5. **Test Bug 38:** Verify `created_at` is timezone-aware
6. **Test Bug 44/45:** Verify health/scheduling initiatives have dedup_key
7. **Test Bug 47:** Verify `complete()` uses entity_id, not initiative_id
8. **Test Bug 50/51:** Verify Neo4j DateTime objects are handled
9. **Test Bug 33:** Verify generators run in parallel
10. **Test Bug 43:** Verify max_initiatives limits output

---

## Implementation Order

1. **Phase 1 (Critical):** Bugs 11, 12, 20, 37, 38, 44, 45, 47, 50, 51
2. **Phase 2 (Medium):** Bugs 13, 14, 22, 33, 36, 43, 54
3. **Phase 3 (Low):** Bugs 15, 19, 26, 40, 41, 57, 58, 59
4. **Phase 4 (Tests):** Update all tests
5. **Phase 5 (Validation):** Run full test suite, manual integration test

---

## Files to Modify

1. `sidecar/colony_sidecar/intelligence/components/initiative_engine.py`
2. `sidecar/colony_sidecar/autonomy/loop.py` (Bug 37 clear_context call)
3. `sidecar/colony_sidecar/autonomy/registry.py` (Bug 41)
4. `sidecar/colony_sidecar/initiatives/store.py` (Bug 26)
5. `tests/test_initiative_engine_generation.py` (new tests)

---

## Rollback Plan

If issues arise:
1. Revert to commit before fixes
2. Cherry-pick critical fixes only (Bugs 11, 37, 47)
3. Deploy with reduced functionality

---

## Acceptance Criteria

- [ ] All 60 bugs fixed or documented as intentional behavior
- [ ] All tests pass (including new tests)
- [ ] Manual integration test with simulated graph data succeeds
- [ ] No regressions in existing initiative delivery pipeline
- [ ] Performance impact of parallel generators measured and acceptable

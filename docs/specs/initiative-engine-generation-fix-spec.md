# Initiative Engine Generation Fix — Technical Specification

**Version:** 1.0  
**Date:** 2026-05-07  
**Author:** Aeva (OpenClaw Agent)  
**Status:** Draft — Pending Review  
**Target Branch:** `fix/initiative-engine-generation` → `main`  
**Target Release:** v0.7.14

---

## 1. Executive Summary

The Colony initiative system on `main` (v0.7.13) has **working delivery** but **broken generation**. The `InitiativeEngine` receives context via `add_context()` but no component actually queries the graph to populate that context. This spec defines a surgical fix: port graph query logic from the abandoned `fix/initiative-system` branch into the current sidecar architecture, keeping all existing delivery, deduplication, and plugin infrastructure intact.

**Scope:** Generation only. Delivery, dedup, persistence, and plugin integration are out of scope — they already work.

---

## 2. Problem Statement

### 2.1 Current Flow (Broken)

```
AutonomyLoop._tick()
    ↓
InitiativeEngine.generate()
    ↓
self._context.get("pending_tasks", [])        ← empty list
self._context.get("neglected_contacts", [])   ← empty list
self._context.get("health_alerts", [])        ← empty list
self._context.get("scheduling_opportunities", []) ← empty list
    ↓
No initiatives generated
    ↓
Nothing to deliver
```

### 2.2 Root Cause

The `InitiativeEngine` is a **passive consumer** of context. Something must call `add_context()` with graph data before `generate()` runs. Currently:

- `AutonomyLoop` does not populate context
- No background task feeds graph data into the engine
- The engine's `_generate_*` methods read from `self._context` but that dict is never populated with live graph queries

### 2.3 Evidence

```python
# sidecar/colony_sidecar/intelligence/components/initiative_engine.py

async def _generate_follow_ups(self) -> List[Initiative]:
    initiatives: List[Initiative] = []
    for item in self._context.get("pending_tasks", []):  # ← ALWAYS EMPTY
        ...
```

The `pending_tasks`, `neglected_contacts`, `health_alerts`, and `scheduling_opportunities` keys are never populated by any caller in the current codebase.

---

## 3. Proposed Solution

### 3.1 High-Level Flow (Fixed)

```
AutonomyLoop._tick()
    ↓
InitiativeEngine.generate()
    ↓
[NEW] _load_graph_context()                     ← queries Neo4j, mind model, etc.
        ├─ _load_blocked_goals()                  ← Neo4j query
        ├─ _load_neglected_contacts()             ← Neo4j query
        ├─ _load_health_trends()                  ← mind model query
        ├─ _load_scheduling_opportunities()       ← calendar state query
        ├─ _load_pending_signals()                ← signal count query
        └─ _load_pending_research_tasks()         ← Neo4j query
    ↓
self._context now populated with live data
    ↓
_generate_follow_ups()        ← reads pending_tasks
_generate_relationship_suggestions()  ← reads neglected_contacts
_generate_health_suggestions()        ← reads health_alerts
_generate_scheduling_suggestions()    ← reads scheduling_opportunities
    ↓
Initiatives generated with real data
    ↓
Existing delivery pipeline (already works)
```

### 3.2 Design Principles

1. **Surgical** — Only modify generation. Do not touch delivery, dedup, persistence, or plugin code.
2. **Defensive** — All graph queries wrapped in try/except. If Neo4j is down, generation degrades gracefully (returns empty list, logs debug).
3. **Lazy** — Graph queries only run during `generate()`, not on init or in background.
4. **Configurable** — Query thresholds (e.g., "neglected after N days") are configurable via env vars.
5. **Backward Compatible** — Existing `add_context()` API still works. External callers can still inject context manually.

---

## 4. Detailed Implementation

### 4.1 File: `sidecar/colony_sidecar/intelligence/components/initiative_engine.py`

#### 4.1.1 Add Graph Client Dependencies

```python
# Existing imports
from typing import Any, Dict, List, Optional
import logging

# NEW imports
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
```

#### 4.1.2 Add Configuration

```python
@dataclass
class InitiativeConfig:
    """Configuration for initiative generation."""
    
    # Contact neglect threshold (days)
    contact_neglect_days: int = 7
    
    # Goal block threshold (days before generating initiative)
    goal_block_threshold_days: int = 1
    
    # Health score threshold (below this generates alert)
    health_score_threshold: float = 70.0
    
    # Calendar gap threshold (hours — gaps larger than this are opportunities)
    calendar_gap_threshold_hours: float = 2.0
    
    # Research task age threshold (days — tasks older than this generate initiatives)
    research_task_age_days: int = 1
    
    # Signal accumulation threshold (count — above this generates initiative)
    signal_accumulation_threshold: int = 10
    
    @classmethod
    def from_env(cls) -> "InitiativeConfig":
        """Load configuration from environment variables."""
        return cls(
            contact_neglect_days=int(
                os.getenv("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", "7")
            ),
            goal_block_threshold_days=int(
                os.getenv("COLONY_INITIATIVE_GOAL_BLOCK_DAYS", "1")
            ),
            health_score_threshold=float(
                os.getenv("COLONY_INITIATIVE_HEALTH_THRESHOLD", "70.0")
            ),
            calendar_gap_threshold_hours=float(
                os.getenv("COLONY_INITIATIVE_GAP_THRESHOLD", "2.0")
            ),
            research_task_age_days=int(
                os.getenv("COLONY_INITIATIVE_RESEARCH_AGE_DAYS", "1")
            ),
            signal_accumulation_threshold=int(
                os.getenv("COLONY_INITIATIVE_SIGNAL_THRESHOLD", "10")
            ),
        )
```

#### 4.1.3 Update `InitiativeEngine.__init__`

```python
def __init__(
    self,
    graph_client: Optional[Any] = None,
    mind_model: Optional[Any] = None,
    store: Optional[Any] = None,
    goal_store: Optional[Any] = None,
    config: Optional[InitiativeConfig] = None,
) -> None:
    self.graph = graph_client
    self.mind_model = mind_model
    self._store = store
    self._goal_store = goal_store
    self._config = config or InitiativeConfig.from_env()
    self._context: Dict[str, List[Dict[str, Any]]] = {}
    self._initiatives: List[Initiative] = []
    
    # Track last graph load to avoid redundant queries within same tick
    self._last_graph_load: Optional[datetime] = None
```

#### 4.1.4 Add Graph Context Loading Methods

```python
async def _load_graph_context(self) -> None:
    """Load live data from graph and mind model into self._context.
    
    This is the core fix — it populates the context dict that
    _generate_* methods read from. All queries are defensive:
    if a subsystem is unavailable, that category is skipped.
    """
    # Avoid redundant loads within same tick
    if self._last_graph_load and (
        datetime.now(timezone.utc) - self._last_graph_load
    ) < timedelta(seconds=10):
        return
    
    self._last_graph_load = datetime.now(timezone.utc)
    
    # Load all data sources in parallel where possible
    await asyncio.gather(
        self._load_blocked_goals(),
        self._load_neglected_contacts(),
        self._load_health_trends(),
        self._load_scheduling_opportunities(),
        self._load_pending_signals(),
        self._load_pending_research_tasks(),
        return_exceptions=True,  # Don't let one failure kill others
    )

async def _load_blocked_goals(self) -> None:
    """Query graph for blocked goals."""
    if self.graph is None:
        return
    
    try:
        query = """
        MATCH (g:Goal {status: 'blocked'})
        WHERE g.blocked_at < datetime() - duration('P%dD')
        RETURN g.id as id, g.title as title, g.description as description,
               g.blocked_at as blocked_at, g.priority as priority
        ORDER BY g.priority DESC, g.blocked_at ASC
        """ % self._config.goal_block_threshold_days
        
        results = await self.graph.run_query(query)
        
        tasks = []
        for record in results:
            blocked_at = record.get("blocked_at")
            days_pending = 0
            if blocked_at:
                if isinstance(blocked_at, str):
                    blocked_at = datetime.fromisoformat(blocked_at.replace('Z', '+00:00'))
                days_pending = (datetime.now(timezone.utc) - blocked_at).days
            
            tasks.append({
                "entity_id": record["id"],
                "description": record.get("title", "Unknown goal"),
                "days_pending": days_pending,
                "priority": record.get("priority", 0.5),
            })
        
        self._context["pending_tasks"] = tasks
        logger.debug("Loaded %d blocked goals", len(tasks))
    except Exception as e:
        logger.debug("Blocked goals query failed: %s", e)
        self._context["pending_tasks"] = []

async def _load_neglected_contacts(self) -> None:
    """Query graph for contacts with no recent interaction."""
    if self.graph is None:
        return
    
    try:
        query = """
        MATCH (p:Person)
        WHERE p.last_interaction < datetime() - duration('P%dD')
          OR p.last_interaction IS NULL
        RETURN p.id as id, p.name as name, p.last_interaction as last_interaction
        ORDER BY p.last_interaction ASC
        """ % self._config.contact_neglect_days
        
        results = await self.graph.run_query(query)
        
        contacts = []
        for record in results:
            last_interaction = record.get("last_interaction")
            days_since = self._config.contact_neglect_days
            if last_interaction:
                if isinstance(last_interaction, str):
                    last_interaction = datetime.fromisoformat(
                        last_interaction.replace('Z', '+00:00')
                    )
                days_since = (datetime.now(timezone.utc) - last_interaction).days
            
            contacts.append({
                "entity_id": record["id"],
                "name": record.get("name", "Unknown"),
                "days_since_contact": days_since,
            })
        
        self._context["neglected_contacts"] = contacts
        logger.debug("Loaded %d neglected contacts", len(contacts))
    except Exception as e:
        logger.debug("Neglected contacts query failed: %s", e)
        self._context["neglected_contacts"] = []

async def _load_health_trends(self) -> None:
    """Query mind model for health anomalies."""
    if self.mind_model is None:
        return
    
    try:
        health_state = await self.mind_model.get_health_state()
        alerts = []
        
        # Check sleep score
        sleep_score = health_state.get("sleep_score")
        if sleep_score is not None and sleep_score < self._config.health_score_threshold:
            alerts.append({
                "metric": "sleep_score",
                "value": sleep_score,
                "target": self._config.health_score_threshold,
                "rationale": f"Sleep score ({sleep_score}) below threshold",
            })
        
        # Check recovery score
        recovery_score = health_state.get("recovery_score")
        if recovery_score is not None and recovery_score < self._config.health_score_threshold:
            alerts.append({
                "metric": "recovery_score",
                "value": recovery_score,
                "target": self._config.health_score_threshold,
                "rationale": f"Recovery score ({recovery_score}) below threshold",
            })
        
        # Check HRV trend
        hrv_trend = health_state.get("hrv_trend")
        if hrv_trend is not None and hrv_trend < -10:
            alerts.append({
                "metric": "hrv_trend",
                "value": hrv_trend,
                "target": 0,
                "rationale": f"HRV declining ({hrv_trend}%)",
            })
        
        self._context["health_alerts"] = alerts
        logger.debug("Loaded %d health alerts", len(alerts))
    except Exception as e:
        logger.debug("Health trends query failed: %s", e)
        self._context["health_alerts"] = []

async def _load_scheduling_opportunities(self) -> None:
    """Query mind model for calendar gaps and overdue commitments."""
    if self.mind_model is None:
        return
    
    try:
        schedule_state = await self.mind_model.get_schedule_state()
        opportunities = []
        
        # Check for calendar gaps > threshold hours
        gaps = schedule_state.get("gaps", [])
        for gap in gaps:
            duration = gap.get("duration_hours", 0)
            if duration > self._config.calendar_gap_threshold_hours:
                opportunities.append({
                    "description": f"Free block: {duration:.1f} hours ({gap['start']} to {gap['end']})",
                    "priority": 0.5,
                    "rationale": "Good time for deep work or catching up",
                    "action_hint": "schedule",
                })
        
        # Check for overdue commitments
        overdue = schedule_state.get("overdue_commitments", [])
        for commitment in overdue:
            opportunities.append({
                "description": f"Overdue: {commitment.get('title', 'Unknown')}",
                "priority": 0.85,
                "rationale": f"{commitment.get('days_overdue', 0)} days overdue",
                "action_hint": "notify_user",
            })
        
        self._context["scheduling_opportunities"] = opportunities
        logger.debug("Loaded %d scheduling opportunities", len(opportunities))
    except Exception as e:
        logger.debug("Scheduling opportunities query failed: %s", e)
        self._context["scheduling_opportunities"] = []

async def _load_pending_signals(self) -> None:
    """Get count of unprocessed signals."""
    if self.mind_model is None:
        return
    
    try:
        count = await self.mind_model.get_pending_signal_count()
        if count > self._config.signal_accumulation_threshold:
            # Add as a single "meta" opportunity
            self._context.setdefault("scheduling_opportunities", []).append({
                "description": f"{count} unprocessed signals awaiting review",
                "priority": min(0.9, 0.5 + count * 0.01),
                "rationale": "Accumulated behavioral signals need processing",
                "action_hint": "process_signals",
            })
        logger.debug("Pending signals: %d", count)
    except Exception as e:
        logger.debug("Pending signals query failed: %s", e)

async def _load_pending_research_tasks(self) -> None:
    """Query graph for pending research tasks."""
    if self.graph is None:
        return
    
    try:
        query = """
        MATCH (t:Task {type: 'research', status: 'pending'})
        WHERE t.created_at < datetime() - duration('P%dD')
        RETURN t.id as id, t.title as title, t.description as description,
               t.priority as priority, t.created_at as created_at
        ORDER BY t.priority DESC, t.created_at ASC
        """ % self._config.research_task_age_days
        
        results = await self.graph.run_query(query)
        
        # Add to pending_tasks context (research tasks are a type of pending task)
        existing_tasks = self._context.get("pending_tasks", [])
        for record in results:
            existing_tasks.append({
                "entity_id": record["id"],
                "description": f"Research: {record.get('title', 'Unknown')}",
                "days_pending": self._config.research_task_age_days,
                "priority": record.get("priority", 0.5),
            })
        
        self._context["pending_tasks"] = existing_tasks
        logger.debug("Loaded %d research tasks", len(results))
    except Exception as e:
        logger.debug("Research tasks query failed: %s", e)
```

#### 4.1.5 Update `generate()` Method

```python
async def generate(
    self,
    types: Optional[List[InitiativeType]] = None,
    min_priority: float = 0.5,
) -> List[Initiative]:
    """Generate proactive suggestions.
    
    [CHANGED] Now loads graph context before generating.
    """
    # NEW: Load live data from graph
    await self._load_graph_context()
    
    # Existing generation logic (unchanged)
    initiatives: List[Initiative] = []
    
    if not types or InitiativeType.FOLLOW_UP in types:
        follow_ups = await self._generate_follow_ups()
        initiatives.extend(follow_ups)
    
    if not types or InitiativeType.RELATIONSHIP in types:
        relationship = await self._generate_relationship_suggestions()
        initiatives.extend(relationship)
    
    if not types or InitiativeType.HEALTH in types:
        health = await self._generate_health_suggestions()
        initiatives.extend(health)
    
    if not types or InitiativeType.SCHEDULING in types:
        scheduling = await self._generate_scheduling_suggestions()
        initiatives.extend(scheduling)
    
    # Existing dedup logic (unchanged)
    deduped = []
    cooldown_tasks = timedelta(hours=12)
    cooldown_contacts = timedelta(hours=72)
    
    for init in initiatives:
        if init.priority < min_priority:
            continue
        
        if init.dedup_key and self._store:
            try:
                recent = self._store.find_recent_by_dedup_key(
                    init.dedup_key,
                    since=datetime.now(timezone.utc) - max(cooldown_tasks, cooldown_contacts),
                )
                if recent:
                    continue
            except Exception:
                pass
        
        deduped.append(init)
    
    result = sorted(deduped, key=lambda i: i.priority, reverse=True)
    
    # Existing persistence logic (unchanged)
    if self._store:
        for initiative in result:
            try:
                if not initiative.dedup_key and initiative.entity_id:
                    initiative.dedup_key = f"{initiative.type.value}:{initiative.entity_id}"
                
                self._store.create(
                    type=initiative.type.value,
                    description=initiative.description,
                    priority=initiative.priority,
                    rationale=initiative.rationale,
                    action_hint=initiative.action_hint,
                    entity_id=initiative.entity_id,
                    dedup_key=initiative.dedup_key,
                    expires_at=initiative.expires_at,
                    source_type="autonomy",
                    created_by="initiative_engine",
                )
            except Exception as e:
                logger.warning("Failed to persist initiative %s: %s", initiative.id, e)
    
    logger.debug(
        "Generated %d initiatives (%d above threshold %.2f)",
        len(initiatives),
        len(result),
        min_priority,
    )
    return result
```

#### 4.1.6 Add Lifecycle Methods

```python
async def complete(self, initiative_id: str, result: str = "") -> None:
    """Mark an initiative as completed.
    
    Args:
        initiative_id: ID of the initiative to complete
        result: Optional result/description of what was done
    """
    self._initiatives = [i for i in self._initiatives if i.id != initiative_id]
    
    if self._store:
        try:
            self._store.update_status(
                initiative_id,
                status="completed",
                metadata={"result": result, "completed_at": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as e:
            logger.warning("Failed to mark initiative %s complete: %s", initiative_id, e)
    
    if self._goal_store:
        try:
            self._goal_store.complete_task(initiative_id, result=result)
        except Exception as e:
            logger.debug("Failed to complete goal %s: %s", initiative_id, e)
    
    logger.info("Completed initiative %s: %s", initiative_id, result)

async def acknowledge(self, initiative_id: str) -> None:
    """Acknowledge an initiative (mark as seen but not acted on).
    
    Args:
        initiative_id: ID of the initiative to acknowledge
    """
    if self._store:
        try:
            self._store.update_status(
                initiative_id,
                status="acknowledged",
                metadata={"acknowledged_at": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as e:
            logger.warning("Failed to acknowledge initiative %s: %s", initiative_id, e)
    
    logger.debug("Acknowledged initiative %s", initiative_id)
```

### 4.2 File: `sidecar/colony_sidecar/intelligence/components/initiative_engine.py` — Structured Format

#### 4.2.1 Improve `formatInitiativeText()`

```python
def formatInitiativeText(init: Record<string, unknown>) -> str:
    """Format initiative as structured text for LLM consumption.
    
    [CHANGED] Now produces a parseable block with richer context.
    """
    lines = [
        "[COLONY_INITIATIVE]",
        f"ID: {init.get('id', 'unknown')}",
        f"Type: {init.get('type', 'unknown')}",
        f"Priority: {init.get('priority', 0)}",
    ]
    
    # Add title if available (new field)
    title = init.get('title')
    if title:
        lines.append(f"Title: {title}")
    
    lines.append(f"Description: {init.get('description', '')}")
    lines.append(f"Rationale: {init.get('rationale', '')}")
    
    # Add action hint
    action_hint = init.get('action_hint')
    if action_hint:
        lines.append(f"Suggested Action: {action_hint}")
    
    # Add entity info
    entity_id = init.get('entity_id')
    if entity_id:
        lines.append(f"Entity ID: {entity_id}")
        entity_type = init.get('entity_type')
        if entity_type:
            lines.append(f"Entity Type: {entity_type}")
    
    # Add context (new field)
    context = init.get('context')
    if context and isinstance(context, dict):
        lines.append("Context:")
        for key, value in context.items():
            lines.append(f"  {key}: {value}")
    
    # Add dedup key
    dedup_key = init.get('dedup_key')
    if dedup_key:
        lines.append(f"Dedup Key: {dedup_key}")
    
    return "\n".join(lines)
```

### 4.3 File: `.env.example` — Add Configuration

```bash
# =============================================================================
# INITIATIVE ENGINE CONFIGURATION
# =============================================================================
# Thresholds for generating proactive initiatives.
# These control how sensitive the initiative system is.

# Days of no contact before generating relationship initiative (default: 7)
# COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS=7

# Days a goal must be blocked before generating follow-up (default: 1)
# COLONY_INITIATIVE_GOAL_BLOCK_DAYS=1

# Health score threshold — below this generates alert (default: 70.0)
# COLONY_INITIATIVE_HEALTH_THRESHOLD=70.0

# Minimum calendar gap (hours) to count as opportunity (default: 2.0)
# COLONY_INITIATIVE_GAP_THRESHOLD=2.0

# Research task age (days) before generating initiative (default: 1)
# COLONY_INITIATIVE_RESEARCH_AGE_DAYS=1

# Signal accumulation threshold before generating initiative (default: 10)
# COLONY_INITIATIVE_SIGNAL_THRESHOLD=10
```

### 4.4 File: `colony_cli/setup.py` — Add Initiative Configuration

Add a `setup_initiative_engine()` function similar to what was built on the stale branch, but adapted for the current config system:

```python
def setup_initiative_engine(config: dict):
    """Configure initiative engine thresholds."""
    print_header("Initiative Engine")
    print_info("Colony can proactively suggest actions based on your graph state.")
    print_info("These settings control how sensitive the system is.")
    print()
    
    # Contact neglect
    days = prompt(
        "Days before suggesting contact follow-up",
        default=str(os.getenv("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", "7"))
    )
    if days:
        save_env_value("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", days)
    
    # Health threshold
    threshold = prompt(
        "Health score alert threshold (0-100)",
        default=str(os.getenv("COLONY_INITIATIVE_HEALTH_THRESHOLD", "70.0"))
    )
    if threshold:
        save_env_value("COLONY_INITIATIVE_HEALTH_THRESHOLD", threshold)
    
    # Calendar gap
    gap = prompt(
        "Minimum free hours to count as scheduling opportunity",
        default=str(os.getenv("COLONY_INITIATIVE_GAP_THRESHOLD", "2.0"))
    )
    if gap:
        save_env_value("COLONY_INITIATIVE_GAP_THRESHOLD", gap)
    
    print_success("Initiative engine configured!")
```

---

## 5. Testing Plan

### 5.1 Unit Tests

```python
# tests/test_initiative_engine_generation.py

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeEngine, InitiativeConfig, InitiativeType
)


class TestGraphContextLoading:
    """Test that graph queries populate context correctly."""
    
    @pytest.fixture
    def engine(self):
        mock_graph = AsyncMock()
        mock_mind = AsyncMock()
        return InitiativeEngine(
            graph_client=mock_graph,
            mind_model=mock_mind,
            config=InitiativeConfig(),
        )
    
    async def test_load_blocked_goals(self, engine):
        engine.graph.run_query.return_value = [
            {
                "id": "goal-1",
                "title": "Test Goal",
                "blocked_at": datetime.now(timezone.utc).isoformat(),
                "priority": 0.8,
            }
        ]
        
        await engine._load_blocked_goals()
        
        assert len(engine._context["pending_tasks"]) == 1
        assert engine._context["pending_tasks"][0]["entity_id"] == "goal-1"
    
    async def test_load_neglected_contacts(self, engine):
        engine.graph.run_query.return_value = [
            {
                "id": "person-1",
                "name": "Alice",
                "last_interaction": None,
            }
        ]
        
        await engine._load_neglected_contacts()
        
        assert len(engine._context["neglected_contacts"]) == 1
        assert engine._context["neglected_contacts"][0]["name"] == "Alice"
    
    async def test_load_health_trends(self, engine):
        engine.mind_model.get_health_state.return_value = {
            "sleep_score": 65,
            "recovery_score": 80,
        }
        
        await engine._load_health_trends()
        
        assert len(engine._context["health_alerts"]) == 1
        assert engine._context["health_alerts"][0]["metric"] == "sleep_score"
    
    async def test_graph_failure_graceful(self, engine):
        """If graph is down, generation should still work (empty results)."""
        engine.graph.run_query.side_effect = Exception("Neo4j down")
        
        await engine._load_blocked_goals()
        await engine._load_neglected_contacts()
        
        assert engine._context["pending_tasks"] == []
        assert engine._context["neglected_contacts"] == []
    
    async def test_generate_populates_context(self, engine):
        """generate() should call _load_graph_context() automatically."""
        engine.graph.run_query.return_value = []
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0
        
        await engine.generate()
        
        # Should have attempted to load all data sources
        engine.graph.run_query.assert_called()
        engine.mind_model.get_health_state.assert_called_once()
```

### 5.2 Integration Tests

```python
# tests/integration/test_initiative_end_to_end.py

import pytest
import asyncio

@pytest.mark.integration
async def test_initiative_generation_from_real_graph():
    """Requires running Neo4j and mind model."""
    from colony_sidecar.intelligence.components.initiative_engine import InitiativeEngine
    
    # Use real graph client (requires COLONY_NEO4J_URI env var)
    engine = InitiativeEngine.from_env()
    
    initiatives = await engine.generate(min_priority=0.3)
    
    # Should generate something if graph has data
    assert isinstance(initiatives, list)
    
    # Each initiative should have required fields
    for init in initiatives:
        assert init.id
        assert init.type
        assert init.description
        assert 0 <= init.priority <= 1
```

### 5.3 Manual Test Checklist

- [ ] Start Colony sidecar with `COLONY_AUTONOMY_MODE=proactive`
- [ ] Verify autonomy tick runs every 5 minutes
- [ ] Check logs for "Loaded N blocked goals" messages
- [ ] Verify initiatives are generated when graph has data
- [ ] Verify initiatives are delivered to OpenClaw
- [ ] Verify LLM receives structured `[COLONY_INITIATIVE]` blocks
- [ ] Test with Neo4j stopped — should log debug, not crash
- [ ] Test `colony setup initiative` wizard

---

## 6. Rollout Plan

### Phase 1: Implementation (This PR)
- [ ] Create branch `fix/initiative-engine-generation` from `main`
- [ ] Port graph query methods
- [ ] Add `InitiativeConfig`
- [ ] Update `generate()` to call `_load_graph_context()`
- [ ] Add `complete()` and `acknowledge()` lifecycle methods
- [ ] Improve `formatInitiativeText()`
- [ ] Add env vars to `.env.example`
- [ ] Add setup wizard section
- [ ] Write unit tests
- [ ] Manual testing

### Phase 2: Review & Merge
- [ ] PR review
- [ ] CI passes (tests, lint)
- [ ] Merge to `main`
- [ ] Tag `v0.7.14`

### Phase 3: Cleanup
- [ ] Delete stale branches:
  - `fix/initiative-system`
  - `implement-initiative-system-v2`
  - `feature/initiative-system-rewrite`
- [ ] Update documentation
- [ ] Announce in CHANGELOG

---

## 7. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Graph queries slow down tick | Medium | Medium | Add caching (10s TTL), run queries in parallel |
| Neo4j schema differs from queries | Medium | High | Defensive queries, test against real graph |
| Context keys conflict with external callers | Low | Low | Document key namespace, use `colony:*` prefix |
| Backward incompatibility | Low | High | Keep `add_context()` API unchanged |

---

## 8. Appendix: What Was Salvaged from Stale Branch

| From Stale Branch | Ported To Main | Notes |
|-------------------|----------------|-------|
| Graph query logic (`_get_blocked_goals`, etc.) | `_load_blocked_goals`, etc. | Adapted for sidecar architecture |
| `StructuredInitiative.format_for_agent()` | `formatInitiativeText()` | Simplified, no custom JSON schema |
| `InitiativeConfig` dataclass | `InitiativeConfig` | Added env var loading |
| Lifecycle methods (`complete`, `acknowledge`) | Same | Added store integration |
| Setup wizard | `setup_initiative_engine()` | Adapted for current CLI |
| `.env.example` entries | Same | Added to main's `.env.example` |
| **NOT ported** | | |
| `OpenClawHookClient` | N/A | Main uses SDK `enqueueSystemEvent` |
| `DeliveryFallbackChain` | N/A | Main has `ProactiveDeliveryBridge` |
| `AutonomyLoop` | N/A | Main has working loop |
| `EventBus` integration | N/A | Main uses MCP tools + WebSocket |

---

## 9. Decision Log

| Decision | Rationale |
|----------|-----------|
| Keep main's delivery architecture | Already works, has proper SDK integration |
| Port only generation logic | This is the actual missing piece |
| Use `self._context` instead of new data structure | Backward compatible, existing `_generate_*` methods work |
| Add 10s cache for graph loads | Prevents redundant queries within same tick |
| Run queries in parallel with `gather()` | Faster tick execution |
| Keep `add_context()` API | External callers can still inject context manually |
| Use env vars for thresholds | Consistent with rest of codebase |

---

**Next Step:** Review this spec, then create branch and implement.

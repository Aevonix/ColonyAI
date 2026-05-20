---
title: Self-Initiative Placeholder Elimination (v0.11.1)
version: 2.0.0
status: draft
created: 2026-05-17
revised: 2026-05-17
---

# Self-Initiative Placeholder Elimination Spec

## Problem Statement

v0.11.0 shipped three empty initiative generators:

- `_generate_capability_gap_initiatives()` — returns `[]`
- `_generate_knowledge_acquisition_initiatives()` — returns `[]`
- `_generate_behavioral_correction_initiatives()` — returns `[]`

Additionally, four bugs were discovered in v0.11.0 shipped code:

1. **`execute_initiative()` skill name mapping is broken** — `initiative.type.value.replace("_", "_")` is a no-op; `"operational"` never maps to `"operational_hygiene"`
2. **`execute_initiative()` drops `entity_type`** — `InitiativeExecutionContext` is built without `entity_type`, so skills branch to `NO_ACTION`
3. **Autonomy loop bypasses execution entirely** — `_phase_execute()` goes straight to `delivery.push_initiative()`; `execute_initiative()` is never called
4. **Two skills call `.publish()` which does not exist** — `DataQualitySkill` and `OperationalHygieneSkill` call `self.events.publish("...")`; EventBus API is `.emit()` / `.emit_async()`

The three existing executor skills (`subsystem_health`, `data_quality`, `operational_hygiene`) are fully implemented but unreachable. Self-initiatives are pushed to Hermes as notifications but never auto-executed.

This spec defines:
1. Direct graph writes (no event bus persistence, no journal projections)
2. Real context loaders and generators
3. Three new executor skills
4. Auto-execution wiring in the autonomy loop
5. Bug fixes in existing code

**No placeholders, TODOs, stubs, or fake data remain after this work.**

---

## 1. Guiding Principles

1. **Graph as ledger** — All state lives in Neo4j. No in-memory caches or event bus projections.
2. **Write at detection** — When a tool fails or a correction is given, write directly to graph immediately. Don't emit events and hope something projects them later.
3. **Auto-execute before notify** — Self-initiatives with matching skills are executed in the sidecar first. Only proposals, escalations, and failures are pushed to Hermes.
4. **Graceful degradation** — If telemetry, graph, or research pipeline is unavailable, the system silently skips that category. It never crashes because a subsystem is down.
5. **Owner privacy** — Behavioral corrections are private to the owner. Never shared, logged externally, or used to train shared models.

---

## 2. Explicitly NOT Building

The following were in v1 of this spec and are **cut**:

- **Event bus persistence for state** — The event bus is in-memory broadcast only. We write directly to graph.
- **Event journal projections** — The journal is write-only audit. Nothing reads it.
- **Repo search indexing** — No repo indexer exists. We won't build one.
- **Obsidian / arXiv integration** — No Obsidian reader or arXiv client exists.
- **NLP correction detection** — v0.11.2 feature. v0.11.1 uses the explicit `/learning/correction` endpoint only.
- **TelemetryStore counter extensions** — Not needed; state lives in graph.
- **Project context loader** — No concept extractor exists. v0.11.1 uses a simplified knowledge gap hook.

---

## 3. Bug Fixes in Existing Code

### 3.1 `execute_initiative()` skill name mapping

**File:** `colony_sidecar/intelligence/components/initiative_engine.py`

**Current (broken):**
```python
category = {"executor_skill": initiative.type.value.replace("_", "_")}
```

For `InitiativeType.OPERATIONAL` (value `"operational"`), this produces `{"executor_skill": "operational"}`. But the skill is named `"operational_hygiene"`. They won't match.

**Fix:**
```python
_SKILL_NAME_MAP = {
    "subsystem_health": "subsystem_health",
    "data_quality": "data_quality",
    "operational": "operational_hygiene",
    "capability_gap": "capability_gap",
    "knowledge_acquisition": "knowledge_acquisition",
    "behavioral_correction": "behavioral_correction",
}

skill_name = _SKILL_NAME_MAP.get(initiative.type.value, initiative.type.value)
category = {"executor_skill": skill_name}
```

### 3.2 `execute_initiative()` missing `entity_type`

**Pre-requisite:** Add `trigger_data: Optional[Dict[str, Any]] = None` to the `Initiative` dataclass in `colony_sidecar/intelligence/components/initiative_engine.py`. Without this field, generators cannot pass full context items through to execution.

**Current:** `InitiativeExecutionContext` is built without `entity_type`.

**Fix:** Pass `entity_type` from the initiative's context data. The context loaders already produce items with `entity_type` (e.g., `"backup"`, `"schema_drift"`, `"orphan_nodes"`).

```python
exec_context = InitiativeExecutionContext(
    initiative_id=initiative.id,
    category_id=initiative.type.value,
    category_name=initiative.type.value,
    entity_id=initiative.entity_id,
    entity_type=(initiative.trigger_data or {}).get("entity_type"),
    trigger_data=initiative.trigger_data or {},
    priority=initiative.priority,
)
```

Also update `InitiativeEngine.generate()` to include the full context item in `trigger_data` when creating initiatives. **This applies to ALL generators** (existing `subsystem_health`, `data_quality`, `operational` as well as the new ones in §7):

```python
# In each generator, pass full context through trigger_data
initiatives.append(
    Initiative(
        ...
        trigger_data={**item, "description": description, "rationale": rationale},
    )
)
```

### 3.3 Autonomy loop bypasses execution

**Current:** `AutonomyLoop._phase_execute()` (line 502) builds the initiative payload and immediately pushes it to delivery. It never calls `engine.execute_initiative()`.

**Fix:** Before pushing to delivery, try auto-execution for all self-initiative types. See §9.2 for the full wiring.

### 3.4 Existing skills call `.publish()` which does not exist

**Files:**
- `colony_sidecar/skills/executors/data_quality.py` line 158
- `colony_sidecar/skills/executors/operational_hygiene.py` line 180

**Current:**
```python
await self.events.publish("index_rebuild_requested", {...})
await self.events.publish("model_refresh_requested", {...})
```

**Fix:** Replace with `emit()`:
```python
await self.events.emit(Event(type="index_rebuild_requested", payload={...}))
await self.events.emit(Event(type="model_refresh_requested", payload={...}))
```

Or remove entirely — the event bus is in-memory broadcast only and nothing subscribes to these events. The skills should return `ExecutionResult.PROPOSAL_CREATED` and let the autonomy loop push the proposal to Hermes.

---

## 4. Graph Schema Extensions

All changes are additive. Existing nodes of these types are unlikely to exist (Pattern is used by SQLite PatternStore, not graph; Capability has no existing nodes; Concept is new).

### 4.1 `Concept` (new node type)

**File:** `colony_sidecar/intelligence/graph/schema.py`

```python
class Concept(BaseModel):
    """A knowledge domain or concept Colony has encountered."""

    id: str = Field(..., description="Unique concept identifier")
    name: str
    domain: str = "general"  # e.g., "technology", "science", "person"
    description: Optional[str] = None
    confidence_score: float = 0.0  # 0 = unknown, 1 = expert
    encounter_count: int = 0
    last_researched_at: Optional[datetime] = None
    last_encountered_at: Optional[datetime] = None
    source: Optional[str] = None  # "web_search", "tool_failure", "owner_query"
    status: str = "open"  # open | researching | learned | archived
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

Add `Concept` to `NODE_TYPES` tuple.

### 4.2 `Capability` (extend existing)

**Current fields:** `id`, `name`, `description`, `available`, `created_at`

**Add:**
```python
status: str = "available"  # available | deprecated | missing | planned
failure_count: int = 0
last_failure_at: Optional[datetime] = None
```

### 4.3 `Pattern` (extend existing graph model)

**Current fields:** `id`, `name`, `description`, `confidence`, `occurrences`, `created_at`

**Add:**
```python
pattern_type: str = "behavioral"  # behavioral | workflow | preference | correction
trigger: Optional[str] = None  # What triggers this pattern
action: Optional[str] = None  # What Colony should do when triggered
recurrence_count: int = 0
last_triggered_at: Optional[datetime] = None
is_active: bool = True
```

Note: The SQLite `PatternStore` uses a different table schema. These graph `Pattern` nodes are used exclusively for behavioral corrections and are distinct from the SQLite pattern store.

### 4.4 `Preference` (new node type)

**File:** `colony_sidecar/intelligence/graph/schema.py`

```python
class Preference(BaseModel):
    """A learned preference or behavioral rule."""

    id: str = Field(..., description="Unique preference identifier")
    trigger: str
    expected: str
    source: str = "behavioral_correction"  # behavioral_correction, owner_config, inferred
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
```

Add `Preference` to `NODE_TYPES` tuple.

### 4.5 New edge types

Already exist in `EdgeType` enum:
- `HAS_CAPABILITY`
- `NEEDS_CAPABILITY`
- `EXHIBITS`
- `TRIGGERS`
- `REFERENCES`

No changes needed.

---

## 5. Detection Hooks (Direct Graph Writes)

### 5.1 Tool Failure Detection

**Hook location:** `colony_sidecar/reasoning/executor.py`, inside `ToolExecutor.execute_batch()`.

**Architectural constraint:** `reasoning/` must not import from `api/routers/` — that creates a circular dependency. Instead, inject the graph client into `ToolExecutor` at construction time.

**Step 1: Add `graph_client` to `ToolExecutor.__init__`**

```python
def __init__(
    self,
    handlers: dict[str, ToolHandler] | None = None,
    registry: SubsystemRegistry | None = None,
    graph_client = None,
) -> None:
    self._handlers: dict[str, ToolHandler] = handlers or {}
    self._registry = registry
    self._graph = graph_client
```

**Step 2: Wire graph client in `server.py` and `host.py`**

In `server.py`, `ToolExecutor` is created in Step 2 before `graph` is initialized in Step 3. Inject graph into the existing executor after Step 3:

```python
# server.py — after graph is initialized (Step 3)
from colony_sidecar.api.routers import host as _host_router
te = _host_router._tool_executor
if te is not None:
    te._graph = graph
```

In `host.py configure_host`, the new `ToolExecutor` is created inline. Pass `_graph` directly:

```python
# host.py configure_host
_reasoning_loop = ReasoningLoop(
    model=new_router,
    tools=ToolExecutor(graph_client=_graph),
)
```

**Step 3: Hook into `execute_batch()`**

Two failure modes:
1. **Missing handler** — `handler is None` (line 144)
2. **Handler exception** — `except Exception` (line 166)

**For missing handler:**
```python
# After: results.append({"error": True, "message": f"Tool '{name}' is not available..."})
await self._record_capability_gap(name, "missing", session_id)
```

**For handler exception:**
```python
# After: results.append({"error": f"Tool '{name}' execution failed: {exc}"})
await self._record_capability_gap(name, "broken", session_id)
```

**Implementation:**
```python
async def _record_capability_gap(
    self, tool_name: str, failure_mode: str, session_id: str
) -> None:
    """Write tool failure directly to graph."""
    if self._graph is None or not hasattr(self._graph, "driver"):
        return
    try:
        async with self._graph.driver.session(database=self._graph.database) as session:
            await session.run("""
                MERGE (c:Capability {name: $tool_name})
                SET c.status = CASE WHEN $failure_mode = 'missing' THEN 'missing' ELSE 'available' END,
                    c.last_failure_at = datetime()
                WITH c
                MATCH (a:Agent {id: 'colony-sidecar'})
                MERGE (a)-[r:NEEDS_CAPABILITY]->(c)
                SET r.failure_count = coalesce(r.failure_count, 0) + 1,
                    r.last_failure_at = datetime(),
                    r.failure_mode = $failure_mode
            """, tool_name=tool_name, failure_mode=failure_mode)
    except Exception as e:
        logger.debug("Failed to record capability gap: %s", e)
```

### 5.2 Behavioral Correction Detection

**Hook location:** `colony_sidecar/api/routers/host.py`, inside `submit_correction()`.

**Current code:**
```python
await _learner.ingest_correction({
    "original": body.original,
    "correction": body.correction,
    "component": body.component,
    "sender_id": body.context.contact_id if body.context else "unknown",
})
```

**After the `_learner.ingest_correction()` call, add:**
```python
# Write correction directly to graph as Pattern node
await _record_correction_pattern(body)
```

**Implementation:**
```python
async def _record_correction_pattern(body: LearningCorrectionRequest) -> None:
    if _graph is None or not hasattr(_graph, "driver"):
        return
    person_id = body.context.contact_id if body.context else "unknown"
    trigger = body.original[:200]  # Truncate for graph storage
    expected = body.correction[:200]
    try:
        async with _graph.driver.session(database=_graph.database) as session:
            await session.run("""
                MERGE (p:Pattern {pattern_type: 'correction', trigger: $trigger})
                SET p.id = coalesce(p.id, $new_id),
                    p.action = $expected,
                    p.name = $expected,
                    p.description = $description,
                    p.recurrence_count = coalesce(p.recurrence_count, 0) + 1,
                    p.last_triggered_at = datetime(),
                    p.is_active = true,
                    p.confidence = 0.9
                WITH p
                MATCH (owner:Person {id: $person_id})
                MERGE (owner)-[r:EXHIBITS]->(p)
                SET r.last_seen = datetime()
            """, trigger=trigger, expected=expected, new_id=str(uuid.uuid4()),
                 description=f"Correction: {trigger} -> {expected}",
                 person_id=person_id)
    except Exception as e:
        logger.debug("Failed to record correction pattern: %s", e)
```

### 5.3 Knowledge Gap Detection

**Hook location:** `colony_sidecar/reasoning/native_tools/web_search.py`, inside `WebSearchTool.execute()`.

**Architectural constraint:** Same as §5.1 — `reasoning/` must not import from `api/routers/`. Inject graph via `ToolExecutor` which already receives it.

**Step 1: Pass graph through `ToolExecutor` to native tools**

In `ToolExecutor.register_native_tools()`, pass `self._graph` to tools that need it:

```python
def register_native_tools(self, search_orchestrator=None, sandbox_dir: str = "") -> None:
    # ... existing calculate and file_ops registration ...

    if search_orchestrator and search_orchestrator.has_providers:
        try:
            from colony_sidecar.reasoning.native_tools.web_search import WebSearchTool
            ws_tool = WebSearchTool(search_orchestrator, graph_client=self._graph)
            self.register("web_search", ws_tool.execute)
        except Exception as exc:
            logger.warning("register web_search tool failed: %s", exc)
```

**Step 2: Accept `graph_client` in `WebSearchTool.__init__`**

```python
class WebSearchTool:
    def __init__(self, search_orchestrator, graph_client=None):
        self._search = search_orchestrator
        self._graph = graph_client
```

**Step 3: Hook into `execute()`**

When `count == 0` or `error` is present:
```python
# After returning {"results": [], "count": 0} or {"error": True, ...}
await self._record_knowledge_gap(query)
```

**Implementation:**
```python
async def _record_knowledge_gap(self, query: str) -> None:
    if self._graph is None or not hasattr(self._graph, "driver"):
        return
    try:
        async with self._graph.driver.session(database=self._graph.database) as session:
            await session.run("""
                MERGE (c:Concept {name: $query})
                SET c.confidence_score = 0.1,
                    c.encounter_count = coalesce(c.encounter_count, 0) + 1,
                    c.last_encountered_at = datetime(),
                    c.status = 'open',
                    c.source = 'web_search'
            """, query=query)
    except Exception as e:
        logger.debug("Failed to record knowledge gap: %s", e)
```

---

## 6. Initiative Context Loaders

Add three new loaders to `InitiativeEngine`. They run during `_load_graph_context()` alongside existing loaders.

### 6.1 `_load_capability_gaps()`

```python
async def _load_capability_gaps(self) -> None:
    """Query graph for tools that have failed repeatedly."""
    if self.graph is None or not hasattr(self.graph, 'driver'):
        return

    query = """
        MATCH (a:Agent)-[r:NEEDS_CAPABILITY]->(c:Capability)
        WHERE r.failure_count >= 3
          AND r.last_failure_at > datetime() - duration({hours: 24})
        RETURN c.name as name, c.id as id, r.failure_count as failure_count,
               r.last_failure_at as last_failure, r.failure_mode as failure_mode
        ORDER BY r.failure_count DESC
        LIMIT 10
    """

    gaps = []
    try:
        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(query)
            async for record in result:
                record = dict(record)
                gaps.append({
                    "entity_id": record.get("id") or record.get("name"),
                    "entity_type": "capability_gap",
                    "name": record.get("name", "Unknown"),
                    "failure_count": record.get("failure_count", 0),
                    "failure_mode": record.get("failure_mode", "unknown"),
                    "last_failure": record.get("last_failure"),
                })
        self._context["capability_gaps"] = gaps
    except Exception as e:
        logger.debug("Capability gap query failed: %s", e)
    if not gaps:
        self._context.setdefault("capability_gaps", [])
```

### 6.2 `_load_behavioral_patterns()`

```python
async def _load_behavioral_patterns(self) -> None:
    """Query graph for recurring correction patterns."""
    if self.graph is None or not hasattr(self.graph, 'driver'):
        return

    query = """
        MATCH (p:Pattern {pattern_type: 'correction'})
        WHERE p.recurrence_count >= 3
          AND p.is_active = true
          AND p.last_triggered_at > datetime() - duration({days: 30})
        RETURN p.id as id, p.trigger as trigger, p.action as action,
               p.recurrence_count as recurrence_count, p.confidence as confidence
        ORDER BY p.recurrence_count DESC
        LIMIT 10
    """

    patterns = []
    try:
        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(query)
            async for record in result:
                record = dict(record)
                patterns.append({
                    "entity_id": record.get("id"),
                    "entity_type": "behavioral_pattern",
                    "trigger": record.get("trigger", ""),
                    "expected_action": record.get("action", ""),
                    "recurrence_count": record.get("recurrence_count", 0),
                    "confidence": record.get("confidence", 0.5),
                })
        self._context["behavioral_patterns"] = patterns
    except Exception as e:
        logger.debug("Behavioral pattern query failed: %s", e)
    if not patterns:
        self._context.setdefault("behavioral_patterns", [])
```

### 6.3 `_load_knowledge_gaps()`

```python
async def _load_knowledge_gaps(self) -> None:
    """Query graph for low-confidence concepts."""
    if self.graph is None or not hasattr(self.graph, 'driver'):
        return

    query = """
        MATCH (c:Concept)
        WHERE c.confidence_score < 0.3
          AND c.status = 'open'
          AND (c.last_researched_at IS NULL
               OR c.last_researched_at < datetime() - duration({days: 7}))
        RETURN c.id as id, c.name as name, c.confidence_score as confidence,
               c.encounter_count as encounter_count, c.source as source
        ORDER BY c.encounter_count DESC
        LIMIT 10
    """

    gaps = []
    try:
        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(query)
            async for record in result:
                record = dict(record)
                gaps.append({
                    "entity_id": record.get("id"),
                    "entity_type": "knowledge_gap",
                    "name": record.get("name", "Unknown"),
                    "confidence": record.get("confidence", 0.0),
                    "encounter_count": record.get("encounter_count", 0),
                    "source": record.get("source", "unknown"),
                })
        self._context["knowledge_gaps"] = gaps
    except Exception as e:
        logger.debug("Knowledge gap query failed: %s", e)
    if not gaps:
        self._context.setdefault("knowledge_gaps", [])
```

### 6.4 Register loaders in `_load_graph_context()`

Add to the loaders list in `InitiativeEngine._load_graph_context()`:

```python
if "capability_gaps" not in self._context:
    loaders.append(self._load_capability_gaps())
if "behavioral_patterns" not in self._context:
    loaders.append(self._load_behavioral_patterns())
if "knowledge_gaps" not in self._context:
    loaders.append(self._load_knowledge_gaps())
```

---

## 7. Initiative Generators

Replace the three placeholder generators.

### 7.1 `_generate_capability_gap_initiatives()`

```python
async def _generate_capability_gap_initiatives(self) -> List[Initiative]:
    """Generate self-initiatives for missing or broken capabilities."""
    initiatives: List[Initiative] = []
    for gap in self._context.get("capability_gaps", []):
        entity_id = gap.get("entity_id", "unknown")
        name = gap.get("name", "Unknown")
        failure_count = gap.get("failure_count", 0)
        failure_mode = gap.get("failure_mode", "unknown")

        priority = min(1.0, 0.5 + failure_count / 10)

        initiatives.append(
            Initiative(
                id=f"capgap-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                type=InitiativeType.CAPABILITY_GAP,
                description=f"Capability gap: {name} ({failure_count} failures, mode={failure_mode})",
                priority=priority,
                rationale=f"{failure_count} failed invocations in 24h. Last mode: {failure_mode}",
                action_hint="Research and implement capability",
                entity_id=entity_id,
                dedup_key=f"capgap:{entity_id}",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                trigger_data=gap,
            )
        )
    return initiatives
```

### 7.2 `_generate_behavioral_correction_initiatives()`

```python
async def _generate_behavioral_correction_initiatives(self) -> List[Initiative]:
    """Generate self-initiatives for recurring correction patterns."""
    initiatives: List[Initiative] = []
    for pattern in self._context.get("behavioral_patterns", []):
        entity_id = pattern.get("entity_id", "unknown")
        trigger = pattern.get("trigger", "")
        expected = pattern.get("expected_action", "")
        recurrence_count = pattern.get("recurrence_count", 0)

        priority = min(1.0, 0.5 + recurrence_count / 10)

        initiatives.append(
            Initiative(
                id=f"behavior-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                type=InitiativeType.BEHAVIORAL_CORRECTION,
                description=f"Behavioral correction: {trigger[:60]}...",
                priority=priority,
                rationale=f"Owner corrected this {recurrence_count} times. Expected: {expected[:60]}",
                action_hint="Update behavior config or create preference rule",
                entity_id=entity_id,
                dedup_key=f"behavior:{entity_id}",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                trigger_data=pattern,
            )
        )
    return initiatives
```

### 7.3 `_generate_knowledge_acquisition_initiatives()`

```python
async def _generate_knowledge_acquisition_initiatives(self) -> List[Initiative]:
    """Generate self-initiatives for low-confidence knowledge areas."""
    initiatives: List[Initiative] = []
    for gap in self._context.get("knowledge_gaps", []):
        entity_id = gap.get("entity_id", "unknown")
        name = gap.get("name", "Unknown")
        confidence = gap.get("confidence", 0.0)
        encounter_count = gap.get("encounter_count", 0)

        priority = min(1.0, 0.4 + (1.0 - confidence))

        initiatives.append(
            Initiative(
                id=f"knowledge-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                type=InitiativeType.KNOWLEDGE_ACQUISITION,
                description=f"Research concept: {name} (confidence: {confidence:.0%})",
                priority=priority,
                rationale=f"Encountered {encounter_count} times with low confidence ({confidence:.0%})",
                action_hint="Queue background research and update world model",
                entity_id=entity_id,
                dedup_key=f"knowledge:{entity_id}",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                trigger_data=gap,
            )
        )
    return initiatives
```

---

## 8. Executor Skills

### 8.1 `CapabilityGapSkill`

**File:** `colony_sidecar/skills/executors/capability_gap.py`

```python
"""Capability gap executor skill.

Checks if a tool exists in the registry and whether it is broken or missing.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class CapabilityGapSkill(InitiativeExecutorSkill):
    """Skill for diagnosing and proposing fixes for capability gaps."""

    skill_name = "capability_gap"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        failure_mode = initiative.trigger_data.get("failure_mode", "unknown")
        self._log("info", "Diagnosing capability gap: %s (mode=%s)", entity_id, failure_mode)

        if self.graph:
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        "MATCH (c:Capability {name: $name}) RETURN c.status as status",
                        name=entity_id,
                    )
                    record = await result.single()
                    if record:
                        status = record.get("status")
                        if status == "missing":
                            self._log("info", "Capability %s is missing — proposing research", entity_id)
                            return ExecutionResult.PROPOSAL_CREATED
                        elif status == "available":
                            self._log("info", "Capability %s is registered but broken — proposing fix", entity_id)
                            return ExecutionResult.PROPOSAL_CREATED
            except Exception as e:
                self._log("warning", "Graph query failed: %s", e)

        # Default: we don't know enough to auto-fix
        return ExecutionResult.PROPOSAL_CREATED
```

**Note:** The skill does not attempt to register new tools or restart broken ones without owner approval. Both outcomes produce a `PROPOSAL_CREATED` with context for the owner to decide.

### 8.2 `BehavioralCorrectionSkill`

**File:** `colony_sidecar/skills/executors/behavioral_correction.py`

```python
"""Behavioral correction executor skill.

Encodes recurring corrections into config or preference memory.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class BehavioralCorrectionSkill(InitiativeExecutorSkill):
    """Skill for encoding behavioral corrections."""

    skill_name = "behavioral_correction"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        trigger = initiative.trigger_data.get("trigger", "")
        expected = initiative.trigger_data.get("expected_action", "")
        recurrence_count = initiative.trigger_data.get("recurrence_count", 0)

        self._log("info", "Encoding correction: %s -> %s (count=%d)", trigger, expected, recurrence_count)

        if not trigger or not expected:
            return ExecutionResult.NO_ACTION

        # Classification heuristic
        is_config = any(kw in expected.lower() for kw in ["always", "never", "use", "format", "prefer"])
        is_preference = any(kw in expected.lower() for kw in ["don't like", "prefer", "instead of"])

        if is_config:
            return await self._update_config(trigger, expected)
        elif is_preference:
            return await self._store_preference(trigger, expected)
        else:
            # Ambiguous — propose to owner
            return ExecutionResult.PROPOSAL_CREATED

    async def _update_config(self, trigger: str, expected: str) -> ExecutionResult:
        """Write a config preference to ~/.colony/.env"""
        env_path = Path(os.path.expanduser("~/.colony/.env"))
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            key = f"COLONY_PREF_{self._sanitize_key(trigger)}"
            new_line = f'{key}="{expected}"'
            # Replace existing or append
            updated = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = new_line
                    updated = True
                    break
            if not updated:
                lines.append(new_line)
            env_path.write_text("\n".join(lines) + "\n")
            self._log("info", "Updated config: %s", key)
            return ExecutionResult.AUTO_FIXED
        except Exception as e:
            self._log("error", "Config write failed: %s", e)
            return ExecutionResult.FAILED

    async def _store_preference(self, trigger: str, expected: str) -> ExecutionResult:
        """Store as a Preference node in the graph."""
        if not self.graph:
            return ExecutionResult.FAILED
        try:
            import uuid
            async with self.graph.driver.session(database=self.graph.database) as session:
                await session.run("""
                    MERGE (p:Preference {trigger: $trigger})
                    SET p.id = coalesce(p.id, $new_id),
                        p.expected = $expected,
                        p.updated_at = datetime(),
                        p.source = 'behavioral_correction'
                """, trigger=trigger, expected=expected, new_id=str(uuid.uuid4()))
            self._log("info", "Stored preference in graph")
            return ExecutionResult.AUTO_FIXED
        except Exception as e:
            self._log("error", "Preference store failed: %s", e)
            return ExecutionResult.FAILED

    @staticmethod
    def _sanitize_key(text: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in text.lower())[:40]
```

### 8.3 `KnowledgeAcquisitionSkill`

**File:** `colony_sidecar/skills/executors/knowledge_acquisition.py`

```python
"""Knowledge acquisition executor skill.

Marks low-confidence concepts for research and proposes the work to the owner.
v0.11.1 is proposal-only; actual pipeline research is v0.11.2.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class KnowledgeAcquisitionSkill(InitiativeExecutorSkill):
    """Skill for flagging knowledge gaps for research."""

    skill_name = "knowledge_acquisition"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        concept_name = initiative.entity_id or "unknown"
        source = initiative.trigger_data.get("source", "unknown")

        self._log("info", "Flagging knowledge gap: %s (source=%s)", concept_name, source)

        # Update Concept status to 'researching' if it exists in graph
        if self.graph:
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    await session.run("""
                        MATCH (c:Concept {id: $id})
                        SET c.status = 'researching',
                            c.last_researched_at = datetime()
                    """, id=concept_name)
            except Exception as e:
                self._log("warning", "Failed to update concept status: %s", e)

        # v0.11.1: Return proposal — owner decides whether to run research
        # v0.11.2: Will invoke ResearchPipeline here for auto-research
        return ExecutionResult.PROPOSAL_CREATED
```

**Note:** v0.11.1 does not auto-run the 6-stage `ResearchPipeline` from a skill because the pipeline is heavy (web search + synthesis + review) and should not block initiative execution. The proposal includes context so The owner can trigger research manually or approve auto-research in v0.11.2.

### 8.4 Register new skills

**File:** `colony_sidecar/skills/registry.py`

Add to `_load_builtin_skills()`:

```python
builtin_skills = [
    "colony_sidecar.skills.executors.subsystem_health",
    "colony_sidecar.skills.executors.data_quality",
    "colony_sidecar.skills.executors.operational_hygiene",
    "colony_sidecar.skills.executors.capability_gap",
    "colony_sidecar.skills.executors.behavioral_correction",
    "colony_sidecar.skills.executors.knowledge_acquisition",
]
```

---

## 9. Auto-Execution Wiring

### 9.1 `InitiativeEngine.execute_initiative()` fixes

Apply §3.1 and §3.2.

### 9.2 `AutonomyLoop._phase_execute()` wiring

**File:** `colony_sidecar/autonomy/loop.py`

**Pre-requisite:** Extend `_build_initiative_context()` to handle self-initiative types. Currently it only handles `follow_up`, `relationship`, `scheduling`, and `health`. Add branches for:
- `capability_gap` → return `{"capability_gap": raw_ctx.get("capability_gaps", [])}`  
- `behavioral_correction` → return `{"behavioral_pattern": raw_ctx.get("behavioral_patterns", [])}`
- `knowledge_acquisition` → return `{"knowledge_gap": raw_ctx.get("knowledge_gaps", [])}`

Replace the current `_phase_execute()` with this flow:

```python
async def _phase_execute(self) -> None:
    """Execute self-initiatives in the sidecar, then push remaining to delivery."""
    engine = self._registry.initiative_engine
    delivery = self._registry.delivery

    for initiative in list(self._pending_initiatives):
        if self.stats.actions_this_hour >= self.config.max_actions_per_hour:
            logger.warning("Hourly action limit reached")
            break

        type_value = (
            initiative.type.value
            if hasattr(initiative.type, "value")
            else str(initiative.type)
        )

        is_self_initiative = type_value in {
            "subsystem_health", "data_quality", "operational",
            "capability_gap", "knowledge_acquisition", "behavioral_correction",
        }

        # Try auto-execute for self-initiatives
        if is_self_initiative and engine is not None:
            try:
                exec_result = await engine.execute_initiative(initiative.id)
                result_status = exec_result.get("status")
                skill_result = exec_result.get("result")

                if result_status == "executed" and skill_result == "auto_fixed":
                    self.stats.actions_executed += 1
                    self.stats.actions_this_hour += 1
                    logger.info("Auto-fixed initiative: %s", initiative.id)
                    continue  # Don't push to delivery

                if result_status == "executed" and skill_result == "proposal_created":
                    # Still push to delivery, but mark as proposed
                    pass

                if result_status in ("no_skill", "not_self_initiative"):
                    # No skill matched — push to delivery for human decision
                    pass
            except Exception as exc:
                logger.error("Auto-execution failed for %s: %s", initiative.id, exc)

        # Build and push payload (existing logic)
        payload = {
            "id": getattr(initiative, "id", str(uuid.uuid4())),
            "type": type_value,
            "priority": getattr(initiative, "priority", 0.5),
            "title": getattr(initiative, "description", "").split(".")[0][:80],
            "description": getattr(initiative, "description", ""),
            "rationale": getattr(initiative, "rationale", ""),
            "suggested_action": getattr(initiative, "action_hint", "notify_user") or "notify_user",
            "entity_id": getattr(initiative, "entity_id", None),
            "entity_type": type_value,
            "channel_hint": "home" if is_self_initiative else (
                "dm" if type_value in ("relationship", "proactive_message") else "home"
            ),
            "context": self._build_initiative_context(initiative, type_value),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        if delivery:
            ok = await delivery.push_initiative(payload)
            if ok:
                self.stats.actions_executed += 1
                self.stats.actions_this_hour += 1

        # WebSocket broadcast (existing)
        try:
            broadcast = _get_broadcast()
            broadcast({"type": "initiative", "occurred_at": datetime.now(timezone.utc).isoformat(), "payload": payload})
        except Exception:
            pass

    self._pending_initiatives = []
```

---

## 10. Testing Strategy

### 10.1 Unit Tests

| Component | Test |
|-----------|------|
| `ToolExecutor._record_capability_gap` | Mock graph driver, assert MERGE query |
| `ToolExecutor` graph injection | Assert constructor accepts `graph_client` |
| `submit_correction` graph write | Mock `_graph`, assert Pattern node created |
| `WebSearchTool._record_knowledge_gap` | Mock graph, assert Concept node created |
| `_load_capability_gaps` | Mock graph result, assert context populated |
| `_generate_capability_gap_initiatives` | Mock context, assert Initiative list |
| `CapabilityGapSkill.execute()` | Mock graph, assert PROPOSAL_CREATED |
| `BehavioralCorrectionSkill._update_config` | Write temp env file, assert content |
| `KnowledgeAcquisitionSkill.execute()` | Mock graph, assert PROPOSAL_CREATED |
| `execute_initiative` skill mapping | Assert "operational" maps to "operational_hygiene" |
| `execute_initiative` entity_type | Assert `entity_type` passed in context |
| `_phase_execute` auto-execution | Mock engine, assert `execute_initiative` called |
| `_phase_execute` delivery skip | Assert AUTO_FIXED skips delivery push |
| `.publish()` removal | `grep` skills for `.publish(` returns 0 matches |

### 10.2 Integration Tests

- End-to-end: Tool missing in `execute_batch` → graph updated → context loader finds it → generator produces initiative → auto-execute returns `PROPOSAL_CREATED` → delivery push succeeds
- End-to-end: Correction submitted → graph updated → generator produces initiative → auto-execute returns `AUTO_FIXED` → no delivery push

### 10.3 Regression Tests

- Existing initiative types (follow_up, relationship, health, scheduling) continue to work
- Self-initiatives always route to home channel
- Sidecar restart succeeds without errors

---

## 11. Rollout Plan

### Phase 1: Schema + Detection + Fixes (Day 1)
1. Add `Concept` and `Preference` to `schema.py`
2. Extend `Capability` and `Pattern` in `schema.py`
3. Fix `.publish()` → `.emit()` in `data_quality.py` and `operational_hygiene.py`
4. Add `graph_client` to `ToolExecutor.__init__` and wire in `server.py`/`host.py`
5. Wire tool failure detection in `ToolExecutor.execute_batch()`
6. Wire correction detection in `submit_correction()`
7. Wire knowledge gap detection in `WebSearchTool.execute()`
8. Update `register_native_tools()` to pass graph to `WebSearchTool`

### Phase 2: Loaders + Generators (Day 1-2)
1. Add `_load_capability_gaps()`, `_load_behavioral_patterns()`, `_load_knowledge_gaps()`
2. Register loaders in `_load_graph_context()`
3. Replace placeholder generators with real implementations
4. Pass `trigger_data` through generators

### Phase 3: Skills (Day 2)
1. Implement `CapabilityGapSkill`
2. Implement `BehavioralCorrectionSkill`
3. Implement `KnowledgeAcquisitionSkill`
4. Register all three in `SkillRegistry`

### Phase 4: Execution Wiring (Day 2-3)
1. Fix `execute_initiative()` skill name mapping
2. Fix `execute_initiative()` to pass `entity_type` and full `trigger_data`
3. Wire auto-execution into `AutonomyLoop._phase_execute()`

### Phase 5: Testing + Deploy (Day 3)
1. Write unit tests
2. Write integration tests
3. Run full test suite
4. Deploy to staging, observe for 24h
5. Tag v0.11.1

---

## 12. Acceptance Criteria

- [ ] `grep -rni "placeholder\|TODO\|FIXME\|Currently a placeholder\|will be populated" colony_sidecar/intelligence/components/initiative_engine.py` returns 0 matches
- [ ] `grep -rni "placeholder\|TODO\|FIXME" colony_sidecar/skills/executors/` returns 0 matches
- [ ] All 6 self-initiative generators return real `Initiative` lists (may be empty if no data)
- [ ] All 6 self-initiative skills return real `ExecutionResult` values (no `NO_ACTION` for valid inputs)
- [ ] `execute_initiative()` correctly maps `OPERATIONAL` → `operational_hygiene`
- [ ] `execute_initiative()` passes `entity_type` through to skills
- [ ] `AutonomyLoop._phase_execute()` calls `engine.execute_initiative()` before delivery push
- [ ] No `.publish()` calls remain in `colony_sidecar/skills/executors/`
- [ ] Tool failures create `(:Capability)` and `[:NEEDS_CAPABILITY]` nodes in graph within 1s
- [ ] Corrections create `(:Pattern {pattern_type: 'correction'})` nodes in graph within 1s
- [ ] `web_search` with 0 results creates `(:Concept)` node in graph within 1s
- [ ] Auto-executed `AUTO_FIXED` initiatives do NOT produce delivery push
- [ ] Auto-executed `PROPOSAL_CREATED` initiatives DO produce delivery push
- [ ] Tests cover ≥ 80% of new code
- [ ] Sidecar restart succeeds without errors
- [ ] 24h staging observation shows no regressions in existing initiative types

---

## 13. v0.11.2 Scope (Not This Release)

- **NLP correction detection** — Two-stage pipeline (keyword filter + LLM parse) for implicit corrections in conversation
- **Knowledge gap depth** — Deep research for concepts recurring in 3+ projects
- **Capability auto-registration** — Auto-register Hermes skills as Colony capabilities when detected
- **Public registry search** — Search PyPI/npm when capability is missing

---

*End of spec.*
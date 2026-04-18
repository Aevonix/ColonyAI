# Autonomy Loop Extraction Spec

## Overview

The AutonomyLoop is Colony's continuous operating cycle — a tick-based
loop that orchestrates all intelligence subsystems on a schedule. It's
the "brainstem" that keeps Colony running autonomously between human
interactions.

Current state: lives in `colony-ai/colony/autonomy/` (~1800 lines).
Target: `colony-core/sidecar/colony_sidecar/autonomy/`.

## Architecture

### Current (colony-ai)

```
AutonomyLoop.__init__(
    event_bus, goal_engine, initiative_engine, anomaly_detector,
    queue_manager, briefing_scheduler, cron_tick,
    config, cognition, memory_consolidator, mind_model,
    skill_scheduler, skill_budget, turboquant_cache,
    graph_client, delivery_bridge, colony_metrics, memory_distiller
)
```

18 constructor args. Tightly coupled to colony-ai's object graph.

### Proposed (colony-core sidecar)

```
AutonomyLoop(registry: SubsystemRegistry, config: AutonomyConfig)
```

Single `SubsystemRegistry` holds references to all wired subsystems.
The loop pulls what it needs lazily — if a subsystem isn't wired,
the corresponding phase is a no-op. This matches the sidecar's
existing module-level wiring pattern.

## SubsystemRegistry

New class that wraps the host router's module-level globals:

```python
class SubsystemRegistry:
    """Provides lazy access to all wired sidecar subsystems."""

    @property
    def graph(self) -> Optional[ColonyGraph]: ...
    @property
    def goals(self) -> Optional[GoalEngine]: ...
    @property
    def initiative(self) -> Optional[InitiativeEngine]: ...
    @property
    def anomalies(self) -> Optional[AnomalyDetector]: ...
    @property
    def queue(self) -> Optional[QueueManager]: ...
    @property
    def briefings(self) -> Optional[BriefingEngine]: ...
    @property
    def events(self) -> Optional[EventBus]: ...
    @property
    def delivery(self) -> Optional[ProactiveDeliveryBridge]: ...
    @property
    def cognition(self) -> Optional[MetaLearner]: ...
    @property
    def connection_discoverer(self) -> Optional[ConnectionDiscoverer]: ...
    @property
    def learner(self) -> Optional[ContinuousLearner]: ...
    @property
    def skills(self) -> Optional[SkillRegistry]: ...
    @property
    def chain(self) -> Optional[ChainManager]: ...
    @property
    def secrets(self) -> Optional[SecretsManager]: ...
```

Each property reads from the host router's module-level variable
(e.g., `_graph`, `_goals_store`, etc.). Returns None if not wired.

## Tick Phases (20 total)

| # | Phase | Deps | Frequency | Changes |
|---|-------|------|-----------|---------|
| 0 | skill_triggers | skill_scheduler | every tick | Remove TurboQuant refs |
| 1 | events | event_bus | every tick | Keep as-is |
| 2 | goals | goal_engine | every tick | Keep as-is |
| 3 | predictions | mind_model | every tick | Remove (no mind_model yet) |
| 4 | anomalies | anomaly_detector | every tick | Keep as-is |
| 5 | cron | cron_tick callback | every tick | Stub — no cron module yet |
| 6 | initiative | initiative_engine | every tick | Keep as-is |
| 7 | execute | queue_manager | every tick | Keep as-is |
| 8 | cognition | metalearner | every tick | Keep as-is |
| 9 | memory_consolidation | graph | hourly | Keep as-is |
| 10 | memory_decay | graph | daily | Keep as-is |
| 11 | memory_pruning | graph | weekly | Keep as-is |
| 12 | memory_distillation | graph | weekly | Keep as-is |
| 13 | task_completion | queue | every tick | Keep as-is |
| 14 | frustration_update | delivery_bridge | every tick | Keep as-is |
| 15 | skill_evict | skill_scheduler | every tick | Keep as-is |
| 16 | relationships | graph, scorer | every tick | Replace import with registry |
| 17 | synthesis | connection_discoverer | every tick | Replace import with registry |
| 18 | bootstrap_check | chain, identity | daily | Replace import with registry |
| 19 | self_reflection | identity, graph | weekly | Replace import with registry |

### Phases to remove/simplify

- **predictions** (phase 3): No mind_model in sidecar yet. Stub as no-op.
- **skill_triggers** (phase 0): Remove TurboQuant cache ref. Simplify to just evaluate triggers.
- **skill_evict** (phase 15): Remove TurboQuant budget ref.
- **cron** (phase 5): No cron module in sidecar. Stub as no-op initially.

### Phases that need import replacement

These phases do `from colony.X import Y` inline. Replace with registry access:

- **relationships**: `from colony.intelligence.relationships.scorer import RelationshipScorer` → use `registry.connection_discoverer` or a new `RelationshipScorer(registry.graph)`
- **synthesis**: `from colony.intelligence.synthesis.connection_discoverer import ConnectionDiscoverer` → `registry.connection_discoverer`
- **bootstrap_check**: `from colony.identity_bootstrap.runner import IdentityBootstrap` → `registry.chain`
- **self_reflection**: `from colony.identity_bootstrap.self_reflection import SelfReflectionComponent` → `registry.chain`
- **execute**: `from colony.task_queue.models import Job` → keep direct import (it's a dataclass)

### Mesh event emission

All `self.events.emit_mesh_event(...)` calls → replace with `self.events.emit(...)` against the sidecar's EventBus. No mesh concept in sidecar.

## API Endpoints

```
GET  /v1/host/autonomy/status   → AutonomyStatusResponse
POST /v1/host/autonomy/start    → AutonomyStatusResponse
POST /v1/host/autonomy/stop     → AutonomyStatusResponse
```

### Schemas

```python
class AutonomyStatusResponse(BaseModel):
    running: bool
    in_quiet_hours: bool
    ticks: int
    events_processed: int
    goals_checked: int
    initiatives_generated: int
    actions_executed: int
    errors: int
    config: Optional[Dict[str, Any]] = None
```

## Server Lifespan Wiring

```python
# After all other subsystems are wired:
if all subsystems ready:
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    autonomy_config = AutonomyConfig.from_env()
    registry = SubsystemRegistry()
    autonomy_loop = AutonomyLoop(registry=registry, config=autonomy_config)
    set_autonomy_loop(autonomy_loop)
    # Don't auto-start — let the host start it via API
```

The loop does NOT auto-start. The host (OpenClaw plugin) calls
`POST /v1/host/autonomy/start` when it's ready. This gives the host
control over when Colony goes autonomous.

## File Structure

```
colony_sidecar/autonomy/
    __init__.py
    config.py        # AutonomyConfig (from_env, from_dict)
    loop.py          # AutonomyLoop (tick phases, lifecycle)
    registry.py      # SubsystemRegistry (lazy access to wired deps)
    schedule_adapter.py  # Cron adapter (stub for now)
```

## Migration Checklist

1. Copy `autonomy/` to sidecar, fix imports
2. Create `SubsystemRegistry` class
3. Rewrite `AutonomyLoop.__init__` to take `(registry, config)` only
4. Replace all inline imports with registry access
5. Remove mesh event emissions → use EventBus.emit
6. Remove TurboQuant references from skill phases
7. Stub predictions phase (no mind_model)
8. Stub cron phase (no scheduler yet)
9. Add API schemas + endpoints
10. Wire into server lifespan
11. Add tests
12. Regenerate TypeScript types

## Config Environment Variables

```
COLONY_AUTONOMY_TICK_INTERVAL_SECS=300
COLONY_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD=0.7
COLONY_AUTONOMY_MAX_ACTIONS_PER_HOUR=20
COLONY_AUTONOMY_QUIET_HOURS_START=22:00
COLONY_AUTONOMY_QUIET_HOURS_END=07:00
COLONY_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD=0.6
COLONY_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS=24
COLONY_AUTONOMY_BOOTSTRAP_CHECK_INTERVAL_HOURS=24
COLONY_AUTONOMY_SELF_REFLECTION_INTERVAL_DAYS=7
```

## Estimated Effort

| Task | Time |
|------|------|
| Copy + fix imports | 30m |
| SubsystemRegistry | 1h |
| Rewrite constructor + phase deps | 2h |
| Remove mesh/TurboQuant/stubs | 1h |
| API endpoints + schemas | 30m |
| Server wiring | 30m |
| Tests | 1h |
| **Total** | **~6.5h** |

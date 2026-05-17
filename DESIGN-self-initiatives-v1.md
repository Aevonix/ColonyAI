# Colony Self-Initiative Architecture v1.0

**Status:** Draft — awaiting review  
**Author:** Aeva (ColonyAI)  
**Date:** 2026-05-16  
**Target Version:** v0.11.0 (minor bump — new capability class)  

---

## 1. Problem Statement

The current `InitiativeEngine` is purely **derivative**: it monitors the *owner's* state (blocked goals, neglected contacts, health metrics) and generates reminders *for the owner*. It treats Colony as a passive notification service.

What is missing is **self-initiative**: Colony observing *its own* state, capabilities, and environment, identifying problems or opportunities, and **acting autonomously** — not reminding the owner to act.

Additionally, the engine's contact/relationship logic is architecturally confused. It conflates the owner's social graph with Colony's operational relationships and generates false-positive "neglected contact" nudges without communication access.

---

## 2. Design Principles

1. **Agent is the actor** — Self-initiatives result in Colony *doing* something, not messaging the owner.
2. **Home channel for progress** — System-level notifications and action monitoring go to the home channel. DMs are reserved for user-facing responses and input requests.
3. **No false relationship nudges** — Contact monitoring is gated behind explicit communication access. Until then, relationship initiatives are disabled.
4. **Fail-closed on ambiguity** — If an initiative requires the owner's input, it becomes a proposal ("should I X?"), not an assumption.
5. **Observability first** — Every self-initiative is logged, measured, and reviewable. The owner can see what Colony did and why.

---

## 3. Self-Initiative Categories

### 3.1 Subsystem Health (HEALTH)
**Trigger:** Error rates, latency, or failure rates in Colony's own components exceed thresholds.

**Data sources:**
- Telemetry store (`_telemetry.touch()` history)
- Component health checks (embedder, graph, event bus, delivery bridge)
- Log error pattern detection

**Concrete examples:**
- Embedding latency > 1s for 30+ minutes → investigate provider/config
- `Signal` object missing `normalized_value` → file bug or auto-fix if safe
- Delivery bridge failing to push initiatives for N ticks → diagnose and retry
- Neo4j schema warnings recurring → propose migration PR
- Graph query failures spiking → check connection pool / restart

**Action pattern:**
1. Diagnose (query logs, check config)
2. Attempt auto-fix (restart component, reload config, patch code if sandboxed)
3. If fix succeeds → log resolution
4. If fix fails or requires approval → escalate to proposal for the owner

**Delivery:** Home channel with progress updates. DM only if escalation needed.

---

### 3.2 Data Quality & Schema Hygiene (DATA)
**Trigger:** Stale indexes, schema drift, empty stores, orphaned records, missing relationships.

**Data sources:**
- Neo4j schema introspection (compare runtime schema vs. expected)
- SQLite store health checks (table existence, row counts, WAL size)
- LanceDB index freshness
- Graph orphan detection (Memory nodes without `:ABOUT` edges, etc.)

**Concrete examples:**
- Neo4j has `ABOUT` edges but queries reference `BELONGS_TO` → generate migration
- SQLite contacts store empty but Neo4j has Person nodes → propose sync
- Goals table has rows with `status=NULL` → normalize to `active`
- LanceDB index older than N days → trigger re-index

**Action pattern:**
1. Introspect current state vs. expected schema
2. If drift detected and fix is deterministic → auto-apply (with rollback snapshot)
3. If drift is ambiguous or destructive → generate proposal for the owner
4. Log all changes to audit trail

**Delivery:** Home channel. DM for proposals requiring approval.

---

### 3.3 Operational Hygiene (OPS)
**Trigger:** Scheduled maintenance windows, resource thresholds, or operational SLAs.

**Data sources:**
- File system (log sizes, disk space, backup age)
- Process health (sidecar uptime, memory usage)
- Dependency freshness (pip packages, model weights)
- Database backup timestamps

**Concrete examples:**
- No database backup in 7 days → trigger backup
- Log files > 100MB → rotate and compress
- Sidecar uptime > 30 days without restart → graceful restart during low-activity window
- Model weights cache stale → refresh from HuggingFace
- Disk usage > 80% → alert + propose cleanup

**Action pattern:**
1. Check threshold
2. If auto-safe (backup, rotation, cache refresh) → execute
3. If requires downtime or deletion → schedule + notify

**Delivery:** Home channel for execution, DM for scheduling conflicts.

---

### 3.4 Capability Gap (GAP)
**Trigger:** The owner requests something Colony cannot do, or a recurring task pattern suggests a missing tool/skill.

**Data sources:**
- Failed tool invocations ("no such tool")
- The owner's corrections ("you can't do X yet")
- Recurring manual workarounds in conversation history
- GitHub issues / PRs mentioning Colony limitations

**Concrete examples:**
- The owner asks to "book a flight" and no travel tool exists → research travel APIs, propose integration
- Repeated pattern of the owner asking for spreadsheet creation → build Sheets skill
- Error: "No calendar integration found" → research Google Calendar API

**Action pattern:**
1. Detect gap (failed invocation or explicit correction)
2. Research available solutions (API docs, existing libraries, MCP servers)
3. Generate proposal: "I can integrate X to enable Y. Should I?"
4. If approved → implement, test, document

**Delivery:** DM (this is a proposal requiring the owner's input).

---

### 3.5 Knowledge Acquisition (LEARN)
**Trigger:** Active projects reference concepts, technologies, or domains Colony has low confidence in.

**Data sources:**
- Project context (repos the owner is working on, tech stacks mentioned)
- World model entity confidence scores
- Research pipeline history
- Conversation topics with high uncertainty

**Concrete examples:**
- The owner mentions "Rayon parallelism" and Colony has no Rust concurrency knowledge → research and index
- New dependency added to `Cargo.toml` or `package.json` → read docs, summarize
- World model shows low confidence on "quantum computing" but the owner's goals reference it → deep dive

**Action pattern:**
1. Identify knowledge gap from project context
2. Run research pipeline (web search, doc reading, synthesis)
3. Store findings in world model / memory
4. Surface summary to home channel: "Learned about X — now available for queries"

**Delivery:** Home channel with summary. No DM unless the owner explicitly asked.

---

### 3.6 Behavioral Correction (CORRECT)
**Trigger:** The owner gives a correction pattern that recurs 3+ times or is marked as important.

**Data sources:**
- Explicit corrections in conversation ("don't do that", "always do this")
- Memory tags: `behavioral_rule`, `preference`, `correction`
- Pattern store entries with high confidence

**Concrete examples:**
- The owner says "never treat Colony as a reminder service" 3+ times → generate initiative to audit all reminder-like behaviors
- "Always git status before work" → inject into procedural memory, update skills
- "Route progress to home channel, not DMs" → update delivery routing defaults

**Action pattern:**
1. Detect recurring correction via pattern store
2. Generate self-initiative: "Update behavior X per the owner's preference"
3. If change is to Colony's own code/config → open PR or patch directly
4. If change is to conversation style → update system prompt / skills
5. Confirm to home channel: "Updated: now doing X instead of Y"

**Delivery:** Home channel confirmation. DM only if the correction is ambiguous.

---

## 4. Initiative Engine Rewiring

### 4.1 Phase: Context Loading

Current `_load_graph_context()` loads:
- `pending_tasks` (blocked goals)
- `neglected_contacts` (person.lastInteraction)
- `health_alerts` (bio-metrics)
- `scheduling_opportunities` (calendar gaps)
- `pending_signals` (signal accumulation)

**New context categories:**
- `subsystem_health` — populated from telemetry and component health checks
- `data_quality_issues` — populated from schema introspection
- `operational_tasks` — populated from scheduled maintenance checks
- `capability_gaps` — populated from failed invocations and correction patterns
- `knowledge_gaps` — populated from project context and world model confidence
- `behavioral_rules` — populated from pattern store correction entries

**Relationship/contact context is removed** until communication monitoring is explicitly enabled.

### 4.2 Phase: Generation

Current `_generate_*` methods produce `Initiative` dataclasses with type in `FOLLOW_UP`, `RELATIONSHIP`, `HEALTH`, `SCHEDULING`.

**New initiative types:**
- `SUBSYSTEM_HEALTH` — auto-fix or diagnose component issues
- `DATA_QUALITY` — schema migration, cleanup, sync
- `OPERATIONAL` — backup, rotation, restart, cache refresh
- `CAPABILITY_GAP` — research and propose integrations
- `KNOWLEDGE_ACQUISITION` — research and index new domains
- `BEHAVIORAL_CORRECTION` — update Colony's own behavior

**Priority rules:**
- `SUBSYSTEM_HEALTH` > 0.8 if component is down or critically degraded
- `DATA_QUALITY` > 0.7 if schema drift causes query failures
- `OPERATIONAL` > 0.6 if SLA is breached (backup age, disk space)
- `CAPABILITY_GAP` > 0.5 (never auto-execute — always proposal)
- `KNOWLEDGE_ACQUISITION` > 0.3 (background, non-blocking)
- `BEHAVIORAL_CORRECTION` > 0.7 if correction recurs 3+ times

### 4.3 Phase: Execution

Current `_phase_execute()` calls `delivery.push_initiative(payload)` for every initiative.

**New execution router:**

```python
async def _execute_self_initiative(self, initiative: Initiative) -> bool:
    """Route self-initiatives to the appropriate executor."""
    type_value = initiative.type.value
    
    if type_value == "subsystem_health":
        return await self._health_executor.run(initiative)
    elif type_value == "data_quality":
        return await self._data_executor.run(initiative)
    elif type_value == "operational":
        return await self._ops_executor.run(initiative)
    elif type_value == "capability_gap":
        return await self._capability_executor.research_and_propose(initiative)
    elif type_value == "knowledge_acquisition":
        return await self._research_pipeline.run(initiative)
    elif type_value == "behavioral_correction":
        return await self._behavior_executor.apply(initiative)
    
    return False
```

**Execution outcomes:**
- `AUTO_FIXED` — Colony resolved the issue. Report to home channel.
- `PROPOSAL_CREATED` — Requires the owner's input. DM with proposal.
- `RESEARCH_QUEUED` — Background research started. Home channel on completion.
- `FAILED` — Error during execution. Home channel with error + next steps.

---

## 5. Delivery Routing

### 5.1 Current Behavior
All initiatives route through `push_initiative()` which sends to the webhook endpoint. The webhook delivers to whatever channel is configured.

### 5.2 New Routing Rules

| Initiative Type | Auto-Execute? | Delivery Channel | Notes |
|---|---|---|---|
| SUBSYSTEM_HEALTH | Yes (safe fixes) | Home channel | Escalate to DM if fix fails |
| DATA_QUALITY | Yes (deterministic) | Home channel | Proposal for destructive changes |
| OPERATIONAL | Yes (scheduled) | Home channel | DM for downtime scheduling |
| CAPABILITY_GAP | No | DM | Always requires approval |
| KNOWLEDGE_ACQUISITION | Yes | Home channel | Summary on completion |
| BEHAVIORAL_CORRECTION | Yes (code/config) | Home channel | DM if ambiguity |

**Delivery context:**
- `user_chat` — DM channel for the owner (used for proposals)
- `home_chat` — Home channel for system progress (used for executions)
- `channel_hint` in payload determines primary target

### 5.3 Progress Reporting

All self-initiatives that execute autonomously must report progress:

```json
{
  "type": "initiative",
  "payload": {
    "initiative_type": "subsystem_health",
    "title": "Embedding latency restored",
    "description": "Embedding latency was >1s for 45min. Restarted embed pipeline. Now 234ms.",
    "priority": 70,
    "status": "completed",
    "agent_name": "Colony",
    "context": {
      "trigger": "Embedding latency 1186ms exceeds 500ms threshold",
      "suggested_actions": ["restart_embed_pipeline"],
      "constraints": {},
      "metadata": {
        "source": "autonomy_loop",
        "entity_id": "embed_pipeline",
        "entity_type": "subsystem",
        "outcome": "AUTO_FIXED",
        "before": "1186ms",
        "after": "234ms"
      }
    }
  },
  "delivery_context": {
    "home_chat": "${HOME_CHANNEL_ID}"
  },
  "channel_hint": "home"
}
```

---

## 6. World Model: Unified Neo4j Graph

Neo4j is the **single source of truth** for both Colony's operational state and the owner's world context. There is no separate "Colony state graph" vs. "the owner's social graph." The unified model enables Colony to reason across both domains.

### 6.1 Node Types

| Label | Purpose | Examples |
|---|---|---|
| `Person` | People in the owner's world | The owner, colleagues, family, contacts |
| `Agent` | Autonomous entities (including Colony itself) | Colony, other AI agents, bots |
| `Project` | Active work items | Repo features, integrations, milestones |
| `Goal` | The owner's goals and Colony's internal objectives | "Ship v0.11.0", "Fix embedding latency" |
| `Task` | Actionable work units | PR review, bug fix, research |
| `Subsystem` | Colony's own components | embed_pipeline, delivery_bridge, event_bus |
| `Capability` | Tools/skills Colony has or needs | github_pr, neo4j_query, calendar_api |
| `Memory` | Indexed knowledge | Conversation excerpts, research findings |
| `Commitment` | Temporal obligations | Deadlines, scheduled meetings |
| `Pattern` | Recurring behavioral patterns | "Owner prefers home channel for progress" |
| `InitiativeCategory` | Dynamic self-initiative types | Registered at runtime via skill system |

### 6.2 Relationships

| Type | From | To | Meaning |
|---|---|---|---|
| `ABOUT` | `Memory` | `Person` | Memory concerns this person |
| `MANAGES` | `Agent` | `Person` | Agent has relationship with person |
| `OWNS` | `Person` | `Project` | Person owns/works on project |
| `DEPENDS_ON` | `Subsystem` | `Subsystem` | Component dependency |
| `HAS_CAPABILITY` | `Agent` | `Capability` | Agent can use this tool |
| `NEEDS_CAPABILITY` | `Agent` | `Capability` | Agent lacks this tool |
| `GENERATED` | `Agent` | `Initiative` | Agent created this initiative |
| `TARGETS` | `Initiative` | `Subsystem` | Initiative targets this component |
| `BELONGS_TO` | `Task` | `Project` | Task is part of project |
| `BLOCKS` | `Goal` | `Goal` | Goal is blocked by another |
| `EXHIBITS` | `Person` | `Pattern` | Person exhibits this pattern |
| `TRIGGERS` | `Pattern` | `InitiativeCategory` | Pattern triggers initiatives of this type |

### 6.3 Colony's Self-Representation

Colony has its own `Agent` node in the graph:

```cypher
(:Agent {
  id: "colony-sidecar",
  name: "Colony",
  version: "0.11.0",
  status: "active",
  capabilities: ["memory", "reasoning", "delivery", ...],
  health_score: 0.95,
  last_tick_at: "2026-05-16T21:00:00Z"
})
```

Colony monitors itself by querying its own node and its relationships:
- `(:Agent {id: 'colony-sidecar'})-[:DEPENDS_ON]->(:Subsystem)` — components I depend on
- `(:Agent)-[:NEEDS_CAPABILITY]->(:Capability)` — gaps I need to fill
- `(:Agent)-[:GENERATED]->(:Initiative)` — initiatives I've created

### 6.4 Owner's Context

The owner's `Person` node is linked to their projects, goals, contacts, and patterns:
- `(:Person {id: '${OWNER_PERSON_ID}'})-[:OWNS]->(:Project)` — what they're working on
- `(:Person)-[:EXHIBITS]->(:Pattern)` — behavioral patterns I've learned
- `(:Person)-[:MANAGES]->(:Person)` — their relationships (colleagues, family)

Colony uses this unified graph to answer questions like:
- "What is the owner working on?" → Traverse `Person-[:OWNS]->Project`
- "What is Colony missing?" → Traverse `Agent-[:NEEDS_CAPABILITY]->Capability`
- "What patterns should trigger initiatives?" → Traverse `Pattern-[:TRIGGERS]->InitiativeCategory`

### 6.5 Contact/Relationship Distinction

There is no separate SQLite contacts store for "Colony's relationships." All relationships live in Neo4j:

- `(:Agent {id: 'colony-sidecar'})-[:MANAGES]->(:Person)` — Colony's operational relationships
- `(:Person {id: '${OWNER_PERSON_ID}'})-[:MANAGES]->(:Person)` — the owner's social relationships

**Relationship initiatives are gated by the `MANAGES` edge.**
- If Colony has `MANAGES` to a Person AND has a communication channel handle, relationship initiatives are enabled for that Person.
- If no `MANAGES` edge exists, or no channel handle exists, no relationship initiatives.
- Colony creates `MANAGES` edges explicitly when the owner authorizes communication with someone, or when they designate them as important.

---

## 7. Dynamic Initiative Categories

Colony is not limited to hardcoded initiative types. It can **register new categories at runtime** via the `InitiativeCategory` skill system.

### 7.1 Category Registry

Each `InitiativeCategory` is a node in Neo4j:

```cypher
(:InitiativeCategory {
  id: "embedding_latency",
  name: "Embedding Latency Monitor",
  description: "Monitor embedding pipeline latency and restart if degraded",
  trigger_query: "MATCH (s:Subsystem {name: 'embed_pipeline'}) WHERE s.latency_ms > 1000 RETURN s",
  action_type: "auto_fix",
  executor_skill: "subsystem_health",
  priority_formula: "0.5 + (s.latency_ms - 1000) / 2000",
  cooldown_minutes: 30,
  auto_execute: true,
  requires_approval: false,
  created_at: "2026-05-16T21:00:00Z"
})
```

Fields:
- `id` — unique category identifier
- `name` — human-readable name
- `description` — what this category monitors
- `trigger_query` — Cypher query that detects the condition
- `action_type` — `auto_fix`, `propose`, `research`, `notify`
- `executor_skill` — which skill handles execution
- `priority_formula` — how to compute priority from query results
- `cooldown_minutes` — minimum time between initiatives of this category
- `auto_execute` — can Colony act without approval?
- `requires_approval` — always ask the owner before acting

### 7.2 Category Lifecycle

**Creation:**
1. Colony detects a recurring pattern that doesn't fit existing categories
2. Colony drafts a new `InitiativeCategory` node with trigger query and action plan
3. Colony proposes the new category to the owner: "I notice X keeps happening. Should I monitor for Y and do Z?"
4. If approved, the category is registered and begins triggering

**Evolution:**
- Categories can be disabled, modified, or deleted
- Colony tracks category effectiveness (how often initiatives lead to successful outcomes)
- Low-effectiveness categories are candidates for revision or removal

**Built-in categories** (seeded at init):
- `subsystem_health` — monitor Colony's own components
- `data_quality` — schema drift, orphan detection
- `operational_hygiene` — backups, rotation, restarts
- `capability_gap` — missing tools/skills
- `knowledge_acquisition` — research new domains
- `behavioral_correction` — update behavior per the owner's preferences

### 7.3 Skill-Based Execution

Each category references an **executor skill** that knows how to act:

```python
# Skill interface
class InitiativeExecutorSkill:
    async def can_execute(self, category: InitiativeCategory, context: dict) -> bool:
        """Check if this skill can handle the given category."""
        
    async def execute(self, initiative: Initiative) -> ExecutionResult:
        """Execute the initiative. Returns AUTO_FIXED, PROPOSAL_CREATED, etc."""
```

Skills are loaded dynamically from `~/.hermes/skills/` and registered at startup. New skills can be added without restarting Colony (hot-reload on file change).

**Example skill: `subsystem_health_skill.py`**
```python
class SubsystemHealthSkill(InitiativeExecutorSkill):
    async def can_execute(self, category, context):
        return category.executor_skill == "subsystem_health"
    
    async def execute(self, initiative):
        subsystem = initiative.entity_id
        # Diagnose
        health = await self.diagnose(subsystem)
        if health.status == "degraded":
            # Attempt restart
            result = await self.restart(subsystem)
            if result.ok:
                return ExecutionResult.AUTO_FIXED
            else:
                return ExecutionResult.FAILED
        return ExecutionResult.NO_ACTION
```

### 7.4 Category Discovery

Colony runs a periodic **category discovery scan**:
1. Query the graph for patterns not covered by existing categories
2. Look for recurring errors, manual workarounds, or unexplained phenomena
3. Draft candidate categories with trigger queries
4. Propose to the owner for approval

This is how Colony evolves its own autonomy over time.

---

## 8. Integration Points

### 8.1 Autonomy Loop

The existing tick phases are preserved. Self-initiatives add new phases:

- Phase 4a: `dynamic_category_scan` (every 50 ticks) — discover new initiative categories
- Phase 4b: `subsystem_health_check` (every tick)
- Phase 4c: `data_quality_check` (every 5 ticks)
- Phase 4d: `operational_hygiene_check` (every tick, time-gated)
- Phase 4e: `capability_gap_scan` (every 10 ticks)
- Phase 4f: `knowledge_gap_scan` (every 10 ticks)
- Phase 4g: `behavioral_correction_scan` (every 5 ticks)

These run alongside existing phases but feed into the skill-based execution router.

### 8.2 Telemetry

New telemetry keys:
- `last_self_initiative_at` — timestamp of last autonomous action
- `self_initiative_count` — running count
- `subsystem_health_score` — aggregate health metric (0-1)
- `data_quality_score` — schema alignment metric (0-1)
- `category_count` — number of registered initiative categories

### 8.3 Event Bus

New event types:
- `SelfInitiativeCreated` — when a self-initiative is generated
- `SelfInitiativeExecuted` — when execution completes
- `SelfInitiativeFailed` — when execution fails
- `CapabilityGapDetected` — when a new capability gap is identified
- `InitiativeCategoryProposed` — when Colony drafts a new category for approval
- `InitiativeCategoryRegistered` — when a category is approved and activated

---

## 9. Backward Compatibility

### 9.1 Existing Initiatives
Follow-up, health, and scheduling initiatives continue to work as before. They are classified as **user-initiatives** (derivative from the owner's state).

### 9.2 Contact/Relationship
Relationship initiatives are disabled by default. They are enabled only when:
- Colony has a `MANAGES` edge to the Person in Neo4j
- AND Colony has an active communication channel for that Person

### 9.3 API
The `/v1/host/initiatives` endpoint returns both user-initiatives and self-initiatives, differentiated by `source: "user" | "self"`.

---

## 10. Implementation Plan

### Phase 1: Foundation (v0.11.0)
1. Add `Agent`, `Subsystem`, `Capability`, `InitiativeCategory` node types to Neo4j schema
2. Create Colony's own `Agent` node with `DEPENDS_ON` edges to subsystems
3. Implement skill-based executor framework (`InitiativeExecutorSkill` base class)
4. Implement built-in skills: `subsystem_health`, `data_quality`, `operational_hygiene`
5. Wire dynamic category loader into `InitiativeEngine`
6. Update delivery routing to respect `channel_hint`
7. Disable relationship initiatives (gated by `MANAGES` + channel)

### Phase 2: Expansion (v0.11.x)
1. Implement `capability_gap` skill with research pipeline integration
2. Implement `knowledge_acquisition` skill
3. Implement `behavioral_correction` skill
4. Add category discovery scan (auto-detect gaps in coverage)
5. Hot-reload for skills

### Phase 3: Polish (v0.12.0)
1. Relationship initiative gating per-platform (when communication access granted)
2. Self-initiative audit UI (what did Colony do and why)
3. Feedback loop: The owner rates self-initiatives, Colony learns
4. Category effectiveness tracking and auto-evolution

---

## 11. Open Questions

1. **Sandboxing**: Should self-initiatives that modify Colony's own code be sandboxed (PR-based) or applied directly?
2. **Approval threshold**: What priority level requires explicit approval vs. auto-execute?
3. **Rollbacks**: If a self-initiative causes damage (bad config change), how is it rolled back?
4. **Relationship gating**: When communication access is granted, should all historical contacts become eligible, or only new ones?
5. **Overlap with scheduled tasks**: Some operational hygiene (backups) might already be cron jobs. How do we avoid duplication?
6. **Category approval**: Should Colony auto-register low-risk categories and only ask for high-risk ones?
7. **Skill versioning**: How do we version skills and roll back bad skill updates?

---

## 12. Review Checklist

- [ ] Neo4j is the unified world model for both Colony and the owner
- [ ] Dynamic category skill system is clear and implementable
- [ ] Self-initiative categories cover real use cases
- [ ] Delivery routing respects home-channel preference
- [ ] Relationship/contact confusion is resolved via `MANAGES` edges
- [ ] Backward compatibility is preserved
- [ ] No false-positive nudges without communication access
- [ ] Rollback/escalation paths are defined
- [ ] Open questions are answered

---

*Ready for review. Please comment inline or reply with revisions.*

# Multi-Agent Colony v0.7.0 — Spec Analysis

## Executive Summary

The spec is **architecturally sound** but has **significant duplication issues** that would waste effort and create maintenance burden. This analysis identifies what to keep, what to modify, and what to remove.

---

## Issues Found

### 🔴 Critical: Duplication with Existing Code

| Proposed in Spec | Existing Code | Action |
|------------------|---------------|--------|
| `initiatives/engine.py` | `intelligence/components/initiative_engine.py` | **MODIFY existing** — don't create new |
| `initiatives/delivery.py` | `delivery/bridge.py` | **EXTEND existing** — don't create new |
| `workers/heartbeat.py` | `autonomy/loop.py` tick-based phases | **INTEGRATE into existing loop** |
| `workers/queue.py` | `autonomy/loop.py` `_phase_initiative()` | **INTEGRATE into existing loop** |
| `workers/timeout.py` | `autonomy/loop.py` phases | **INTEGRATE into existing loop** |
| `workers/cleanup.py` | `autonomy/loop.py` phases | **INTEGRATE into existing loop** |
| `api/routers/agents.py` | `api/routers/host.py` (`/v1/host/` router) | **ADD to existing router** |
| `api/routers/initiatives.py` | `api/routers/host.py` | **ADD to existing router** |
| `api/routers/websocket.py` | `api/routers/host.py` | **ADD to existing router** |

### 🟡 Medium: File Consolidation Opportunities

| Proposed in Spec | Recommendation |
|------------------|----------------|
| `agents/store.py` + `agents/registry.py` | Merge into single `agents/store.py` |
| `initiatives/store.py` + `initiatives/queue.py` | Merge into single `initiatives/store.py` |
| `api/schemas/agents.py` + `api/schemas/initiatives.py` | Add to existing `api/schemas/host.py` |

### 🟢 Correct: New Components Needed

| Component | Status | Notes |
|-----------|--------|-------|
| `agents/store.py` | ✅ NEW | Agent registry (SQLite) |
| `agents/invites.py` | ✅ NEW | Invite generation/validation |
| `agents/websocket.py` | ✅ NEW | WebSocket server for remote agents |
| `initiatives/store.py` | ✅ NEW | Initiative persistence (SQLite) |
| `initiatives/assignment.py` | ✅ NEW | Assignment engine |
| CLI commands for agents | ✅ NEW | Add to existing `cli.py` |
| Plugin WebSocket connection | ✅ MODIFY | Extend existing `src/plugin.ts` |

---

## Existing Architecture to Leverage

### 1. InitiativeEngine (intelligence/components/initiative_engine.py)

**Current State:**
```python
class InitiativeEngine:
    def __init__(self, graph_client, event_bus, mind_model):
        self._initiatives: List[Initiative] = []  # In-memory only
        self._context: Dict[str, List[Dict]] = {}
    
    async def generate(self, types, min_priority) -> List[Initiative]
    async def dismiss(self, initiative_id)
    async def get_active() -> List[Initiative]
```

**What Needs Adding:**
- SQLite persistence (currently in-memory)
- Assignment tracking
- Retry/reassignment logic
- Deduplication

**Action:** Modify existing file, don't create new one.

---

### 2. ProactiveDeliveryBridge (delivery/bridge.py)

**Current State:**
```python
class ProactiveDeliveryBridge:
    async def push_to_gateway(self, platform, chat_id, message) -> bool
    async def push_initiative(self, initiative) -> bool
    def deliver(self, person_id, content, channel, urgency, source) -> str
```

**What Needs Adding:**
- WebSocket delivery for remote agents
- Agent-aware routing (local vs remote)

**Action:** Extend existing file, don't create new one.

---

### 3. AutonomyLoop (autonomy/loop.py)

**Current State:**
```python
class AutonomyLoop:
    async def start()
    async def _tick()
    async def _phase_events()
    async def _phase_goals()
    async def _phase_initiative()  # ← Already has initiative phase
    async def _phase_execute()     # ← Already has execution phase
    async def _phase_cognition()
    # ... 12 more phases
```

**What Needs Adding:**
- Agent heartbeat monitoring phase
- Initiative timeout checking phase
- Agent queue assignment phase

**Action:** Add phases to existing loop, don't create separate workers.

---

### 4. SubsystemRegistry (autonomy/registry.py)

**Current Pattern:**
```python
class SubsystemRegistry:
    @property
    def graph(self) -> Optional[ColonyGraph]
    @property  
    def goals_store(self) -> Optional[GoalStore]
    @property
    def commitment_store(self) -> Optional[CommitmentStore]
    # ... more subsystems
```

**What Needs Adding:**
- `agent_store` property
- `initiative_store` property

**Action:** Add to existing registry.

---

### 5. API Router (api/routers/host.py)

**Current Pattern:**
```python
router = APIRouter(prefix="/v1/host")

@router.get("/health")
@router.post("/memory/write")
@router.post("/goals")
# ... 50+ endpoints
```

**What Needs Adding:**
- `/agents` endpoints
- `/initiatives` endpoints
- WebSocket route

**Action:** Add to existing router.

---

## Corrected File List

### Files to CREATE (8 files)

```
sidecar/colony_sidecar/
├── agents/
│   ├── __init__.py
│   ├── store.py          # Agent registry + invites (merged)
│   └── websocket.py      # WebSocket server
├── initiatives/
│   ├── __init__.py
│   ├── store.py          # SQLite persistence (merged with queue)
│   └── assignment.py     # Assignment engine
```

### Files to MODIFY (7 files)

```
sidecar/colony_sidecar/
├── intelligence/components/
│   └── initiative_engine.py    # Add SQLite persistence
├── delivery/
│   └── bridge.py               # Add WebSocket delivery
├── autonomy/
│   ├── registry.py             # Add agent_store, initiative_store
│   └── loop.py                 # Add agent monitoring phases
├── api/
│   ├── routers/host.py         # Add /agents, /initiatives endpoints
│   └── schemas/host.py         # Add schemas
├── cli.py                      # Add agent commands
├── server.py                   # Wire new stores + WebSocket
└── src/plugin.ts               # Add WebSocket connection
```

**Total: 15 files (8 new + 7 modified)** vs spec's 30+ files.

---

## Corrected Effort Estimate

| Component | Hours | Notes |
|-----------|-------|-------|
| Agent Store + Invites | 3h | Merged, single module |
| WebSocket Server | 3h | New component |
| Initiative Store | 2h | SQLite persistence |
| InitiativeEngine modification | 1h | Add persistence calls |
| Assignment Engine | 2h | New component |
| Bridge extension | 1h | Add WebSocket delivery |
| AutonomyLoop phases | 2h | Add monitoring phases |
| API endpoints | 2h | Add to existing router |
| CLI commands | 1h | Add to existing CLI |
| Plugin WebSocket | 2h | Remote agent connection |
| Testing | 3h | |
| **Total** | **22h** | vs spec's 25h |

---

## User Experience Analysis

### Current Spec: Good UX ✅

1. **Simple onboarding:**
   ```bash
   # Colony host
   colony agent invite
   # → COLONY-7X9K-M2P4-QR8W
   
   # Remote agent
   colony agent connect --setup-code COLONY-7X9K-M2P4-QR8W --colony-url https://...
   ```

2. **Automatic mode detection:** Plugin detects local vs remote based on URL

3. **Unified context:** All agents see same facts/goals

4. **Graceful failover:** Agent offline → reassign work

### No Changes Needed

The UX in the spec is already optimal. The corrections above don't affect user experience — they're purely about code organization.

---

## Risk Analysis

### Risk 1: WebSocket Complexity

**Mitigation:** Start with HTTP polling fallback. WebSocket is an optimization, not a requirement.

```python
# Phase 1: HTTP polling (simple, works everywhere)
GET /v1/host/agents/{id}/assignments  # Agent polls every 30s

# Phase 2: WebSocket (lower latency)
ws://colony:7777/v1/host/agents/{id}/stream
```

### Risk 2: SQLite Locking

Multiple workers accessing same SQLite could cause locks.

**Mitigation:** Use `WAL` mode and connection pooling:

```python
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")  # 5s timeout
```

### Risk 3: Assignment Race Conditions

Two agents claiming same initiative.

**Mitigation:** Use atomic SQLite UPDATE:

```python
UPDATE initiatives 
SET status = 'assigned', assigned_agent_id = ? 
WHERE id = ? AND status = 'pending'
```

Check `affected_rows == 1` to confirm claim.

---

## Recommended Implementation Order

### Phase 1: Foundation (8h)

1. Create `agents/store.py` — agent registry + invites
2. Create `initiatives/store.py` — SQLite persistence
3. Modify `initiative_engine.py` — use SQLite store
4. Add to `autonomy/registry.py` — wire stores

**Deliverable:** Persistent agents and initiatives, existing flow works.

### Phase 2: Assignment (6h)

1. Create `initiatives/assignment.py` — assignment engine
2. Modify `autonomy/loop.py` — add assignment phase
3. Add `/agents` and `/initiatives` API endpoints
4. Add CLI commands

**Deliverable:** Initiatives auto-assigned to agents.

### Phase 3: Remote Agents (8h)

1. Create `agents/websocket.py` — WebSocket server
2. Modify `delivery/bridge.py` — add WebSocket delivery
3. Modify `src/plugin.ts` — WebSocket connection
4. Add agent heartbeat monitoring phase

**Deliverable:** Remote agents connect via WebSocket.

---

## What to Remove from Spec

### Remove These Files (12 files)

```
# Duplicate — extend existing instead
- agents/registry.py              # Merge into store.py
- initiatives/engine.py           # Modify existing
- initiatives/delivery.py         # Extend existing
- initiatives/queue.py            # Merge into store.py
- workers/heartbeat.py            # Integrate into autonomy loop
- workers/queue.py                # Integrate into autonomy loop
- workers/timeout.py              # Integrate into autonomy loop
- workers/cleanup.py              # Integrate into autonomy loop
- api/routers/agents.py           # Add to host.py
- api/routers/initiatives.py      # Add to host.py
- api/routers/websocket.py        # Add to host.py
- cli/agent.py                    # Add to cli.py
- cli/initiatives.py              # Add to cli.py
```

### Keep These (8 new files)

```
agents/store.py          # NEW
agents/websocket.py      # NEW
initiatives/store.py     # NEW
initiatives/assignment.py # NEW
```

---

## Conclusion

**The spec's architecture is correct**, but the implementation approach creates 30+ files when 15 would suffice.

**Recommendations:**

1. ✅ Keep the UX design — it's optimal
2. ✅ Keep the data model — SQLite tables are correct
3. ✅ Keep the WebSocket protocol — well-designed
4. ❌ Replace separate workers with autonomy loop phases
5. ❌ Extend existing files instead of creating new ones
6. ❌ Merge related modules (store + registry, store + queue)

**Result:** Same functionality, 50% less code to maintain, leverages existing patterns.

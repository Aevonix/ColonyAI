# the agent Heartbeat & Agent Snapshot Spec

> **Status:** Draft — pending owner review  
> **Replaces:** `OwnerCheckInTask` (removed in v0.12.1)  
> **Colony version target:** 0.13.0+  

---

## Problem Statement

Colony is the autonomous agent — it runs initiatives, executes actions, tracks telemetry, and "thinks" in the background. That is correct and must stay.

The old `OwnerCheckInTask` was wrong for a different reason: **Colony was bypassing the agent to message the owner directly.** It hardcoded both the trigger ("1 hour of silence") AND the message text ("anything I can help with?"). Colony decided when to talk and what to say — the agent had no say.

That violates the autonomy principle: Colony should act autonomously on the owner's behalf, but **communication decisions belong to the agent.** Colony informs the agent of state; the agent decides whether, when, and how to communicate.

We need an architecture where:
1. **Colony runs everything autonomously** — initiatives, execution, telemetry, scheduling
2. **Colony exposes state to the agent** — via a snapshot endpoint, not direct messages
3. **the agent evaluates and decides** — whether, when, and what to communicate
4. **A heartbeat ensures the agent stays aware** — even during owner silence

Colony remains the brain. the agent remains the voice. The brain never speaks directly — it informs the voice, and the voice decides when to speak.

---

## Core Principles

| Principle | Rule |
|-----------|------|
| Colony never directly messages the owner | Colony emits data/signals only |
| the agent decides on outreach | the agent evaluates context and chooses action |
| State survives session resets | All temporal state lives in Colony, not the agent memory |
| No upstream harness changes | Hermes core remains untouched; all wiring via Colony extension points |

---

## Architecture

```
┌─────────────────┐         ┌──────────────────────┐         ┌─────────────────┐
│   Colony        │         │   Hermes Gateway     │         │   the agent (Agent)  │
│   Sidecar       │         │   (no changes)       │         │                 │
├─────────────────┤         ├──────────────────────┤         ├─────────────────┤
│ TelemetryStore  │◄────────│                      │         │                 │
│  + last_the-agent_   │  API    │                      │         │                 │
│    outreach_at  │         │                      │         │                 │
│                 │         │                      │         │                 │
│ InitiativeStore │◄────────│                      │         │                 │
│                 │  API    │                      │         │                 │
│                 │         │                      │         │                 │
│ Agent Snapshot  │────────►│  Webhook or          │────────►│ Evaluates       │
│   Endpoint      │  push   │  Cron trigger        │         │   snapshot      │
│                 │         │                      │         │                 │
│ Record Outreach │◄────────│                      │◄────────│ Records decision │
│   Endpoint      │  API    │                      │         │                 │
└─────────────────┘         └──────────────────────┘         └─────────────────┘
```

---

## Phase 1: Colony Data Exposure

### 1.1 TelemetryStore Extension

**File:** `colony_sidecar/telemetry.py`

Add `last_the-agent_outreach_at` to track when the agent last proactively messaged the owner.

```python
@dataclass
class TelemetryStore:
    started_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_initiative_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    last_the-agent_outreach_at: Optional[datetime] = None  # NEW
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

The `touch()` method already supports arbitrary keys via `setattr`, so `await telemetry.touch("last_the-agent_outreach_at")` works immediately after this change.

**TelemetryStore `to_dict()` update required:**
```python
async def to_dict(self, thresholds: Dict[str, float]) -> dict:
    started = self.started_at.isoformat() if self.started_at else None
    sync_at = self.last_sync_at.isoformat() if self.last_sync_at else None
    tick_at = self.last_tick_at.isoformat() if self.last_tick_at else None
    init_at = self.last_initiative_at.isoformat() if self.last_initiative_at else None
    prefetch_at = self.last_prefetch_at.isoformat() if self.last_prefetch_at else None
    outreach_at = self.last_the-agent_outreach_at.isoformat() if self.last_the-agent_outreach_at else None
    silence = {}
    for key in thresholds:
        silence[key] = await self.silence_hours(key)
    flags = await self.stale_flags(thresholds)
    return {
        "started_at": started,
        "last_sync_at": sync_at,
        "last_tick_at": tick_at,
        "last_initiative_at": init_at,
        "last_prefetch_at": prefetch_at,
        "last_the-agent_outreach_at": outreach_at,
        "silence_hours": silence,
        "stale_flags": flags,
    }
```

**No other telemetry fields need changing.**

---

### 1.2 New Endpoint: `GET /v1/host/agent-snapshot`

**File:** `colony_sidecar/api/routers/host.py`

Returns a comprehensive snapshot of everything the agent needs to evaluate whether to reach out.

**Request:** None (GET, auth via `Authorization: Bearer <COLONY_API_KEY>` header)

**Response schema** (new Pydantic model in `api/schemas/host.py`):

```python
class AgentSnapshotInitiative(BaseModel):
    id: str
    type: str
    description: str
    priority: float
    status: str
    rationale: Optional[str] = None
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    dedup_key: Optional[str] = None
    created_at: str
    expires_at: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    acknowledged_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    failed_reason: Optional[str] = None


class AgentSnapshotResponse(BaseModel):
    # ── Temporal State ──
    timestamp: str  # ISO 8601 UTC
    telemetry: Dict[str, Any]  # {started_at, last_sync_at, last_tick_at,
                               #  last_initiative_at, last_prefetch_at,
                               #  last_the-agent_outreach_at, silence_hours: {...},
                               #  stale_flags: [...]}

    # ── Initiatives ──
    pending_initiatives: List[AgentSnapshotInitiative]
    pending_count: int
    assigned_count: int
    failed_count: int  # status=failed initiatives
    recently_completed: List[AgentSnapshotInitiative]  # last 24h, status=completed only

    # ── Autonomy Loop ──
    autonomy_mode: str  # reactive | proactive
    autonomy_running: bool
    last_tick_age_minutes: Optional[float]

    # ── Signals ──
    flags: List[str]  # high-priority signals requiring attention
```

**Endpoint implementation** (pseudocode):

```python
@router.get("/agent-snapshot", response_model=AgentSnapshotResponse)
async def agent_snapshot() -> AgentSnapshotResponse:
    now = datetime.now(timezone.utc)

    # Telemetry
    thresholds = {"sync": 1.0, "tick": 1.0, "initiative": 4.0, "prefetch": 24.0}
    telemetry_dict = await _telemetry.to_dict(thresholds)

    # Pending initiatives (top 20 by priority)
    pending = _initiative_store.list(status=["pending"], limit=20)

    # Recently completed (top 10 by priority — store orders by priority DESC)
    # NOTE: InitiativeStore has no completed_after filter. If true recency
    # by completion time is needed, filter the result in Python by completed_at.
    recent = _initiative_store.list(
        status=["completed"],
        limit=10,
    )

    # Failed initiatives
    failed = _initiative_store.list(status=["failed"], limit=10)

    # Compute last tick age
    last_tick = _telemetry.last_tick_at
    tick_age = (now - last_tick).total_seconds() / 60 if last_tick else None

    # Flags: high-signal items the agent should know about
    flags = []
    if telemetry_dict.get("silence_hours", {}).get("initiative", 0) > 4:
        flags.append("long_initiative_silence")
    if failed:
        flags.append("failed_initiatives")
    if pending and any(i.priority > 0.8 for i in pending):
        flags.append("high_priority_pending")
    if tick_age and tick_age > 30:
        flags.append("stale_autonomy_loop")

    return AgentSnapshotResponse(
        timestamp=now.isoformat(),
        telemetry=telemetry_dict,
        pending_initiatives=[...],  # map StoredInitiative fields to AgentSnapshotInitiative
        pending_count=len(pending),
        assigned_count=_initiative_store.count(status=["assigned"]),
        failed_count=len(failed),
        recently_completed=[...],  # map StoredInitiative fields to AgentSnapshotInitiative
        autonomy_mode=_autonomy_loop.config.mode.value if _autonomy_loop else "unknown",
        autonomy_running=_autonomy_loop.is_running if _autonomy_loop else False,
        last_tick_age_minutes=tick_age,
        flags=flags,
    )
```

---

### 1.3 New Endpoint: `POST /v1/host/agent-snapshot/record-outreach`

**File:** `colony_sidecar/api/routers/host.py`

the agent calls this after proactively messaging the owner. Colony records the timestamp for future snapshot queries.

**Request:**
```python
class RecordOutreachRequest(BaseModel):
    agent_id: str = "test-agent"  # identifier for the agent instance
    channel: str = "whatsapp"  # platform used
    reason: Optional[str] = None  # why the agent decided to reach out
```

**Response:**
```python
class RecordOutreachResponse(BaseModel):
    recorded_at: str
    last_the-agent_outreach_at: str
```

**Implementation:**
```python
@router.post("/agent-snapshot/record-outreach", response_model=RecordOutreachResponse)
async def record_outreach(body: RecordOutreachRequest) -> RecordOutreachResponse:
    now = datetime.now(timezone.utc)
    await _telemetry.touch("last_the-agent_outreach_at")
    logger.info(
        "the agent outreach recorded: agent=%s channel=%s reason=%s",
        body.agent_id, body.channel, body.reason,
    )
    return RecordOutreachResponse(
        recorded_at=now.isoformat(),
        last_the-agent_outreach_at=now.isoformat(),
    )
```

---

### 1.4 Files Modified (Colony Side)

| File | Action | Lines |
|------|--------|-------|
| `colony_sidecar/telemetry.py` | Add `last_the-agent_outreach_at` field + update `to_dict()` | ~+3 |
| `colony_sidecar/api/schemas/host.py` | Add `AgentSnapshotResponse`, `AgentSnapshotInitiative`, `RecordOutreachRequest`, `RecordOutreachResponse` | ~+60 |
| `colony_sidecar/api/routers/host.py` | Add `GET /agent-snapshot` and `POST /agent-snapshot/record-outreach` handlers | ~+100 |

---

## Phase 2: the agent Heartbeat Trigger

### 2.1 Hermes Cron Job

the agent needs a periodic trigger to evaluate Colony state even when the owner is silent.

**Approach:** Hermes native cron job (no Colony changes needed).

The owner configures via Hermes CLI:
```bash
hermes cron create \
  --name "test-agent-colony-heartbeat" \
  --schedule "*/20 * * * *" \
  --prompt "Query Colony agent snapshot at http://127.0.0.1:7777/v1/host/agent-snapshot. Evaluate whether to proactively message the owner based on: pending initiatives, failed items, silence duration, and last outreach time. If you decide to reach out, compose a natural message and use the send_message tool. After sending, record the outreach via POST to /v1/host/agent-snapshot/record-outreach. If you decide not to reach out, do nothing." \
  --toolsets web,terminal
```

**Why 20 minutes?**
- Frequent enough to catch urgent items
- Not so frequent as to be noisy
- Aligns with Colony's default tick interval (5 min) × 4

**Why a Hermes cron job?**
- No upstream harness changes needed
- the agent runs in a fresh session with full tool access
- Colony pushes nothing — the agent pulls everything
- Respects the owner's existing cron infrastructure

---

### 2.2 Colony-Driven Lightweight Signal (Optional Future)

If the cron approach has too much latency, Colony could emit a lightweight `AGENT_CONTEXT_AVAILABLE` signal via webhook. This does **not** contain a message — just a timestamp and urgency hint. The Hermes webhook handler stores it, and the agent checks on its next turn.

**Deferred to future** — start with cron-only, add signal if needed.

---

## Phase 3: the agent Decision Logic

### 3.1 Decision Rubric

When the agent evaluates the snapshot, it applies these heuristics:

| Condition | Action |
|-----------|--------|
| `high_priority_pending` flag + silence > 2h + no outreach in 4h | Reach out with initiative summary |
| `failed_initiatives` flag + any destructive action failed | Reach out with failure summary |
| `stale_autonomy_loop` flag (no tick for 30+ min) | Alert owner that Colony autonomy loop may be stuck |
| `long_initiative_silence` (4h+) + no outreach in 6h | Optional soft check-in (the agent's discretion) |
| Owner in active session (within last 10 min) | Defer — don't interrupt |
| Quiet hours (22:00–07:00 owner time) | Defer unless `high_priority_pending` |
| Reached out < 2h ago | Suppress — avoid spam |
| Nothing pending, no flags | Stay silent |

### 3.2 Message Composition Guidelines

When the agent decides to reach out:
- **Never use hardcoded templates** — compose naturally based on context
- **Lead with the most important item** — highest priority initiative or failed action
- **Offer specific help** — not "anything I can help with?" but "The PR review for X is still pending — want me to check it?"
- **Respect tone** — match the owner's usual communication style
- **After sending, record outreach** — `POST /v1/host/agent-snapshot/record-outreach`

---

## Phase 4: State Persistence

All temporal state lives in Colony:

| State | Location | Updated By |
|-------|----------|------------|
| `last_the-agent_outreach_at` | `TelemetryStore` (in-memory, no persistence needed for MVP) | `POST /agent-snapshot/record-outreach` |
| Initiative statuses | `InitiativeStore` (SQLite) | Autonomy loop + the agent actions |
| Telemetry timestamps | `TelemetryStore` | Autonomy loop |

**Note:** `TelemetryStore` is currently in-memory only. If sidecar restarts, `last_the-agent_outreach_at` resets to `None`. This is acceptable for MVP — the agent will simply be more conservative after a restart. If persistence is needed later, extend `TelemetryStore` with JSON file backing (similar to the old `autonomy_checkin.json` pattern).

---

## Testing Strategy

### Unit Tests

1. **TelemetryStore** — verify `touch("last_the-agent_outreach_at")` works
2. **Agent snapshot endpoint** — verify response structure, flag computation, empty state
3. **Record outreach endpoint** — verify timestamp updates, response format

### Integration Tests

1. **End-to-end:** Generate a pending initiative → call snapshot → verify pending count = 1 and flag includes `high_priority_pending`
2. **Outreach flow:** Call record-outreach → verify subsequent snapshot returns updated `last_the-agent_outreach_at`
3. **Autonomy loop integration:** Run a tick → verify snapshot reflects new `last_tick_at`

### Manual Validation

1. Start sidecar → `curl` snapshot → verify 200 and schema
2. Create a test initiative → `curl` snapshot → verify it appears in pending
3. Call record-outreach → verify timestamp in next snapshot

---

## Migration & Backward Compatibility

- **No breaking changes** — new endpoints only
- **Old env vars removed** (`COLONY_OWNER_CHECK_IN_*`) stay removed; no conflicts
- **TelemetryStore field addition** is backward-compatible (new optional field)
- **Hermes plugin** needs no changes for Phase 1; cron job is user-configured

---

## Rollout Plan

1. **Phase 1** (this spec): Implement Colony endpoints + telemetry extension
2. **Phase 2** (post-merge): The owner configures Hermes cron job
3. **Phase 3** (post-deployment): Tune decision rubric based on real behavior
4. **Phase 4** (optional): Add Colony-driven lightweight signal if cron latency is unacceptable

---

## Open Questions

1. **Telemetry persistence:** Is in-memory `last_the-agent_outreach_at` sufficient, or do we need JSON file backing?
2. **Cron frequency:** Is 20 minutes right? Should it be configurable per-owner?
3. **Decision rubric:** Should any of the thresholds be Colony-configurable (env vars) or the agent-configurable?
4. **Multi-owner:** If Colony ever supports multiple owners, how does `last_the-agent_outreach_at` scope per owner?

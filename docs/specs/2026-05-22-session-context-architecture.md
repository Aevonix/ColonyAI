# Session Context Architecture

**Status:** Draft — awaiting review  
**Date:** 2026-05-22  
**Target Release:** v0.14.0  

---

## 1. Problem Statement

Colony generates initiatives and pushes them directly to the Hermes webhook via `ProactiveDeliveryBridge.push_initiative()`. This spawns an isolated agent session that:

- Has no memory of recent conversations with the owner
- Cannot see whether the owner was already informed about the same issue recently
- Cannot see whether another session is actively working on the problem
- Is instructed by the webhook prompt to "send to user immediately"

This produces fragmented, out-of-context interruptions.

### 1.1 Current Flow

```
Colony loop detects issue
  → Creates initiative in InitiativeStore
  → push_initiative() → POST Hermes webhook
    → New isolated agent session spawns
      → Agent reads initiative payload only
      → Agent sends message to owner (no context)
```

### 1.2 Target Flow

```
Colony loop detects issue
  → Creates initiative in InitiativeStore
  → Stops. No webhook push.

[Agent session starts — any trigger]
  → GET /v1/host/context-digest
    → Pending initiatives
    → Recent session summaries
    → System health flags
    → Last outreach timestamp
  → Agent evaluates: "Given full context, is this worth mentioning?"
    → Yes → ONE concise message
    → No → Silence

[When agent session ends]
  → POST /v1/host/session-report
    → Summary of what was discussed
    → What was resolved / what's pending
    → Whether owner was notified about anything
```

---

## 2. Design Principles

1. **Colony exposes state; the agent decides.** Colony never messages the owner directly.
2. **Every agent session is context-aware.** Boot → fetch digest → act with full history.
3. **Session summaries persist cross-session.** The agent writes back; future sessions read back.
4. **Minimal Colony surface area.** No core harness changes. Only existing extension points.
5. **Config-gated, not deleted.** Disable `push_initiative()` via flag so it can be re-enabled if needed.

---

## 3. Architecture Overview

### 3.1 New Components

| Component | Location | Purpose |
|---|---|---|
| `SessionReportStore` | `colony_sidecar/sessions/reports.py` | Lightweight store for agent session summaries |
| `POST /v1/host/session-report` | `api/routers/host.py` | Agent writes a summary at session end |
| `GET /v1/host/context-digest` | `api/routers/host.py` | Agent fetches full context at session start |
| `proactive_delivery_enabled` | `autonomy/config.py` | Gate for `push_initiative()` calls |

### 3.2 Data Flow

```
├─────────────────┐      ├──────────────────┐
│  Agent Session   │      │   Colony Sidecar  │
│   (Hermes)      │◄────►│                   │
└─────────────────┘      │  ├─────────────┐  │
         │               │  │ Initiative  │  │
         │ GET /context-digest│    Store    │  │
         │───────────────►│  └─────────────┘  │
         │               │  ├─────────────┐  │
         │               │  │  Session    │  │
         │               │  │   Report    │  │
         │               │  │   Store     │  │
         │               │  └─────────────┘  │
         │               │  ├─────────────┐  │
         │               │  │  Telemetry  │  │
         │               │  │    Store    │  │
         │               │  └─────────────┘  │
         │               └──────────────────┘
         │
         │ POST /session-report
         │──────────────────────────────────►
```

---

## 4. API Specification

### 4.1 `POST /v1/host/session-report`

The agent calls this when a session ends (or periodically during long sessions).

**Request Body:**
```json
{
  "session_id": "hermes-session-uuid",
  "contact_id": "whatsapp:+1XXXXXX",
  "started_at": "2026-05-22T15:00:00Z",
  "ended_at": "2026-05-22T15:30:00Z",
  "summary": "Resolved authentication issues in the polling script, cleaned up redundant scheduled jobs, and discussed architecture for proactive agent behavior. Awaiting specification approval before implementation.",
  "topics": ["auth-fix", "job-cleanup", "architecture-review"],
  "resolutions": ["Fixed auth header in polling script", "Removed redundant scheduled jobs"],
  "pending": ["Write spec for session context architecture"],
  "notified_user": false,
  "metadata": {
    "message_count": 24,
    "tools_used": ["patch", "cronjob", "terminal"]
  }
}
```

**Response:**
```json
{
  "stored": true,
  "report_id": "sr-uuid"
}
```

### 4.2 `GET /v1/host/context-digest`

The agent calls this at every session start.

**Query Parameters:**
- `contact_id` (str, optional) — filter to specific contact's context
- `hours` (int, default=24) — how far back to look for session reports
- `initiative_limit` (int, default=10) — max pending initiatives to include

**Response:**
```json
{
  "generated_at": "2026-05-22T16:00:00Z",
  "contact_id": "whatsapp:+1XXXXXX",
  "session_reports": [
    {
      "report_id": "sr-uuid",
      "started_at": "2026-05-22T15:00:00Z",
      "ended_at": "2026-05-22T15:30:00Z",
      "summary": "Resolved authentication issues in the polling script...",
      "topics": ["auth-fix", "job-cleanup", "architecture-review"],
      "resolutions": ["Fixed auth header in polling script"],
      "pending": ["Write spec for session context architecture"],
      "notified_user": false
    }
  ],
  "pending_initiatives": [
    {
      "id": "init-uuid",
      "initiative_type": "follow_up",
      "title": "Follow up on session reset issue",
      "priority": 82,
      "status": "pending",
      "created_at": "2026-05-22T14:00:00Z"
    }
  ],
  "system_state": {
    "autonomy_running": true,
    "mode": "proactive",
    "last_tick_age_minutes": 2.3,
    "silence_hours": {
      "sync": false,
      "tick": false,
      "initiative": false,
      "prefetch": false
    }
  },
  "last_outreach": {
    "at": "2026-05-22T14:00:00Z",
    "reason": "high_priority_initiative"
  }
}
```

---

## 5. Data Models

### 5.1 `SessionReport` (dataclass)

```python
@dataclass
class SessionReport:
    report_id: str
    session_id: str
    contact_id: str
    started_at: datetime
    ended_at: Optional[datetime]
    summary: str
    topics: List[str]
    resolutions: List[str]
    pending: List[str]
    notified_user: bool
    metadata: Dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

### 5.2 `SessionReportStore`

```python
class SessionReportStore:
    """Lightweight in-memory store for recent agent session summaries.
    
    Keeps last N reports per contact_id. Older reports are evicted.
    This is NOT long-term memory — it is a short-term cross-session bridge.
    For long-term memory, use the graph store.
    """
    
    def __init__(self, max_per_contact: int = 20) -> None:
        self._reports: Dict[str, List[SessionReport]] = {}
        self._max_per_contact = max_per_contact
    
    async def add_report(self, report: SessionReport) -> str:
        if report.contact_id not in self._reports:
            self._reports[report.contact_id] = []
        self._reports[report.contact_id].append(report)
        # Evict oldest if over limit
        if len(self._reports[report.contact_id]) > self._max_per_contact:
            self._reports[report.contact_id] = self._reports[report.contact_id][-self._max_per_contact:]
        return report.report_id
    
    async def get_recent(
        self,
        contact_id: str,
        hours: int = 24,
        limit: int = 10,
    ) -> List[SessionReport]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        reports = self._reports.get(contact_id, [])
        recent = [r for r in reports if r.created_at >= cutoff]
        return recent[-limit:]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "contacts": len(self._reports),
            "total_reports": sum(len(v) for v in self._reports.values()),
        }
```

### 5.3 Pydantic Schemas (`api/schemas/host.py`)

```python
class SessionReportRequest(BaseModel):
    session_id: str
    contact_id: str
    started_at: str  # ISO datetime
    ended_at: Optional[str] = None
    summary: str
    topics: List[str] = Field(default_factory=list)
    resolutions: List[str] = Field(default_factory=list)
    pending: List[str] = Field(default_factory=list)
    notified_user: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

class SessionReportResponse(BaseModel):
    stored: bool
    report_id: str

class ContextDigestSessionReport(BaseModel):
    report_id: str
    started_at: str
    ended_at: Optional[str]
    summary: str
    topics: List[str]
    resolutions: List[str]
    pending: List[str]
    notified_user: bool

class ContextDigestResponse(BaseModel):
    generated_at: str
    contact_id: Optional[str]
    session_reports: List[ContextDigestSessionReport]
    pending_initiatives: List[AgentSnapshotInitiative]
    system_state: AgentSnapshotSystemState
    last_outreach: Dict[str, Any]
```

---

## 6. Colony Code Changes

### 6.1 Add `proactive_delivery_enabled` Config Flag

**File:** `sidecar/colony_sidecar/autonomy/config.py`

Add to `AutonomyConfig` dataclass (after `owner_contact_id`):
```python
    # — Proactive delivery (push initiatives to webhook) —
    proactive_delivery_enabled: bool = False
```

Add to `from_colony_config()` constructor call:
```python
            owner_contact_id=_get("owner_contact_id", defaults.owner_contact_id),
            proactive_delivery_enabled=bool(_get("proactive_delivery_enabled", defaults.proactive_delivery_enabled)),
```

Add to `from_env()` constructor call:
```python
            owner_contact_id=os.environ.get(
                "COLONY_OWNER_CONTACT_ID",
                defaults.owner_contact_id,
            ),
            proactive_delivery_enabled=_bool(
                "COLONY_PROACTIVE_DELIVERY_ENABLED",
                defaults.proactive_delivery_enabled,
            ),
```

Add to `from_env()` docstring:
```
            COLONY_PROACTIVE_DELIVERY_ENABLED
```

### 6.2 Gate `push_initiative()` Calls

**File:** `sidecar/colony_sidecar/autonomy/loop.py`

There are 3 call sites. At each site, wrap with `getattr` for backward compatibility:

**Site 1 (~line 632):**
```python
                if getattr(self.config, "proactive_delivery_enabled", False):
                    ok = await delivery.push_initiative(payload)
                    if ok:
                        self.stats.actions_executed += 1
                        self.stats.actions_this_hour += 1
                        logger.info("Pushed initiative: %s", payload["id"])
                        try:
                            from colony_sidecar.api.routers.host import _telemetry
                            if _telemetry is not None:
                                await _telemetry.touch("last_initiative_at")
                        except Exception:
                            pass
                else:
                    logger.debug("Proactive delivery disabled — initiative stored for agent polling")
```

**Site 2 (~line 731):**
```python
                if delivery and hasattr(delivery, "push_initiative"):
                    if getattr(self.config, "proactive_delivery_enabled", False):
                        await delivery.push_initiative({
                            "id": initiative_id,
                            "type": "agent_action",
                            "priority": getattr(initiative, "priority", 0.5),
                            "title": f"Approval required: {getattr(initiative, 'description', '')[:60]}",
                            "description": getattr(initiative, "description", ""),
                            "rationale": getattr(initiative, "rationale", ""),
                            "suggested_action": "colony_approve_initiative",
                            "entity_id": getattr(initiative, "entity_id", None),
                            "channel_hint": "dm",
                            "context": {"job_id": job_id, "action_hint": action_hint},
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        })
                    else:
                        logger.debug("Proactive delivery disabled — approval request stored for agent polling")
```

**Site 3 (~line 1257):**
```python
                    try:
                        if getattr(self.config, "proactive_delivery_enabled", False):
                            ok = await delivery.push_initiative(payload)
                            if ok:
                                repushed += 1
                        else:
                            logger.debug("Proactive delivery disabled — skipping startup re-push")
                    except Exception as exc:
                        logger.debug("Failed to re-push initiative %s: %s", initiative.id, exc)
```

**Important:** The initiative is ALREADY stored in `InitiativeStore` before these call sites. Disabling push does not lose it.

### 6.3 Create `SessionReportStore`

**File:** `sidecar/colony_sidecar/sessions/reports.py` (new)

Full implementation as specified in §5.2. In-memory with per-contact eviction. No external DB dependency.

### 6.4 Register Store in Server Lifespan

**File:** `sidecar/colony_sidecar/server.py`

**Import block** (add after `set_telemetry`):
```python
    set_session_report_store,
```

**Instantiation** (after `set_telemetry(telemetry)` ~line 922):
```python
    from colony_sidecar.sessions.reports import SessionReportStore
    session_report_store = SessionReportStore()
    set_session_report_store(session_report_store)
    logger.info("SessionReportStore initialized")
```

**Shutdown cleanup** (in the shutdown block ~line 999):
```python
    set_session_store(None)
    set_session_report_store(None)
    set_task_queue(None)
```

### 6.5 Add Endpoints

**File:** `sidecar/colony_sidecar/api/routers/host.py`

**Global and setter** (after `_task_queue` block ~line 3364):
```python
_session_report_store = None

def set_session_report_store(store) -> None:
    global _session_report_store
    _session_report_store = store
```

**Endpoints** (after `record_outreach` ~line 5345):

Add import at module level:
```python
from colony_sidecar.sessions.reports import SessionReport
```

```python
@router.post("/session-report", response_model=SessionReportResponse)
async def session_report(body: SessionReportRequest) -> SessionReportResponse:
    """Store a session summary from the agent for future context retrieval."""
    if _session_report_store is None:
        raise HTTPException(status_code=501, detail="Session report store not initialized")
    
    from datetime import datetime
    report = SessionReport(
        report_id=str(uuid.uuid4()),
        session_id=body.session_id,
        contact_id=body.contact_id,
        started_at=datetime.fromisoformat(body.started_at.replace("Z", "+00:00")),
        ended_at=datetime.fromisoformat(body.ended_at.replace("Z", "+00:00")) if body.ended_at else None,
        summary=body.summary,
        topics=body.topics,
        resolutions=body.resolutions,
        pending=body.pending,
        notified_user=body.notified_user,
        metadata=body.metadata,
    )
    await _session_report_store.add_report(report)
    return SessionReportResponse(stored=True, report_id=report.report_id)
```

```python
@router.get("/context-digest", response_model=ContextDigestResponse)
async def context_digest(
    contact_id: Optional[str] = None,
    hours: int = 24,
    initiative_limit: int = 10,
) -> ContextDigestResponse:
    """Return a comprehensive context digest for agent session boot.
    
    Combines recent session reports, pending initiatives, system state,
    and outreach history into a single response.
    """
    now = datetime.now(timezone.utc)
    
    # Session reports
    session_reports = []
    if _session_report_store is not None and contact_id:
        reports = await _session_report_store.get_recent(contact_id, hours=hours, limit=10)
        session_reports = [
            ContextDigestSessionReport(
                report_id=r.report_id,
                started_at=r.started_at.isoformat() if r.started_at else "",
                ended_at=r.ended_at.isoformat() if r.ended_at else None,
                summary=r.summary,
                topics=r.topics,
                resolutions=r.resolutions,
                pending=r.pending,
                notified_user=r.notified_user,
            )
            for r in reports
        ]
    
    # Pending initiatives (reuse agent-snapshot logic)
    pending = []
    if _initiative_store is not None:
        pending = _initiative_store.list(status=["pending"], limit=initiative_limit)
    
    # System state (reuse agent-snapshot logic)
    thresholds = {"sync": 1.0, "tick": 1.0, "initiative": 4.0, "prefetch": 24.0}
    telemetry_dict = await _telemetry.to_dict(thresholds) if _telemetry else {}
    
    tick_age = None
    if _telemetry is not None and _telemetry.last_tick_at is not None:
        tick_age = (now - _telemetry.last_tick_at).total_seconds() / 60
    
    silence_flags = telemetry_dict.get("silence_hours", {})
    stale_flags = telemetry_dict.get("stale_flags", [])
    
    # Last outreach
    last_outreach = {"at": None, "reason": None}
    if _telemetry is not None and _telemetry.last_agent_outreach_at is not None:
        last_outreach = {
            "at": _telemetry.last_agent_outreach_at.isoformat(),
            "reason": None,
        }
    
    # Map initiatives (module-level helper extracted from agent-snapshot)
    def _map_initiative(i):
        return AgentSnapshotInitiative(
            id=i.id,
            type=i.type,
            description=i.description,
            priority=i.priority,
            status=i.status,
            rationale=i.rationale,
            action_hint=i.action_hint,
            entity_id=i.entity_id,
            dedup_key=i.dedup_key,
            created_at=i.created_at.isoformat() if i.created_at else "",
            expires_at=i.expires_at.isoformat() if i.expires_at else None,
            assigned_agent_id=i.assigned_agent_id,
            acknowledged_at=i.acknowledged_at.isoformat() if i.acknowledged_at else None,
            completed_at=i.completed_at.isoformat() if i.completed_at else None,
            failed_at=i.failed_at.isoformat() if i.failed_at else None,
            failed_reason=i.failed_reason,
        )
    
    system_state = AgentSnapshotSystemState(
        autonomy_running=_autonomy_loop.is_running if _autonomy_loop else False,
        mode=_autonomy_loop.config.mode.value if _autonomy_loop else "unknown",
        last_tick_age_minutes=tick_age,
        silence_hours=silence_flags,
        stale_flags=stale_flags,
    )
    
    return ContextDigestResponse(
        generated_at=now.isoformat(),
        contact_id=contact_id,
        session_reports=session_reports,
        pending_initiatives=[_map_initiative(i) for i in pending],
        system_state=system_state,
        last_outreach=last_outreach,
    )
```

**Note:** The `AgentSnapshotSystemState` schema is added to `api/schemas/host.py` as a reusable component for `ContextDigestResponse` only. `AgentSnapshotResponse` is left unchanged to avoid breaking existing clients.

```python
class AgentSnapshotSystemState(BaseModel):
    autonomy_running: bool
    mode: str
    last_tick_age_minutes: Optional[float] = None
    silence_hours: Dict[str, Any] = Field(default_factory=dict)
    stale_flags: List[str] = Field(default_factory=list)
```

---

## 7. Files Modified

| File | Change Type | Description |
|---|---|---|
| `sidecar/colony_sidecar/autonomy/config.py` | Patch | Add `proactive_delivery_enabled`; update `from_colony_config()` and `from_env()` |
| `sidecar/colony_sidecar/autonomy/loop.py` | Patch | Gate 3 `push_initiative()` calls behind `getattr(config, "proactive_delivery_enabled", False)` |
| `sidecar/colony_sidecar/sessions/reports.py` | Create | `SessionReportStore` and `SessionReport` dataclass |
| `sidecar/colony_sidecar/api/schemas/host.py` | Patch | Add `SessionReportRequest`, `SessionReportResponse`, `ContextDigestSessionReport`, `ContextDigestResponse`, `AgentSnapshotSystemState` |
| `sidecar/colony_sidecar/api/routers/host.py` | Patch | Add `_session_report_store` global, `set_session_report_store()`, `session-report` and `context-digest` endpoints |
| `sidecar/colony_sidecar/server.py` | Patch | Import `set_session_report_store`; instantiate `SessionReportStore`; wire setter; add shutdown cleanup |
| `sidecar/tests/test_session_reports.py` | Create | Unit tests for store, endpoints, schema validation |
| `docs/specs/2026-05-22-session-context-architecture.md` | Create | This spec |

---

## 8. Migration Plan

### Step 1: Deploy Config Flag (No User Impact)

1. Add `proactive_delivery_enabled: bool = False` to `AutonomyConfig`
2. Gate `push_initiative()` calls with `getattr(..., False)`
3. Deploy → Colony stops pushing to webhook
4. Initiatives accumulate in store as "pending"

### Step 2: Add Session Report Infrastructure

1. Create `SessionReportStore`
2. Add `session-report` endpoint
3. Add `context-digest` endpoint
4. Write tests
5. Deploy

### Step 3: Update Agent Behavior

1. Modify session start to fetch `context-digest`
2. Modify session end to POST `session-report`
3. Update heartbeat to use digest for outreach decisions
4. Deploy

### Step 4: Monitor and Tune

1. Verify no webhook spam
2. Verify agent sessions have full context
3. Tune heartbeat frequency and evaluation thresholds
4. Archive old `push_initiative` code if stable after 1 week

---

## 9. Rollback Plan

If issues arise:

1. Set `proactive_delivery_enabled: bool = True` in config
2. Colony resumes `push_initiative()` — initiatives flow to webhook immediately
3. Remove any agent-side cron jobs that were created
4. No data loss — initiatives remain in store regardless

---

## 10. Verification Checklist

- [ ] `push_initiative()` calls are gated by config flag with `getattr(..., False)` default
- [ ] Setting flag `True` restores original webhook behavior
- [ ] `session-report` endpoint accepts valid payloads and stores them
- [ ] `context-digest` endpoint returns correct structure with all sections
- [ ] `context-digest` returns empty `session_reports` when queried for unknown contact
- [ ] Initiatives continue to be created and stored when delivery is disabled
- [ ] No 500 errors when `SessionReportStore` is queried for contact with no history
- [ ] Existing tests pass (regression check)
- [ ] New tests cover store eviction, endpoint validation, schema round-trip
- [ ] `AgentSnapshotResponse` still works correctly (unchanged; `AgentSnapshotSystemState` is only used by `ContextDigestResponse`)
- [ ] Shutdown correctly clears `_session_report_store`

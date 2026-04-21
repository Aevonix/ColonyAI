# Commitment Tracking — Build Spec

## Overview

Commitment tracking gives Colony a reliable memory for promises. When an agent says "I'll check on that tomorrow" or "Remind me next week," Colony records it, tracks it, and surfaces it when relevant. This is the single highest-trust feature an agent can have: following through.

## Scope

**In scope (v0.1):**
- Commitment store (SQLite)
- REST API (create, list, update, delete)
- Autonomy loop integration (overdue detection)
- Context assembly injection (pending commitments)
- Event emission (commitment events)
- Plugin-side client + cache invalidation
- Unit + integration tests

**Out of scope:**
- LLM-based commitment extraction from conversations (future)
- Recurring commitments (future)
- Commitment delegation between Colonies (federation feature)
- Commitment templates or presets

## Data Model

### Commitment

| Field | Type | Description |
|---|---|---|
| id | str (UUID) | Unique commitment identifier |
| person_id | str | Contact this commitment relates to |
| description | str | Natural language description of the commitment |
| made_at | datetime (UTC) | When the commitment was made |
| due_at | datetime (UTC, optional) | When it should be fulfilled |
| fulfilled_at | datetime (UTC, optional) | When it was actually fulfilled |
| status | enum | `pending`, `fulfilled`, `overdue`, `cancelled` |
| source_context | str (optional) | Session/conversation context where commitment was made |
| source_type | str | `manual`, `autonomy`, `conversation` (for future LLM extraction) |
| priority | int (0-100) | 0 = low, 100 = critical. Default 50 |
| metadata | JSON (optional) | Arbitrary key-value pairs |

### Status Transitions

```
pending → fulfilled  (PATCH with fulfilled_at)
pending → overdue    (autonomy loop detects past due_at)
pending → cancelled  (PATCH with status=cancelled)
overdue → fulfilled  (PATCH with fulfilled_at)
overdue → cancelled  (PATCH with status=cancelled)
```

No other transitions allowed. `fulfilled` and `cancelled` are terminal states.

## Storage

SQLite database at `{COLONY_STATE_DIR}/colony-commitments.db`.

### Schema

```sql
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    description TEXT NOT NULL,
    made_at TEXT NOT NULL,
    due_at TEXT,
    fulfilled_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    source_context TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    priority INTEGER NOT NULL DEFAULT 50,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_commitments_person ON commitments(person_id);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_commitments_due ON commitments(due_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_commitments_person_status ON commitments(person_id, status);
```

### Access Patterns

1. List pending commitments for a person — indexed on (person_id, status)
2. Find overdue commitments — indexed on (due_at) WHERE status = 'pending'
3. Get single commitment by id — primary key lookup
4. List all commitments (paginated) — table scan with status filter

## API

### Endpoints

#### POST /v1/host/commitments

Create a new commitment.

**Request:**
```json
{
  "person_id": "marc",
  "description": "Check on the DGX cluster status by Friday",
  "due_at": "2026-04-25T18:00:00Z",
  "priority": 70,
  "source_type": "manual",
  "source_context": "session:abc123",
  "metadata": {"topic": "infrastructure"}
}
```

**Response:** 201 Created
```json
{
  "id": "cmt-uuid-here",
  "person_id": "marc",
  "description": "Check on the DGX cluster status by Friday",
  "made_at": "2026-04-21T17:14:00Z",
  "due_at": "2026-04-25T18:00:00Z",
  "fulfilled_at": null,
  "status": "pending",
  "source_type": "manual",
  "source_context": "session:abc123",
  "priority": 70,
  "metadata": {"topic": "infrastructure"}
}
```

Validation:
- `description` required, non-empty, max 1000 chars
- `person_id` required, non-empty
- `due_at` optional, must be in the future if provided
- `priority` optional, clamped to 0-100
- `source_type` optional, one of `manual`, `autonomy`, `conversation`

#### GET /v1/host/commitments

List commitments with optional filters.

**Query parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| person_id | str | null | Filter by contact |
| status | str | null | Filter by status (comma-separated for multiple) |
| overdue_only | bool | false | Shortcut for status=overdue |
| limit | int | 50 | Max results (1-200) |
| offset | int | 0 | Pagination offset |

**Response:**
```json
{
  "commitments": [...],
  "total": 12,
  "limit": 50,
  "offset": 0
}
```

#### GET /v1/host/commitments/{commitment_id}

Get a single commitment.

**Response:** 200 OK with commitment object, or 404.

#### PATCH /v1/host/commitments/{commitment_id}

Update a commitment.

**Request:**
```json
{
  "status": "fulfilled",
  "fulfilled_at": "2026-04-23T10:00:00Z"
}
```

Allowed fields for update:
- `status` — must follow valid transition rules
- `fulfilled_at` — auto-set to now if status changed to `fulfilled` and not provided
- `description` — can edit text
- `due_at` — can reschedule
- `priority` — can reprioritize
- `metadata` — merge with existing

Setting `status` to `fulfilled` without `fulfilled_at` auto-fills with current UTC time.

#### DELETE /v1/host/commitments/{commitment_id}

Delete a commitment. Only allowed for `fulfilled` or `cancelled` commitments (terminal states). Pending/overdue commitments must be cancelled first.

**Response:** 204 No Content, or 409 Conflict if commitment is still active.

### Pydantic Schemas

Add to `sidecar/colony_sidecar/api/schemas/host.py`:

```python
class CommitmentCreateRequest(BaseModel):
    person_id: str
    description: str = Field(..., min_length=1, max_length=1000)
    due_at: Optional[datetime] = None
    priority: int = Field(default=50, ge=0, le=100)
    source_type: Literal["manual", "autonomy", "conversation"] = "manual"
    source_context: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class CommitmentUpdateRequest(BaseModel):
    status: Optional[Literal["fulfilled", "cancelled"]] = None
    fulfilled_at: Optional[datetime] = None
    description: Optional[str] = Field(None, min_length=1, max_length=1000)
    due_at: Optional[datetime] = None
    priority: Optional[int] = Field(None, ge=0, le=100)
    metadata: Optional[Dict[str, Any]] = None

class CommitmentResponse(BaseModel):
    id: str
    person_id: str
    description: str
    made_at: datetime
    due_at: Optional[datetime] = None
    fulfilled_at: Optional[datetime] = None
    status: str
    source_type: str
    source_context: Optional[str] = None
    priority: int
    metadata: Optional[Dict[str, Any]] = None

class CommitmentListResponse(BaseModel):
    commitments: List[CommitmentResponse] = []
    total: int
    limit: int
    offset: int
```

## Store Implementation

**File:** `sidecar/colony_sidecar/commitments/store.py`

```python
class CommitmentStore:
    """SQLite-backed commitment store."""
    
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()
    
    def _init_db(self) -> None: ...
    def create(self, ...) -> CommitmentResponse: ...
    def get(self, commitment_id: str) -> Optional[CommitmentResponse]: ...
    def list(self, person_id=None, status=None, overdue_only=False, limit=50, offset=0) -> CommitmentListResponse: ...
    def update(self, commitment_id: str, ...) -> Optional[CommitmentResponse]: ...
    def delete(self, commitment_id: str) -> bool: ...
    def get_overdue(self) -> List[CommitmentResponse]: ...
    def get_pending_for_person(self, person_id: str) -> List[CommitmentResponse]: ...
```

Thread safety: use `sqlite3.connect(..., check_same_thread=False)` with a threading lock, same pattern as the goals store.

## Autonomy Integration

### Overdue Detection

Add a new autonomy condition type: `commitment_overdue`.

The autonomy loop already runs periodic checks. Add:

```python
async def _check_commitment_overdue(params: dict) -> dict:
    """Check for commitments that are past their due_at and still pending."""
    store = _commitment_store  # set during server init
    if store is None:
        return {"condition_met": False}
    
    overdue = store.get_overdue()
    if not overdue:
        return {"condition_met": False, "overdue_count": 0}
    
    # Mark pending → overdue
    for c in overdue:
        store.update(c.id, status="overdue")
        emit("commitment.overdue", {"commitment_id": c.id, "person_id": c.person_id, "description": c.description})
    
    return {
        "condition_met": True,
        "overdue_count": len(overdue),
        "overdue_commitments": [{"id": c.id, "description": c.description, "person_id": c.person_id} for c in overdue[:5]]
    }
```

### Autonomy Initiative

When an overdue commitment is detected, the autonomy loop can:
1. Emit a `commitment.overdue` event (WS push to harness)
2. Create a proactive delivery suggesting follow-through
3. Surface in the next enriched context for the relevant contact

### Scheduled Checks

Default: check every 30 minutes. Configurable via `COLONY_COMMITMENT_CHECK_INTERVAL_MINUTES` (default 30). The check is cheap (single indexed query).

## Context Assembly Integration

In the `enriched_context` endpoint, after existing sections:

```python
# Pending commitments
if _commitment_store is not None and contact_id and features.get("commitments", True):
    pending = _commitment_store.get_pending_for_person(contact_id)
    if pending:
        body_text = "\n".join(
            f"- {c.description} (due {c.due_at.strftime('%b %d') if c.due_at else 'no deadline'}, priority {c.priority})"
            for c in pending[:5]
        )
        sections.append(ContextSection(
            id="colony-commitments",
            title="Pending Commitments",
            body=body_text,
            priority=72,  # Between goals (75) and insights (65)
        ))
```

Add `commitments` to `EnrichedContextRequest.features` (default True).

## Event Types

New event types emitted through the broadcaster:

| Event Type | When | Payload |
|---|---|---|
| `commitment.created` | New commitment recorded | commitment_id, person_id, description |
| `commitment.fulfilled` | Marked as fulfilled | commitment_id, person_id |
| `commitment.overdue` | Past due_at, still pending | commitment_id, person_id, description |
| `commitment.cancelled` | Cancelled | commitment_id, person_id |

Add to `HostEventType` in TypeScript types.

## Plugin-Side Changes

### sidecar-client.ts

Add methods:

```typescript
createCommitment(body: CommitmentCreateRequest): Promise<CommitmentResponse>
listCommitments(params?: CommitmentListParams): Promise<CommitmentListResponse>
getCommitment(id: string): Promise<CommitmentResponse>
updateCommitment(id: string, body: CommitmentUpdateRequest): Promise<CommitmentResponse>
deleteCommitment(id: string): Promise<void>
```

### types.ts

Add `CommitmentCreateRequest`, `CommitmentUpdateRequest`, `CommitmentResponse`, `CommitmentListResponse` interfaces.

Add event types to `HostEventType`: `commitment.created`, `commitment.fulfilled`, `commitment.overdue`, `commitment.cancelled`.

### config.ts

Add optional config:

```typescript
commitmentsEnabled: z.boolean().default(true),
```

### context-cache.ts

Add `"commitments"` to `CacheChannel` type.

### event-handlers.ts

Handle commitment events in `dispatchHostEvent`:
- `commitment.overdue` → invalidate commitments cache + log warning
- `commitment.created` → invalidate commitments cache
- `commitment.fulfilled` → invalidate commitments cache

## Subsystem Registration

Register with `SubsystemRegistry` during server init:

```python
commitment_store = CommitmentStore(state_dir / "colony-commitments.db")
registry.register("commitments", commitment_store)
```

Add to capabilities list in health endpoint.

## Server Wiring

In `server.py` `create_app()`:

1. Create `CommitmentStore` instance
2. `set_commitment_store(store)` on host router module
3. Add to lifecycle (close DB on shutdown)
4. Register with subsystem registry

## File Manifest

New files:
- `sidecar/colony_sidecar/commitments/__init__.py`
- `sidecar/colony_sidecar/commitments/store.py`
- `sidecar/tests/test_commitments.py`

Modified files:
- `sidecar/colony_sidecar/api/routers/host.py` — new endpoints + context assembly + autonomy check
- `sidecar/colony_sidecar/api/schemas/host.py` — new request/response schemas
- `sidecar/colony_sidecar/server.py` — wire CommitmentStore
- `sidecar/colony_sidecar/autonomy/condition_worker.py` — add commitment_overdue check
- `sidecar/colony_sidecar/events/types.py` — commitment event dataclasses (optional)
- `src/sidecar-client.ts` — new API methods
- `src/types.ts` — new interfaces + event types
- `src/config.ts` — commitmentsEnabled flag
- `src/context-cache.ts` — "commitments" channel
- `src/event-handlers.ts` — handle commitment events
- `src/plugin.ts` — pass commitmentsEnabled to features

## Tests

### Unit Tests (test_commitments.py)

1. **Store CRUD**
   - Create commitment → returns full object with defaults
   - Create with all fields → preserves everything
   - Create with past due_at → rejected
   - Get by id → returns commitment
   - Get nonexistent id → returns None
   - List all → returns all commitments
   - List by person_id → filtered
   - List by status → filtered
   - List with multiple statuses → union
   - List overdue_only → only overdue
   - Pagination (limit/offset) → correct slice + total
   - Update status to fulfilled → auto-sets fulfilled_at
   - Update description → changed
   - Update status with invalid transition → rejected
   - Delete fulfilled commitment → succeeds
   - Delete pending commitment → rejected (409 equivalent)
   - get_overdue → returns commitments past due_at with status pending
   - get_pending_for_person → filtered by person_id + status pending

2. **API Endpoints**
   - POST /commitments → 201 + correct body
   - POST /commitments with empty description → 422
   - GET /commitments → list with pagination
   - GET /commitments?person_id=X → filtered
   - GET /commitments/{id} → single commitment
   - GET /commitments/{bad_id} → 404
   - PATCH /commitments/{id} → updated commitment
   - PATCH /commitments/{id} status=fulfilled → auto fulfilled_at
   - DELETE /commitments/{id} fulfilled → 204
   - DELETE /commitments/{id} pending → 409

3. **Context Assembly**
   - Contact with pending commitments → section appears
   - Contact with no commitments → no section
   - Feature disabled → no section

4. **Autonomy**
   - No overdue → condition not met
   - Overdue commitment → condition met, status updated, event emitted

### Integration Tests

Run against live sidecar on Spark 2:
- Full CRUD cycle via HTTP
- Commitment appears in enriched context
- Overdue detection fires after due_at passes (mock time)

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `COLONY_COMMITMENT_CHECK_INTERVAL_MINUTES` | 30 | How often autonomy checks for overdue commitments |
| `COLONY_COMMITMENTS_ENABLED` | true | Master toggle for the subsystem |

## Dependencies

- Existing: `SubsystemRegistry`, `EventBus` (broadcaster), `AutonomyLoop`, context assembly, `ContactsStore`
- New: None (SQLite is stdlib)

## Failure Modes

| Failure | Behavior |
|---|---|
| SQLite DB corrupted | Log error, return empty results, don't crash |
| Store not initialized | Endpoints return 501, context section skipped |
| Invalid status transition | Return 422 with clear error message |
| Delete active commitment | Return 409, must cancel first |
| due_at in the past | Reject at creation with 422 |

## Future (v0.2+)

- LLM-based commitment extraction from conversation text
- Recurring commitments ("every Monday")
- Commitment templates
- Commitment aging (auto-cancel after X days overdue)
- Cross-Colony commitment delegation (federation)

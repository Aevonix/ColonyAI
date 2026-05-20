# Cognition Substrate — Build Spec

## Overview

The cognition substrate gives Colony the ability to think in the background. When conversations happen, events fire, or anomalies surface, Colony processes them asynchronously through an OpenClaw subagent — no foreground blocking, no LLM infrastructure in Colony.

The first consumer of this substrate is commitment tracking: automatically extracting promises from conversations and surfacing them when relevant. Future consumers (Theory of Mind affect updates, pattern extraction) plug into the same pipeline.

## Architecture

```
Turn sync / Signal ingest / Anomaly detected
    ↓
Colony fires cognition trigger (debounced, throttled)
    ↓
Colony calls sessions_spawn with cognition prompt + context
    ↓
OpenClaw subagent runs on configured model (async, non-blocking)
    ↓
Subagent calls Colony API endpoints (commitments, affect, etc.)
    ↓
Subagent completes → OpenClaw pushes notification
```

The cognition channel is NOT a Colony subsystem. It's an OpenClaw subagent configuration. Colony owns the trigger pipeline, the cognition prompt, and the API surface. OpenClaw owns the LLM execution, model routing, concurrency, and token budgeting.

## Build Layers

### Layer 1: Commitment Store + API

Data foundation. No LLM, no cognition. Pure CRUD.

**Store:** SQLite at `{COLONY_STATE_DIR}/colony-commitments.db`

**Data Model:**

| Field | Type | Description |
|---|---|---|
| id | str (UUID) | Unique identifier |
| person_id | str | Contact this commitment relates to |
| description | str | Natural language description (1-1000 chars) |
| made_at | datetime (UTC) | When the commitment was made |
| due_at | datetime (UTC, optional) | When it should be fulfilled |
| fulfilled_at | datetime (UTC, optional) | When it was fulfilled |
| status | enum | pending, fulfilled, overdue, cancelled |
| source_context | str (optional) | Session/conversation where made |
| source_type | str | manual, autonomy, cognition |
| priority | int (0-100) | Default 50 |
| metadata | JSON (optional) | Arbitrary key-value pairs |

**Status transitions:**
- pending → fulfilled (PATCH with fulfilled_at, auto-set if not provided)
- pending → overdue (autonomy loop detects past due_at)
- pending → cancelled
- overdue → fulfilled
- overdue → cancelled
- Fulfilled and cancelled are terminal states

**Schema:**
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

**API Endpoints:**

`POST /v1/host/commitments` — Create
- person_id: required
- description: required, 1-1000 chars
- due_at: optional, must be future
- priority: optional, 0-100, default 50
- source_type: manual | autonomy | cognition
- source_context, metadata: optional
- Returns 201 with full commitment object

`GET /v1/host/commitments` — List
- Filters: person_id, status (comma-separated), overdue_only
- Pagination: limit (1-200, default 50), offset
- Returns commitments array + total + limit + offset

`GET /v1/host/commitments/{id}` — Get single (404 if not found)

`PATCH /v1/host/commitments/{id}` — Update
- Updatable: status, fulfilled_at, description, due_at, priority, metadata
- status=fulfilled auto-fills fulfilled_at
- Invalid transitions → 422

`DELETE /v1/host/commitments/{id}` — Delete
- Only terminal states (fulfilled/cancelled)
- Active commitments → 409 (cancel first)

**Pydantic Schemas** (add to `api/schemas/host.py`):
- `CommitmentCreateRequest`
- `CommitmentUpdateRequest`
- `CommitmentResponse`
- `CommitmentListResponse`

**Store Implementation** (`commitments/store.py`):
- `CommitmentStore.__init__(db_path: Path)`
- `_init_db()`, `create()`, `get()`, `list()`, `update()`, `delete()`
- `get_overdue()`, `get_pending_for_person(person_id)`
- Thread safety: same pattern as goals store (lock + check_same_thread=False)

**Files:**
- `sidecar/colony_sidecar/commitments/__init__.py`
- `sidecar/colony_sidecar/commitments/store.py`
- `sidecar/colony_sidecar/api/schemas/host.py` (modified)
- `sidecar/colony_sidecar/api/routers/host.py` (modified — new endpoints)
- `sidecar/colony_sidecar/server.py` (modified — wire store)
- `sidecar/tests/test_commitments.py`

### Layer 2: Context Assembly + Autonomy Integration

Makes commitments visible to the agent and keeps them current.

**Context Assembly:**

In enriched_context, after existing sections, inject "Pending Commitments" section when contact has outstanding commitments:

```
- Check DGX cluster status (due Apr 25, priority 70)
- Follow up on PR review (no deadline, priority 50)
```

- Section id: `colony-commitments`
- Priority: 72 (between goals at 75 and insights at 65)
- Max 5 commitments shown
- Controlled by `features.commitments` (default true)

**Autonomy Integration:**

New condition type: `commitment_overdue`

Every 30 minutes (configurable via `COLONY_COMMITMENT_CHECK_INTERVAL_MINUTES`):
1. Query commitments where due_at < now AND status = 'pending'
2. Mark them as 'overdue'
3. Emit `commitment.overdue` event for each
4. Return condition_met=true with count

**Event Types:**

| Event | When | Payload |
|---|---|---|
| commitment.created | New commitment | id, person_id, description |
| commitment.fulfilled | Marked done | id, person_id |
| commitment.overdue | Past due, still pending | id, person_id, description |
| commitment.cancelled | Cancelled | id, person_id |

Add to `HostEventType` in TypeScript types.

**Plugin-Side:**
- `sidecar-client.ts`: createCommitment, listCommitments, getCommitment, updateCommitment, deleteCommitment
- `types.ts`: new interfaces + HostEventType additions
- `config.ts`: commitmentsEnabled (default true)
- `context-cache.ts`: "commitments" channel
- `event-handlers.ts`: handle commitment events → invalidate cache

**Files:**
- `sidecar/colony_sidecar/autonomy/condition_worker.py` (modified)
- `sidecar/colony_sidecar/events/types.py` (modified)
- `src/sidecar-client.ts` (modified)
- `src/types.ts` (modified)
- `src/config.ts` (modified)
- `src/context-cache.ts` (modified)
- `src/event-handlers.ts` (modified)
- `src/plugin.ts` (modified)

### Layer 3: Cognition Prompt + Subagent Wiring

The bridge between "something happened" and "Colony thought about it."

**Cognition Prompt:**

A system prompt template that tells the subagent what Colony can do and how to think:

```
You are Colony's background cognition. You observe conversations and events,
and you produce structured actions when you notice something worth recording.

You have access to these Colony API endpoints:
- POST /v1/host/commitments — Record a commitment someone made
- PATCH /v1/host/commitments/{id} — Update a commitment status
- GET /v1/host/commitments — List existing commitments

What to look for:
1. COMMITMENTS: Any promise, follow-up, or obligation expressed by anyone.
   "I'll check on that tomorrow" → commitment, due tomorrow
   "Let me get back to you about X" → commitment, no deadline
   "Remind me to..." → commitment with implicit deadline
   Casual future tense ("I might look into it") is NOT a commitment.

2. EMOTIONAL CUES: Note strong emotional states for future use.
   (Currently logged but not stored — future Theory of Mind integration)

When in doubt, do not act. False negatives are better than false positives.
It is worse to record a non-commitment than to miss a real one.
```

**Trigger Endpoint:**

`POST /v1/host/cognition/trigger`

```json
{
  "trigger_type": "turn_sync" | "signal_ingest" | "anomaly" | "manual",
  "context": {
    "conversation_text": "...",
    "person_id": "owner",
    "session_id": "abc123"
  },
  "priority": "high" | "normal" | "low"
}
```

This endpoint:
1. Checks if cognition is enabled (`COLONY_COGNITION_ENABLED`)
2. Checks throttle (max 1 active cognition task per `COLONY_COGNITION_THROTTLE_SECONDS`)
3. Builds the cognition prompt with context
4. Calls `sessions_spawn` with:
   - `model`: from `COLONY_COGNITION_MODEL` config
   - `task`: cognition prompt + conversation context
   - `toolsAllow`: Colony API endpoints only
   - `mode: "run"` (one-shot)
5. Returns 202 Accepted with task reference

**Configuration:**

| Env Var | Default | Description |
|---|---|---|
| `COLONY_COGNITION_ENABLED` | false | Master toggle for cognition substrate |
| `COLONY_COGNITION_MODEL` | (none, required if enabled) | Model identifier for cognition subagent |
| `COLONY_COGNITION_THROTTLE_SECONDS` | 30 | Min seconds between cognition tasks |

**Pydantic Schemas:**
- `CognitionTriggerRequest`
- `CognitionTriggerResponse`

**Files:**
- `sidecar/colony_sidecar/cognition/__init__.py`
- `sidecar/colony_sidecar/cognition/prompt.py` — prompt templates
- `sidecar/colony_sidecar/cognition/trigger.py` — trigger endpoint + spawn logic
- `sidecar/colony_sidecar/api/schemas/host.py` (modified)
- `sidecar/colony_sidecar/api/routers/host.py` (modified)
- `sidecar/tests/test_cognition.py`

### Layer 4: Trigger Pipeline

Automatic firing of cognition triggers from Colony's event flow.

**Turn Sync Hook:**

After `/v1/host/turns/sync` processes a turn, fire a cognition trigger with:
- trigger_type: "turn_sync"
- context: conversation text, person_id, session_id
- priority: "normal"

**Signal Ingest Hook:**

After `/v1/host/signals/ingest` processes a high-priority signal (anomaly, user correction), fire a cognition trigger with:
- trigger_type: "signal_ingest"  
- priority: "high" (skips throttle queue)

**Debouncing:**

When multiple turns arrive in rapid succession, debounce:
- Track last trigger timestamp
- If a turn arrives within `COLONY_COGNITION_THROTTLE_SECONDS`, queue it
- On throttle expiry, fire with the accumulated context (all turns since last trigger)
- Max accumulated context: 4000 tokens (compressed if needed using existing compression module)

**Files:**
- `sidecar/colony_sidecar/cognition/debounce.py` — debounce + accumulation logic
- `sidecar/colony_sidecar/cognition/trigger.py` (modified)
- `sidecar/colony_sidecar/api/routers/host.py` (modified — hook into turn sync + signal ingest)

## Configuration Summary

| Env Var | Default | Description |
|---|---|---|
| `COLONY_COMMITMENTS_ENABLED` | true | Commitment tracking toggle |
| `COLONY_COMMITMENT_CHECK_INTERVAL_MINUTES` | 30 | Overdue check frequency |
| `COLONY_COGNITION_ENABLED` | false | Cognition substrate toggle |
| `COLONY_COGNITION_MODEL` | (none) | Model for cognition subagent |
| `COLONY_COGNITION_THROTTLE_SECONDS` | 30 | Min seconds between cognition tasks |

## Failure Modes

| Failure | Behavior |
|---|---|
| Commitment store corrupted | Log error, return empty results, don't crash |
| Store not initialized | Endpoints return 501, context section skipped |
| Invalid status transition | 422 with clear message |
| Delete active commitment | 409, must cancel first |
| due_at in past | 422 at creation |
| Cognition model not configured | Trigger endpoint returns 503 |
| Cognition subagent fails | Log error, no retry (cognition moves on) |
| Cognition subagent makes bad API call | API validation catches it, no corrupt state |
| Throttle active | 202 accepted, queued for next available slot |

## Tests

### Layer 1 Tests (test_commitments.py)

**Store CRUD (16 tests):**
- Create with all fields, with defaults
- Create with past due_at → rejected
- Get by id, get nonexistent → None
- List all, by person_id, by status, overdue_only
- Pagination correctness
- Update status, description, priority
- status=fulfilled auto-fills fulfilled_at
- Invalid transition → rejected
- Delete fulfilled → succeeds, pending → rejected
- get_overdue, get_pending_for_person

**API Endpoints (10 tests):**
- Full CRUD via HTTP
- Validation (empty description, past due_at)
- 404/409/422 error cases

### Layer 2 Tests

**Context Assembly (3 tests):**
- Pending commitments → section appears
- No commitments → no section
- Feature disabled → no section

**Autonomy (2 tests):**
- No overdue → not met
- Overdue → met + event emitted + status updated

### Layer 3 Tests (test_cognition.py)

**Trigger Endpoint (5 tests):**
- Cognition disabled → 503
- No model configured → 503
- Valid trigger → 202 accepted
- Throttle active → queued
- Manual trigger works

**Prompt Generation (3 tests):**
- Prompt includes conversation context
- Prompt includes available API endpoints
- Prompt respects max token budget

### Layer 4 Tests

**Debouncing (4 tests):**
- Single turn → immediate trigger
- Rapid turns → debounced to one trigger
- Throttle expiry → fires with accumulated context
- Accumulated context exceeds max → compressed

**Integration (2 tests):**
- Turn sync fires cognition trigger
- High-priority signal bypasses throttle

## Subsystem Registration

- CommitmentStore registered with SubsystemRegistry as "commitments"
- Cognition trigger service registered as "cognition"
- Both appear in health/capabilities endpoint
- Both toggle-able via config

## Future Consumers

Once the cognition substrate is working for commitments, adding new consumers is straightforward:

1. **Theory of Mind affect updates** — Add affect observation to the cognition prompt, add `POST /v1/host/theory-of-mind/{person_id}/affect` endpoint, subagent starts calling it
2. **Pattern extraction** — Add pattern observation to the prompt, add pattern recording endpoints
3. **Self Model updates** — Add self-reflection to the prompt, add self model mutation endpoints

Each new consumer = new API endpoint + new prompt section. No new infrastructure.

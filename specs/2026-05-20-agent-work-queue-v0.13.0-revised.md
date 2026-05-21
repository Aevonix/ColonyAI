# Agent Work Queue v0.13.0 — Revised Specification

**Status:** DRAFT  
**Supersedes:** `2026-05-19-agent-driven-initiative-delivery-v0.12.0.md`  
**Date:** 2026-05-20  
**Author:** Aeva (deep-dive validated against codebase)  

---

## 1. Overview

Colony already has a distributed task queue (`sidecar/colony_sidecar/task_queue/`). This spec leverages that existing infrastructure rather than building a new one.

**Goal:** Enable Colony to classify certain initiatives as *agent-actionable* — tasks Aeva can execute autonomously using her Hermes toolsets. These become Jobs in the existing queue. Aeva polls Colony, claims matching jobs, executes them, and reports results. Colony manages the lifecycle: generation → queuing → claiming → execution → completion → acknowledgment → archival.

**Non-goal:** Building a new queue, modifying Hermes core harness, or adding LLM-based implicit acknowledgment detection.

---

## 2. Current State Assessment (Validated Against Codebase)

### 2.1 What Exists and Works

| Component | File | Status |
|-----------|------|--------|
| `QueueManager` (SQLite, atomic claim, audit trails, worker registry) | `task_queue/queue_manager.py` | ✅ Functional |
| `TaskQueueManager` (singleton facade over `QueueManager`) | `task_queue/queue_manager.py:995` | ✅ Functional |
| `Scheduler` (priority-aware, capability-matching) | `task_queue/scheduler.py` | ✅ Functional |
| `WorkerNode` (in-process polling, heartbeats) | `task_queue/worker.py` | ✅ Functional |
| Task queue schema (`jobs`, `workers`, `job_audit`, `heartbeats`) | `task_queue/schema.sql` | ✅ Defined |
| `JobType` enum (11 types) | `task_queue/models.py` | ✅ Defined |
| `JobStatus` enum (8 states incl. `BLOCKED`) | `task_queue/models.py` | ✅ Defined |
| `InitiativeType` enum (11 types) | `intelligence/components/initiative_engine.py:77` | ✅ Defined |
| `InitiativeStore` (SQLite, dedup, assignment history) | `initiatives/store.py` | ✅ Functional |
| `context/assemble` endpoint | `api/routers/host.py:1291` | ✅ Functional |
| Colony-memory provider `prefetch()` | `~/.hermes/plugins/colony-memory/provider.py` | ✅ Functional |
| `SubsystemRegistry` with `initiative_store` | `autonomy/registry.py:175` | ✅ Exposed |
| `/v1/host/initiatives/{id}/respond` endpoint | `api/routers/host.py:5182` | ✅ Functional |
| `colony_initiative_feedback` MCP tool | `mcp/server.py:368` | ✅ Functional |

### 2.2 Critical Bugs (Block All Reliable Initiative Flow)

**Bug A — Dedup is completely broken**  
`initiative_engine.py:1069-1092` calls `self._goal_store.list_recent()` (method does not exist on any goal store). The bare `except Exception: pass` swallows the `AttributeError` every tick. Result: zero dedup, duplicate initiatives spam the bridge.

**Bug B — Initiatives are never persisted before dispatch**  
`autonomy/loop.py:520-604` (`_phase_execute`) builds a payload dict and calls `delivery.push_initiative(payload)` directly. It never calls `initiative_store.create()`. If the sidecar restarts, all in-flight initiatives evaporate.

**Bug C — `get_in_session_context` auto-consumes unconditionally**  
`delivery/bridge.py:408-426` sets `d.sent = True` for every in-session delivery it returns. If the agent ignores the injected text (doesn't act on it), the item is lost forever. No recovery path.

**Bug D — `_phase_execute` bypasses rate limiting**  
`push_initiative()` at `bridge.py:243` immediately POSTs to Hermes without checking rate limits. The bridge `deliver()` method enforces per-person caps, but `_phase_execute` never calls it. Webhook failures are silent (logged but not retried).

### 2.3 Architectural Gaps (Not Bugs, but Missing)

| Gap | Location | Impact |
|-----|----------|--------|
| Zero HTTP routes for task queue | `api/routers/` | Queue is internal-only; external agents cannot interact |
| `SubsystemRegistry` has no `task_queue` property | `autonomy/registry.py` | Code accessing the queue must import from `host.py` global |
| `InitiativeType` has no `AGENT_ACTION` | `initiative_engine.py:77` | Cannot semantically classify agent-actionable initiatives |
| `JobType` has no `AGENT_ACTION` | `task_queue/models.py:39` | Cannot filter queue for agent-only work |
| `initiatives` table has no `job_id` column | `initiatives/store.py:84` | Cannot link an initiative to its queued job |
| `context/assemble` has no initiative section | `host.py:1291` | In-session injection has no data source |
| Hermes plugin lacks initiative tools | `~/.hermes/plugins/colony-memory/provider.py` | Agent cannot acknowledge or manage initiatives from Hermes |

### 2.4 What Does NOT Exist (Previous Spec Was Wrong)

1. **The scheduler does NOT push-assign jobs.** `Scheduler.notify_worker()` (`queue_manager.py:920`) is a no-op. Workers poll `claim_job()` and self-assign atomically. The scheduler's role is abandonment detection, retry, and deadline expiry.
2. **Aeva is NOT a mesh SOVEREIGN node.** Mesh roles (`SOVEREIGN`, `REGENT`, `VASSAL`) are for distributed Colony networking. The task queue has a separate `WorkerCapabilities` registry. Aeva registers as a **task queue worker**, not a mesh node.
3. **There is NO implicit acknowledgment detection.** No code analyzes Hermes response text for initiative acknowledgment. This would require a new webhook consumer + text analysis pipeline.
4. **There is NO `pre_llm_call` hook that can modify the system prompt.** The hook only appends a string to the user message. Silent injection must use the existing `prefetch()` path.
5. **`colony_acknowledge_initiative` does NOT exist in the Hermes plugin.** The MCP server has `colony_initiative_feedback` (which calls `/v1/host/initiatives/{id}/respond`), but the Hermes colony-memory provider does not expose it.

---

## 3. Design Principles

1. **Use what exists.** The task queue is real, tested, and wired into `server.py`. Extend it; don't replace it.
2. **No core harness changes.** All integration uses existing extension points (plugin tools, webhooks, `prefetch()`).
3. **Bug fixes first.** The four critical bugs in §2.2 must be fixed before any new feature is reliable.
4. **Explicit acknowledgment only.** No implicit detection in this version. The agent acknowledges via a tool call (`colony_initiative_feedback`).
5. **Destructive ops require approval by default.** Toggleable via env var. Default = message the owner for approval.
6. **Advisory scheduler, atomic self-claim.** Document the actual pull-based semantics. Don't pretend the scheduler pushes.
7. **Coexist with internal WorkerNode.** `server.py:802` already starts an in-process `WorkerNode` that handles compute jobs (INFERENCE, TRAINING, etc.). Aeva is an external worker that handles `AGENT_ACTION` jobs. They claim different job types and do not interfere.

---

## 4. Data Model Changes

### 4.1 Add `AGENT_ACTION` to `InitiativeType`

```python
class InitiativeType(str, Enum):
    # ... existing types ...
    AGENT_ACTION = "agent_action"  # NEW
```

**Trigger rule (v1):** If `action_hint` starts with `agent_` (e.g., `agent_check_repo_status`, `agent_research_topic`), classify as `AGENT_ACTION`. Everything else keeps existing classification.

### 4.2 Add `AGENT_ACTION` to `JobType`

```python
class JobType(str, Enum):
    # ... existing types ...
    AGENT_ACTION = "agent_action"  # NEW
```

This prevents Aeva from claiming non-agent jobs (e.g., `CUSTOM` jobs meant for the internal WorkerNode).

### 4.3 Add `job_id` to `initiatives` table

**Schema migration:**
```sql
ALTER TABLE initiatives ADD COLUMN job_id TEXT;
CREATE INDEX IF NOT EXISTS idx_initiatives_job_id ON initiatives(job_id);
```

**Update `initiatives/store.py`:**
- Add `job_id` to `_UPDATABLE_COLUMNS`.
- Add `job_id` parameter to `create()` and `update()`.

### 4.4 Add `blocked_reason` tag convention for Jobs

No schema migration needed. Use `tags` JSON:
```json
{"blocked_reason": "awaiting_owner_approval", "initiative_id": "uuid-here"}
```

When approved, clear the tag and transition status to `QUEUED`.

---

## 5. API Surface

### 5.1 New Router: `api/routers/task_queue.py`

Do NOT bloat `host.py` (already 5200+ lines). Create a dedicated router mounted at `/v1/host/queue/`.

**Implementation note:** Use `TaskQueueManager.get_instance().queue` to access the `QueueManager` singleton.

**Endpoints:**

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/workers/register` | Register worker capabilities | Bearer |
| POST | `/workers/{node_id}/heartbeat` | Worker heartbeat | Bearer |
| POST | `/workers/{node_id}/deregister` | Remove worker | Bearer |
| POST | `/jobs` | Post a new job | Bearer |
| POST | `/jobs/{job_id}/claim` | Atomically claim a job | Bearer |
| POST | `/jobs/{job_id}/complete` | Mark job completed | Bearer |
| POST | `/jobs/{job_id}/fail` | Mark job failed | Bearer |
| POST | `/jobs/{job_id}/heartbeat` | Job progress heartbeat | Bearer |
| GET | `/jobs/pending` | List pending jobs (for Aeva's poll) | Bearer |
| GET | `/jobs/completed?since=ISO` | List completed since timestamp | Bearer |
| GET | `/stats` | Queue stats | Bearer |
| POST | `/initiatives/{id}/approve` | Owner approves blocked initiative | Bearer |

**Bearer token source:** `COLONY_AGENT_API_TOKEN` env var. Shared secret between Colony sidecar and Aeva cron job. Phase 2 migrates to Ed25519 mesh crypto when VASSAL workers are added.

### 5.2 Update `context/assemble` endpoint and schema

**Step 1:** Add `include_initiatives` to `ContextAssembleRequest` (`api/schemas/host.py:143`):

```python
class ContextAssembleRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    incoming_message: HostMessage
    available_tools: Optional[List[str]] = None
    citations_mode: Optional[Literal["off", "inline", "appendix"]] = None
    include_initiatives: Optional[bool] = None  # NEW
```

**Step 2:** Add initiative section in `context_assemble()` (`api/routers/host.py:1291`):

```python
if body.include_initiatives:
    pending = initiative_store.list(status=["pending"], limit=10)
    if pending:
        body_text = "\n".join(
            f"• [{i.type}] {i.description} (priority: {i.priority:.0%})"
            for i in pending
        )
        sections.append(ContextSection(
            id="colony-initiatives",
            title="Pending Initiatives",
            body=body_text,
            priority=50,
        ))
```

**Step 3:** Update colony-memory provider `prefetch()` (`~/.hermes/plugins/colony-memory/provider.py:398`) to pass `include_initiatives: true`:

```python
json={
    "identity": {"host_id": "hermes"},
    "context": {...},
    "incoming_message": {"role": "user", "content": query},
    "include_initiatives": True,  # NEW
}
```

The provider's `_format_sections()` already wraps everything in `<memory-context>` blocks, so initiatives will appear as:
```
<memory-context>
[Colony Cognitive Context]

## Pending Initiatives [priority 50]
• [agent_action] Check repo status (priority: 70%)
</memory-context>
```

---

## 6. Agent Worker Lifecycle

### 6.1 Registration

Aeva (via cron job every 5 minutes):
1. Reads `~/.hermes/config.yaml` enabled toolsets.
2. Maps toolsets to Colony capability names:
   - `terminal` → `shell`
   - `file` → `filesystem`
   - `web` → `web_search`
   - `browser` → `web_browser`
   - `search` → `code_search`
   - `github` → `git`
   - `spotify` → `media_control`
3. POSTs to `/v1/host/queue/workers/register`:
   ```json
   {
     "node_id": "aeva-hermes-agent",
     "capabilities": ["shell", "filesystem", "web_search", "web_browser", "code_search", "git", "media_control"],
     "job_types": ["agent_action"],
     "max_concurrent": 1,
     "available": true,
     "load": 0.0
   }
   ```

### 6.2 Polling and Claiming

Aeva's cron job:
1. POST `/v1/host/queue/jobs/claim` with body `{"node_id": "aeva-hermes-agent", "job_types": ["agent_action"]}`.
2. If a job is returned, execute it using available tools.
3. If execution succeeds: POST `/v1/host/queue/jobs/{job_id}/complete` with result payload.
4. If execution fails: POST `/v1/host/queue/jobs/{job_id}/fail` with error details.
5. Send heartbeat during long-running jobs: POST `/v1/host/queue/jobs/{job_id}/heartbeat`.

**Claim semantics:** `QueueManager.claim_job()` is atomic SQLite (`UPDATE ... WHERE status='queued'`). Two workers cannot claim the same job.

### 6.3 Concurrent Session Safety

If Marc has sent a message within the last 5 minutes (active session), Aeva's cron job should:
- Skip claiming jobs that involve filesystem writes, git operations, or service restarts.
- Still claim read-only jobs (research, monitoring, health checks).

Implementation: Add `last_user_message_at` tracking in Colony (new column or in-memory). The cron job checks this timestamp before claiming write-capable jobs.

---

## 7. Approval & Escalation State Machine

### 7.1 Destructive vs. Non-Destructive Classification

An initiative is **destructive** if its `action_hint` matches any of:
- `agent_git_push`
- `agent_git_commit`
- `agent_service_restart`
- `agent_file_delete`
- `agent_deploy_*`

**Default behavior:** Destructive jobs require owner approval. Toggle via `COLONY_AGENT_AUTO_APPROVE=true` (default: `false`).

### 7.2 State Flow

```
Initiative generated by engine
  → initiative_store.create(status="pending")
  → Classified as AGENT_ACTION
    → If destructive AND auto_approve=false:
        → Job posted with status=BLOCKED, tags={"blocked_reason": "awaiting_owner_approval"}
        → Webhook to Hermes: "Approval required: [description]"
        → I message Marc with approve/reject quick-reply
        → Marc approves via colony_approve_initiative tool
        → Job status → QUEUED
        → Aeva can now claim it
    → If destructive AND auto_approve=true:
        → Job posted with status=QUEUED
        → Aeva claims immediately
    → If non-destructive:
        → Job posted with status=QUEUED
        → Aeva claims immediately
```

### 7.3 Escalation Rules

| Condition | Action |
|-----------|--------|
| Job fails after max retries | Message owner with failure summary |
| Job exceeds timeout | Message owner; mark ABANDONED |
| Agent encounters unexpected error | Message owner with error context |
| Agent completes successfully | Store result; do NOT message owner (silent completion) |
| Digest window elapsed | Bundle completed job summaries and message owner |

---

## 8. In-Session Injection

### 8.1 Mechanism

Use the **existing** `prefetch()` path (§5.2):

1. Colony `context/assemble` endpoint includes pending initiatives as a `ContextSection` when `include_initiatives=true`.
2. Colony-memory provider calls `/v1/host/context/assemble` with `include_initiatives=true`.
3. Hermes injects the formatted result into the user message as a `<memory-context>` block.
4. I (Aeva) see the initiatives in my context and can act on them or acknowledge them.

### 8.2 Acknowledgment

I acknowledge via the `colony_initiative_feedback` tool (already exists in MCP server at `mcp/server.py:368`, needs to be added to Hermes plugin). This calls `/v1/host/initiatives/{id}/respond` with `action: "acknowledged"`, which sets `initiative.status = "acknowledged"` and stops re-injection.

### 8.3 Anti-Spam: Dedup and Cooldown

The initiative engine's dedup (once Bug A is fixed) uses `dedup_key`. For agent-action initiatives, the dedup key should include the action type and target entity:
```python
dedup_key = f"agent_action:{action_hint}:{entity_id or 'global'}"
```

Cooldown: If an initiative was generated within the last `N` hours (configurable, default 4h), do not regenerate even if the dedup window expires.

---

## 9. Digest Mode

Instead of messaging on every completion, completed job results accumulate in the task queue.

**Digest trigger:** Cron runs every 6 hours (or on-demand via owner command).

**Digest content:**
```
[Colony Digest — 6h]
✅ Completed (3)
  • Checked repo status — clean
  • Researched topic X — summary attached
  • Monitored system health — all green
⚠️  Needs attention (1)
  • Health check on Node Y failed after 3 retries
```

**Implementation:** Query `GET /v1/host/queue/jobs/completed?since={last_digest_time}`. Format and send. No new storage needed. `QueueManager.get_completed_jobs_since()` already exists at `queue_manager.py:423`.

---

## 10. Implementation Phases

**Dependency rule:** Each phase depends on all previous phases. Do not skip.

### Phase 0 — Bug Fixes (Prerequisite)

**P0-A: Fix dedup** (`initiative_engine.py:1069-1092`)
- Replace `self._goal_store.list_recent()` with `initiative_store.list()` + time-based filtering.
- Remove bare `except Exception: pass`. Log dedup failures loudly.

**P0-B: Persist initiatives before dispatch** (`autonomy/loop.py:520-604`)
- Before calling `delivery.push_initiative()`, call `initiative_store.create(...)`.
- **Critical:** `initiative_store` uses synchronous `sqlite3`. `_phase_execute` is async. Wrap the call in `asyncio.get_event_loop().run_in_executor(None, store.create, ...)` to avoid blocking the event loop.
- Map payload fields to store fields.
- If `store.create()` returns a dedup hit, skip dispatch.
- If `store.create()` fails, log error and skip dispatch (don't lose silently).

**P0-C: Fix `get_in_session_context` auto-consume** (`delivery/bridge.py:408-426`)
- Do NOT set `d.sent = True` on retrieval.
- Add a max-age expiry (24h) for unacknowledged in-session deliveries in the bridge's ghost cleanup phase.
- When `colony_initiative_feedback` is called with `action="acknowledged"`, also remove matching deliveries from `bridge._pending` (match by `initiative_id` metadata).

**P0-D: Add rate limit check before `push_initiative()`**
- Before calling `push_initiative()`, check `bridge._rate_limiter.can_deliver(person_id, urgency=priority)`.
- If rate-limited, skip the webhook push but keep the initiative in `initiative_store` for retry on next tick.
- Do NOT route through `bridge.deliver()` for now — that would change the delivery mechanism from webhook-push to gateway-poll, which requires gateway changes out of scope.

### Phase 1 — Data Model & Registry

**P1-A: Add `AGENT_ACTION` to `InitiativeType`**  
**P1-B: Add `AGENT_ACTION` to `JobType`**  
**P1-C: Add `job_id` column to initiatives table** (schema migration + store.py updates)  
**P1-D: Add `task_queue` property to `SubsystemRegistry`**

### Phase 2 — Task Queue API Exposure

**P2-A: Create `api/routers/task_queue.py`** with all 12 endpoints (§5.1).  
**P2-B: Mount router in FastAPI app** (`server.py:1096` or app factory).  
**P2-C: Add Bearer auth middleware** (shared secret from `COLONY_AGENT_API_TOKEN`).  
**P2-D: Update `context/assemble`** to include initiatives section when requested (§5.2).  
**P2-E: Update colony-memory provider** to pass `include_initiatives: true` in `prefetch()`.

### Phase 3 — Agent Action Classification

**P3-A: Add classification rule** to initiative engine: `action_hint.startswith("agent_")` → `InitiativeType.AGENT_ACTION`.  
**P3-B: Update `_phase_execute`** for `AGENT_ACTION` initiatives:
- Create initiative in store.
- Post job to task queue via `TaskQueueManager.get_instance().submit(task_type="agent_action", ...)` or direct `QueueManager.post()`.
- Store returned `job_id` in initiative record.
- If destructive: post with `status=BLOCKED`, tag `awaiting_owner_approval`.
- If non-destructive: post with `status=QUEUED`.
- Do NOT call `delivery.push_initiative()` for `AGENT_ACTION` initiatives (they go to queue, not webhook).

### Phase 4 — Hermes Plugin Tools

**P4-A: Add tool schemas** to Colony Hermes plugin (`~/.hermes/plugins/colony-memory/provider.py`):
- `colony_claim_task` — poll and claim a job
- `colony_complete_task` — report success
- `colony_fail_task` — report failure
- `colony_heartbeat_task` — report progress
- `colony_approve_initiative` — owner approves blocked job (calls `/v1/host/initiatives/{id}/respond` with `action="approved"`)
- `colony_list_pending_tasks` — list my pending jobs
- `colony_initiative_feedback` — acknowledge/dismiss/snooze initiatives (maps to existing `/v1/host/initiatives/{id}/respond`)

**P4-B: Implement tool handlers** in Colony sidecar (or route to queue API).

### Phase 5 — Aeva Cron Worker

**P5-A: Create cron job** in `~/.hermes/cron/`:
- Runs every 5 minutes.
- Registers worker capabilities (derived from config.yaml toolsets).
- Claims one `AGENT_ACTION` job.
- Executes with tools.
- Reports result.

**P5-B: Add `last_user_message_at` tracking** for concurrent session safety.

### Phase 6 — Digest & Polish

**P6-A: Implement digest cron** (every 6h).  
**P6-B: Add escalation rules** (failure notification, timeout alerts).  
**P6-C: Add metrics/logging** for queue throughput, claim latency, execution success rate.

---

## 11. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| SQLite blocking event loop (sync `initiative_store` from async context) | High | High | Use `run_in_executor()` for all `initiative_store` calls from `_phase_execute`. |
| Cron job edits files while Marc is also editing | Medium | High | Skip write-capable jobs when `last_user_message_at < 5 min ago`. |
| Aeva claims a job then the session resets mid-execution | Low | Medium | Job heartbeat + timeout. If heartbeat stops, Colony marks ABANDONED and re-queues. |
| Approval message fails to reach Marc (Hermes down) | Low | High | Approval is a tool call, not a webhook. The tool handler updates the job status directly in SQLite. No external dependency. |
| Duplicate agent_action jobs from multiple initiative triggers | Medium | Medium | Strong dedup keys (`agent_action:{hint}:{entity}`) + cooldown in initiative engine. |
| Bearer token leaks | Low | High | Token is env-var only, never logged. Rotate via `COLONY_AGENT_API_TOKEN`. Phase 2 replaces with Ed25519. |

---

## 12. Appendix: Colony Task Queue Semantics

### 12.1 Scheduler is Advisory

`Scheduler._assign_queued_jobs()` calculates the best worker for each job and calls `queue_manager.notify_worker(best_worker.node_id, job.job_id)`. However, `notify_worker()` is a no-op log statement. The actual assignment happens when a worker calls `claim_job()`.

**What the scheduler actually does:**
- Runs every `scheduler_tick_secs` (default 2s).
- Detects abandoned jobs (heartbeat timeout).
- Retries failed jobs (up to `max_retries`).
- Expires jobs past deadline.
- Pre-filters eligible workers for each job (capability matching).

**What workers actually do:**
- Poll `claim_job(node_id, capabilities_filter)` periodically.
- Atomically claim one job at a time.
- Send heartbeats while running.
- Report completion/failure.

### 12.2 Worker Registration is Ephemeral

Workers must re-register after sidecar restart. There is no persistent worker registry across restarts. Aeva's cron job must re-register on every tick (or at minimum, handle 404 from heartbeat by re-registering).

### 12.3 Job Payload Schema for Agent Actions

```json
{
  "initiative_id": "uuid",
  "action_hint": "agent_check_repo_status",
  "description": "Check if colony-work repo has uncommitted changes",
  "entity_id": "colony-work",
  "destructive": false,
  "auto_approve": false,
  "expected_tools": ["terminal", "file"],
  "context": {
    "repo_path": "~/colony-work",
    "branch": "main"
  }
}
```

### 12.4 Internal vs. External Workers

`server.py:802` starts an in-process `WorkerNode` with handlers from `build_default_handlers()`. This worker claims jobs of type `INFERENCE`, `TRAINING`, `DESKTOP`, `BROWSER`, etc. Aeva is an external worker (cron job) that claims only `AGENT_ACTION` jobs. They share the same `QueueManager` but claim different job types, so they do not compete.

---

## 13. Migration Notes

### 13.1 Database

Run once on sidecar startup (idempotent):
```sql
-- initiatives table
ALTER TABLE initiatives ADD COLUMN job_id TEXT;
CREATE INDEX IF NOT EXISTS idx_initiatives_job_id ON initiatives(job_id);

-- No changes to jobs/workers/job_audit tables needed
```

### 13.2 Environment Variables

```bash
# Required
export COLONY_AGENT_API_TOKEN="$(openssl rand -hex 32)"

# Optional
export COLONY_AGENT_AUTO_APPROVE="false"   # default
export COLONY_AGENT_DIGEST_INTERVAL_HOURS="6"
```

### 13.3 Backwards Compatibility

- Existing `InitiativeType` values unchanged.
- Existing `JobType` values unchanged.
- Existing webhook flow (`push_initiative`) unchanged for non-`AGENT_ACTION` initiatives.
- `context/assemble` only includes initiatives when `include_initiatives=true` is passed.
- Internal `WorkerNode` continues handling existing job types.

---

## 14. Acceptance Criteria

- [ ] Bug A fixed: dedup prevents duplicate initiatives within cooldown window.
- [ ] Bug B fixed: `_phase_execute` persists every initiative to `initiative_store` before dispatch (using `run_in_executor`).
- [ ] Bug C fixed: `get_in_session_context` does not auto-consume; items survive until explicitly acknowledged or expired (24h max-age).
- [ ] Bug D fixed: `_phase_execute` checks rate limits before calling `push_initiative()`.
- [ ] `AGENT_ACTION` exists in both `InitiativeType` and `JobType`.
- [ ] `initiatives` table has `job_id` column.
- [ ] `ContextAssembleRequest` has `include_initiatives` field.
- [ ] Colony-memory provider passes `include_initiatives: true` in `prefetch()`.
- [ ] Task queue API router exists at `/v1/host/queue/` with all 12 endpoints.
- [ ] Aeva cron job successfully claims, executes, and completes a non-destructive `agent_action` job end-to-end.
- [ ] Destructive job flows through BLOCKED → approval → QUEUED → claimed → completed.
- [ ] Digest bundles completed jobs and delivers to owner.
- [ ] All existing tests pass; new tests cover queue API and agent action classification.

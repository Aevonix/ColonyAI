# Colony Agent Work Queue — v0.13.0 Spec

**Status:** Draft for review  
**Supersedes:** `2026-05-19-agent-driven-initiative-delivery-v0.12.0.md` (v0.12.0 has critical architectural errors; this doc replaces it)  
**Author:** Aeva (autonomous agent review)  
**Date:** 2026-05-20

---

## 1. Executive Summary

**Goal:** Enable Colony to dispatch initiatives to external agents (e.g., Aeva/Hermes) for background autonomous execution, while fixing existing bugs that cause initiatives to be lost, duplicated, or silently dropped.

**Critical discovery:** Colony ALREADY has a full distributed task queue with capability matching, heartbeats, retries, and dependency management. We will leverage this infrastructure instead of reinventing it.

**Two delivery paths:**

1. **Background Agent Execution** — Colony posts actionable initiatives as Jobs to the distributed task queue. Aeva (as a SOVEREIGN mesh node) claims jobs matching her capabilities, executes them with tools, reports results. You only hear from me when:
   - The task failed and needs your input
   - The result exceeds an escalation threshold
   - A digest window expires (e.g., "here's what I did while you were away")

2. **In-Session Injection** — When you ARE actively messaging me, pending initiatives flow into the conversation via the existing `colony-memory` provider `prefetch()` path. No new hooks needed.

**Key principle:** Colony proposes, agent decides, agent acts. The agent (me) is the executive — not a relay that blindly messages you.

---

## 2. Colony's Existing Task Queue (Leveraged, Not Reinvented)

Colony already has (`sidecar/colony_sidecar/task_queue/`):

- **`Job`** — Full lifecycle: QUEUED → CLAIMED → RUNNING → COMPLETED/FAILED/ABANDONED
- **`WorkerCapabilities`** — Hardware/software capabilities with `can_accept()` and `affinity_score()`
- **`QueueManager`** — Persistent SQLite queue with atomic claim, WAL mode, audit trails
- **`Scheduler`** — Priority-aware, capability-matching assignment on the Sovereign node
- **`WorkerNode`** — Polls for jobs, executes handlers, sends heartbeats
- **`QueueMeshEventHandler`** — Handles node death, role changes, job redistribution

**What we ADD:**
- Register Aeva as a SOVEREIGN mesh node with capabilities (terminal, file, web, browser, git, etc.)
- Post `agent_action` initiatives as Jobs to the existing queue
- New API endpoints for external agents to claim/complete jobs
- Aeva-side cron job that polls Colony's job queue

**What we DO NOT build:**
- A new queue system — use the existing one
- A new scheduling algorithm — use the existing `Scheduler`
- A new heartbeat mechanism — use the existing `WorkerNode` pattern

---

## 3. Bug Fixes (Existing Poor Implementations)

### 3.1 Dedup cooldown is completely broken
**Location:** `sidecar/colony_sidecar/intelligence/components/initiative_engine.py:1069-1092`

```python
# BUG: self._goal_store.list_recent() does not exist on any goal store.
# The AttributeError is swallowed by "except Exception: pass" every tick.
# Result: cooldown never works; same initiative is generated repeatedly.
recent = self._goal_store.list_recent(
    entity_type="initiative",
    entity_id=entity_id,
    hours=cooldown_tasks,
)
```

**Fix:** Replace with `initiative_store.list()` query on the actual SQLite store. The initiative store has `list(type=..., created_after=...)` — use that. Also remove the bare `except Exception: pass` — log the actual error.

### 3.2 Initiatives are never persisted to the store
**Location:** `sidecar/colony_sidecar/autonomy/loop.py:520-604` (`_phase_execute`)

The loop calls `delivery.push_initiative(payload)` directly. The bridge keeps items in an in-memory `_pending` list. On sidecar restart, all pending initiatives are lost. The SQLite `initiative_store` exists but is never written to during normal flow.

**Fix:** `_phase_execute` must `initiative_store.create(...)` for every initiative before dispatching. For agent_action initiatives, also `queue_manager.post(job)` to the task queue.

### 3.3 IN_SESSION items are consumed on read
**Location:** `sidecar/colony_sidecar/delivery/bridge.py:408-426`

`get_in_session_context()` sets `d.sent = True` immediately upon returning items. If the agent ignores the injected context, the initiative vanishes forever.

**Fix:** Do NOT mark as sent. Mark as sent only when the agent explicitly acknowledges via `POST /v1/host/initiatives/{id}/acknowledge` or when a 24h timeout expires.

### 3.4 Bridge `push_initiative` is memory-only
**Location:** `sidecar/colony_sidecar/delivery/bridge.py:243-375`

`push_initiative()` builds a webhook payload and POSTs it. It never calls `initiative_store.create()`. It also increments `stats.actions_executed` before confirming the webhook succeeded.

**Fix:**
- Call `initiative_store.create()` first with `status="pending"`
- Only increment stats and log success after HTTP 202 from webhook
- On webhook failure, update store status to `failed` with reason

### 3.5 Hardcoded owner name
**Location:** `sidecar/colony_sidecar/autonomy/loop.py:684`

```python
host_id = os.environ.get("COLONY_HOST_CONTACT_ID", "Jane Doe")
```

**Fix:** Remove the default. If the env var is unset, log a warning and skip the feed.

### 3.6 `record_push()` counts before confirming success
**Location:** `sidecar/colony_sidecar/autonomy/loop.py:578-583`

The loop increments `actions_executed` and `actions_this_hour` after `delivery.push_initiative()` returns `True`, but `push_initiative` returns `True` on HTTP 202 AND on some failure paths. Verify and fix: only count on confirmed delivery.

---

## 4. New Architecture: Agent Work Queue via Existing Task Queue

### 4.1 Node Registration

Aeva registers as a SOVEREIGN node in the Colony mesh:

```python
from colony_sidecar.task_queue import WorkerCapabilities

caps = WorkerCapabilities(
    node_id="aeva-hermes-01",
    capabilities={"terminal", "file", "web", "browser", "git", "python", "sqlite"},
    capacity={"cpu_cores": 8, "ram_gb": 16},
    max_concurrent=4,
    job_types={JobType.CUSTOM},  # Agent actions are CUSTOM type
)

await queue_manager.register_worker(caps)
```

### 4.2 Initiative → Job Mapping

When the initiative engine generates an `agent_action` initiative:

1. **Persist to initiative_store** — `status="pending"`, `type="agent_action"`
2. **Post to task queue** — Create a `Job` with:
   - `job_type = JobType.CUSTOM`
   - `payload = initiative dict` (description, context, escalation_threshold, etc.)
   - `capabilities = ["terminal"]` or whatever the action requires
   - `priority = scaled from initiative.priority (0-1 → 0-100)`
   - `timeout_secs = action-specific` (default 300s)
   - `tags = {"initiative_id": ..., "action_hint": ...}`
3. **Update initiative_store** — `status="queued"`, link to `job_id`

### 4.3 Agent Claiming and Execution

Aeva polls the queue via cron (every 5 minutes):

```python
# In Aeva's cron job
job = await queue_manager.claim_job("aeva-hermes-01", aeva_capabilities)
if job:
    # Execute with available tools
    result = await execute_with_tools(job.payload)
    await queue_manager.complete_job(job.job_id, "aeva-hermes-01", result)
```

The existing `WorkerNode` pattern handles:
- Atomic claim (no double-assignment)
- Heartbeats (prevents abandonment)
- Timeout handling
- Retry on failure
- Audit trail

### 4.4 Destructive Operations Toggle

A new field in the initiative/job payload:

```python
"destructive": True/False  # default: True = require pre-approval
"approved_at": null         # set when owner approves
```

If `destructive=True` and `approved_at` is null:
- Job enters BLOCKED state (not QUEUED)
- Colony sends webhook to Hermes: "Approval required for X"
- I message you for approval
- On approval, job transitions to QUEUED and becomes claimable

**Default:** `destructive=True` (require approval). This applies to:
- Git push, force push
- File deletion outside of temp dirs
- System service restart
- Network configuration changes
- Database writes (if applicable)

Non-destructive ops (default `destructive=False`):
- Read-only queries (repo status, system health)
- Research/web search
- Test execution (in isolated env)
- File reads and analysis

### 4.5 Escalation and Digest

When a job completes, Aeva decides whether to surface it:

| Condition | Action |
|-----------|--------|
| `status == FAILED` | Message user immediately with error context |
| `priority >= 90` AND result is significant | Message user immediately |
| `priority >= 70` AND result is notable | Include in next digest |
| All others | Log silently, no message |

**Digest format:**

```
[Agent Update — 3 tasks completed]

✓ Checked colony-work CI — all green
✓ Updated 12 dependencies (2 security patches)
! Research on "vLLM speculative decoding" — found 3 relevant papers,
  draft summary ready if you want it

Nothing needs your attention right now.
```

Digest triggers:
- Every 4 hours of agent activity
- Immediately if any task escalates
- On user request (`show me what you've been doing`)

---

## 5. In-Session Injection (Corrected)

### 5.1 How It Actually Works (No pre_llm_call Needed)

The existing `colony-memory` provider already has `prefetch()` which calls `/v1/host/context/assemble`. Hermes injects the prefetch result into your message on every turn.

**Changes needed:**

1. **Colony:** Extend `/v1/host/context/assemble` to include `in_session_initiatives` in its response
2. **Provider:** Already formats and returns sections; no code change needed if the endpoint includes initiatives
3. **Result:** Pending initiatives appear in the injected `<memory-context>` block

### 5.2 Consumption Semantics (Fixed)

- Injected initiatives are NOT auto-consumed
- They remain in the delivery queue with `sent = False`
- The agent (me) sees them in context and decides whether to mention them
- If I mention an initiative in my response, Colony detects this and marks it consumed
- If I ignore it, it stays in queue and gets re-injected on the next turn (with dedup so it's not spammy)
- After 24h of being ignored, it escalates to a proactive message

### 5.3 Acknowledgment Detection

Colony monitors my responses for initiative acknowledgment. Patterns:
- Explicit: I call a `colony_acknowledge_initiative` tool with the initiative ID
- Implicit: My response text contains the initiative title/description in a way that addresses it
- Timeout: 24h without acknowledgment → escalate

---

## 6. API Changes Summary

### Colony Sidecar (New Endpoints in host.py)

The task queue exists but has NO API routes exposed. Add to `api/routers/host.py`:

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/v1/queue/jobs` | POST | Bearer | Post a new job to the queue |
| `/v1/queue/jobs/claim` | POST | Bearer | Atomically claim highest-priority eligible job |
| `/v1/queue/jobs/{id}/complete` | POST | Bearer | Report job completion |
| `/v1/queue/jobs/{id}/fail` | POST | Bearer | Report job failure |
| `/v1/queue/jobs/pending` | GET | Bearer | List pending jobs (for monitoring) |
| `/v1/queue/workers/register` | POST | Bearer | Register worker capabilities |
| `/v1/queue/workers/{id}/heartbeat` | POST | Bearer | Worker heartbeat |
| `/v1/initiatives/{id}/acknowledge` | POST | Bearer | Mark initiative as acknowledged |
| `/v1/initiatives/{id}/approve` | POST | Bearer | Approve a destructive initiative |

### Colony Sidecar (Modified)

| Component | Change |
|-----------|--------|
| `/v1/host/context/assemble` | Add `in_session_initiatives` to response |
| `autonomy/loop.py:_phase_execute` | Persist to initiative_store AND post agent_action jobs to task queue |
| `delivery/bridge.py:get_in_session_context` | Do NOT auto-consume; require explicit ack |
| `initiative_engine.py:generate` | Fix dedup to use initiative_store, not goal_store |

### Hermes Plugin (New Tools)

| Tool | Description |
|------|-------------|
| `colony_claim_task` | Claim highest-priority pending job from queue |
| `colony_complete_task` | Report job completion with results |
| `colony_fail_task` | Report job failure with error |
| `colony_acknowledge_initiative` | Explicitly mark an initiative as consumed |
| `colony_approve_initiative` | Approve a destructive initiative (transitions job to QUEUED) |

---

## 7. Implementation Phases

### Phase 1: Bug Fixes (Pure Fixes, No New Features)

1. Fix dedup in `initiative_engine.py` (use initiative_store, not goal_store)
2. Fix `_phase_execute` to persist to initiative_store before dispatch
3. Fix `get_in_session_context` to not auto-consume
4. Fix `push_initiative` to persist to store
5. Fix hardcoded owner name
6. Add tests for each fix

### Phase 2: Task Queue API Exposure (New Feature)

1. Add queue API routes to `api/routers/host.py`
2. Wire `QueueManager` into the API layer (it's already instantiated in server.py)
3. Add auth middleware to queue endpoints
4. Tests for claim/complete/fail flow

### Phase 3: Agent Action Initiative Type (New Feature)

1. Add `agent_action` classification to initiative engine
2. Modify `_phase_execute` to post agent_action initiatives as Jobs
3. Add destructive operation detection and approval flow
4. Implement `colony_claim_task`, `colony_complete_task`, `colony_fail_task` tools

### Phase 4: Aeva Node Registration (Hermes Side)

1. Hermes cron job that registers as Colony mesh node
2. Capability advertisement (terminal, file, web, browser, etc.)
3. Poll loop for job claiming
4. Execute with tools and report back

### Phase 5: In-Session Injection (Corrected)

1. Extend `/v1/host/context/assemble` with `in_session_initiatives`
2. Update provider formatting (if needed)
3. Implement acknowledgment detection
4. Add escalation after 24h ignored

### Phase 6: Digest Mode

1. Accumulate completed agent_action results
2. Format and send bundled digest
3. Configurable digest interval

---

## 8. Multi-Agent Vision (Queen/Worker Architecture)

Colony's mesh model already defines:
- `NodeRole.SOVEREIGN` — The queen node with full brain capabilities
- `NodeRole.REGENT` — Backup brain, can assume sovereignty if needed
- `NodeRole.VASSAL` — Worker node, handles delegated tasks

**Aeva's role:** SOVEREIGN — She is the primary external agent that Marc interacts with. She can:
- Claim and execute jobs from the task queue
- Post new jobs back to Colony for other workers
- Escalate to Marc when decisions are needed

**Future workers:** Additional VASSAL nodes can register with specific capabilities:
- A coding specialist node (claims `JobType.CUSTOM` with `capabilities={"code", "review"}`)
- A research node (claims `JobType.RESEARCH`)
- A monitoring node (claims `JobType.MONITORING`)

The existing `Scheduler` handles affinity-based assignment automatically. Aeva posts jobs, the scheduler assigns them to the best worker, and results flow back.

**Worker capacity:** Each node advertises `max_concurrent` jobs. The scheduler respects this and won't over-assign. Aeva's default: 4 concurrent jobs.

---

## 9. Migration Notes

- **No breaking changes to existing webhooks** — `/v1/delivery/pending` continues to work for gateway polling
- **Existing self-initiatives unchanged** — `subsystem_health`, `data_quality`, etc. still auto-execute via skills
- **initiative_store becomes source of truth** — bridge in-memory list becomes a cache, not the primary store
- **Task queue is additive** — Existing flows don't change; agent_action initiatives get a new path
- **Agent registration** — The agent identifies itself as `"aeva-hermes-01"` (configurable via env var)

---

## 10. Design Decisions

### 10.1 Dynamic Capabilities — YES

Aeva derives her advertised capabilities from the Hermes toolsets enabled in `~/.hermes/config.yaml`:

| Hermes Toolset | Colony Capability |
|----------------|-------------------|
| `terminal` | `terminal` |
| `file` | `file` |
| `web` | `web` |
| `browser` | `browser` |
| `git` (implicit via terminal) | `git` |
| `code_exec` | `code_execution` |

On cron startup, Aeva reads her current toolsets, builds a `WorkerCapabilities` object, and registers (or re-registers) with Colony. If toolsets change, the next cron tick updates her capabilities. This prevents claiming jobs she cannot execute.

### 10.2 Job Type — Add `AGENT_ACTION`

Add `AGENT_ACTION` to the `JobType` enum in `task_queue/models.py`:

```python
class JobType(str, Enum):
    INFERENCE = "inference"
    TRAINING = "training"
    DATA_PROCESSING = "data_processing"
    SYSTEM_MAINTENANCE = "system_maintenance"
    RESEARCH = "research"
    MONITORING = "monitoring"
    SYNTHESIS = "synthesis"
    DESKTOP = "desktop"
    BROWSER = "browser"
    AGENT_ACTION = "agent_action"  # NEW
    CUSTOM = "custom"
```

**Why:** Semantically correct, avoids collision with other `CUSTOM` jobs, lets the scheduler filter/agent jobs separately. Backward-compatible because SQLite stores `job_type` as TEXT and the scheduler matches against worker `job_types` without validating against a fixed enum list.

### 10.3 Cross-Node Trust — Bearer Auth Now, Mesh Crypto Later

**Phase 1 (now):** Use the existing Bearer token auth for queue API endpoints. All nodes in Marc's trusted local mesh share the same `COLONY_API_KEY`. Simple, works today, no crypto changes needed.

**Phase 2 (future):** When VASSAL workers are added and the mesh crypto is hardened (real keypairs instead of `f"sig-{uuid}"` placeholders), migrate to request signing:
- Each worker generates an Ed25519 keypair
- Signs `POST /v1/queue/jobs/claim` requests with its private key
- Sovereign verifies against the worker's registered public key in the mesh

This aligns with the existing `MeshNode.public_key` field and the Colony vision, but it is NOT a blocker for v0.13.0. The fake node certificate issue (TODOs.md #2) should be fixed as a separate security hardening effort.

---

## 11. Rejected Approaches (Documented for Posterity)

- **`pre_llm_call` hook injection** — Hermes hook signature doesn't support modifying system prompt or messages list. Returns string appended to user message only. Architecturally invalid for silent context injection.
- **Pure webhook-driven proactive messaging** — Creates orphan messages that bypass agent judgment. Agent becomes a relay, not an executive.
- **pre_llm_call + tool-based discovery hybrid** — Over-complicated. Prefetch path is simpler and already exists.
- **Building a new queue system** — Colony already has `QueueManager`, `Scheduler`, `WorkerNode`. Reinventing would be wasteful and fragment state.

---

*End of spec. Awaiting review before implementation.*

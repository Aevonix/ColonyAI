# P3 Cognition Spine Migration and Rollback

This runbook does not authorize a live deployment. It defines a reversible
cutover after the source commit is merged and pinned.

## Preconditions

1. Record the Colony revision, tree hash, launch definition, active feature
   flags, queue depth, and current legacy Goal/Initiative/Project counts.
2. Stop if any participating repository or deployed release cannot be
   reproduced from a pinned revision.
3. Back up, checksum, and test-open copies of:
   - `colony-workspace.db`;
   - `colony-projects.db`;
   - `task_queue.db`;
   - the legacy goals and initiatives databases;
   - the intended `colony-cognition.db` path if it already exists.
4. Inventory queue workers. Exactly one configured
   `COLONY_THOUGHT_WORKER_NODE_ID` may own thoughts. It must load
   `ThoughtOnlyInferenceHandler`; empty/all-types or generic inference workers
   are not valid. Do not manually grant `cognition_scoped` to another worker.
5. Confirm the canonical WorkOrder receipt verifier is wired. P3 Project
   settlement will remain open without verified execution receipts.
6. Confirm autonomous delivery remains held. Do not add `messaging:send` to
   bypass the missing DeliveryAuthorityV1 envelope.
7. Assign one writer for the cognition/project plane during each cutover
   step. Do not edit `server.py`, ProjectEngine, or queue wiring concurrently.

## Implemented server integration

`server.py` now calls one directly tested `_attach_cognition_spine` seam after
ProjectEngine initialization. It creates no cognition database while the flag
is off. When enabled, it requires the durable task queue, workspace, concern
store, project store/engine, and directive manager before it creates or
attaches anything. Missing dependencies fail closed for autonomous cognition
without taking owner-directed daily work or the sidecar offline.

The startup snapshot fixes the concurrent-project limit and capability set for
the service lifetime. The default capability authority remains the weakest
useful set (`memory:read,reasoning,web:read`), and the project limit must be an
integer from 1 through 100. Change either only through a reviewed config
release and restart. Do not construct a second store or engine elsewhere.

## Stage 1 — source verification, flags off

Keep `COLONY_COGNITION_SPINE=off`.

Run:

```bash
python -m pytest -q tests/test_cognition_goal_spine.py
python -m pytest -q tests/test_cognition_phase_b_runtime.py
python -m pytest -q tests/test_cognition_server_wiring.py
python -m pytest -q tests/test_event_concerns.py tests/test_workspace.py \
  tests/test_projects.py tests/test_work_orders.py tests/test_prompt_evals.py
python -m pytest -q tests
```

Acceptance: full test suite green; no new live database; legacy behavior is
unchanged. Rollback: revert the source/release pin.

## Stage 2 — shadow canary

Suggested minimum: 24 hours and at least 25 completed thought jobs across at
least three concern kinds.

Set:

```text
COLONY_COGNITION_SPINE=shadow
```

Keep the existing event/workspace/project modes unchanged. Restart only the
Colony sidecar after the deployment's normal idle/health precheck.

Verify:

- exactly one queue job per concern material digest/attempt;
- no duplicate posts across restart;
- all jobs are `thought`, read-only, bounded, and have matching digests;
- no world-model or skill rows are written as a thought side effect;
- invalid output remains a resumable active concern;
- GoalProposals have five inspectable decisions when all checks pass;
- Projects created by P3: zero;
- concerns settled by P3: zero;
- legacy daily work remains functional;
- p50/p95 queue wait and execution latency remain within the agreed
  background-work budget;
- `GET /v1/host/cognition/spine` reports the exact runtime and thought worker
  route without exposing an owner-private trace to a guest credential.

Rollback: set the flag `off` and restart the sidecar. Preserve the cognition
database as shadow audit evidence.

## Stage 3 — reconciliation before exclusive live

Inventory every non-terminal legacy Goal and thinker/project Initiative.
For each, record exactly one disposition:

- map to an existing owner Project;
- create an explicit owner Project;
- archive as duplicate/obsolete;
- leave running to completion with a named sunset date.

Do not silently copy legacy goals into P3: they lack Concern/ThoughtJob and
policy provenance. Drain or explicitly grandfather in-flight legacy queue
jobs. Capture the zero/known remainder as an artifact.

## Stage 4 — live canary

Start with the smallest capability set and one owner-private concern class.

```text
COLONY_COGNITION_SPINE=live
COLONY_WORKSPACE=live
COLONY_COGNITION_AVAILABLE_CAPABILITIES=memory:read,reasoning,web:read
COLONY_THOUGHT_WORKER_NODE_ID=<exact local cognition worker node>
```

If `COLONY_DRIVE_GOVERNANCE_MODE=live`, activate an owner-ratified charter
before expecting P3 to admit work. A missing or expired active charter is an
intentional hold. Shadow/legacy concerns also remain held until the scoped
owner promotes their exact material digest; promotion does not bypass the
five P3 policy gates or the WorkOrder/action approval path.

Live acceptance requires a production-like trace showing:

1. one scoped event and concern;
2. one durable ThoughtJob and typed GoalProposal;
3. five allowed policy decisions;
4. one deterministic Project;
5. one WorkOrder carrying every upstream reference;
6. one verified ExecutionResult/receipt;
7. Project `outcome=succeeded`;
8. one evidence-bearing concern settlement;
9. no upstream source auto-resolution;
10. no legacy autonomous goal/project creation;
11. guest replay reveals no owner-private reference or content;
12. no deliver job is emitted;
13. a worker stop changes P3 health to unhealthy before another thought can
    be claimed;
14. a guest cognition trace contains no owner-private concern or proposal.

Suggested canary: 48 hours and at least ten complete receipt-backed traces,
with zero duplicate Projects, scope leaks, unverified settlements, or
unexpected daily-function regressions.

## Rollback

1. Set `COLONY_COGNITION_SPINE=off` and restart only the Colony sidecar.
2. Confirm legacy goal inference/activation and direct workspace paths return
   according to their pre-cutover flags.
3. Leave P3-created WorkOrders in their current canonical queue state; do not
   duplicate or delete them. Cancel only through the normal audited queue API
   if the owner chooses.
4. Preserve `colony-cognition.db`, additive Project columns, and concern
   settlements as evidence.
5. If a binary/database compatibility issue requires file restoration, stop
   the sidecar, verify backup checksums, restore all related SQLite files as
   one timestamped set, then start and run the pre-cutover probes.

Rollback success: daily legacy work is functional, no new P3 jobs appear,
existing queue authority digests are unchanged, and every live artifact is
still attributable to a pinned revision.

## Phase B source verification record (2026-07-12)

- focused cognition, P7, worker, scoped-authority, and canonical-approval
  matrix: `295 passed`;
- complete Colony sidecar suite: `3232 passed, 118 skipped, 21 warnings`.

The warnings are the existing async/deprecation warnings in causal-query,
world-model, vector, and legacy component tests; this slice added no warning.

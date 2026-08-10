# P6 Situation + Expectation Migration and Rollback

This runbook keeps the new spine off by default and preserves the existing
expectation database. Perform every stateful step against a pinned release.

## Before enabling

1. Confirm the sidecar release revision and a clean source tree.
2. Stop autonomous writers or use the deployment's normal idle-queue gate.
3. Take SQLite online backups (not file copies of live WAL databases) of:
   `colony-expectations.db`, `colony-cognition.db`, and any project/workspace
   database used by the deployment.
4. Record the event-journal cursor/high-water and preserve the current service
   configuration/plist/unit.
5. Run the focused P6 tests and the full sidecar suite from the pinned tree.

## Stage 0 — off/default

Leave `COLONY_SITUATION_SPINE` unset or `off`. Do not construct
`SituationStore`; the default-off attachment path must create no new database.
The additive expectation migration occurs only if the already-enabled legacy
expectation engine opens its existing database.

Verify:

- no `colony-situation.db` was created;
- existing `/self/expectations` behavior remains available;
- voice, delivery, action, and project services are unchanged.

Rollback: none required.

## Stage 1 — shadow

Set:

```text
COLONY_SITUATION_SPINE=shadow
COLONY_SITUATION_BOOTSTRAP=tail
```

Use `tail` for the first live canary so old retained events are not mistaken
for present state. Use `replay` only in an isolated copy with an explicitly
reviewed retention window.

Attach the store/reducer/observer, but do not replace a live P3 validator or
delivery policy. Run the reducer periodically and compare observer snapshots
with the source events.

Verify:

- cursor advances monotonically and status has no retention gap/error;
- replaying a processed event is a duplicate, not a second mutation;
- no fact has model/inference/prose source kind;
- stale and unknown categories are visible;
- owner and subject-private projections do not cross;
- no live project/delivery decision changed.

Rollback:

1. Set `COLONY_SITUATION_SPINE=off`.
2. Restore the prior service configuration and restart only the sidecar under
   the deployment's normal restart guard.
3. Keep `colony-situation.db` as evidence, or archive it. It is not read in off
   mode.

## Stage 2 — live policy canary

Prerequisites:

- shadow snapshots match real source receipts over an agreed observation
  window;
- real adapters emit fresh resource and service/capability observations;
- retention-gap status is clear;
- P3 remains shadow while comparing situation decisions.

Set `COLONY_SITUATION_SPINE=live`. Compose the gate with P3's existing project
capacity validator; never replace capacity, boundary, or authority checks.
Start with one owner-private subject and read-only/reversible proposals.

Acceptance traces must show:

1. concern/source event;
2. immutable situation snapshot;
3. situation verdict and its evidence refs;
4. the independent P3 charter/boundary/authority decisions;
5. no project for unknown/stale/degraded cases;
6. no cross-scope observer result.

Rollback is the same flag/config rollback as Stage 1. The situation database
is additive and independent, so rollback does not require rewriting Colony's
project or expectation state.

## ExpectationV2 canary

1. Back up `colony-expectations.db` with SQLite online backup.
2. Open it with the pinned release and verify all old rows read as V1.
3. Generate one future, evidence-bearing V2 expectation in shadow.
4. Ingest one disposable structured outcome receipt.
5. Verify first-valid-decision-wins, exact scope, event time versus horizon,
   and the calibration cohort.
6. Verify a bare boolean resolver cannot settle that V2 row.

Rollback code by restoring the pinned prior release. The new columns/tables are
additive and ignored by the old code. If database rollback is required, stop
the expectation writer and restore the online backup; do not copy a live WAL
database over the active file.

## Startup and API attachment (implemented)

`server._attach_situation_spine` now runs after P3 attachment. Its contracts
are deliberately stricter than the earlier draft:

- `off` clears stale in-process handles, constructs no store, and creates no
  `colony-situation.db`;
- `shadow` attaches the store, reducer, periodic `situation_reduce` observer,
  and read API, but does **not** replace P3's validator. A shadow preview is
  never returned to or reused by a live project writer;
- when the canonical task queue is present, the same periodic observer records
  one scoped `resource` observation from queue execution readiness and
  heartbeat-fresh worker availability. Probe failure records no replacement
  fact, so the prior fact expires under its heartbeat-capped TTL;
- `live` first preserves P3's existing capacity verdict. A capacity denial is
  returned unchanged; only an allow proceeds to a bounded reducer refresh,
  scoped snapshot, and P6 gate;
- reducer error/status failure, snapshot failure, invalid gate output, gate
  exception, or live attachment failure all produce an explicit deny. None
  falls back to capacity-only allow;
- shutdown clears the HTTP handles, restores P3's original validator, and
  idempotently closes the P6 SQLite connection.

The periodic interval is bounded to 5–3600 seconds and defaults to 30. Override
it with `COLONY_SITUATION_REDUCE_INTERVAL_SECONDS` only in the pinned service
configuration.

`GET /v1/host/self/situation` requires `cognition:read`. The handler derives
the subject and viewer lane from `RequestAuthority`; a query may select only a
person already granted to that credential. It never accepts a viewer scope or
shareability assertion from the caller.

## Implementation validation (2026-07-12)

The regressions were written around the reproduced failure contracts before
the implementation was accepted: late fulfillment scored as an on-time hit,
receipt-less boolean resolution, scope-crossing projections, replay races, and
unknown situation treated as safe. Final isolated-worktree results:

```text
Focused P6/legacy expectation suite: 56 passed
Full sidecar suite: 2640 passed, 118 skipped, 21 pre-existing warnings
```

The warnings are the existing FastAPI/Pydantic deprecations, aiosqlite test
loop teardown warnings, vector-store deprecation, and two existing un-awaited
mock warnings; no P6 test emitted a new warning.

The later P6/P7 server-wiring pass added startup, attachment-failure,
lifecycle, route-scope, cross-viewer, and complete HTTP approval-flow
regressions. On the isolated `833e564` integration base its final evidence was:

```text
Focused P3/P4/P6/P7/expectation/authority integration: 147 passed
Full sidecar suite: 2733 passed, 118 skipped, 21 pre-existing warnings
```

The warning families are the pre-existing Starlette/Pydantic deprecations,
aiosqlite test-loop teardown warnings, vector-store deprecation, and existing
un-awaited mocks; the wiring tests emitted no warning.

# Receipt-derived cognition evidence migration and rollback

This procedure is source-only until a deployment owner explicitly authorizes
a canary. It does not authorize a service restart, live project execution,
provider traffic, voice change, or Hermes patch.

## 1. Capture independent rollback points

Record the source revision, effective environment, and installed Colony import
before changing anything:

```text
git rev-parse HEAD
git status --short
python -c "import colony_sidecar; print(colony_sidecar.__file__)"
python -m pip show colony-sidecar
```

Save the pinned prior source artifact or wheel as well as the candidate. A git
revision alone is not a local-install rollback artifact.

Pause project dispatch before taking a data rollback point. Use SQLite online
backup, not a file copy of a live WAL database, for:

- `colony-projects.db`;
- `colony-self-model.db`;
- `colony-expectations.db` when expectations are enabled;
- `colony-cognition-evidence.db` after it exists.

Record the host journal cursor and preserve its event directory while its
writer is stopped. These databases and the journal form one evidence rollback
unit once live learning begins. Preserve newer generations read-only when
rolling back; never delete evidence to make a dashboard look clean.

Before any sidecar restart on a host deployment, verify the custom phone
system's `call_state` is `IDLE`. This feature does not require a Voice Core,
intercom, Meet, or Hermes restart.

## 2. Verify the pinned candidate

From the candidate tree:

```text
PYTHONPATH=sidecar python -m pytest -q \
  sidecar/tests/test_project_event_outbox.py \
  sidecar/tests/test_cognition_evidence_pipeline.py \
  sidecar/tests/test_cognition_evidence_server_wiring.py

PYTHONPATH=sidecar python -m pytest -q sidecar/tests
```

Do not proceed if the source tree is dirty, the focused suite fails, or the
full sidecar suite regresses.

This is the first deployable revision of the evidence outbox. Its signed
project evidence contracts are V2 so the earlier source-only V1 reducer also
fails them closed on a code rollback. A database from an earlier candidate may
contain payload-only V1 outbox digests and no signed first-stage mode; this
revision intentionally fails those rows closed.
Do not point the canary at such a development database. Preserve it read-only,
start from the coordinated pre-candidate backup (or an isolated clean state),
and keep `COLONY_COGNITION_EVIDENCE=off` until the new outbox is healthy.

## 3. Stage 0 — learning off

Leave `COLONY_COGNITION_EVIDENCE` unset or set it to `off`.

Expected behavior:

- `colony-cognition-evidence.db` contains only the durable off-mode
  passthrough cursor/range audit and no evidence projection rows;
- `project_event_outbox` is added additively to `colony-projects.db`;
- the `cognition_evidence_reduce` scheduler drains immutable project result
  events and checkpoints the off interval without learning;
- projected outbox rows carry a receipt digest over their exact journal
  sequence, event ID, recorded time, and staged-envelope identity; cleanup must
  leave any row with missing or mismatched receipt data unacknowledged;
- every new outbox payload captures the normalized server-side mode at its
  first staging, so an event racing a later live cutover remains ineligible
  for learning;
- existing project competence behavior is unchanged;
- `/v1/host/cognition/evidence` reports learning off and truthful outbox state.

Exercise one source-only or isolated result fixture and verify keyed replay
does not duplicate its journal event. No live action is needed for this stage.

Rollback: restore the prior pinned Colony package/source. Old code ignores the
additive outbox table. Keep the migrated project database and journal for
audit, or restore the coordinated Stage-0 backup while all writers are stopped.

## 4. Stage 1 — shadow

Use an explicitly pinned service environment:

```text
COLONY_COGNITION_EVIDENCE=shadow
COLONY_COGNITION_EVIDENCE_BOOTSTRAP=tail
COLONY_COGNITION_EVIDENCE_INTERVAL_SECONDS=30
COLONY_COGNITION_EVIDENCE_GAP_POLICY=stop
```

Use `tail` for the first live-system canary so retained historical traffic is
not silently reinterpreted as a new observation window. Use `beginning` only
on an isolated copy or after explicitly reviewing journal retention.

Acceptance criteria:

1. attachment state is `attached`, cursor advances, lag returns to zero, and
   outbox status has no error;
2. verified successes, local failures, neutral results, and unverified claims
   receive the expected projection classifications;
3. no new `cognition_evidence_v1` competence event is written;
4. no `ExpectationV2` is resolved by the evidence reducer;
5. owner-private and subject-private traces do not cross credentials;
6. voice, intercom, phone, meetings, and Hermes behavior are byte-for-byte
   outside the change.

Before moving from shadow to live, require evidence cursor lag zero and a
healthy outbox. Events first staged in shadow remain trace-only after the
switch; only newly staged live events can reach learning sinks.

Rollback: set the mode to `off`, verify `call_state=IDLE`, and restart only the
Colony sidecar through the deployment's normal rollback path. Preserve the
shadow evidence database; off mode opens it only to checkpoint the passthrough
cursor and never adds projection or learning rows.

## 5. Stage 2 — live learning canary

Prerequisites:

- the shadow window has no unexplained outbox errors, journal gaps, scope
  mismatches, or duplicate projections;
- the canonical SelfModel and autonomy scheduler are healthy;
- independent receipt verification is configured for any successful external
  effect being used as evidence;
- the owner has reviewed the exact project/result trace in the Operator Deck;
- the rollback rehearsal restored both source and local installed version.

Set `COLONY_COGNITION_EVIDENCE=live`, retaining `GAP_POLICY=stop`. Begin with
one owner-private, read-only or reversible WorkOrder. This flag grants no action
authority and does not bypass WorkOrder, boundary, approval, or receipt gates.

Acceptance criteria:

1. exactly one verified result produces exactly one competence event with
   source `cognition_evidence_v1`, an immutable `execution-attempt:*` source
   reference, and contract `ExecutionResultV1:1`;
2. the older direct ProjectEngine outcome writer produces no second row;
3. only an expectation created before that event and matching its exact scope
   and subject is resolved; retryable failures do not settle logical tasks and
   step results do not settle whole-project expectations;
4. replay and a reducer crash after either sink create no duplicate row;
5. unverified external reports and unverified successes remain trace-only;
6. a forged digest, malformed outbox row, journal corruption/gap, or local-
   ledger mismatch stops the cursor and is visible;
7. a copied otherwise-valid project payload under a second journal identity
   fails its exact outbox rejoin and cannot produce a second learning row;
8. action, delivery, approval, and voice latency remain within their existing
   independently owned budgets.

Any custom project-journal acknowledger must accept and validate the four exact
receipt fields (`expected_seq`, `expected_event_id`, `expected_recorded_at`, and
`expected_request_digest`). Key-only acknowledgement callbacks are rejected at
attachment so production cannot release a marker without receipt validation.

Do not set `COLONY_COGNITION_EVIDENCE_GAP_POLICY=acknowledge` merely to clear a
red health tile. First preserve the evidence generation, identify the missing
sequence range, and record why losing those events is acceptable. The durable
gap record is an admission of missing evidence, not reconstructed truth.

## 6. Functional and code rollback

Immediate functional rollback:

1. stop new project dispatch;
2. wait for in-flight WorkOrders to reach a durable terminal or held state;
3. verify the project outbox is healthy and the evidence cursor lag is zero;
4. set `COLONY_COGNITION_EVIDENCE=off`;
5. verify `call_state=IDLE`;
6. restart only the Colony sidecar;
7. verify new future project outcomes use the previous writer and the evidence
   database receives no new projection or learning rows. Its passthrough cursor
   continues to advance so a later return to live cannot relearn off-period
   outcomes. The project outbox continues to retain and project operational
   truth.

If zero lag cannot be reached, do not hide it by switching modes. Preserve the
outbox, journal, and databases as one generation and investigate first. The
first-stage mode binding prevents an off-to-live double-learn race, but a live
event deliberately skipped during rollback is still evidence that must be
reconciled rather than silently discarded.

For code rollback, restore the prior pinned local Colony install and effective
environment. A pre-feature release ignores the additive outbox/evidence
schema; the source-only V1 candidate does not understand the hardened envelope
and may report it unhealthy. If a data rollback is required after live
learning, stop all related writers and
restore the coordinated project, competence, expectation, cognition-evidence,
and journal generation captured in section 1. Preserve the newer generation
read-only for reconciliation. Never restore only `colony-self-model.db`; that
would separate learned state from the receipt and cursor that justified it.
An older source-only evidence reducer rejects the V2 rows and must remain off
after a code rollback; re-enabling that older reducer requires restoring the
coordinated older data generation too.

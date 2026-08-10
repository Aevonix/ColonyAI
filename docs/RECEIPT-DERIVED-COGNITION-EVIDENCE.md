# Receipt-derived cognition evidence

Colony can now learn from completed project work without trusting a model,
worker, queue status, or external producer to declare its own success. The
feature is migration-gated and defaults to off.

## Ownership and flow

The durable flow is:

1. `ProjectStore.save_execution_result()` commits the exact
   `ExecutionResultV1`, execution attempt, step reference, and immutable
   `ProjectExecutionEvidenceV2` outbox row in one SQLite transaction. The
   server-controlled cognition mode from the first staging is part of that
   signed payload; it is never supplied by a worker or recaptured on replay.
2. `ProjectEventProjector` appends the outbox row to the canonical host journal
   using a stable event key. A crash on either side reuses that key and cannot
   create a second event. The outbox digest binds the key, type, occurred time,
   and payload; malformed JSON or envelope tampering is retained as a visible
   per-row error and cannot poison the rest of the drain. A second receipt
   digest binds that envelope to the exact journal sequence, event ID, and
   recorded time. Cleanup releases the keyed journal marker only after both
   digests and the marker's keyed-request identity match.
3. `CognitionEvidenceReducer` replays the host journal and rejoins each event
   first to the exact projected outbox row (stable key, envelope digest,
   journal sequence and event ID, type, time, and payload), then to the local
   immutable WorkOrder, immutable execution-attempt reference, result digest,
   server-owned queue time, project scope, and receipt references. A copied
   valid payload in a second journal event is not evidence.
4. Every projection is written to the append-only cognition evidence ledger.
   In live mode only, verified non-neutral outcomes are folded idempotently
   into project competence and matching `ExpectationV2` rows.

The project outbox is operational event truth and is drained even when
learning is off. Default-off creates only the small evidence database needed
for a durable passthrough cursor; it writes no projection or learning rows.
This prevents results already counted by the legacy writer while off from
being replayed as new receipt-derived learning when live is restored. The
signed first-stage mode also closes the cutover race where an off-period result
is projected after the last off-mode cursor checkpoint: off- and shadow-staged
events remain trace-only even if their first reduction occurs in live mode.

## Evidence authority

- A success is `verified` only when the local result says `succeeded`, its
  independent verification result is `verified`, and at least one bounded
  receipt reference is present.
- A locally bound terminal execution failure is failure evidence, never a
  success claim. A retryable failure does not settle the logical task's
  expectation until its bounded attempt budget is exhausted. Cancelled and
  skipped work is neutral.
- A succeeded result without independent verification is retained as
  unverified and cannot train competence or resolve an expectation.
- Project completion is verified only when every step is done and every result
  is an independently verified success. An unverified completion remains an
  active concern rather than cosmetically resolving one. Individual step
  results cannot settle a whole-project expectation; only the verified
  terminal-project projection can.
- `cognition.external.*` events are always `reported_unverified`. Their
  server-derived scope envelope is checked, but they never train this pipeline.
- Voice, meetings, audio, Hermes turn execution, and communication transport
  authority are outside this feature. The host's custom voice core is unchanged.

The reducer is the sole project-competence writer in live mode. The older
in-process `ProjectEngine._record_outcome()` writer is suppressed in both
shadow and live, so shadow is observation-only and one real result cannot be
counted twice. Off restores the previous writer for future results while its
passthrough cursor prevents off-to-live replay duplication.

## Modes

`COLONY_COGNITION_EVIDENCE=off|shadow|live` defaults to `off`. Unknown values
resolve to off.

- `off`: no projection validation or evidence-derived learning. The reducer
  drains the project outbox and advances only an audited passthrough cursor;
  the legacy direct writer remains active.
- `shadow`: validate and ledger projections, expose scoped traces, and write no
  competence or expectation outcome.
- `live`: do everything in shadow and fold only verified non-neutral evidence.
  Both the current reducer mode and the event's signed first-stage mode must be
  live. Startup requires the canonical `SelfModel` and autonomy scheduler. An
  unhealthy initial outbox or reducer fails attachment closed.

Additional controls:

- `COLONY_COGNITION_EVIDENCE_BOOTSTRAP=beginning|tail` defaults to
  `beginning`.
- `COLONY_COGNITION_EVIDENCE_INTERVAL_SECONDS` defaults to 30 and is bounded
  to 5–3600 seconds.
- `COLONY_COGNITION_EVIDENCE_GAP_POLICY=stop|acknowledge` defaults to `stop`.
  `acknowledge` records an explicit durable gap; it does not recreate lost
  evidence and must not be used as an automatic availability setting.
- `COLONY_EVENT_CONCERNS_GAP_POLICY` and `COLONY_SITUATION_GAP_POLICY` use the
  same stop-by-default contract for the other host-journal cognition reducers.

## Operator visibility

`GET /v1/host/cognition/evidence` requires `cognition:read`. It returns:

- attachment mode and health;
- journal cursor, high-water, and lag;
- project outbox pending/projected/acknowledgement status and last error;
- a request-authority-scoped evidence trace, optionally filtered by project.

The handler derives the viewer lane from server authentication. It does not
accept viewer scope or shareability from the caller. This is the intended
Operator Deck data source for explaining why the assistant did or did not learn from a
result.

## Crash and replay properties

- Project result plus outbox staging is one transaction.
- Journal append plus outbox completion is recoverable with a stable keyed
  append. Journal acknowledgement failure never reprojects the event. Missing,
  malformed, or modified projection-receipt metadata remains unacknowledged and
  unhealthy, preserving the keyed marker for recovery.
- Every project outcome must match the exact server-owned outbox projection;
  a valid payload copied under another journal identity fails closed.
- Off- or shadow-staged outcomes cannot become learning solely because the
  reducer later starts in live mode.
- Competence uses a unique source/event key.
- Evidence ledger rows and gap admissions are database-enforced append-only.
  Each trace row revalidates both its projection and full ledger-envelope
  digest before the operator API may return it.
- Expectation observations use a deterministic event/prediction identity.
- Competence and expectation evidence point at the immutable attempt rather
  than the logical result head that a later retry may replace.
- Exact result redelivery remains idempotent, but a genuinely new attempt is
  rejected once its step or project is terminal. This keeps an already staged
  terminal-project projection replayable against a stable result head.
- Queue polling inserts missing WorkOrder parents or validates their persisted
  lifecycle and full authority provenance atomically; it never blind-upserts a
  stale project or step back out of a terminal state.
- A crash after either learning sink but before evidence-ledger commit safely
  replays without duplicate learning.
- A later attempt cannot overwrite an expectation sealed by another final
  observation; retryable failures do not prematurely create that seal.
- An expectation created after the outcome cannot be resolved by replaying
  that older event.
- Checksum corruption, replay failure, retention loss, and internal sequence
  gaps stop before the missing sequence by default. Every explicit gap
  acknowledgement is durable and visible to the operator.

The migration and rollback procedure is in
`docs/runbooks/RECEIPT-DERIVED-COGNITION-EVIDENCE-MIGRATION.md`.

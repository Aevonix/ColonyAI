# Explicit shared undertakings

Two Hermes sessions can reserve the same existing commitment ID. One wins the
SQLite transaction and the other receives the current undertaking, including
the owning session and obligation description. This closes an explicit
coordination loop; it does not prevent contradictory free-text promises.
Existing deduplicating creation now checks and inserts in the same SQLite
transaction, so racing creates return one ID under the existing matching rule.
Introspection uses that path too. Deliberately non-deduplicating creation and
previously stored duplicate IDs remain distinct obligations.

`colony_commitment_work` exposes `claim`, `status` and `release`. The adapter
supplies the participant and native session/task/turn binding. The model cannot
supply those fields or the fencing token. The HTTP operation reuses the existing
`turns:write` scope and exact person grants. Legacy anonymous/global identity is
insufficient. The new `commitment_work` table lives in `commitments.db`.

A claim lasts 120 seconds. Once a turn explicitly holds it, each subsequent
native tool checks and renews its token before dispatch. Ordinary turns have no
additional coordination request. Native children inherit the parent's held
undertaking. A native compression session change retains the same task/turn.

An inactive lease may be reclaimed without asking the owner. The new claim has
a different token. A previous holder cannot renew or release it, and its next
tool step stops. Uncontested renewal and competing reclaim serialize in the
same SQLite transaction. Sidecar unavailability holds only a turn that already
owns an explicit undertaking; it does not stop unrelated chat or owner tools.
The agent can inspect status and retry when the service returns. An explicit
release also detaches this turn after an authoritative closed or superseded
response, allowing it to stop that work and continue elsewhere without changing
the new holder. Failed renewals alone never detach a token. Children retain
their own token snapshots after a parent stops, including across compression.

`release` ends work coordination; it does not claim completion. The existing
commitment resolution path remains responsible for fulfilled/cancelled state
and consent. Closed obligations cannot be reclaimed. Context contains the
commitment ID, description, due state and work/session state. Expiration means
the previous observation is stale, not that work completed.

The owner current-work view also reads bounded claimed/running records directly
from the existing task queue. It includes a short task description, worker ID,
canonical claim attempt, status and heartbeat age. This covers Colony workers
and the private action executor while they use that queue protocol. No duplicate
worker observations or heartbeat writer are introduced. Guest views receive no
global queue rows. Missing heartbeats mean unknown liveness, never completion.

This lease is not external-effect authority. Spending, sending, production
changes and other consequential work retain their existing consent and effect
idempotency contracts. It cannot cancel an already-running shell command,
retract a dispatched effect, or prove that an unrelated legacy queue job belongs
to a particular commitment. Canonical queue state remains the source for such
jobs; an uncertain effect must be reconciled there before repeating it. There
is no global idle gate, controller or new approval ladder.

Qualification includes independent SQLite clients racing and reopening the
store, stale-token rejection after unattended reclaim, real scoped HTTP
authority, native adapter sessions racing through that API, old-holder tool
blocking, and installed-wheel native Hermes dispatch against the real durable
store. The canonical worker view is tested through actual queue claim, start,
heartbeat aging and completion. A live two-session commitment exercise remains
the deployment acceptance test.

The same owner-only current-work surface also projects accepted local capability
briefings from the existing initiative ledger. It exposes the initiating event,
native job/execution IDs, current initiative status, and a bounded unverified
result excerpt/report path. Read-time projection never creates, recovers or
executes an initiative. Missing data is reported unavailable; assigned status
does not establish process liveness. Guest views contain no such rows. This is
operational work context, not an additional factual-memory writer or a grant.
Ordinary turn context includes active work and only the latest capability result;
the bounded seven-day result history remains available through the API.

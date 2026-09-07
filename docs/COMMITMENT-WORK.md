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

## Sharing an accepted local draft

`colony_accept_local_draft` associates ordinary session requests with one active
local draft for the same explicit commitment. Concurrent callers receive the
same initiative and native Kanban task. Paraphrasing the question or reordering
the source paths joins the existing active request: its canonical question and
sources are returned with `acceptance_matches_request: false`. The caller must
report that scope rather than claiming its changed request was performed.
This association does not deduplicate arbitrary free-text promises or unrelated
commitment IDs. Existing legacy duplicates are not silently merged.
A failed legacy cron draft remains active while its existing transient-error
retry budget remains; a permanent failure or exhausted budget permits a fresh
draft. Acceptance and legacy reconciliation use the same retry rule.

After work ends, an exact question/source-path match returns the historical
result. It does not read files again or establish that they are unchanged. A
fresh draft requires an explicit owner request and `new_draft: true` in a new
turn; a changed scope without that choice, or a fresh request while work is
active, returns the existing initiative ID and a scope-conflict reason. Each
session/turn acceptance retains its original association, so replaying a lost
response still returns that draft after another fresh draft has started.
Standalone drafts without a commitment retain their existing per-turn identity.

If the accepting turn holds this commitment, the adapter supplies its exact
transport-held token with the acceptance and detaches only after the backend
confirms release. The native worker then uses its existing claim path; it does
not inherit a lease held by the accepting chat. Model arguments cannot supply
holder fields or tokens. A different undertaking held by the accepting turn
must be explicitly released first. Existing child snapshots remain fenced.

The acceptance mapping and initiative share the existing initiative database.
The attached transaction serializes competing requests and rolls back ordinary
failures, but the two stores use WAL, so it does **not** promise host-crash
atomicity across the initiative and commitment files. A replay reconciles a
persisted acceptance with its exact old held token before confirming release;
it never releases a newer worker's token. In the inverse partial state, the
exact original released token and absence of that acceptance permit its draft
to be recovered, including an explicit fresh generation after older work ended.
The same recovery restores a missing join to an existing canonical draft.
A different token is rejected. Existing native
task association and retained-artifact reconciliation handle later interrupted
dispatch or completion. There is no additional recovery process.

Focused qualification covers independent authenticated HTTP callers, changed
wording/source-order collisions, explicit fresh drafts, replay after reopening,
normal rollback and both simulated persisted handoff boundaries. The packaged
Hermes test uses two normal sessions' actual tools and a native worker with
controlled model responses: one task, one retained report, and the same saved
result on both session replays. Source mutation and interrupted native completion
retain their existing rejection/reconciliation tests. Completing a draft still
does not fulfil the broader commitment or authorize delivery. Physical voice
identity, capture and playback remain separate, unqualified hardware paths.

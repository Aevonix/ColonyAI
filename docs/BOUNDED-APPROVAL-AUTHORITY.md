# Bounded Approval Authority

Colony's approval ledger is transport-neutral. A host may collect a decision
from a phone, web console, CLI, or another trusted adapter, but Colony accepts
authority only from the authenticated API principal. Body fields such as
`approved_by` and `rejected_by` are deprecated compatibility inputs and are
ignored.

## Durable model

A gated queue job is first committed as an unclaimable approval-held row.
Only then does Colony materialize its `ApprovalRequest` or consume an exact
bounded grant, followed by the queue transition. This queue-first ordering
means a process crash can leave a safe `BLOCKED` row for scheduler repair but
can never leave a prompt or spent grant without a canonical job. The job
remains `BLOCKED` unless direct authority or an exact grant closes the gate.
Colony records the request in
`$COLONY_STATE_DIR/approval_authority.db` with:

- the exact job ID and canonical action digest;
- a canonical queue job ID matching
  `^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$`, validated before the queue or
  approval ledger can change;
- a server-derived scope containing job type, registered action, risk, and
  hashes of required action constraints;
- an immutable, bounded `ColonyApprovalPresentationV1` containing redacted
  summary, action, risk/effect, target, capabilities, deadline,
  reversibility, and hashed constraints (never the raw job payload or context
  references); owner display text also replaces complete or unterminated PEM
  private-key blocks before whitespace normalization;
- creation and one canonical expiry time; and
- a monotonic status (`pending`, `approved`, `rejected`, `expired`, or
  `superseded`).

The default request lifetime is 24 hours and is shortened to the queue job's
deadline or the configured approval timeout when either comes first. The
exact stored `expires_at` is copied to the queue hold and governs scheduler
terminalization, so transport polling, queue cleanup, and decisions cannot
disagree about whether a request is still live. A scheduler reconciliation
pass repairs pre-migration or interrupted approval holds; read endpoints never
create or change authority.

The first valid decision is final. An exact retry of the same `decision_id` is
idempotent; reusing a decision ID elsewhere, changing the action digest, or
deciding an expired/superseded request fails closed.

An approval may optionally mint an exact-scope grant. By default every grant
has both an expiry and a use cap. The scope must exactly equal the request
scope. Each future job consumes one use transactionally before it can move
from `BLOCKED` to `QUEUED`; a retry of the same immutable action does not spend
a second use.

The deployment-wide envelope is configured with
`COLONY_GRANT_MAX_TTL_SECONDS` (default `2592000`) and
`COLONY_GRANT_MAX_USES` (default `100`). A TTL integer >= 60 seconds and a use
integer >= 1 set finite ceilings and preserve validation-time rejection above
those limits. The exact, case-insensitive literal `unlimited` is the only
standing sentinel: it removes expiry or the use cap for grants issued while
that envelope is active. An empty, unrecognized, zero, negative, or otherwise
unparseable value refuses sidecar startup.
Standing selection is logged at warning level, exposed by
`GET /v1/host/autonomy/posture`, and reported as a distinct WARN by
`colony doctor`.

Changing a standing setting back to finite affects future issuance only; it
does not rewrite an existing owner decision. The posture includes active
standing-grant counts, and `colony doctor` continues to WARN until those
persisted grants terminate through a remaining finite expiry/use cap or are
explicitly revoked.

Standing changes only duration/count. The durable grant still binds job type,
action name, risk, and hashes of required parameters; every use still writes a
receipt tied to the originating owner decision. Revocation is checked at the
same atomic point of use. Autonomy enable/disable remain non-grantable in both
the worker allowlist and the action-plane backstop.

At the hostworker boundary, a standing source grant is represented only by the
literal `unlimited` in `bounded_grant_expires_at_epoch`. The per-action gate's
own `expires_at_epoch` remains finite, so each consumed execution authorization
still has a bounded dispatch window. Null, zero, and malformed grant-expiry
values fail closed.

## API migration

The preferred flow is:

1. Discover queue-backed pending records from
   `GET /v1/host/queue/jobs/blocked`. Never use the raw request ledger as a
   bridge feed.
2. Display only the canonical `presentation` and retain its
   `request_digest`, `binding_digest`, `presentation_digest`,
   `action_digest`, and `scope_digest` for the eventual exact decision.
3. POST a fresh transport-generated `decision_id`, the displayed
   `expected_action_digest`, and `decision` to
   `/v1/host/queue/approvals/requests/{request_id}/decision`.
4. To authorize a few matching future actions, include `grant` with
   `expires_in_seconds`, `max_uses`, and optionally the exact displayed scope.

Finite envelope settings reject a requested value above the configured
ceiling. When a configured dimension is `unlimited`, a newly minted grant
records that dimension as standing regardless of the request's finite default;
the explicit server setting is therefore the standing-authority decision.

All approval reads require `approvals:read`. Exact decisions
require `approvals:decide`; the bridge principal does not need worker claim,
lifecycle, or attestation authority. The canonical endpoints are:

- `GET /v1/host/queue/jobs/blocked`
- `GET /v1/host/queue/approvals/jobs/{job_id}`
- `POST /v1/host/queue/approvals/requests/{request_id}/decision`

`GET /approvals/requests` is an administrative ledger view filtered to
requests that still own a canonical queue job. It must not be used for bridge
discovery because other Colony subsystems can share the authority store.

Blocked discovery remains a JSON list for backward compatibility. Results are
filtered to approval-held jobs, emit only canonical IDs, and are ordered by
canonical `job_id`. A bridge may
request up to 200 items and pass the last observed ID as `after`; the next page
contains only IDs lexically greater than that cursor. Continue until a page is
shorter than the requested limit. The cursor need not name a currently present
job, but it must satisfy the canonical job-ID contract. Cursor reads are
projection-only: they never create prompts, consume grants, or update jobs.
Because this is live keyset pagination rather than a snapshot, a newly inserted
lower ID is discovered on the next no-cursor poll.

Pre-contract blocked rows with noncanonical IDs are never emitted or silently
treated as executable approvals. They are counted, after applying the same
`task_type` filter, in `X-Colony-Blocked-Legacy-Count`. A nonzero count makes
bridge readiness degraded until those rows are reconciled offline; GET remains
strictly read-only and never rewrites or deletes the legacy inventory.

The decision body is exactly:

```json
{
  "decision": "approve",
  "decision_id": "transport-unique-id",
  "expected_action_digest": "64-lowercase-hex",
  "grant": {
    "expires_in_seconds": 900,
    "max_uses": 1
  }
}
```

`grant` is optional. When present, `exact_scope` may be omitted to select the
request's exact server-owned scope. Any supplied difference is rejected.

## Host bridge contract

Colony owns the one canonical request. The host may mirror it into a short-lived
phone or Operator Deck challenge, but must not mint a second approval identity
or wait for a second approval. The mirror remains bound to Colony's canonical
`request_id` and `expires_at` (up to the default 24-hour lifetime); it cannot
extend or replace either. Operator Deck decisions still require a fresh
authenticated OperatorSession, and phone decisions require the exact attested
code/reply contract. Shorter reissuable challenge windows are optional future
hardening, not part of this bridge contract.

`GET /approvals/jobs/{job_id}` returns
`ColonyApprovalAuthorizationProjectionV1`. The host may import authority only when
all applicable job, action, scope, binding, presentation, and request digests
match their canonical records, the SHA-256 over canonical JSON after removing
`projection_digest` recomputes exactly, and `authorization.status` is
`authorized` with one of:

- `kind=direct_decision`, whose approved request and server-attested decision
  principal are included; or
- `kind=bounded_grant`, whose exact operation consumption is joined to an
  approved source request with matching source request, decision, grant,
  actor, and scope provenance.

`kind=request`, `kind=none`, `missing_source_request`, `invalid_binding`, or
`invalid_provenance` is not authority. Queue tags and caller/body claims are
never bridge evidence. The projection includes the redacted
`ColonyApprovalPresentationV1`, not the raw queue payload.

The projection also binds the current strictly parsed `authority_mode` and a
bounded `queue_authority_state` (`job_status`, `hold_kind`, and
`blocked_reason`) into `projection_digest`. The hold is recomputed from the
canonical authorization, actual dependency graph, and dependency statuses; it
never copies job tags. This lets a relay distinguish an approved job that is
legitimately dependency-held from a decision whose separate queue transition
has not committed. Effects-on consumers require current `enforce` mode plus
exact scoped decision evidence.

The historical `/queue/jobs/{id}/approve` and `/reject` endpoints remain as
wrappers. In the default `COLONY_APPROVAL_AUTHORITY_MODE=shadow` they accept
legacy traffic, derive the actor from its credential, and record whether that
credential would pass enforcement. Under the default envelope,
`{"always": true}` creates a seven-day, five-use exact-scope grant. It becomes
standing only when the deployment has explicitly selected `unlimited` for the
corresponding envelope dimension.

Provision decision adapters as restricted scoped keyring principals with
`allow_unscoped_api: false`, `api:access`, and `approvals:decide`. Read-only
operator surfaces use `approvals:read`; grant revokers use
`approvals:manage`. Approval routes always map to those exact scopes, including
in shadow, so the bridge never needs generic API fallback. After every consumer
is observed using the new request/digest fields, set
`COLONY_APPROVAL_AUTHORITY_MODE=enforce`. In enforcement mode, the middleware
requires the exact approval scope and the decision handler rejects anonymous,
legacy-global, missing-request-ID, missing-digest, and missing-decision-ID
traffic.

The old `standing_approvals.json` remains only for non-queue compatibility.
New entries are capped and expiring. Historical unbounded entries are
converted on first use into one-use, 24-hour migration grants. They do not
authorize queue jobs.

## Backup and rollback

Back up `approval_authority.db` with the other Colony SQLite stores before an
enforcement cutover. A code rollback may keep the database in place; older
versions ignore it. If rolling forward again, the monotonic decision and grant
ledgers resume without replaying spent uses. Keep the old JSON file during the
shadow window so a code rollback retains its previous compatibility state.

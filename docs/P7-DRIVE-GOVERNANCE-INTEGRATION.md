# P7 Server Attachment and API Scope Contract

This file specifies the reconciled integration now implemented in `server.py`,
the host router, and `api/authority.py`. The underlying P7 module remains
deployment-neutral and does not create projects, execute actions, or mint
authority.

## Server attachment

Attach P7 only after the P3 `CognitionSpineStore`, `ProjectEngine`, shared
`ProjectStore`, `WorkspaceEngine`, and `DirectiveManager` exist. Startup now
verifies that P3's `ProjectEngine` owns the exact store handed to P7 and that
the cognition store exposes the durable policy-decision resolver. A missing or
mismatched dependency fails the attachment before the P7 database is opened.

When P4 already constructed the canonical `ApprovalAuthorityStore`, P7 reuses
that exact object. Otherwise live mode opens the same
`approval_authority.db`; shadow mode does not create approval state merely to
observe proposals. Shutdown clears host handles and idempotently closes the P7
store.

Do not instantiate `DriveGovernanceStore` in off mode. This preserves the
contract that default-off creates no state.

Build `GoalRankInputV1` only from a durable P3-created `Project` and its stored
fields:

- `Project.id` -> `goal_id`
- `Project.goal_proposal_id` -> `proposal_id`
- `Project.goal_fingerprint` -> `goal_fingerprint`
- bounded `title` and `objective`
- persisted `evidence_refs`
- persisted `policy_decision_refs`
- persisted `subject_person_id`, `viewer_scope`, and `shareability`

Never accept policy decisions, scope, person IDs, weights, or active charter
IDs from an HTTP body or model result. The policy resolver must point to
`CognitionSpineStore.get_policy_decision` (or an equivalent immutable store),
not a request mapping.

For clarity, the `weights` rule above applies to ranking inputs: the ranking
endpoint cannot submit or override active weights. An immutable charter
*proposal* necessarily contains proposed `drive_weights`, but those weights
have no effect until the existing exact approval and owner ratification flow
activates that revision.

The scheduler may consume `effective_order` only when:

- the batch says `ranking_applied: true`;
- each selected result says `eligible: true`;
- every result says `authorization_effect: "none"`; and
- the normal Project/WorkOrder/action gates remain enabled.

Evaluate `status == "global_pause_active"` before that rule and hold every
goal. That status is a fresh result from the existing directive kill switch,
not permission granted by the ranking layer. The normal execution-time pause
check remains mandatory as a second enforcement point.

Ranking is not a project admission check and is not an execution receipt.
Separately, when P7 is configured `live`, the server composes the active
owner-ratified charter into P3's existing `charter` stage. It requires a fresh
active revision, cites the revision and active lifecycle projection, and
allows only proposal scopes that are no broader than the charter. It does not
interpret charter prose as authority. P7 `shadow` does not install this gate.

## API routes and exact scopes

The host API registers these exact middleware scopes rather than collapsing
them into `api:access`:

| Method and route | Required scope | Notes |
|---|---|---|
| `GET /v1/host/cognition/drives` | `drives:read` | Viewer-filtered definitions/signals |
| `GET /v1/host/cognition/charters` | `charter:read` | Viewer-filtered lifecycle projection |
| `GET /v1/host/cognition/rankings` | `drives:read` | Owner-visible bounded rationale/evidence |
| `GET /v1/host/cognition/spine` | `cognition:read` | Viewer-filtered P3 runtime/worker/read trace |
| `POST /v1/host/cognition/concerns/{id}/promote` | `cognition:manage` | Scoped owner attestation of one exact material digest; no execution |
| `POST /v1/host/cognition/drives` | `drives:propose` | Register immutable candidate definition |
| `POST /v1/host/cognition/drive-signals` | `drives:signal` | Evidence-derived immutable signal |
| `POST /v1/host/cognition/charters` | `charter:propose` | Proposal only; cannot activate |
| `POST /v1/host/cognition/charters/{id}/request-activation` | `charter:request` | Creates exact ApprovalRequest in live only |
| `POST /v1/host/cognition/charters/{id}/request-revocation` | `charter:request` | Creates exact ApprovalRequest in live only |
| `POST /v1/host/cognition/charters/{id}/ratify` | `charter:ratify` | Also requires owner `approvals:decide` authority |

The decision itself continues to use the existing approval routes and
`approvals:decide`. The ratification handler passes the middleware-derived
`RequestAuthority` object to `DriveGovernance.ratify_transition`; it must not
construct authority from body fields. A ratifying principal therefore needs
both `charter:ratify` and `approvals:decide`, plus the `owner` audience.

Read handlers must derive `viewer_person_id` and audiences from
`request_authority(request)` and call the store/batch observer projection.
Query or body fields may narrow visibility but cannot broaden it.

Mutation body boundaries are equally explicit:

- drive definitions cannot submit scope, person IDs, creator identity, or
  timestamps; the server derives scope and creation time;
- drive signals name a durable `project_id`, never a goal fingerprint. The
  server reloads the complete P3 project, verifies the project and drive are
  visible to the credential, and derives its stored fingerprint;
- charter proposals cannot submit scope, proposer identity, timestamps, or an
  active-charter selection; the server derives those fields. Proposed weights
  remain inert immutable content until ratification;
- ranking is GET-only and accepts no goals, policy decisions, scopes, weights,
  or charter selection. It filters persisted projects to complete nonterminal
  `source="cognition_spine"` rows and lets the ranker re-resolve every P3 gate;
- ratification passes the middleware-created `RequestAuthority` unchanged and
  requires both `charter:ratify` and `approvals:decide` plus the `owner`
  audience. Legacy authority is refused. Transition requests also require the
  referenced charter to be visible and do not distinguish hidden from unknown.

## Operator workflow

The Operator Deck or a transport adapter displays the existing immutable
ApprovalRequest digest, transition, expiry, charter label, bounded principle
summary, weights, and evidence references. The Deck does not call a separate
P7 approval mechanism. Its authenticated decision goes through the same
approval authority used for other bounded actions, followed by the ratify
route above.

No communication channel is part of the charter trust model. A deployment can
deliver the approval request over any transport already attested by its host.

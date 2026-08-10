# P3 Cognition Goal Spine

Status: source complete; shipped default **off**; no live deployment implied.

P3 establishes one typed autonomous goal path:

```text
durable scoped event
  -> Concern
  -> ThoughtJobV1 (durable read-only task queue job)
  -> one ThoughtOutputV1
  -> GoalProposalV1
  -> five server policy decisions
  -> provenance-bearing Project
  -> ProjectEngine
  -> WorkOrderV1
  -> verified ExecutionResultV1 / receipt
  -> concern settlement
```

It is an orchestration and evidence layer, not an action worker. It has no
tool executor, messaging client, external mutation primitive, or private
queue. ProjectEngine and the canonical WorkOrder bridge remain the only
route from an accepted autonomous objective to execution.

## Modes

`COLONY_COGNITION_SPINE=off|shadow|live` defaults to `off`.

- `off`: no P3 behavior and no legacy behavior changes.
- `shadow`: queue and validate bounded thoughts and record policy decisions,
  but create no Projects and settle no concerns. Legacy autonomous goal
  writers continue, so a canary does not stop daily work.
- `live`: create the one canonical Project and make legacy autonomous goal
  writers read-only. Explicit owner-created Projects remain available.

The deployed runtime composes P3, workspace, event-reducer, and P7 modes into
`CognitionRuntimeContractV1`. A live P3 requires a live workspace. Shadow P7
and a shadow event reducer remain observational and do not globally stop
separately-produced live concerns. Instead, each Concern carries producer
name/mode/revision. A non-live or legacy/unknown Concern cannot feed live P3
until an authenticated owner promotes that exact material digest. New material
clears the promotion automatically.

When P7 itself is configured `live`, its store must be attached and one fresh
active owner-ratified charter must exist. Absence, expiry, or a failed charter
read holds new P3 cognition; it does not fall back to an unratified charter.
The active charter can only narrow the proposal scope accepted by P3.

In live mode:

- SelfDirectedThinker may only add owner-private workspace candidates;
- it cannot create or deliver initiatives or Projects;
- ProjectEngine cannot adopt legacy thinker initiatives;
- GoalEngine conversation inference and conversation-synthesis goal creation
  are held;
- the autonomy loop does not activate or feed legacy goals into new
  initiatives;
- the legacy direct workspace thinker and `_workspace_act` path are held.

Lifecycle handling of already-running legacy work is not destructively
rewritten. Reconcile/archive it before live cutover.

## ThoughtJobV1 authority

The job ID is derived from a SHA-256 digest over every input that can affect
the judgment:

- concern and material-event digest;
- exact source/event references;
- subject, viewer scope, and shareability;
- attempt number;
- exact read capability allowlist;
- input, output-token, runtime, and deadline budgets;
- system/user prompt digests and output contract version.

Thought jobs use `JobType.THOUGHT`. The default worker registry maps it to a
separate `ThoughtOnlyInferenceHandler`; the generic `InferenceHandler` is a
different object. Worker registration refuses to advertise the private
thought route unless its handler carries the strict thought-only contract.
That handler:

- rejects non-read capabilities;
- performs no implicit contact lookup unless separately supported;
- does not update the world model after inference;
- does not feed an unvalidated result to the router self-learner;
- bypasses the outbound ResponseGate because the result is internal and
  must pass the typed parser instead;
- suppresses the generic post-task skill-learning write hook;
- applies a per-call output-token ceiling and the worker runtime timeout.

Each thought also requires the queue capability `cognition_scoped`. A worker
advertises it only when it deliberately loads the strict Thought handler, so
an unscoped all-types worker cannot accidentally claim owner-private thought
content.

Expired queue jobs are not claimable even if the expiration scheduler is
late. A timeout/failure does not spend the concern's thought budget or
resolve it; a bounded next attempt can be posted with a new deterministic
ID. After three failed attempts, the ledger reports resumable budget
exhaustion and waits for review or new material evidence.

## Typed outputs

Exactly one `ThoughtOutputV1` object is accepted:

- `Note`
- `MemoryWriteProposal`
- `GoalProposal`
- `ExperimentProposal`
- `NoAction`

Unknown kinds and extra fields are rejected. Evidence references must be an
exact subset of references supplied to the thought job. A model cannot emit
`Resolve`, add a viewer/recipient, widen a capability, or make a proposal
itself authoritative.

`Note`, `MemoryWriteProposal`, and `ExperimentProposal` are idempotently
written to `cognition_routed_outputs`. Every row says
`effect_executed=false`; P3 has no consumer that can execute those proposals.
A reviewable `NoAction` may settle only the scoped
cognitive concern in live mode. It never changes the upstream commitment,
service, relationship, or other source record.

## Goal policy and scope

An accepted GoalProposal has five immutable decisions, in order:

1. `charter`: deployment validator confirms the typed objective is in scope;
2. `boundary`: DirectiveGuard checks the concrete objective and fails closed;
3. `situation`: deployment validator confirms capacity/current conditions;
4. `duplicate`: ProjectStore rejects an already-open scoped fingerprint;
5. `authority`: every requested capability must be server-available.

The resulting Project uses a deterministic ID and stores:

- concern and source-event references;
- thought job and typed result references;
- goal proposal and evidence references;
- all policy-decision references;
- subject/viewer/shareability scope;
- the exact capability allowlist and goal fingerprint.

ProjectEngine propagates those references into every WorkOrder context and
checks each planned and dispatched step against the Project capability
allowlist. A planner cannot widen the GoalProposal.

## Delivery is deliberately held

P3 rejects autonomous GoalProposals requesting `messaging:send` with:

`p3_deliver_held_missing_attested_recipient_artifact_envelope`

It also instructs the planner not to create `deliver` steps and blocks one
if it appears. Current WorkOrderV1 identifies a risk/capability but does not
bind an exact attested recipient and bounded message/artifact to the action
digest. Inferring a recipient from a concern, a phone number, a model output,
or a Hermes route would be an authority escalation.

The future coordinated schema is `DeliveryAuthorityV1`:

```json
{
  "schema": "DeliveryAuthorityV1",
  "version": 1,
  "recipient_principal_id": "server-resolved stable ID",
  "recipient_scope": "contact:<stable ID>",
  "channel": "exact server-attested transport",
  "transport_account_id": "configured sender account ref",
  "message_ref": "bounded immutable draft ref, XOR artifact_ref",
  "artifact_ref": "bounded immutable artifact ref, XOR message_ref",
  "content_digest": "sha256 of exact content/artifact envelope",
  "purpose": "bounded disclosure purpose",
  "allowed_disclosures": ["exact classes"],
  "attestation_ref": "transport/identity authority record",
  "attested_by": "server principal",
  "issued_at": "RFC3339",
  "expires_at": "RFC3339",
  "authority_digest": "sha256 of every preceding authority field"
}
```

Adoption requires a versioned WorkOrder schema whose authority digest and
ID cover the entire delivery envelope, dual-version support in the host's Action
Plane, independent attestation verification, and a receipt that names the
same recipient/content digest. Until that coordinated migration exists,
delivery remains held. Hermes is not a recipient resolver or transport for
this path.

## Durable state and rollback

`CognitionSpineStore` adds its own SQLite database (recommended live path:
`$COLONY_HOME/data/colony-cognition.db`) containing immutable jobs, results,
proposals, policy decisions, project links, routed non-action proposals, and
revision-keyed admission history. Admission keys include the concern material,
producer/promotion, runtime mode, boundary policy, situation, and active
charter revisions. Repeated holds use durable exponential backoff capped at
five minutes; a changed revision creates a new retry key immediately.

`GET /v1/host/cognition/spine` (`cognition:read`) exposes the current runtime,
exact thought-route readiness, and a viewer-filtered read trace. It never
returns an owner-private row to a guest. An authenticated, non-legacy owner
principal with `cognition:manage` may call
`POST /v1/host/cognition/concerns/{id}/promote` with the expected material
digest. The server derives the immutable promotion reference from the scoped
principal; the endpoint does not run cognition or approve/execute an action.

ConcernStore adds `concern_settlements`. ProjectStore adds additive Project
outcome, provenance, scope, authority, and fingerprint columns. Old code
ignores these additions. No migration deletes or rewrites a legacy goal,
initiative, project, event, or competence row.

Setting `COLONY_COGNITION_SPINE=off` is the immediate functional rollback.
Keep the new database and additive columns as audit evidence; restore a
pre-cutover database copy only if an older binary cannot open the files.

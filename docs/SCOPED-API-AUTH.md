# Scoped API authentication

Colony supports per-service API principals alongside the legacy global
`COLONY_API_KEY`. The dual-accept period is deliberate: consumers can move one
at a time without turning a live deployment silent.

## Authority model

Each scoped credential resolves server-side to one exact principal with:

- exact API scopes;
- optional `allow_unscoped_api: false`, which denies every route that falls
  back to generic `api:access` while preserving explicitly mapped scopes;
- a default `viewer_person_id`;
- optional additional exact `person_ids`;
- explicit `viewer`, `owner`, `shared`, and `global` audience grants;
- principal and credential `status` plus optional RFC3339 `accept_until`.

Credential IDs are public provenance identifiers, not secrets. They must match
`^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$`; this keeps scoped approval evidence
canonical and below the 512-character cross-runtime contract.

The supported focused scopes are `memory:read`, `memory:search`,
`memory:write`, `context:read`, `events:read`, `turns:write`,
`cognition:read`, `cognition:manage`, `cognition:benchmark-read`,
`cognition:benchmark-manage`, `cognition:experiment-read`,
`cognition:experiment-manage`, `approvals:read`, `approvals:decide`,
`approvals:manage`, `tom:read`, `tools:mutate`, `workers:register`,
`workers:claim`, `workers:lifecycle`, `workers:contract`,
`workers:inspect`, `workers:attest`, `work:read`, `work:control`, and
`auth:admin`.
`auth:admin` protects the privacy-safe migration status surface.
The three worker scopes protect exact registration, claim, and claimant
lifecycle lanes when worker HTTP authority is enforced.
`workers:contract` reads the deploy-pinned queue protocol contract;
`workers:inspect` reads one exact job's claim/attempt/route canary projection.
`workers:attest` belongs only to a separate receipt verifier that may promote
an exact, digest-bound Action Plane result; never grant it to the executor.
`work:read` protects the WorkControl projection and the exact collection reads
for goals, projects, pending jobs, and neutral jobs. Those four collection
routes continue to accept an existing `api:access` principal only when its
`allow_unscoped_api` setting is true; this compatibility is route-local and
does not make `api:access` a parent of other focused scopes. `work:control`
protects WorkControl mutations and is never implied by the read scope.
`tom:read` protects the bounded P8 status/Operator Deck read models and the
owner-scoped Tom2 content report.
`context:read` protects both context assembly endpoints because they retrieve
memory and relationship context. `events:read` protects both the event replay
endpoint and the WebSocket handshake. `cognition:read` projects the scoped
workspace; `cognition:manage` is required to resolve a concern because that
operation may settle a linked commitment or project. Benchmark and experiment
reads are separate from evidence ingestion and mutation. Other endpoints
require `api:access` (or the deliberately broad `*`). Scope names are exact;
Colony does not infer a wider scope from a prefix.

`tools:mutate` is a second, handler-level grant for P8 reasoning surfaces. It
does not make a principal the owner and it cannot be body-claimed: the request
must also be a server-attested exact-owner viewer. Exact-owner private reads do
not require mutation authority. Non-owner and legacy-bearer callers receive
only the public/general tool subset while P8 is attached. Every production
ToolExecutor dispatch remains subject to standing owner directives regardless
of P8 mode.

Approval routes always map to exact middleware scopes: operator views read
immutable requests with `approvals:read`, transport adapters submit decisions
with `approvals:decide`, and revocation tools need `approvals:manage`. Shadow
retains legacy-bearer compatibility; enforce accepts only scoped principals
that also carry `api:access`. See
[Bounded Approval Authority](BOUNDED-APPROVAL-AUTHORITY.md).

The host approval bridge should use a dedicated principal with
`allow_unscoped_api: false`, `api:access`, `approvals:read`, and
`approvals:decide`. It discovers canonical pending queue jobs only at
`GET /v1/host/queue/jobs/blocked`, verifies final direct/grant authority
at `GET /v1/host/queue/approvals/jobs/{job_id}`, and posts the owner's exact
decision to `POST /v1/host/queue/approvals/requests/{request_id}/decision`.
Do not add worker, executor, or attestation scopes to this transport adapter.
Colony creates the request at effect-job birth; phone and Operator Deck
surfaces are short-lived mirrors of that one request, not additional approval
ledgers.

A channel adapter that submits a structured transport `sender` may also be
granted `turns:resolve-sender`. Colony then ignores the body's initial contact
claim, starts from the principal's viewer binding, and lets the server-side
participant resolver map the attested platform/user identifier. Without that
additional scope, the initial turn contact must be one of the principal's exact
person grants. This keeps multi-contact adapters usable without granting
arbitrary body-selected person authority.

Every principal that writes completed conversation turns should declare its
server-owned transport role explicitly. The role is an exact platform set;
wildcards are invalid, and neither a request body, channel name, nor session
identifier can add to it:

```json
"turn_ingress_platforms": ["rcs", "sms", "whatsapp"]
```

An explicit empty array means the principal is ineligible for transport turn
ingress. For compatibility during a phased keyring rollout, a `turns:write`
principal that omits this field derives the role only from its already
validated static `attested_contact_grants.platforms`. A senderless writer
without either source remains ineligible. Once the field is explicit, every
attested contact platform must be its subset; there is no wildcard or
body-derived fallback.

### Server-attested contact projection

A channel/context principal may opt into bounded exact-contact refresh without
changing its secret-bearing keyring record:

```json
"attested_contact_grants": {
  "platforms": ["rcs", "sms", "whatsapp"],
  "max_person_ids": 512
}
```

The policy requires `turns:resolve-sender`, exact platform names, and a cap of
1..4096 IDs. Wildcard platforms and wildcard person IDs are rejected. When a
turn from an allowed platform reaches `ParticipantResolver`, Colony discards
the body's initial contact claim, resolves the sender server-side, and only
then atomically adds that one exact contact ID to the principal's projection.
A body-asserted ID, contact lookup, or ordinary context request cannot create a
grant. The projection can add neither API scopes nor `owner`, `shared`, or
`global` audience lanes.

The projection defaults to
`$COLONY_STATE_DIR/api-contact-grants.json`; override it with
`COLONY_API_CONTACT_GRANTS_PATH`. It contains no key material, is required to
be a regular service-user-owned mode-0600 file, and hot-reloads after atomic
replacement. An invalid replacement fails dynamic grants closed without
invalidating the separately configured legacy bearer or scoped keyring.

For memory requests, an omitted `person_id` is replaced with the authenticated
viewer binding. A supplied `person_id` is accepted only when it is one of that
principal's exact grants. The optional `audience` field selects one explicit
lane. The graph query still uses one hard `(:Memory)-[:ABOUT]->(:Person)`
candidate boundary. Colony never retries a scoped miss against global memory.
If a request supplies both an audience and a person/contact field, they must
resolve to the same exact ID.

Legacy query-string selectors named `person_id`, `contact_id`, or
`viewer_person_id` pass the same principal-bound check before routing.

### Worker node authority

Worker authentication is staged independently from the server-side
WorkerGovernor. `COLONY_WORKER_AUTHORITY_MODE=shadow` is the default: the
legacy bearer and not-yet-provisioned scoped consumers keep working, while
each successful claim records the principal, credential class, and whether
enforcement would deny it. After every worker has a scoped secret and a
normal-use observation window is clean, set the mode to `enforce`.

An enforcing worker principal needs only the queue scopes it uses and one or
more exact `worker_grants` in the private keyring:

```json
{
  "principal": "execution-worker",
  "scopes": [
    "api:access",
    "workers:register",
    "workers:claim",
    "workers:lifecycle"
  ],
  "worker_grants": [{
    "node_id": "execution-worker-1",
    "capabilities": ["research"],
    "capacity": {"ram_gb": 16},
    "max_concurrent": 1,
    "job_types": ["research"]
  }]
}
```

The grant is the server-owned ceiling. A register or claim body may omit
fields (using the grant) or narrow capabilities, capacity, concurrency, and
job types; it cannot expand them. Node IDs are unique across the keyring, and
wildcards and empty job-type grants are rejected. Start, complete, fail,
heartbeat, release, and deregistration requests are accepted only from the
principal that owns the exact claimant node. Embedded in-process workers keep
their trusted local queue lane and do not acquire HTTP authority.

Every external consumer must call the start endpoint successfully after a
claim and before it performs work. A failed start is released instead of being
executed. Completion and failure timing comes from the server's durable
claim/start audit ledger; a client's `started_at` field remains accepted only
for wire compatibility and is never evidence. Queue writes are serialized on
the shared SQLite connection so one request's rollback cannot undo another
request's transition. Historical negative or non-finite durations remain in
the database for forensics but are excluded from benchmark and competence
inputs.

Each claim now returns a server-minted `claim_attempt_id` and bounded
`claim_expires_at`. The worker must echo that exact attempt ID to start,
complete, fail, heartbeat, and release. A stale response from an older claim
cannot start or finish a newer claim even when the node ID is unchanged.
Start atomically rejects an expired deadline or claim-start lease; completion
cannot turn work past its server-measured deadline/execution timeout into
success. The production queue scheduler independently expires claim leases,
heartbeats, deadlines, and execution timeouts.

Completion/failure evidence is committed to a durable queue outbox in the
same transaction as the terminal transition. Cancellation or restart leaves a
pending row for scheduler reconciliation. Stable event IDs make competence
folding idempotent, including across a crash after the competence write but
before outbox acknowledgement. The event's original off/shadow/live posture
is durable, so a later mode change cannot graduate old shadow evidence.

During shadow migration the scoped worker also retains `api:access`, because
the compatibility route scope remains unchanged for old consumers. Do not
switch authority mode and revoke the legacy bearer together. First install
the grant while shadow remains active, move one worker secret at a time,
confirm claim tags show `worker_authority_would_deny=false`, rehearse rollback
to the legacy credential, and only then enable enforcement. Once enforcement
and a normal-use soak pass, remove `api:access` from a dedicated worker that
uses no non-worker endpoint. An invalid mode fails closed rather than silently
weakening policy.

A keyring execution grant containing `workers:claim` must also contain
`workers:lifecycle`; an incomplete principal is rejected at load time instead
of producing a misleading green claim-only shadow canary. Lifecycle responses
also expose their exact future enforcement posture.

The embedded in-process worker remains enabled by default for generic Colony
installs. A health-only deployment can set
`COLONY_EMBEDDED_WORKER_ENABLED=false`; this skips worker construction while
leaving the independent queue scheduler and evidence reconciler running.

Generic `agent_action` consumers must select an exact non-effect lane via
`COLONY_AGENT_WORKER_ROUTES=agent_sync`, `hermes_run`, or the combined default
`agent_sync,hermes_run`. The generic parser rejects `action_plane` and
`work_order`; those belong to the separately pinned host Action Plane node.
Set `COLONY_AGENT_SYNC_WORKER_NODE_ID`,
`COLONY_HERMES_RUN_WORKER_NODE_ID`, and
`COLONY_ACTION_PLANE_WORKER_NODE_ID` to make route ownership exact. The global
`COLONY_AGENT_JOB_CLAIMS_ENABLED=false` containment switch stops every generic
claim loop without stopping initiative forwarding.

The private Thought lane is independent from those generic routes. Production
P3 startup requires `COLONY_EMBEDDED_WORKER_ENABLED=true`, a configured LLM
router, and `COLONY_THOUGHT_WORKER_NODE_ID` equal to the local durable node ID.
The worker advertises both `cognition_scoped` and `thought_engine:v1`; the queue
reports the lane unready until that exact handler registers. A wrong node
cannot evict an already healthy Thought owner.

Release probes call `GET /v1/host/queue/contract` with `workers:contract` and
match `COLONY_RELEASE_COMMIT` (40 lowercase hex characters) plus
`COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256` (64 lowercase hex characters).
`GET /v1/host/queue/inspection/jobs/{job_id}` requires `workers:inspect` and
binds a canary to its exact claimant, attempt, expiry, capabilities, and route
tags. Runtime readiness is intentionally outside the contract digest, so a
handler restart changes readiness without pretending the deployed source did.

Generic state-changing Action Plane completions remain `neutral` until a
separate principal with `workers:attest` submits an
`ActionReceiptAttestationV1` to
`POST /v1/host/queue/attestations/jobs/{job_id}`. The receipt binds the exact
server-minted claim attempt, immutable action digest, server-derived effect
class, bounded scheme-qualified receipt references, and verification time.
Colony computes the canonical evidence and receipt-reference hashes; callers
cannot submit those hashes. It stores the hashes and canonical reference-only
attestation, never the receipt artifacts themselves. The global migration
bearer is rejected. A keyring principal with `workers:attest` is invalid if it
also has any worker grant or register/claim/lifecycle scope. Keep the verifier
secret in a separate process/user and out of every executor/LLM environment.
The verifier is the evidence oracle: it must read and validate the underlying
Action Plane receipt chain before submitting the canonical assertion.
Dependencies remain blocked while either a WorkOrder or generic action is
awaiting verification and release only after the matching attestation.
An exact replay returns the same server-computed hashes with `replayed=true`;
a changed receipt, verifier, attempt, action, or assertion is rejected with
HTTP 409. `workers:inspect` exposes the non-secret `action_digest`, result
contract, attempt, and verification-pending fields needed by a reconciler.

Webhook execution is a split claimant/lifecycle principal. The queue worker
or built-in Agent Bridge claims under its configured node ID (the built-in
default is `sidecar-bridge`), while the Hermes `colony-jobs` route sends the
heartbeat/complete/fail requests. Before worker-authority enforcement, the
credential in Hermes' `COLONY_API_KEY` must therefore have
`workers:lifecycle` and an exact worker grant for that same node ID and
`agent_action` job type. Do not pass a broad bearer in the webhook payload.
Migrate this consumer alongside the claimant and verify a normal heartbeat +
completion before revoking the compatibility bearer. A future per-attempt
delegation token can remove this shared-node credential requirement.

`WorkOrderV1` has an additional deterministic routing capability:
`work_order:v1`. Its executor grant and every register/claim body must carry
that protocol capability plus every canonical capability in the WorkOrder's
`capability_allowlist` (for example `memory:read`, `reasoning`, and
`web:read`). Generic queue/Agent Bridge workers intentionally do **not**
advertise `work_order:v1`, so they cannot race a deployment's receipt-aware
Action Plane. The host action executor is therefore a mandatory migration
consumer before live worker enforcement: update it to advertise
`work_order:v1` and the canonical action capabilities, echo the exact
`claim_attempt_id` on start/heartbeat/complete/fail/release, and include the
attempt map in bulk heartbeats. Do not add a Colony legacy bypass; an old
executor must fail closed until it implements the contract.

Agent webhook dispatch uses the same strong route secret as Hermes. Set a
per-route HMAC secret in Hermes and in `COLONY_HERMES_WEBHOOK_SECRET`; Colony
signs the exact body using pinned Hermes' timestamped
`X-Webhook-Signature-V2` contract and emits a stable `X-Request-ID`.
Raw-body V1 can be enabled only as an explicit older-receiver migration
compatibility flag. `INSECURE_NO_AUTH` is a loopback-only development posture,
not a production migration shortcut.

When Hermes' canonical general Colony plugin is active, the separate
`colony-memory` provider publishes and dispatches only its non-duplicating
read/context allowlist. Standalone queue mutation tools require the explicit
`COLONY_MEMORY_WORKER_TOOLS=1` opt-in. Initiative approval is never exposed as
a model tool on that provider; approval remains transport/operator authority.

The provider requires `COLONY_PREFETCH_QUERY_CHECK=1` and
`COLONY_PREFETCH_TURN_CONTACT=1`; disabling either is a startup error. Set
`COLONY_MCP_CONTACT_ID` to the exact owner contact and use
`COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY=owner_system` only for a non-channel
CLI/system owner lane. Real RCS/SMS/WhatsApp senders never fall back to that
default. The provider first calls
`GET /v1/host/context/projection-readiness` (`context:read`) and then sends
`projection_policy=scoped_viewer_required` atomically with guest assembly. A
guest response is accepted only when the server attests the exact turn contact,
a supported scoped projection is ready, and legacy-global context is excluded.
`projection_backend=p8` retains the existing shadow/live visibility projection.
When P8 is absent, `projection_backend=canonical_sources` provides the guest's
own canonical source quotations, source-backed claims and image captions through
the existing single recall selector and character budget, plus proven shared
commitments and the current UTC clock. A commitment's `person_id` is only a
subject selector. Guest visibility additionally requires metadata
`source_turn_id` naming their current person-scoped source, with the displayed
description present in that source. Unclassified legacy tasks and owner notes
about a guest are omitted; reservation session IDs are not exposed. This rule
does not backfill provenance or assume that introspection already supplies it. Checkpoint evidence retains its original
session restriction. This backend does not query legacy graph, contact profiles,
relationship/affect, shared-fact mirrors, deployment identity, skills, or global
producers. Its response notices describe these omitted capabilities; it does not
claim that P8 is running. The exact scoped owner and temporary legacy migration
bearer retain their existing context behavior. Older providers which require P8
will keep withholding guest context until their adapter is upgraded. Guest temporal context is local-clock-only, and legacy global
reply-window lookup is disabled pending a transport-attested endpoint. The
provider's direct commitments/affect/facts/timeline tools are likewise
owner/system-only until those endpoints emit scoped P8 projections; guest
turns use atomic assembled context and receive a clear unavailable envelope
from a direct tool call.

`catalog_attestation()` distinguishes the completed memory-provider privacy
boundary (`provider_governance_ready=true`) from the broader general-plugin
follow-on (`general_plugin_governance_ready=false`). Do not graduate the
general plugin until its global proactive event injection, shared fallback,
dropped handler context, and startup LLM mutation blockers are closed.

Graduated policy does not currently auto-queue outbound/contact delivery, even
when the contact store resolves an allowed recipient. Contact resolution is
identity/context, not execution authority. Outbound work stays approval-blocked
until an immutable phone/Operator Deck decision or bounded grant is consumed.
This is a deliberate temporary usability tradeoff: restore a fast path only
after Colony can issue and verify a durable target plus transport attestation;
never infer authority from `outbound_target` or
`auto_approved_by_policy` caller tags.

The lane-to-person mappings are deployment configuration:

| Variable | Default |
|---|---|
| `COLONY_OWNER_PERSON_ID` | `COLONY_OWNER_CONTACT_ID`, then `owner` |
| `COLONY_SHARED_PERSON_ID` | `shared` |
| `COLONY_GLOBAL_PERSON_ID` | `global` |
| `COLONY_DEV_PERSON_ID` | `dev-anonymous` |

Anonymous loopback dev mode remains convenient: callers may use an ordinary
scratch `person_id`, and omission derives `COLONY_DEV_PERSON_ID`. It cannot
select, read, or write the configured owner/shared/global IDs. Set the owner
mapping to the real owner contact ID before relying on this boundary.

`COLONY_API_KEY` remains a deprecated compatibility principal during
migration. It retains its historical unrestricted body-selected behavior; it
is therefore not the desired steady state.

## Keyring

Install [`sidecar/api-keyring.example.json`](../sidecar/api-keyring.example.json)
outside the repository, then replace every placeholder while it is private:

```bash
install -m 600 sidecar/api-keyring.example.json ~/.colony/api-principals.json
${EDITOR:-vi} ~/.colony/api-principals.json
chmod 600 ~/.colony/api-principals.json
```

Set `COLONY_API_KEYRING_PATH` to that absolute path. Colony rejects a keyring
with any group/world permission bits, malformed JSON, duplicate principal IDs,
duplicate credential secrets, unknown status/audience values, or timestamps
without a timezone. Never commit the populated file.

The middleware checks inode, size, timestamps, and permission mode on requests,
so an atomic replacement reloads credentials without a service restart. If a
changed file is invalid, scoped credentials fail closed until a valid file is
restored; the separately configured legacy key continues working.

`status` may be `active`, `retiring`, `disabled`, or `revoked`. Only `active`
and `retiring` accept requests, and neither accepts at or after
`accept_until`; a `retiring` record must provide that bound. Multiple
credentials under one principal support overlap during rotation. A caller may
send `X-Colony-Principal`; if present, it must exactly match the principal
resolved from the token.

## Migration evidence

Colony persists privacy-safe authentication counters in
`$COLONY_STATE_DIR/colony-auth-telemetry.db` by default; override that path with
`COLONY_AUTH_TELEMETRY_PATH`. Counters contain only authenticated principal
name, legacy/scoped/anonymous class, required scope, framework route template,
allow/deny reason, count, and first/last-seen timestamps. Tokens, credential
IDs, headers, bodies, query values, peer addresses, and concrete path IDs are
never recorded.

`GET /v1/host/admin/auth/status` requires `auth:admin` (the legacy migration
credential remains compatible) and returns telemetry plus exact-contact grant
counts and loader health. `colony status` summarizes it and `colony doctor`
warns while legacy traffic remains, scoped traffic is absent, persistence is
unhealthy, or the grant projection fails. Do not revoke the legacy bearer
until a complete normal-use observation window shows no required legacy
traffic, including the WebSocket event subscriber.
`COLONY_AUTH_LEGACY_QUIET_HOURS` controls the doctor's required legacy-silence
window and defaults to 24 hours; cumulative historical counts are retained, so
the decision uses the legacy principal's last-seen timestamp rather than
pretending old traffic never happened.

## Consumer-by-consumer migration

1. Keep the existing `COLONY_API_KEY` configured.
2. Install a mode-private keyring and set `COLONY_API_KEYRING_PATH` while the
   legacy key remains present.
3. Restart once to add the keyring path. Verify both old and new credentials.
4. Move one consumer at a time to its scoped secret. Give it only the endpoint
   scopes and audience/person lanes it needs.
5. Observe successful traffic under the exact principal, then migrate the next
   consumer.
6. Rotate a scoped principal by adding a second credential, atomically replacing
   the keyring, moving the consumer, and marking the old credential `retiring`
   with a bounded `accept_until`.
7. Remove `COLONY_API_KEY` only after the consumer inventory shows no legacy
   traffic and rollback has been rehearsed.

## Rollback

During migration, rollback is configuration-only: point each affected consumer
back to `COLONY_API_KEY`, remove `COLONY_API_KEYRING_PATH`, and restart the
sidecar. No database migration or memory rewrite is part of scoped auth. Restore
the previous keyring atomically if only a credential rotation needs rollback.

After the legacy key is finally revoked, rollback should restore a previously
saved mode-0600 keyring rather than reintroduce a shared global bearer.

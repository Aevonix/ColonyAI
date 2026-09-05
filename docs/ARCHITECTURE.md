# Architecture and ownership

Status: target architecture and migration rules. This document does not claim
that the current runtime implements every boundary below. Current defaults and
known gaps remain documented in the README and `KNOWN-GAPS.md`.

Colony supplies persistent cognition to a host runtime. A private deployment
supplies one agent's identity, integrations and operating environment. Build on
the existing implementation by giving each kind of state and work one owner.
Move working callers onto that boundary before retiring their predecessors.

## Responsibilities

| Owner | Responsibility |
| --- | --- |
| Host runtime | Channels, interactive tool loop, working transcripts, native delegation, user-facing schedules and runtime-specific lifecycle. |
| Colony | Durable experience, knowledge, identity-state representation, relationships, commitments, work coordination, authority, consent and evaluated learning. |
| Private agent | Its constitution, preferences, opinions, contacts, credentials, retained data, model policy, deployment bindings and custom integrations. |
| Model endpoints | Interchangeable inference for named functions. A model's hidden state or embedding space is not authoritative agent memory. |
| Integration adapters | Translate a particular transport, application or device into scoped turns, observations, operations and outcomes. |

Identity mechanisms belong in Colony; a particular identity belongs to its
private instance. Multiple interfaces may explicitly share that instance.
Different deployments must not be merged by discovering their configuration
directories on the same host.

Hardware-specific software remains in the deployment even when its code could
theoretically be reused. Phone bridges, camera ownership, room audio, firmware,
device provisioning and fleet inventories are not prerequisites for Colony.
Extract an optional public integration only when another deployment establishes
a real need for it.

## A small core

Organize the existing Python code around four domains:

1. Identity and authority: principals, assurance, grants, corrections and
   relationships. Familiarity can affect communication; it cannot grant access.
2. Evidence and memory: source records, beliefs, opinions, procedures, retrieval,
   contradiction handling and erasure.
3. Work and commitments: accepted intentions, proposals, consent, execution,
   leases, cancellation, receipts and uncertain outcomes.
4. Learning and evaluation: observed failures, hypotheses, candidate changes,
   held-out evaluation, promotion and regression detection.

These are module boundaries, not four services. Model calls must not hold
database transactions. Privileged workers, real-time media and the deterministic
recovery supervisor remain separate processes because their failure modes and
access differ. Add other process boundaries only for measured operational needs.

Keep existing package names while moving actual callers. Do not create empty
facades, a service per cognitive faculty or another scheduler for tasks the host
already executes. Delete a predecessor only after its consumers, persisted
state, recovery behavior and relevant tests have migrated.

## Integration contract

The following are responsibilities for the shared client/API contract, not new
endpoints already implemented by this change. Evolve the existing schemas and
adapters rather than installing a parallel protocol.

| Operation | Input and result |
| --- | --- |
| Prepare a turn | Trusted caller scope, conversation/turn identifiers, audience, utterance, attachment references and deadline produce authorized context, state revisions and source handles. |
| Record experience | Idempotent source/event identifiers, occurrence time, provenance, scope and content/media references receive a durable acceptance acknowledgement. |
| Commit an intention | Task, targets, resource preconditions and applicable authority produce an accepted commitment, a conflict or a decision proposal. |
| Execute and reconcile | A scoped worker claims admitted work and reports observed completion, failure, cancellation or uncertainty with effect receipts. |
| Inspect or forget | Authorized queries expose accepted state; erasure propagates to source owners, derived stores, transcripts, caches and replay queues. |

An adapter's fields cannot assert arbitrary human identity or privileges. Its
registered authority constrains the principals, sources and operations it may
represent. A device credential proves the device, not its speaker.

Use existing tool descriptions, including MCP where appropriate, for operation
names, schemas and targets. Capability declarations describe what an adapter can
do; grants determine what it may do. Colony can remember a resource and its
observations without owning the resource's firmware, room registry or media
transport. Namespaced metadata and stable resource references are sufficient.

Adapters own local buffering and retries. Acknowledging local capture is
different from acknowledging central acceptance. Effects outside the database
need endpoint idempotency where available and reconciliation where the result is
uncertain; a queue alone cannot guarantee exactly-once physical effects.

## Hermes boundary

Keep one integration distribution with the native general-plugin and exclusive
memory-provider registrations. Share their client and durable ingestion code,
with explicit callback ownership so a turn is recorded once.

Use the memory provider for automatic per-turn recollection, native hooks for
lifecycle, middleware for execution checks, and MCP for explicit tools. Retain
native compression after a supported durable checkpoint. Do not insert memory
globally through a model proxy or replace the host tool loop.

Delegate native scheduling and subagent execution to Hermes. Pass scoped task
context to children and ingest their outcomes into the same work state. A
rewound conversation does not rewind a real-world commitment or completed
effect.

Attachment selects an exact runtime and profile. Configuration staging,
activation and observed behavioral readiness are different states. A copied
plugin or successful health request does not prove the integration is active.
Unknown or incompatible installations receive an actionable compatibility
result, not silent core patching.

The current source-checkout attachment repair is deliberately narrower: choose
one home, preflight it, preserve unrelated configuration and stage resources
without provisioning global services. Packaged resources, safe activation and
runtime conformance remain explicit follow-up work.

## State ownership and recovery

The target authority is PostgreSQL with relational constraints for invariants
and extensible payloads where needed. LanceDB is a rebuildable retrieval
projection. Source media lives behind a content-addressed storage interface.
SQLite remains appropriate for bounded local adapter outboxes.

This is a migration decision, not a claim that the existing SQLite, graph and
vector stores have been replaced. Transfer write ownership by domain using
captured deltas and short fences. Preserve identifiers, evidence, corrections,
consumed consent, revocations, tombstones and uncertain effects. Never perform
best-effort dual writes or reconstruct historical authority with a model.

Commit an accepted state change, its event and its delivery/index outbox in one
transaction. Search returns candidate identifiers; current authoritative scope,
revision and deletion checks run before private content reaches a model.
Recent unindexed records remain recallable. Index generations record an
immutable embedding model revision or digest, dimensions, preprocessing and
modality. Never mix incompatible generations even when role names, model names
and dimensions match.

Recovery must establish an acknowledged authority/erasure watermark from a
surviving source. A stale backup cannot certify itself. If current authority
cannot be established, hold consequential work and quarantine restored private
recall. Roll back compatible code against current state instead of rewinding
grants and effect receipts.

## Consent without blocking unrelated work

Keep proposals, consent and execution distinct. A proposal holds neither an
execution lease nor a global drain lock. Finish a candidate's authorized build,
tests and recovery preparation before requesting a concrete decision.

The same durable decision is accessible through authenticated interfaces.
Notifications are optional transports. Their delay or expiry cannot imply
approval, erase work or block unrelated tasks. Bind consent to the actual
effect, artifact, target, constraints and recovery plan. A still-valid grant
can issue fresh worker credentials without another owner decision.

Revalidate before dispatch, lock only conflicting resources, execute the
approved work and verification, and reconcile the result. Preserve independently
checked trust boundaries and existing digest conventions while removing
duplicated orchestration. Simplification is not permission to make an old
approval reusable.

## How to extend and verify it

A new integration should need an adapter and configuration, not edits across
memory, identity, routing and permission engines. Prove this with an independent
test adapter before designing a universal device framework.

Every feature names the observable behavior it serves and its sole execution
owner. Its completion evidence includes the actual trigger, accepted state,
far-end result and recovery behavior. Tests should exercise lost acknowledgements,
duplicate delivery, conflicting sessions, stale recall, revocation and restart
where relevant. Keep status honest: staged, active, degraded and unverified are
different outcomes.

The first integrated demonstration is a fact recalled in a new session, one
shared commitment observed through another interface, and a decision that can
wait while independent work continues. Richer self-state, multimedia and
learning then extend the same contracts.

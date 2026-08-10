# P7 Owner-Visible Drives and Charter Governance

## Outcome

P7 adds a generic, deterministic way to explain why already-admitted goals
are ordered without giving a model new authority. The layer is independent of
any harness, communication channel, or deployment persona.

The governing rule is:

> A drive score can order a goal only after all persisted P3 policy decisions
> allow it. A score never authorizes, executes, widens scope, or settles work.

Every ranking result carries `authorization_effect: "none"` and names the five
required P3 stages: `charter`, `boundary`, `situation`, `duplicate`, and
`authority`.

## Data contracts

`DriveV1` is an immutable, versioned definition. It includes a bounded maximum
contribution, signal-count budget, enabled/disabled state, optional expiry,
scope, a short public-safe definition, and evidence references. Registering a
drive makes it available to a proposed charter; it has no effect by itself.

`DriveSignalV1` is immutable evidence about one drive and one exact goal
fingerprint. Signals require evidence references, confidence, a normalized
value in `[-1, 1]`, an explicit active/disabled/unknown state, scope, and a
maximum 90-day lifetime. Signal scope cannot be broader than its drive.

`CharterRevisionV1` is an immutable proposal containing principles, drive
weights, ranking budgets, scope, evidence, parent revision, proposer identity,
and expiry. Drive weights must sum to at most `1.0`. A revision lasts at least
one hour and at most 366 days. Expiry turns P7 ranking off; it does not remove
P3's existing boundary, situation, duplicate, authority, or execution gates.
At the server integration layer, configuring both P3 and P7 `live` also makes
the fresh active owner-ratified charter a required narrowing input to new P3
goal admission. An absent/expired charter holds admission. P7 `shadow` remains
ranking-only and never changes P3 admission.

`GoalRankInputV1` is built by the server from a P3-created project's immutable
provenance. Its policy decision references are resolved from
`CognitionSpineStore`; caller-supplied gate booleans are never used.

`GoalRankResultV1` exposes only bounded rationale summaries, contribution
arithmetic, states, and evidence references. It contains no hidden reasoning
or chain-of-thought field.

## Lifecycle and authority

A revision has a projected lifecycle of `proposed`, `active`, `superseded`,
`revoked`, or `expired`. Revision rows are never updated. Activation appends an
`activate` event and, when replacing an active parent, a `supersede` event.
Revocation appends `revoke`.

Models can register immutable candidate drives, submit evidence signals, and
propose charter revisions. None of those operations can activate a revision.

Activation and revocation reuse Colony's `ApprovalAuthorityStore`:

1. P7 derives an `ActionBinding` over the exact transition, content digest,
   revision, and expected active revision.
2. The existing approval service records the first valid decision against that
   digest.
3. Ratification requires the same unexpired request, an authenticated
   non-legacy principal with `approvals:decide`, the `owner` audience, and
   exact server-derived principal/credential evidence.
4. One approval request can be consumed by one transition. Operation IDs and
   approval request IDs are independently replay fenced.

The default approval window is one hour and is capped at 24 hours. A stale
parent, changed action digest, expired request, replayed decision, legacy
bearer, non-owner principal, disabled drive, or expired revision fails closed.

## Ranking

For each eligible goal and active drive, the ranker selects a bounded number of
fresh signals in deterministic ledger order. It computes:

`mean(signal value × confidence) × owner-ratified drive weight`

The result is clipped by the drive's absolute contribution budget. Goal scores
are clipped to `[-1, 1]`, then ordered by descending score and stable goal ID.
Unknown, disabled, expired, and budget-exhausted inputs contribute zero and are
visible as explicit states.

Before scoring, the ranker:

- resolves all five durable P3 decision references and rejects missing,
  conflicting, or denied evidence;
- rechecks the existing `DirectiveManager`, so a newly issued boundary still
  wins; and
- checks the global autonomy pause even if the P7 charter is absent or expired.

In `shadow`, the suggested order is observable but `effective_order` preserves
the input order. In `live`, only goals that pass every P3 gate and the fresh
directive check appear in `effective_order`. Downstream consumers must still
enforce their normal action and execution gates.

## Scope and observers

Scope is carried through drive definitions, signals, charters, goal inputs,
contributions, and rank results. Combining inputs narrows the result to the
most private compatible lane; incomparable private lanes become owner-only.
An owner can inspect all scoped records. Shared, public, and subject viewers
receive only records visible to their server-derived audience/person binding.

`DriveGovernanceStore.observer_projection()` exposes current lifecycle state,
active revision, definitions, signals, evidence references, and expiries. It
does not expose private records or even a private active-revision ID to an
unauthorized viewer.

## Persistence

The additive SQLite ledger uses schema/user version 7 and stores:

- immutable drive definitions and evidence signals;
- immutable charter revisions and append-only lifecycle events;
- proposal/signal operation fences;
- transition operation fences; and
- one-use approval-request consumption records.

The file is created mode `0600`. Constructing the feature with
`DriveGovernance.lazy(..., mode="off")` creates no file or directory.

See [P7-DRIVE-GOVERNANCE-INTEGRATION.md](P7-DRIVE-GOVERNANCE-INTEGRATION.md)
for the intentionally uncommitted server/API attachment contract and
[P7-DRIVE-GOVERNANCE-CANARY-ROLLBACK.md](runbooks/P7-DRIVE-GOVERNANCE-CANARY-ROLLBACK.md)
for rollout.

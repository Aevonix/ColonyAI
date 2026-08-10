# P6 Situation and Expectation Spine

Status: implemented behind migration gates; not attached by default.

This phase adds one durable present-tense situation model and upgrades the
existing expectation engine without replacing its working V1 API. It is
generic Colony code: it contains no deployment identity, phone, host, or
channel secret.

## Non-negotiable contracts

- A situation fact originates in a structured Colony/host event, probe, or
  transport/action receipt. A model assertion, inference, or prose is rejected
  as a source.
- Every observation has a durable evidence reference, event time, finite TTL,
  exact subject/viewer/shareability scope, and idempotency identity.
- `SituationSnapshotV1` is immutable. It reports each known category as
  `fresh`, `stale`, or `unknown`; absence is never silently interpreted as a
  safe fact.
- Snapshot projections are bounded to 50 facts per category and 200 overall
  (lower caller limits are supported). Facts from another subject or viewer
  are not included.
- `AppropriatenessGate` returns `allow`, `ask`, or `hold`. Its verdict includes
  a snapshot/evidence chain and an explicit `does_not_grant_authority=true`.
  It cannot mint a capability, approve an action, or select a recipient.
- `ExpectationV2` needs source evidence at creation and an
  `OutcomeObservationV1` receipt to become a hit or miss. A positive outcome
  after the horizon is scored as a miss.
- Outcome receipts are first-valid-resolution-wins and idempotent. A bare
  boolean resolver remains accepted only for migrated V1 predictions.
- Calibration is the proper Brier score, with the formula and lower-is-better
  direction exposed. Reports include domain, declared cohort, confidence-bin,
  and horizon-bucket slices.

## Situation data model

`SituationObservationV1` covers these bounded categories:

| Category | Present-tense examples | Default TTL |
|---|---|---:|
| `owner_engagement` | available, busy, do-not-disturb | 2 min |
| `activity` | current activity/interruption cost | 5 min |
| `conversation` | active/ended session | 5 min |
| `person` | recent attested participant/presence | 5 min |
| `channel` | active, available, degraded, offline | 3 min |
| `service` | healthy, degraded, recovered | 2 min |
| `approval` | pending, approved, denied, expired | 5 min |
| `commitment` | pending, overdue, fulfilled | 1 hour |
| `project` | planning, active, blocked, completed | 5 min |
| `resource` | device/work-order/capacity state | 5 min |
| `capability` | available/unavailable capability | 5 min |
| `relationship` | follow-up/cadence state | 1 day |

The observation ledger is append-only. A separate latest-fact index changes
only when a newer observation for the same
`(subject, viewer, category, entity)` arrives. Older observations remain
auditable but do not rewind current state. Replaying an observation/event ID
with different content fails closed.

`JournalSituationAdapter` consumes the durable host-journal envelope and maps
structured event families for presence/person, owner engagement, activity,
conversation, channel/delivery/outreach, service/health, approval,
commitment, project, work order/resource/device, capability, and
relationship/contact. It deliberately ignores `summary`, `description`, and
other narrative fields. A conversation turn is evidence of a recent
participant and channel, not proof of physical room occupancy.

The adapter also revalidates the exact
`cognition.external.service_state` V2 journal projection before unpacking its
typed `service` and `state` attributes. Its summary and free-form detail never
become situation facts.

Shared/public scope in an event is honored only when the boundary supplies
`boundary_attested=true`; otherwise the adapter downgrades it to
owner-private.

## Deterministic appropriateness

Policy callers declare the operation, required situation categories, target
channel, and recipient identity as structured arguments. The gate does not
infer those authority-bearing fields from goal text.

- unknown required fact -> `ask`;
- stale required fact -> `hold`;
- degraded service/target channel -> `hold`;
- unknown owner availability for outreach -> `ask`;
- busy/DND/driving/call or active conversation -> `hold`;
- another present person in a recipient-private outreach context -> `hold`;
- otherwise -> `allow`, which means only “situation is appropriate.”

Off and shadow modes cannot return an operational allow. P3 shadow comparison
may evaluate a separately constructed live-policy preview because P3 shadow
cannot create a project; that preview must never be reused by a live writer.

## ExpectationV2 compatibility and expansion

The existing `predictions` table and `Prediction` import remain valid. Startup
performs additive `ALTER TABLE` migration; existing rows are surfaced as
`schema_version=1` and are not rewritten or re-scored. `Prediction` is a
compatibility alias for the expanded record.

New code uses `ExpectationStore.create_v2`. The existing commitment generator
now creates V2 rows, while causal/world-model generators that still use the
V1 call retain their old resolver behavior. The engine adds bounded structured
generators for:

- contact cadence;
- task duration;
- service recovery;
- relationship follow-up.

Each generator accepts at most 100 records, requires evidence references, and
skips missing/invalid or already-past horizons. Generation does not resolve an
expectation. Subsystems settle V2 through `ingest_outcome` with a typed receipt.

Observer projections filter both records and calibration to the exact subject
and viewer. A viewer cannot infer another scope from aggregate cohort counts.

## Deliberate limits

- This phase does not fabricate device, physical-presence, channel-health, or
  approval events. Deployments must journal their real observations. The
  server attachment's one built-in direct probe is task-queue execution
  capacity: it combines the existing maintenance-readiness gate with the
  durable worker registry's canonical heartbeat freshness, and its observation
  TTL never exceeds that worker-liveness TTL.
- A recent conversation participant is not treated as current physical
  presence.
- Situation is not a long-term world model and does not preserve narrative.
- Situation `allow` is not approval. P3 boundary, authority, duplicate, and
  capability checks remain mandatory and later action execution still needs
  its own WorkOrder/approval authority.
- No host router, benchmark, experiment, voice, Hermes, or host-deployment file is changed
  by this bridge. Server attachment remains migration-flag driven: `off` stays
  dark, while `shadow` and `live` attach during normal startup.

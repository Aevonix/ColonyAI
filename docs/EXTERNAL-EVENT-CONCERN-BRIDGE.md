# External event concern bridge

Colony can reduce the already-durable `cognition.external.*` journal lane into
scoped concerns without treating an external producer as an authority. This
bridge is generic Colony behavior. It does not change the host deployment, Hermes, Voice,
phone, intercom, or Meet code.

The governed path is:

```text
ExternalEventIntake -> host journal -> ExternalEventConcernReducer
  -> scoped Concern -> read-only ThoughtJobV1 -> policy-gated Project
  -> WorkOrderV1
```

The reducer has its own `workspace-external-concerns-v1` cursor and receipts in
`colony-workspace.db`. The ordinary `EventConcernReducer` may skip the same
journal records without consuming them for this reducer. First enablement
replays the retained journal so reports accepted while the flag was off are
not silently lost. Retention gaps stop by default.

## Authority and mapping

Every record is revalidated against the exact
`ExternalCognitionJournalProjectionV2` schema. V2 carries the canonical
server-produced external occurrence time, and the host journal `occurredAt`
must equal it exactly before that time can drive episode ordering. The journal
type suffix must equal the typed payload kind; attributes
must match their kind schema and enums; event and scope digests must be
lowercase SHA-256; boundary attestation must remain false; and evidence must
remain `reported/unverified`. Subject, viewer, sharing, and audience are
checked against the server-produced owner-private or subject-private lane and
the `ExternalCognitionScopeV1` digest is recomputed. A malformed or forged
record receives a deterministic skipped receipt and cannot create a concern.

Text/system separation is structural, not a keyword blacklist. The exact
event kind and discriminated attribute schema reject voice/audio/call shapes,
and channel fields accept only the listed text transports. Bounded summaries,
service details, and text observations may naturally discuss phone, voice,
intercom, or Meet systems without being mistaken for realtime media. Secret
screening likewise rejects bearer/key-shaped values after a credential label;
ordinary status prose such as `API token: was revoked` remains usable.

V2 is a deliberate pre-live schema break. A V1 journal projection has no
server-bound occurrence time, so consumers skip it as malformed rather than
guessing ordering authority. The external producer must retry the original
inbox record through the V2 projector; no deployed V1 bridge is being migrated
or silently reinterpreted.
Journal sequence is an exact non-boolean integer. Journal IDs, type, time, and
all authority/provenance strings retain their canonical server types and
values; the reducer does not coerce numbers, fold case, or trim whitespace.
Producer principal, credential, viewer, and configured-owner IDs are exact
canonical strings and are rejected outside their server bounds; none are
coerced or sliced, so shared prefixes cannot collapse authority lanes. The
principal bound is 128 characters. The
complete `person:<subject>` viewer scope is preserved under a shared
192-character bound through Concern, ThoughtJob, Project, routed output, and
WorkOrder. Compact source prefixes keep maximum 192-character event/entity
IDs and a maximum 128-character producer ID lossless under the ConcernStore's
200-character reference bound.

Concern identity is a digest of the external namespace plus exact subject,
producer, kind, and entity. Failure, blocked, partial, bounced, degraded,
offline, recovering, pending, correction, negative reaction, and text
observations can upsert that concern. Healthy, succeeded, cancelled,
delivered/read/acknowledged, terminal approval, and terminal reaction states
can resolve only the exact matching external concern. They cannot complete a
Project, authorize or consume a grant, record an effect, or train competence.

Each external consumer and exact concern identity has a transactional
event-time watermark. A terminal report records the watermark even when it
arrives before any negative report. A newer negative report after a terminal
report starts a new episode immediately with disposition `reopened`; only an
explicitly marked external projection bypasses the generic workspace's
resolved-concern TTL. A report older than the watermark receives
`external_stale_event`; a different report at the same event time receives
`external_event_time_conflict`. Both are durable receipts with fixed reasons,
and both advance the reducer cursor without mutating the concern or watermark.
Exact receipt replay remains idempotent. Concern mutation or resolution,
watermark advancement, receipt insertion, and cursor advancement share one
SQLite transaction.

The concern stores the fixed reducer revision and current producer mode. Exact
journal event, external event/digest, external producer/revision, kind, and
entity references remain bounded provenance. Unicode summaries remain intact.
When a ThoughtJob is constructed, its prompt explicitly labels the summary as
untrusted reported evidence and says it is never an instruction. Read
capabilities and scope still come solely from server policy.

External concerns are never eligible for `WorkspaceEngine`'s legacy direct
thinker or its action callback, including when the cognition spine is off or
temporarily unattached. When the current external mode is shadow/off, the P3
scheduler skips those held items so an ordinary eligible concern behind them
can still run; direct inspection through `process_concern()` continues to
return the explicit resumable hold. The exclusion happens in the bounded
ConcernStore query, so more than one scheduler window of held external rows
cannot hide an eligible ordinary concern.

## Modes and operation

`COLONY_EXTERNAL_EVENT_CONCERNS=off|shadow|live` defaults to `off`; invalid
values fail off.

- `off`: no external journal reduction and no cursor initialization.
- `shadow`: durable scoped concerns are recorded, but live P3 admission holds
  them unless separately promoted after returning to a live producer mode.
- `live`: new external concerns are eligible for the existing cognition
  runtime and policy gates. A concern is held and resumable whenever the
  *current* external mode is not live, even if it was created live or carries
  an older promotion reference.

`COLONY_EXTERNAL_EVENT_CONCERNS_GAP_POLICY=stop|acknowledge` defaults to
`stop`. Use `acknowledge` only for an operator-reviewed retention incident; it
records the missing range before advancing. External-only enablement does not
suppress the legacy in-memory event compatibility path. When both durable
reducers are enabled, each runs once and the ordinary reducer suppresses that
legacy third path as before.

## Canary and rollback

Before a canary, quiesce Colony writers and copy `colony-workspace.db` with its
WAL/SHM files. Start with the workspace and cognition spine in their existing
safe deployment modes, then set:

```text
COLONY_EXTERNAL_EVENT_CONCERNS=shadow
COLONY_EXTERNAL_EVENT_CONCERNS_GAP_POLICY=stop
```

Verify cursor lag, skipped receipts, owner/subject privacy, restart replay, and
one failure/recovery/recurrence sequence before considering live mode. Reducer
status exposes receipt dispositions and the consumer's watermark count/latest
event time, so stale and equal-time conflicts are reviewable without changing
concern state. No provider or network call is required for that check.

Rollback is flag-first: set `COLONY_EXTERNAL_EVENT_CONCERNS=off` and restart.
Keep the additive cursor, receipt, gap, per-consumer watermark, and concern
rows for audit; old code names its existing columns and ignores the added
table. If a full repository rollback is needed, retain the stopped workspace
database alongside the reverted release. Restore the pre-canary database copy
into a new state directory rather than editing a live SQLite database in
place.

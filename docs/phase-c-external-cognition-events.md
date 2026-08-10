# Phase C external cognition events

Phase C slice 1 adds one narrow intake for durable text and system evidence:
`POST /v1/host/cognition/events`. It does not add a voice, phone, Meet,
intercom, FaceTime, realtime-audio, approval, or action-execution path.

## Authority and schema

The route requires the exact `cognition:events-ingest` scope on a scoped,
non-legacy principal with a credential and viewer binding. The request body may
contain only `event_id`, `kind`, `occurred_at`, `summary`, and bounded flat
`attributes`.

Allowed kinds are:

- `action_outcome`
- `delivery_outcome`
- `service_state`
- `approval_state`
- `operator_reaction`
- `text_turn_observation`

Each kind has a closed, versioned attribute discriminator. Reducers never infer
structure from the summary:

| Kind | Required attributes |
| --- | --- |
| `action_outcome` | `action_id`, bounded `outcome`; optional lowercase 64-hex `action_digest` |
| `delivery_outcome` | `delivery_ref` or `message_ref`, bounded `outcome` |
| `service_state` | `service`, bounded `state` |
| `approval_state` | `request_id`, bounded `state` |
| `operator_reaction` | `target_ref`, bounded `reaction` |
| `text_turn_observation` | `turn_id`, text-only `channel`, normalized nonempty 1–500 character `observation` |

Unknown per-kind attributes, malformed digests, booleans in numeric fields,
non-finite values, and out-of-range durations, latency, sample counts, or
reaction intensity are rejected.

Principal, credential revision, subject, viewer, audience, shareability, scope,
scope digest, and `boundary_attested=false` are server-derived. Caller fields or
attributes that attempt to assert identity, scope, approval, grants, receipts,
verification, or outcome authority are rejected. Reported outcomes enter the
journal with `evidence_status=reported/unverified`; only a later exact join to a
local receipt ledger may upgrade that status.

Secret-bearing fields and values, nested attributes, unbounded text, non-finite
numbers, and realtime/voice aliases—including GMeet, telephone/telephony, VoIP,
PSTN, and SIP—are rejected before the inbox is written.

## Durability and replay

`ExternalEventInboxStore` reserves the immutable event digest and receipt before
journal projection. Exact `event_id` replay returns the stored receipt; changed
content returns HTTP 409.

Journal projection uses a deterministic event key. A minimal marker containing
only sequence/ULID/timestamp and request digests is written before the event
file, allowing restart recovery across both relevant crash windows:

1. inbox receipt committed, journal append unavailable;
2. journal append committed, inbox finalization interrupted.

The journal retention pass marks keyed records as pruning before deleting the
record. If the inbox has not acknowledged the projection, retention preserves a
fixed-size, no-content tombstone containing only the original sequence, ULID,
recorded timestamp, and digests. Replay returns those same coordinates with
`retained=false` and never rewrites pruned content. The inbox commits that
truthful projection metadata before acknowledging and removing the marker;
projected replays clean up an acknowledgement interrupted by a crash. Normal
markers are therefore short-lived, and crash tombstones remain bounded records
with no summary, attributes, event key, or other private event content.

## Rollback

The change is additive. Reverting the Phase C commit removes the route and
startup attachment. The SQLite inbox and journal marker directory may remain on
disk; older code ignores both. No approval, queue, action-plane, host-deployment, Hermes,
or voice semantics are changed.

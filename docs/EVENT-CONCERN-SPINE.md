# Durable event-to-concern spine

Colony's cognitive workspace can consume the durable host event journal through
`EventConcernReducer`. This replaces periodic state polling and a process-local
event bus as the source of cognitive salience. It is generic Colony behavior;
it does not depend on Hermes and does not change the host's voice path.

## Safety and truth contract

- The host journal sequence and ULID are the source event identity.
- Concern mutation, one event receipt, and the consumer cursor commit in one
  SQLite transaction in `colony-workspace.db`.
- A new event with the same material state advances the cursor but does not
  bump salience or rewrite the concern's last-material-event fields.
- Unknown and malformed sequenced events receive an audited `skipped` receipt;
  they are not interpreted by an LLM.
- A retention gap stops the reducer by default. An explicit
  `COLONY_EVENT_CONCERNS_GAP_POLICY=acknowledge` records the missing range in an
  append-only gap ledger before resuming; it never pretends those events were
  processed.
- Subject, viewer scope, shareability, source refs, and last material event are
  stored on the concern. Missing scope is owner-private. A payload cannot make
  itself shared/public without `boundary_attested=true` from a server boundary.
- Scoped workspace reads filter deterministically. A subject may view a
  subject-private concern, but only the owner principal (or the temporary
  legacy migration principal) can resolve one because resolution may cascade
  into a commitment or project mutation.
- Scoped clients require `cognition:read` to inspect the workspace and the
  separate `cognition:manage` scope to resolve a concern. Ordinary channel,
  worker, model, and memory principals receive neither by implication.

## Flags

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `COLONY_EVENT_CONCERNS` | `off`, `shadow`, `live` | `off` | Construct and consume the durable reducer. Shadow and live have identical read-side reduction; downstream thought/action mode remains governed by `COLONY_WORKSPACE`. |
| `COLONY_EVENT_CONCERNS_BOOTSTRAP` | `tail`, `replay` | `tail` | On the first boot, start after current high-water or replay the retained journal. Tail avoids manufacturing present concerns from ambiguous history. |
| `COLONY_EVENT_CONCERNS_GAP_POLICY` | `stop`, `acknowledge` | `stop` | Stop on retention loss, or explicitly audit and advance across the gap. Do not leave `acknowledge` configured after recovery. |

External `cognition.external.*` reports use a separate consumer and flag so
the ordinary reducer cannot consume their cognition lifecycle. See
`docs/EXTERNAL-EVENT-CONCERN-BRIDGE.md` for its strict reported-evidence,
privacy, rollout, and rollback contract.

The reducer currently maps material commitment, surprise/expectation,
anomaly/service, project/work, approval, and relationship/cadence events.
Ordinary conversation turns and non-material status chatter are explicitly
skipped.

## Additive migration

Opening the existing workspace database adds nullable scope/material-event
columns and three tables:

- `concern_event_cursors`
- `concern_event_receipts`
- `concern_event_gaps`

Legacy concerns remain readable and retain their existing lifecycle. The
migration drops or rewrites nothing.

Before a live canary, take a consistent copy of `colony-workspace.db` and its
WAL/SHM while Colony writers are quiesced. Enable:

```text
COLONY_EVENT_CONCERNS=shadow
COLONY_EVENT_CONCERNS_BOOTSTRAP=tail
COLONY_EVENT_CONCERNS_GAP_POLICY=stop
```

Then append one synthetic server-scoped `service.degraded` event and one
matching `service.recovered` event. Verify exactly one create and one resolve,
restart the sidecar, and confirm the cursor and receipt counts do not change.
Run a two-contact test and verify neither scoped reader sees the other contact's
concern. Leave `COLONY_WORKSPACE=shadow` until the later governed ThoughtJob and
Concern-to-Project path are live.

## Rollback

Set `COLONY_EVENT_CONCERNS=off` and restart the sidecar. The existing periodic
workspace ingest becomes the compatibility fallback. Keep the additive columns,
cursor, receipts, and gaps for audit; code rollback can read the same database
because old queries name their columns explicitly. If SQLite compatibility is
ever in doubt, restore the paired pre-canary workspace database into a new state
directory and point the old release at that copy rather than modifying the
failed database in place.

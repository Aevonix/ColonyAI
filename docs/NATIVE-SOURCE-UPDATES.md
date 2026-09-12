# Source-bound updates during native work

An authenticated host can register a new instruction with the active task's
`SuppliedInput`. Hermes still owns task execution, `/steer`, queued follow-ups,
interruption and session recovery. This opt-in adapter API creates no task
runner, durable queue or new authority grant.

```python
from colony_hermes.input_provenance import SourceUpdate

update = SourceUpdate(
    update_id=retained_update_id,
    contact_id=current_owner_contact,
    instruction=retained_instruction,
    input_refs=captured_input_refs,
    source_refs=canonical_source_refs,
)
carrier = supplied.register_update(
    update,
    validate=owner_grant_is_current,
    observe=retain_source_update_receipt,
)
```

The host first admits the original human input through its canonical source
path and verifies current ownership of both the task and the new input.
Different channels can identify the same owner; equality of an unverified
contact string cannot establish that fact. A host must preserve the true
source channel and cannot impersonate the original adapter principal.
Registration is not a replacement for the host's authority or source checks.

`input_refs` contain exact `source_id` and `input_message_hash`; `source_refs`
contain exact `source_id` and `source_version`. The record copies its references
into immutable internal pairs. Its returned reference properties are copies.
The update ID allows 1-128 ASCII letters, digits, dots, underscores, colons or
hyphens. An instruction is nonempty and at most 32 KiB in UTF-8. One active
input object retains at most 16 updates, 64 KiB of instruction text, and 64
distinct input/source parents of each kind including the original task.

The carrier is deterministic for the exact normalized record. The owning host
may restore that record before native hooks run; it remains inert until the
original `transport_input` binds an authenticated root. It cannot bind another
contact or an unrelated native session. Registration of the same record is
idempotent within this object; reusing its ID for different data raises an
error. A closed or blocked object cannot be reopened by another registration.

The host passes this exact carrier through its supported native steering
boundary. Marker text alone grants no source lineage. Only a registered carrier
that survives the existing authenticated RequestMemory filter adds its parents
to the task. Those parents remain dependencies when a joined child runs or the
native request no longer repeats the carrier. Canonical source currentness and
erasure use the existing request checks. Revoked host grants, unavailable
source checks and erased consumed input withhold the existing task request and
its tools rather than forwarding stale instructions.

## Callbacks and evidence

Both callbacks are synchronous and must be bounded by the host. `validate()`
must return exactly `True` after rechecking current local owner/transport
grants. Canonical source checks remain in Colony's request path. A false return
or exception blocks dependent continuation.

`observe(metadata)` must return exactly `True` after the host durably saves the
update's consumed-parent relationship. A false return, `None`, or an exception
withholds the request before continuation. This allows a resumed task to keep
its update dependencies after a crash before the final answer was recorded.
Omit this callback only for a host making no durable update-recovery claim.
Receipts must be idempotent; they do not dispatch another instruction.

Metadata contains `update_id`, `stage`, the exact native `session_id`, `task_id`
and `turn_id`, a request SHA-256, and an explicit `boundary`:

| Stage | What it establishes |
|---|---|
| `middleware_visible` | The exact registered carrier survived Hermes request middleware. |
| `native_request_visible` | The carrier survived Colony's turn-scoped Relay callback immediately before `next_call`; `boundary` is `relay_before_next_call`. Later downstream callbacks or the SDK may still change or fail the request. |

The hash describes that boundary's request, encoded as sorted compact JSON with
ASCII escapes. It is not a claim about final network bytes. The active object
keeps the first observation per update, stage and native tuple in a bounded
cache. Cache eviction can repeat an idempotent receipt, never an instruction.

Neither stage means the model followed the instruction, an artifact changed,
an effect was approved, or a message reached its recipient. Actual SDK exposure
and behavioral application require their own observations. Controlled native
tests exercise the actual OpenAI SDK codec and Hermes summary path, but their
fixture responses are not measurements of model quality.

## Host lifecycle obligations

- Keep the root task identity and original source distinct from each later
  update. Persist accepted and consumed dispositions separately.
- Associate the object with the actual native handler lifetime, and remove
  that association when the handler leaves its context. A new context created
  around a separate HTTP callback is not the running worker's context.
- Rehydrate retained registered carriers and consumed parents for a legitimate
  native resume or queued follow-up. Registration alone must not resend them.
- Hermes may carry a late `/steer` as a subsequent turn. Preserve its canonical
  input identity and distinguish an intermediate notice from the final task
  result.
- Hermes' current slash acknowledgment provides no durable steer-ID deduplication
  or structured applied receipt. Do not automatically resend an ambiguous
  dispatch or call it applied because the control command was acknowledged.

This API does not physically delete native transcripts, remove unknown
paraphrases, enable detached-child steering or change foreground busy policy.
It does not make an ordinary foreground conversation share a build's execution
session. Use the [current Hermes qualification target](HERMES-HOOK-COMPATIBILITY.md)
and the `apsimo-hermes[native-memory]` extra for the native request boundary.

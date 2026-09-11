# Native task handoff storage

`colony_hermes.task_handoffs.TaskHandoffs` holds durable associations between a
captured instruction and a Hermes-owned task. It has no worker, scheduler,
model client, HTTP server, or tool registration. This extraction is available
to adapters; importing it does not enable background tasks on a fresh install.
The optional native text adapter is described in
[NATIVE-TASK-CHANNELS.md](NATIVE-TASK-CHANNELS.md).

## Boundary

The constructor takes three trusted callbacks:

```python
TaskHandoffs(
    database,       # () -> context manager yielding a SQLite connection
    resolve_source, # (source, dependencies=None) -> normalized current source
    resolve_owner,  # (source, *, require_task_grant: bool) -> current contact ID
    error_type=TaskHandoffError,
    reply_effect="retained_for_transport",
)
```

The database connection uses `sqlite3.Row`, commits successful operations, rolls
back failed operations, and closes on exit. An adapter supplies its existing
state database. The store does not create another task queue or an execution
registry alongside Hermes.

`resolve_source` verifies canonical source readability, freshness and current
ownership. Its normalized record contains `version`, `principal`,
`source_session_id`, `input_refs`, `source_refs`, `watermark`, and `contact_id`.
An optional `origin` records the actual native channel, sender, session and turn
when the trusted resolver can attest them. It must come from the actual native
scope, not a model argument or a copied request field. Existing voice sources
omit this optional field and keep their exact stored encoding and identifiers.

`resolve_owner` independently checks the current participant binding. It must
not require the original content to remain readable: an authorized participant
must be able to stop work after its input has been erased. When
`require_task_grant=True`, it also checks the current task grant. Both the
original and steering source must satisfy this stronger check. A score,
display name, or claimed identity is not an owner binding.

These callbacks are application authority boundaries. The store is not a
remotely callable authorization API. The adapter must authenticate the caller
and compare its current owner with the task owner before offering status,
stop or steering across channels. `control(..., principal=...)` retains the
older exact-principal control path. Omitting `principal` permits an already
authenticated adapter to implement same-owner control across channels; it
does not authorize an arbitrary caller who knows a task ID.

## Lifecycle retained from the existing adapter

| Operation | Retained behavior |
| --- | --- |
| `admit` | Captured source and bounded instruction determine one stable ID. Reusing a request ID for different content is rejected. Erasure watermark changes do not create another task. |
| `bind` | A correlated native hook records the exact session, task and turn. A successor clears a previous turn's termination receipt. |
| `admit_update` | An immutable source identifies one steering update. Current owners and grants are checked for both channels. Acceptance alone does not mean consumption. |
| `claim_update_dispatch` | Exactly one durable claim precedes native dispatch. A crash with an ambiguous dispatch remains unknown and is not blindly replayed. |
| `observe_update` | Middleware and native request observations retain consumed source dependencies before continuing. Request visibility does not prove provider delivery or semantic application. |
| `request_stop` | One SQL decision orders stop intent against reply retention. Reply-first stays complete; stop-first prevents late reply retention. |
| `observe_terminal` | Only a matching native session/task/turn receipt can establish observed turn termination. A late old-turn receipt cannot settle a successor. |
| `observe_stop` | Native control acknowledgement remains distinct from termination. Admission blocking and verified native startup-resume suppression have their own cancellation basis. |
| `complete_source`, `retain_reply` | Results require a current canonical source receipt. Persistence does not claim delivery or playback. |
| `erase_task_handoffs` | The existing erasure pass removes owned instruction/result/update copies, including consumed parents. It preserves IDs, native associations and stop intent. |

`pending` and `pending_updates` are bounded readers for the adapter's existing
drain, not independent schedulers. Native observations must come from the
correlated native callback or hook, never an external request body. The store
always reports process cleanup as unobserved. A matching turn-finalizer receipt
does not prove physical child-process cleanup.

The current cancellation scope remains limited before a native turn has been
bound. An accepted stop alone cannot prove an early, unbound native turn has
terminated. The adapter must retain that uncertainty until it has a supported
observation; a missing session guard is insufficient.

## Compatibility and adoption

The existing `native_voice_handoffs` and `native_voice_updates` table names are
preserved intentionally. They describe the original storage contract, not a
requirement to relabel a text instruction as voice. Adoption keeps the same
database and rows. There is no export/import, backfill, or second task identity.
The legacy row's `origin_session_id` remains the first native execution session;
it is distinct from the captured source session and optional channel `origin`.

An existing voice adapter can reduce its storage module to a constructor wrapper
that supplies its database, canonical source resolver and owner resolver. It
passes its existing exception class and
`reply_effect="retained_for_voice_transport"` to preserve catch boundaries and
reply encoding. Its existing erasure function forwards to
`erase_task_handoffs(database, contact_id, rules)`, keeping callers intact.

The adapter must retain stop-aware readers during rollback. Merely accepting
the additive SQLite columns is insufficient: an old reader that ignores
`stop_json` could re-admit a cancelled task that never acquired a native session.
Do not restore an old database snapshot or delete stop intent to make rollback
appear compatible.

## Native text integration

1. Capture the current ordinary instruction through the existing
   `followups.capture_instruction(scope, client)` boundary. Use
   `client.source_message_hash` for the original user input reference. The
   source session and origin remain the actual native text conversation.
2. Resolve the actual sender through the existing scoped participant resolver.
   Supply the normalized source and current owner callbacks above. No synthetic
   voice session is needed.
3. Dispatch the association through a thin native task platform using the
   existing Hermes gateway task execution path. Return the task ID immediately
   so the foreground conversation can continue. Do not reuse foreground
   `inject_message` as a second task executor.
4. Supply steering as a registered, source-bearing native update. Route stop
   through the supported native stop event for the exact owned task. Preserve
   the distinction between accepted control, request visibility and observed
   terminal state.
5. Use the existing current-work projection and the same association ID for
   status across channels. Keep endpoint, phone, panel and audio adapters in
   the deployment layer.

The useful next integration check holds a real native task at its controlled
provider boundary while another ordinary text turn completes. A second channel
mapped to the same owner steers and stops that same task; a foreign contact
cannot. An unrelated task completes, a late reply cannot replace cancellation,
and reopening storage cannot revive stopped admission. This is separate from
the storage qualification in `tests/hermes_adapter/test_task_handoffs.py`.

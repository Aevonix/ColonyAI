# Native background tasks from ordinary conversations

The optional `colony_task` tool lets an authenticated owner start a task and
continue the foreground conversation. Another channel currently mapped to that
same owner can inspect, steer or stop the retained task ID. The gateway runs
the work through its normal adapter, agent loop, session store, interruption
and recovery path. Apsimo supplies source provenance and durable associations.

This feature does not create another executor or network endpoint. Hardware
adapters can subclass the public native adapter and supply their existing
authentication and delivery policy. The native text adapter itself retains
results for inspection; it does not send unsolicited completion messages.

## Enable

Configure the ordinary Apsimo plugin, owner identity, source ledger and memory
provider first. Then enable both the tool and its native execution platform:

```yaml
plugins:
  enabled: [colony]
  colony:
    native_tasks:
      enabled: true
      # Optional; defaults beside the configured turn outbox.
      # state_path: /private/agent-state/native-tasks.sqlite3

platforms:
  colony_task:
    enabled: true
```

The gateway must support concurrent session-scoped plugin callbacks for the
source-binding and finalization hooks. Qualification must cover the exact
Hermes interface used by the deployment. Merely enabling this configuration
does not establish concurrency or production readiness.

The execution adapter uses the configured canonical owner as its native sender
and the native trusted-upstream authorization contract. It has no external
ingress, and its controller and correlated handler verify the retained actual
owner before native admission or recovery. This avoids copying canonical owner
IDs into each channel's unrelated environment allowlist. A configured shared
transport must authenticate its own callback before dispatching a retained ID.
Standalone transport subclasses keep the base adapter's ordinary authorization
policy; the configured controller applies trusted upstream delegation only
after the shared source and owner checks.
It does not create a `colony_task` contact handle. Its public transport scope
comes from the retained original channel and current owner binding, and keeps
that original handle for ordinary per-tool authority checks. Explicitly
attested local platforms keep their existing local owner policy.

## Tool contract

| Operation | Fields | Observation |
| --- | --- | --- |
| `submit` | `request` | Stable `task_id`, durable acceptance and any observed native admission. Acceptance is not completion. |
| `status` | `task_id` | Existing native state and, when still readable, the retained result. |
| `steer` | `task_id`, `request` | One captured source update and separate native control/request visibility receipts. Visibility does not prove model obedience. |
| `stop` | `task_id` | Durable stop intent and matching native termination or verified admission/resume suppression when observed. |
| `list` | none | Recent owned associations, with an explicit incomplete-running-inventory marker. Use the existing current-work view for the wider activity picture. |

The model cannot supply an owner, arbitrary source envelope or slash command.
Submission and steering capture the actual current ordinary instruction through
the existing canonical source API. Original source and update owners are
resolved independently. Derived task turns and subagents cannot manufacture
new ordinary instructions through this tool.

Stop remains available to the current owner after original input erasure or a
memory service outage, provided the current owner binding can be established.
It does not expose erased request or reply text. Source annotation invalidates
an old instruction; a fresh corrected instruction must be captured. It is not
silently treated as unchanged merely because its source bytes still exist.

## Execution, recovery and reuse

`NativeTasks` captures instructions and schedules retained IDs onto its
registered adapter's existing event loop. A bounded callback timeout leaves
the same ID inspectable rather than cancelling partially admitted work.
Independent dispatch uses a fresh copy of the context captured when that
adapter connected. It preserves gateway/profile routing without inheriting the
foreground tool's managed execution ancestry. Source lineage is retained in
the handoff; it is not inferred from a calling thread's context.
`NativeTaskAdapter` supplies source context around the real gateway message
handler and records its exact session/task/turn. Native `/steer` and `/stop`
events operate only on that owned origin. Terminal-before-stop stays complete;
stop-first prevents a late response from replacing cancellation.

Pending admissions and updates are reconsidered on adapter connection and the
existing native Kanban dispatch tick, in bounded batches. This adds no timer.
If gateway Kanban dispatch is disabled, automatic retry is limited to the next
connection; explicit status/control still works. A claimed but ambiguous
steering dispatch is never blindly resent.

The SQLite associations retain the original `native_voice_handoffs` and
`native_voice_updates` tables so existing deployments can reuse their rows and
IDs. The private transport supplies its database and source/owner resolvers;
it retains phone, panel, call and outward-delivery details. Adopting the generic
text platform does not automatically merge an existing hardware adapter's
separate database or control surface. That bridge requires an explicit
same-owner source adapter and its own integrated qualification.

An existing transport can configure exactly one controller factory:

```yaml
plugins:
  colony:
    native_tasks:
      enabled: true
      factory: private_agent.task_sources:build
      # Other fields are interpreted by this private factory.
```

The importable factory receives `build(client, outbox, owner_contact_id,
*, config)`, where `config` is the `native_tasks` subsection, and returns a
`NativeTasks` instance. Factory failures propagate; there is no fallback to a
different registry or source policy. Its constructor accepts the existing
`database()` context manager, `sources`, an optional `NativeTaskAdapter`
subclass as `adapter_type`, and optional storage `error_type` and `reply_effect`
compatibility values. The source policy supplies `capture`, `resolve_source`,
`resolve_owner`, `authorize_control` and `execution_identity`. The last method
projects an already verified source into the existing canonical sender fields;
it cannot establish authority from caller-supplied contact IDs. The public
`NativeTaskSources.actor_contact(scope)` resolves an ordinary actor without
manufacturing an instruction or enrolling a voice session.

The shared callback calls `controller.dispatch(payload)` with the retained ID
and existing action fields. This selects the original adapter from the real
native session store, then calls its `dispatch_native_event`. An optional
constructor `adapter_resolver(origin)` can resolve an already owned adapter;
otherwise the controller can capture the gateway from its native dispatch
hook. This is not a startup adapter-discovery guarantee. A migration without
a startup resolver must drain old admissions and confirm old native work is
inactive and nonresumable before switching admission platforms. Missing or
ambiguous origins remain unavailable, and old tasks are never relabelled or
restarted on a new platform. Retained completion and observed cancellation
remain inspectable without the old adapter being online.

See [NATIVE-TASK-HANDOFFS.md](NATIVE-TASK-HANDOFFS.md) for storage and compatible
rollback requirements. Stop-aware readers must remain in a recovery selection.

## Useful qualification

`tests/hermes_adapter/test_native_task_channels.py` drives the registered tool
through actual gateway conversations, using a controlled SDK response and the
real canonical/contact APIs. It holds two native task roots, completes an
ordinary foreground conversation, steers and stops one task from another owner
channel, then confirms that the other task completes and the late stopped
result is not retained. No synthetic task contact is enrolled.
`test_task_controller_dependencies.py` uses the actual native session store to
check injected-store compatibility, original-platform routing, missing-origin
non-admission and retained terminal results without a running legacy adapter.

This controlled test establishes transport, source and lifecycle behavior. It
does not measure model instruction following, physical messaging, production
latency, or child-process cleanup. Before-first-binding cancellation remains
unqualified until an exact native terminal or recovery observation exists.

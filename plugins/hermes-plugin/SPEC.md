# Colony Hermes general-plugin governance specification

## Scope

This plugin is a generic Colony sidecar adapter for Hermes 0.18.2-compatible
hosts. It does not own persona, deployment identity, voice, meetings, or direct
execution. The host deployment supplies those layers externally.

The runtime contract is:

1. `pre_llm_call` binds the exact Hermes session/task/turn to transport and
   sender metadata supplied by the host.
2. Real channels resolve the sender with `create=false`. No missing/failed
   lookup becomes an owner or shared default contact.
3. Private legacy read tools require an owner/system authority lane. Guest
   context comes from the canonical Colony memory provider.
4. Every enabled model-visible effect tool emits an immutable intent. Generic
   actions use `HermesToolActionIntentV1`; owner-directed contact messages use
   a separately credentialed deployment producer that cannot call a provider
   from the plugin process.
5. `tool_execution` middleware preserves `api_request_id`, `turn_id`, and
   `tool_call_id` that Hermes does not pass to registry handlers.
6. `post_llm_call` synchronously commits one stable participant-bound turn to
   a durable local outbox before a small bounded network drain. The memory
   provider and old worker tool writers must be disabled.
7. `transform_llm_output` applies ResponseGuard only to text. Voice, phone,
   intercom, and Meet surfaces are explicitly excluded.

## Model catalog

`_TOOL_SCHEMAS` is an explicit sorted catalog. Names are partitioned between
`read_tool_names` and `action_intent_tool_names`; there are no direct-effect
handlers. `colony_autonomy_status` is a bounded owner/system-only GET projection
that omits the endpoint's private configuration. `governance_attestation()`
hashes the exact schema and empty event catalog for source preflight, but never
claims runtime or live readiness.

The source read catalog is an upper bound. If `enabled_read_tools` is omitted,
runtime registers that full read catalog unchanged for backward compatibility.
An explicit configuration registers only its validated exact subset, including
the useful empty subset for message-only profiles. Blank, duplicate, unknown,
or malformed entries fail before registration. Registration filtering is the
model-visibility boundary; dispatcher enforcement of the same subset is an
independent fail-closed layer. Action and message subset semantics are
orthogonal and unchanged.

`runtime_governance_attestation(config)` is the separate local runtime proof.
It requires a safe mediator origin, resolved bounded credential, valid
principal, nonempty exact enabled-action subset, and an initialized private
outbox with attested SQLite/filesystem configuration. It exposes only component
booleans, normalized read/action/message names and digests, read-selection
source, and a path digest—never a credential or filesystem path. No network
reachability is claimed or tested by this local
proof. Configuration readiness is explicitly separate from physical media
verification: `physical_power_loss_verified=false` is invariant here.
Consequently its `live_ready` field is always false, even when every local
runtime component is ready. Operational liveness belongs to a separate
deployment-owned network/canary proof.

Intent identity is derived from the host call identity, not model arguments.
Changing arguments while replaying the same tool call produces a deterministic
conflict. `intent_id` and `intent_digest` bind the client submission; the
canonical UUID `action_id` and lowercase SHA-256 `action_digest` are assigned
by the server. The exact version-1 admission response is pending-only, always
has `effect_performed=false`, requires an `approval_id`, and cannot forward
extra mediator fields. An unavailable mediator performs no fallback sidecar
call.

The static schema is the governed upper-bound catalog, not an executability
claim. Runtime registers an effect schema only when it appears in the explicit
`enabled_action_tools` subset and the mediator has an allowed loopback/approved
origin, resolved credential, and safe principal. Unknown configured tool names
fail registration. Deployments must enable only actions backed by exact
idempotent execution and verification; unsupported actions remain invisible.

`colony_send_message` is registered only when its exact message mediator and
explicit enabled subset are ready. It requires either a resolved owner on a
text transport or an explicitly attested local system turn; guests,
unresolved senders, and speech surfaces are denied before admission. Retry
identity comes from Hermes session/turn/tool-call metadata. The model may
supply only a recipient display name, message content, and an optional channel
bounded to WhatsApp/RCS/SMS. Omitting channel preserves the exact legacy V1
WhatsApp wire request; choosing one uses V2. Standing, authority-free contact
scope, verified-handle, ResponseGuard, exact active fixed-route, and provider
lifecycle evidence remain the deployment Action Plane's responsibilities. A
missing route is held and never becomes `proactive_new_target` or a fresh
owner prompt. Attested local-system initiation is emitted as V3 with a
server-derived origin and explicit channel, keeping autonomous cadence/policy
truth distinct from a genuine owner instruction.

## State

The only cross-callback state is bounded and keyed by exact session/turn or
idempotency key. It is lock-protected and contains no process-global “last
sender,” “last contact,” or shared event cache. Rebinding the same host turn to
a different participant poisons that scope.

`GOVERNED_EVENT_TYPES` is empty until Colony can provide an exact
viewer-attested event projection. Event context must not be injected from a
process-wide subscriber.

The canonical turn writer is a SQLite outbox whose immediate parent is an
exact mode-`0700`, current-effective-user directory. Every path component is
opened with POSIX directory descriptors and no-follow semantics. The leaf is
created atomically as mode `0600`, or accepted only when it is already a
regular, current-user, mode-`0600`, single-link file. Existing files are never
chmodded. The held descriptor and no-follow pathname identity must agree before
and after SQLite reopens the file. These checks assume a POSIX filesystem with
`O_DIRECTORY` and `O_NOFOLLOW`; a process already running as the same uid is
outside this local boundary.

The dedicated schema is accepted only when its columns, types, null/default/PK
posture, canonical state CHECK source and behavior, non-unique pending-index
order, object set, application/user versions, and `quick_check` all agree.
Foreign keys, triggers, unknown indexes/tables, partial schemas, and unknown
versions fail without repair. Initialization and the sole exact pre-lease
schema migration are transactional and preserve rows; unknown databases are
never rebuilt or deleted.

Enqueue uses SQLite configured with `synchronous=FULL`, `fullfsync=ON`, and
`checkpoint_fullfsync=ON`, bounded canonical JSON, and a stable envelope digest.
Those read-back settings plus local `fsync` prove configuration readiness only;
they do not simulate sudden power loss or verify physical persistence.

A short committed lease claims work; network I/O occurs after releasing the
database lock, and finalization requires the exact lease. Crashed leases expire
and recover. The post-turn path drains a bounded row count within one cooperative
wall budget covering SQLite busy waits, claim, callback, and finalization, so
recovered backlog can shrink without a two-second lock tail. The delivery
callback executes synchronously on the caller thread, receives the remaining
`timeout_seconds`, and is required to honor it. No delivery thread or daemon is
created. Internal schema-lock acquisition is deadline-bounded as well as SQLite
busy waiting. The bundled client applies one monotonic deadline across connect,
TLS, write, and read operations; its deadline-bound turn URL accepts only an IP
literal or exact `localhost`, avoiding unbounded synchronous DNS. Explicit
recovery performs local preparation first, then uses that same bounded drain
contract. Remote timeouts retain an outcome-ambiguous lease and rely on the
exact idempotent turn `PUT` for safe retry. Stored error values are fixed
redacted codes. A failed enqueue or drain never withholds the already-safe reply.

## Required runtime latches

Registration initializes and validates the private outbox, then fails before
exposing any middleware, tool, hook, or command unless all three values are
exact:

```text
COLONY_GENERAL_PLUGIN_ACTIVE=1
COLONY_MEMORY_WORKER_TOOLS=0
COLONY_MEMORY_TURN_WRITER=disabled
```

These values make the general plugin the only Hermes turn writer and prevent
the memory provider's legacy model tools from coexisting.

## ResponseGuard

Pinned Hermes tag `v2026.7.7.2` at commit
`9de9c25f620ff7f1ce0fd5457d596052d5159596` invokes
`transform_llm_output(response_text, session_id, model, platform)` before
`post_llm_call`, and passes the transformed final response to the post hook.
Enforce mode validates policy identity, candidate digest, surface,
mode, applicability, decision, and status before releasing an allow verdict.
Any transport/protocol failure withholds the text. Shadow mode is asynchronous
and observational.

Hermes persistence occurs before/around host finalization differently across
versions, and Colony alone cannot prove correction of already-streamed tokens.
Enable enforce only on the stateless non-streaming `hermes -z -t` deployment
path until the deployment preflight proves session persistence and streaming
are disabled. This limitation is not included in the tool/context governance
`ready` claim.

## Legacy paths

Slash commands lack sufficient call attestation and return a disabled notice.
The initiative poller, queue worker, direct activity notifier, gateway restart
runner, and webhook example are inert. The installer overwrites the two old
poller script paths with inert targets while preserving backups, so surviving
scheduled invocations cannot perform effects. The old patch runner is read-only
and a clean deployment has no Hermes core patches.

## Rollback

Install without `--force` to back up the existing plugin directory. Existing
legacy poller scripts are copied to timestamped `.pre-governance.*` files before
their paths are made inert. Reverting the Colony repository and restoring those
backups is mechanically possible, but re-enabling direct legacy workers should
be treated as a deliberate governance rollback.

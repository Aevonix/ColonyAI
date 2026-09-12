# Governed PacoMind plugin for Hermes

This is PacoMind's generic sidecar integration through Hermes interfaces. It gives Hermes
transport-scoped PacoMind reads, mediator-only action intents, one exact turn
writer, and text ResponseGuard at the pinned `transform_llm_output` hook.

It intentionally does not provide a second memory/context engine, an event
subscriber, cron autonomy, direct initiative/queue workers, voice handling, or
deployment-specific identity. PacoMind memory is supplied by the separate
`pacomind-memory` provider; execution belongs to an authenticated action plane.

An optional [`pacomind_task` tool](../../docs/NATIVE-TASK-CHANNELS.md) starts
background work through the existing Hermes gateway while the conversation
continues. The same owner can inspect, steer or stop that task from another
authenticated channel. This adds an adapter and durable source associations;
it uses the native executor and keeps hardware and outward delivery in the
deployment layer.

## Configuration

```yaml
plugins:
  pacomind:
    url: http://127.0.0.1:7777
    api_key: ${PACOMIND_API_KEY}
    owner_contact_id: <deployment-owned-contact-id>
    attested_system_platforms: [cli]
    # Optional. Omit to register the complete current read catalog.
    # An explicit empty list gives a profile no PacoMind read tools.
    enabled_read_tools:
      - pacomind_list_commitments
      - pacomind_list_goals
    action_mediator_url: http://127.0.0.1:8785/v1/action-intents
    action_mediator_api_key: ${PACOMIND_ACTION_MEDIATOR_API_KEY}
    action_mediator_principal: hermes-pacomind-plugin
    # Advertise only capabilities the deployed mediator can execute exactly.
    enabled_action_tools:
      - pacomind_autonomy_disable
      - pacomind_autonomy_enable
      - pacomind_create_commitment
      - pacomind_resolve_commitment
    # Deployment example: a separate credential reaches only its message ingress.
    owner_message_mediator_url: http://127.0.0.1:18802/internal/owner-deliver
    owner_message_mediator_api_key: ${PACOMIND_OWNER_MESSAGE_MEDIATOR_API_KEY}
    owner_message_mediator_principal: hermes-owner-message
    enabled_message_tools: [pacomind_send_message]
    turn_outbox_path: ~/.hermes/state/pacomind-turn-outbox.sqlite3
    turn_outbox_drain_timeout_ms: 250
    turn_outbox_drain_limit: 16
```

Only genuinely host-attested local system lanes belong in
`attested_system_platforms`. Phone, SMS/RCS, WhatsApp, and other real channels
must carry a sender and resolve to an exact PacoMind contact.

Set the coexistence latches documented in [SPEC.md](SPEC.md), then enable both
the `pacomind` general plugin and canonical `pacomind-memory` provider. If the
mediator is omitted or lacks an allowed origin, resolved credential, or safe
principal, reads still work but no effect tools are registered. Effect tools
also require an explicit `enabled_action_tools` entry; the static catalog is an
upper bound, not a claim that a deployment executor supports every action.

`enabled_read_tools` is an optional deployment-profile boundary. Omitting it
registers the complete current read catalog. Supplying
it registers only that exact subset; `[]` registers no reads without disabling
a separately configured action or message tool. Lists and comma-separated
strings are accepted, but blank entries, duplicates after whitespace
normalization, unknown names, and other value shapes fail before registration.
The dispatcher independently enforces the same effective subset.

The mediator accepts `HermesToolActionIntentV1` and must return only the exact
versioned `HermesToolActionAdmissionV1` pending-admission projection. The
client's deterministic `intent_id`/`intent_digest` are not Action Plane IDs:
`action_id` and `action_digest` come from the server, and `approval_id` is
required. Redirects and URL query strings are rejected.

`pacomind_send_message` is a separate contact-message intent boundary. It is
available to an authenticated owner text turn and to an explicitly attested
local system turn, which lets PacoMind autonomy initiate and follow up without
inventing human authority. Guests and speech surfaces cannot call it. The
model selects an existing contact by display name, never a transport address,
principal, or contact ID. An optional `channel` is bounded to WhatsApp, RCS,
or SMS; omitting it emits the V1 WhatsApp request. An explicit channel produces
a versioned V2 request; the receiver must implement that contract. An attested system turn uses
V3 and carries a server-derived `attested_system` origin plus an explicit
channel (WhatsApp when omitted by the model), so autonomous outreach is never
misreported as an owner instruction.

The deployment's authenticated producer resolves exactly one non-owner PacoMind
contact with `authority=none`, `context_class=scoped_or_empty`, current
standing, one exact verified handle, and one active fixed route. No route is a
held result; this tool never falls into `proactive_new_target` or creates a
per-message owner prompt. Its credential must be distinct from the normal
PacoMind delivery ingress credential. That private bearer is the producer's
service identity; `owner_message_mediator_principal` is
local audit metadata and is not sent as an unverified identity header.

`post_llm_call` commits a bounded canonical envelope using SQLite configured as
`synchronous=FULL`, `fullfsync=ON`, and `checkpoint_fullfsync=ON` before
attempting network delivery. These settings and the explicit local `fsync`
establish configuration readiness; they are not a physical power-loss test, so
the attestation always reports `physical_power_loss_verified=false`.

The default hook drains up to 16 rows within one shared 250 ms cooperative
budget covering SQLite lock acquisition, claim, delivery, and finalization. It
never holds a database lock during HTTP. The delivery callback runs on the
calling thread, receives the remaining `timeout_seconds`, and must honor it; the
adapter starts no delivery daemon and leaves no local callback continuing after
return. The bundled `PacoMindClient` is stronger than that generic callback
contract: its connect, TLS, write, and read operations all consume one absolute
monotonic deadline rather than independent HTTP phase timeouts. To keep that
claim honest without a resolver thread, the deadline-bound turn-delivery URL
must use an IP literal or exact `localhost`; the documented loopback default
already satisfies this requirement. The exact turn ID is sent through an
idempotent `PUT`, so a remote timeout with an unknown outcome can be retried
after its lease expires without changing content.

Operators may also call the source-only
`recover_turn_outbox(config, deliver, limit=..., timeout_seconds=...)` API. It
performs local preparation before the drain budget begins, then applies the same
caller-thread callback contract and bounded drain. It performs no network I/O
except through the supplied deadline-aware delivery callback.

When source erasure removes only part of a newly captured turn, the original
receipt remains `erased`. Its `survivor_turn_id` and `survivor_state` identify
the separate durable replacement containing only retained evidence. A pending
survivor triggers the same bounded post-turn or checkpoint drain immediately;
it does not require another user message. Failed delivery remains pending for
the existing recovery path. Complete erasure creates no survivor or delivery.

The outbox has an intentionally small POSIX trust contract. Its absolute path
may not contain symlink components; the immediate directory must be owned by
the current effective user with exact mode `0700`; and an existing database
must already be a regular, current-user, single-link file with exact mode
`0600`. Registration creates a missing directory/database safely, but never
chmods an existing file or follows an alias. A normal one-time setup is:

```bash
install -d -m 0700 "$HOME/.hermes/state"
```

If a database has a different mode, first verify that it is not a
symlink, is a regular file owned by the current user, and has link count one;
only then correct it manually. A rejected file is left unchanged.

The dedicated database is schema-attested before registration: exact columns,
CHECK source/behavior, non-unique pending-index order, absence of foreign keys
and triggers, SQLite application/user versions, and `quick_check` must all
agree. New empty ledgers and the one exact pre-lease predecessor are initialized
transactionally. Unknown or partially migrated databases are rejected without
being rebuilt or deleted.

## Installation and verification

```bash
./install.sh --memory
```

The launcher runs guided setup against the selected Hermes home.
Managed refresh validates the current attachment and preserves private state.
`--force` does not bypass those checks. Use the
[current setup guide](../../docs/LOCAL-HERMES-SETUP.md) for package installation,
adapter refresh and recovery.

Run the focused governance suite with:

```bash
PYTHONPATH=sidecar python -m pytest -q \
  sidecar/tests/test_hermes_general_governance.py \
  sidecar/tests/test_hermes_plugin_contract.py
```

The exported `governance_attestation()` is import-pure and reports source
catalog readiness only; it always reports runtime/live readiness false. The
separate `runtime_governance_attestation(config)` initializes and verifies the
private outbox's SQLite/filesystem configuration plus the exact mediator origin,
resolved credential, principal, and nonempty enabled-action subset without
making a network call. It also reports the effective normalized read subset,
its digest, and whether it came from the default or an explicit
configuration. Deployment preflight must require the runtime schema and
must not map source readiness to a live claim. The local runtime proof also
always keeps `live_ready=false`; only a separate deployment-owned network/canary
probe may claim operational liveness. `turn_outbox_configuration_ready=true` is
usable local configuration readiness; it never changes the separate
`physical_power_loss_verified=false` truth field.

`install.sh` forwards to `python -m pacomind init`; install the PacoMind CLI first. The guided installer checks the selected Hermes interpreter and profile before changing the attachment.

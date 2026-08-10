# Colony memory provider for Hermes

This provider mounts Colony as a context sidecar without changing Hermes core.
It binds every real-channel turn to the transport sender resolved by Colony and
keeps the host's custom voice/phone/intercom path independent.

## Configuration

```yaml
memory:
  provider: colony
  config:
    url: http://127.0.0.1:7777
    api_key: ${COLONY_API_KEY}
    contact_id: replace-with-exact-owner-contact-id
    turn_writer: auto # auto | enabled | disabled
```

Required environment posture:

```bash
COLONY_PREFETCH_QUERY_CHECK=1
COLONY_PREFETCH_TURN_CONTACT=1
COLONY_MCP_CONTACT_ID=replace-with-exact-owner-contact-id
COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY=owner_system
```

The two prefetch flags are mandatory; explicitly disabling either prevents the
provider from starting. `owner_system` permits fallback only when there is no
real channel sender/chat and the lane is CLI, internal, system, owner, API,
worker, or cron. RCS, SMS, WhatsApp, and other real channels never use the
provider-wide default.

## Context privacy contract

For a guest turn the provider:

1. resolves the exact sender contact;
2. calls `GET /v1/host/context/projection-readiness`;
3. requires a server-attested viewer matching that contact, P8 `shadow`/`live`
   scoped projection readiness, and `legacy_global_allowed=false`;
4. sends `projection_policy=scoped_viewer_required` with the assembly request;
5. verifies the same attestation on the response.

Failure, timeout, malformed posture, P8-off, or a viewer mismatch yields no
Colony content. Guest time is local-clock-only. The old quote-based reply
timeline lookup is disabled until a transport-attested scoped reply endpoint
exists. Direct legacy read-tool endpoints are owner/system-only; guests use the
scoped assembled context instead.

The server returns 503 before any legacy-global producer runs when a scoped
guest requests context without P8. Exact scoped owner and temporary legacy
migration credentials retain explicit compatibility carve-outs. The currently
wired deployment canary is `COLONY_RECIPIENT_SIMULATOR_MODE=shadow`; `live` is
reserved by the protocol but is not wired by the present shared integration.

## General-plugin coexistence

With `COLONY_GENERAL_PLUGIN_ACTIVE=1`, this provider is read/context-only. Its
model-visible catalog is exactly:

- `colony_check_commitments`
- `colony_get_affect`
- `colony_get_facts`
- `colony_timeline`

Person selectors are removed from the schemas. Direct calls are available only
for the explicitly configured owner/system lane until scoped versions of those
tool endpoints exist. Queue tools, approvals, memory writes, memory mirroring,
and pre-compression signal writes remain disabled. Standalone installs may use
the fallback turn writer; it still requires an exact per-turn participant.

`catalog_attestation()` is the machine-readable admission contract. A true
`provider_governance_ready` covers this provider's privacy/prompt boundary.
`general_plugin_governance_ready` deliberately remains false until the separate
Hermes-session-governance slice removes global proactive injection, shared
fallback, dropped handler context, and startup LLM mutation.

## Cache and outage behavior

Prefetch cache entries bind query, effective session, platform, sender, chat,
and resolved contact and are consumed once. Concurrent senders cannot consume
one another's entry. Positive sender resolution is TTL-cached; one failed
sender resolution is negatively cached for the current turn (maximum five
seconds) and is retried on the next `on_turn_start`, preventing one outage from
blocking the same turn several times.

Install with `../hermes-plugin/install.sh --memory` or
`colony init --agent-harness hermes`. No Hermes core patch is required.

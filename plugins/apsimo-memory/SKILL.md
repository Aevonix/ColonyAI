---
name: apsimo
version: 1.3.0
description: Apsimo context sidecar for Hermes with exact per-turn participant binding, scoped P8 guest projection, and read-only general-plugin coexistence.
author: Aevonix
---

# Apsimo memory provider

Use Apsimo as Hermes' intelligence/context sidecar without patching Hermes
core. This integration does not own or replace the host's custom voice, phone, or
intercom turn path.

## Required posture

```bash
APSIMO_PREFETCH_QUERY_CHECK=1
APSIMO_PREFETCH_TURN_CONTACT=1
APSIMO_MCP_CONTACT_ID=replace-with-exact-owner-contact-id
APSIMO_MEMORY_DEFAULT_CONTEXT_AUTHORITY=none
```

Bind owner CLI context through the general adapter's explicit
`attested_system_platforms: [cli]` configuration. Every real channel resolves
its sender independently; a miss yields no Apsimo context or write. Existing
`COLONY_*` environment names remain supported.

Guest context requires a `context:read` scoped channel principal, exact
server-resolved contact grant, and P8 scoped projection. The provider preflights
`/v1/host/context/projection-readiness`, sends
`projection_policy=scoped_viewer_required`, and verifies the response viewer.
If any step fails, use no Apsimo context. Never substitute owner context.

## Model tools

When the general Apsimo plugin is active, the only model-visible provider tools
are:

- `colony_check_commitments`
- `colony_get_affect`
- `colony_get_facts`
- `colony_timeline`

Their person selectors are server/provider-bound, not model arguments. Direct
legacy tool endpoints are owner/system-only until scoped P8 tool projections
exist; guest conversations rely on atomic assembled context. Do not advertise
memory writes, goals, search, queue mutation, or approval tools from this
provider.

## Lifecycle

In general-plugin coexistence, turn sync, built-in memory mirroring, and
pre-compression signal writes are disabled because the general plugin owns
ingestion. Standalone fallback writing is allowed only after resolving the
exact current participant. Reply-window lookup against the global timeline is
disabled pending a transport-attested scoped endpoint.

Use `catalog_attestation()` for deployment admission. A true
`provider_governance_ready` does not imply the broader general plugin is ready;
`general_plugin_governance_ready` remains false until its named follow-on slice
is complete.

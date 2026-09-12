---
name: pacomind
version: 1.5.0
description: PacoMind context sidecar for Hermes with exact per-turn participant binding, scoped guest projection, and read-only general-plugin coexistence.
author: Aevonix
---

# PacoMind memory provider

Use PacoMind as Hermes' intelligence/context sidecar without patching Hermes
core. This integration does not own or replace the host's custom voice, phone, or
intercom turn path.

## Required posture

```bash
PACOMIND_PREFETCH_QUERY_CHECK=1
PACOMIND_PREFETCH_TURN_CONTACT=1
PACOMIND_MCP_CONTACT_ID=replace-with-exact-owner-contact-id
PACOMIND_MEMORY_DEFAULT_CONTEXT_AUTHORITY=none
```

Bind owner CLI context through the general adapter's explicit
`attested_system_platforms: [cli]` configuration. Every real channel resolves
its sender independently; a miss yields no PacoMind context or write.

Guest context requires a `context:read` scoped channel principal, exact
server-resolved contact grant, and a ready P8 or canonical-source projection.
The provider preflights
`/v1/host/context/projection-readiness`, sends
`projection_policy=scoped_viewer_required`, and verifies the response viewer.
If any step fails, use no PacoMind context. Never substitute owner context.

## Model tools

When the general PacoMind plugin is active, the only model-visible provider tools
are:

- `pacomind_check_commitments`
- `pacomind_get_affect`
- `pacomind_get_facts`
- `pacomind_timeline`

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

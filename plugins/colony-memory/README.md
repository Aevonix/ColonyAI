# Colony Memory Provider for Hermes

Implements Hermes's `MemoryProvider` ABC to inject Colony's cognitive context
(commitments, affect, facts, patterns, world model) into Hermes conversations
and sync turns back for extraction.

This directory is the SINGLE canonical copy of the provider (the former
`plugins/hermes-memory/` and `plugins/hermes-plugin/memory_provider/` copies
were consolidated into it in v0.22.0). It carries both the reply thread-window
context injection and the `colony_resolve_commitment` tool, plus the
`pre_llm_call` contact-resolution/current-time hook in `__init__.py`.

## Installation

```bash
../hermes-plugin/install.sh --memory
```

Or `colony init --agent-harness hermes`, or manually copy this directory to
`~/.hermes/plugins/colony-memory/`.

## Configuration

Set in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: colony
  config:
    url: http://127.0.0.1:7777
    api_key: ${COLONY_API_KEY}
    contact_id: default
```

Or via environment variables:
- `COLONY_URL` — sidecar URL
- `COLONY_API_KEY` — API key
- `COLONY_MCP_CONTACT_ID` — default contact ID

## Features

- **Prefetch**: Injects Colony context into every turn
- **Turn sync**: Sends completed turns to Colony for extraction (background thread)
- **Tool proxy**: Exposes Colony tools to the LLM
- **Circuit breaker**: Opens after 3 connection failures, closes after 60s
- **Retry**: 3 attempts with 0.5s backoff for connection errors
- **Diagnostics**: `get_diagnostics()` returns health state
- **Temporal awareness**: System prompt instructs agent to prefer host time over stored timestamps

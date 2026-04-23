---
name: colony
version: 0.1.0
description: Colony cognitive memory provider for Hermes. Injects commitments, affect, facts, patterns, and world model into Hermes conversations and syncs turns back for extraction.
author: Aevonix
---

# Colony Memory Provider

Mounts Colony's cognitive infrastructure as a Hermes memory provider.

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"
```

## Requirements

- Colony sidecar running at the configured URL
- `httpx` Python package

## What It Does

**prefetch()** — Before each turn, calls Colony's `/v1/host/context/assemble` and injects the returned sections (commitments, affect state, shared facts, patterns, surprises, world model entities) as a `<memory-context>` block.

**sync_turn()** — After each turn, POSTs the user message and assistant response to Colony's `/v1/host/turns/sync` for extraction of commitments, affect, and facts.

**system_prompt_block()** — Adds a brief note that Colony cognitive infrastructure is active.

If the sidecar is unreachable, all calls degrade silently. No errors are raised for transient failures.

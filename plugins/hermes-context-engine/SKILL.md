---
name: colony-context-engine
version: 0.1.0
description: Colony cognitive context engine for Hermes. Injects commitments, affect, facts, patterns, and world model into Hermes's prompt via Colony's sidecar.
author: Aevonix
---

# Colony Context Engine

Mounts Colony's cognitive context into Hermes's prompt as an ephemeral layer.

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
context_engine:
  plugin: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"
```

## Requirements

- Colony sidecar running at the configured URL
- `httpx` Python package

## What It Does

On each prompt assembly, calls Colony's `/v1/host/context/assemble` endpoint
and injects the returned sections (commitments, affect state, shared facts,
patterns, surprises, world model entities) into Hermes's prompt as an
ephemeral context layer.

If the sidecar is unreachable, the engine returns None and Hermes proceeds
without Colony context. No errors are raised for transient failures.

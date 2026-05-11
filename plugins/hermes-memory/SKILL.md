---
name: colony
version: 0.2.0
description: Colony cognitive memory provider for Hermes. Injects commitments, affect, facts, patterns, and world model into Hermes conversations and syncs turns back for extraction. Exposes Colony tools (colony_check_commitments, colony_list_goals, colony_write_memory, etc.) and lifecycle hooks for session rotation, memory mirroring, and pre-compression.
author: Aevonix
---

# Colony Hermes Integration Suite

Mounts Colony's cognitive infrastructure as three Hermes plugins:

1. **Memory Provider** (`~/.hermes/plugins/memory/colony/`)
2. **Context Engine** (`~/.hermes/plugins/context_engine/colony/`)
3. **General Plugin** (`~/.hermes/plugins/colony/`)

## Quick Start

```bash
cd ColonyAI/plugins/hermes-memory
./install.sh
```

Then add to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"

context_engine: colony

plugins:
  colony:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"
```

Restart Hermes.

## Memory Provider

### prefetch()
Before each turn, calls Colony's `/v1/host/context/assemble` and injects the returned sections (commitments, affect state, shared facts, patterns, surprises, world model entities) as a `<memory-context>` block.

### sync_turn()
After each turn, POSTs the user message and assistant response to Colony's `/v1/host/turns/sync` for extraction of commitments, affect, and facts. **Non-blocking** (daemon thread per Hermes threading contract).

### get_tool_schemas() / handle_tool_call()
Exposes Colony tools to the LLM:

- `colony_check_commitments` — Check active commitments for the current contact
- `colony_get_affect` — Get current affect state (valence/arousal)
- `colony_get_facts` — Retrieve shared facts about a contact
- `colony_get_patterns` — Get detected behavioral patterns
- `colony_write_memory` — Persist a fact/insight to Colony
- `colony_list_goals` — List user goals with status and progress
- `colony_record_affect` — Record an emotional state event
- `colony_search_memory` — Search Colony's memory graph

### Lifecycle Hooks

- `on_session_switch()` — Handles session rotation, clears cache on reset
- `on_turn_start()` — Called at the start of each turn
- `on_memory_write()` — Mirrors built-in memory writes back to Colony
- `on_pre_compress()` — Fires a signal ingest before context compression discards old messages
- `on_session_end()` — Flushes pending context and fires a best-effort final sync

### Config Schema
Implements `get_config_schema()` and `save_config()` for the `hermes memory setup` wizard.

## Context Engine

Replaces Hermes's built-in compressor with Colony-aware summarization:

- `should_compress()` — Checks token count against threshold
- `compress()` — Calls Colony's reasoning loop to produce a cognitive summary, preserving commitments and facts while discarding noise. Falls back to local compression if Colony is unreachable.

## General Plugin

### Native Tools
Registers additional Colony tools via `ctx.register_tool()`:

- `colony_memory_search`
- `colony_list_goals`
- `colony_get_briefing`
- `colony_record_insight`
- `colony_query_entities`
- `colony_task_complete`
- `colony_task_snooze`
- `colony_task_dismiss`
- `colony_initiative_feedback`

### WebSocket Event Subscriber
Connects to Colony's `/v1/host/events` WebSocket, caches the most recent events per type, and injects them via the `pre_llm_call` hook.

### Slash Commands
- `/colony status` — Sidecar health + capabilities
- `/colony goals` — Active goals list
- `/colony context` — Fetch cognitive context
- `/colony events` — Recent cached events
- `/colony sync` — Force a turn sync

### CLI Commands
- `hermes colony status`
- `hermes colony goals`
- `hermes colony context`
- `hermes colony sync`

## Requirements

- Colony sidecar running at the configured URL
- `httpx` Python package
- `websockets` Python package (general plugin only)

## What It Does

If the sidecar is unreachable, all calls degrade silently. No errors are raised for transient failures.

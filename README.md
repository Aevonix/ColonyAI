<div align="center">

# Colony

A persistent memory and cognition layer for AI agents.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/colonyai.svg)](https://pypi.org/project/colonyai/)
[![CI](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml/badge.svg)](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Aevonix/ColonyAI)](https://github.com/Aevonix/ColonyAI/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/colonyai/)

</div>

## Overview

Colony is a sidecar process that gives an AI agent durable memory and a model of
the people it talks to. It runs alongside the agent's host (a chat gateway, a
coding tool, or any harness that can call an HTTP API), assembles relevant
context before each turn, and learns from each turn afterward.

The agent stays stateless; Colony holds the state. Memories, facts, commitments,
relationships, and time all persist across sessions and outlive any single
conversation.

Colony is not an agent and does not generate user-facing replies. It is the
layer underneath one.

## What it does

- **Long-term memory.** Stores and recalls memories with semantic search,
  recency weighting, and content-hash deduplication. Backed by a graph
  (Neo4j) for entities and relationships and a vector index (LanceDB) for recall.
- **Temporal awareness.** Tracks an authoritative current time per agent and a
  timezone per contact, and journals every turn onto a unified timeline so the
  agent can reason about when things happened and what is overdue.
- **Contacts and relationships.** Maintains a contact per person with one or more
  channel handles (WhatsApp, email, SMS, …), a trust tier, and a relationship
  score derived from interaction recency, frequency, and sentiment.
- **Theory of mind.** Extracts affect (emotional valence and arousal) and shared
  facts from conversations, and builds an evolving engagement profile per
  contact — Big Five traits plus communication style — that it surfaces as
  concrete guidance on how to engage with that person.
- **Commitments and goals.** Records what the agent and its owner have agreed to
  and tracks goals and their progress.
- **Communication governance.** Keeps a cross-channel record of every exchange
  and decides whether, how, and when to reach out — with a cooldown to avoid
  over-messaging and an owner-approval gate for proactive outreach.
- **Autonomy.** An optional background loop generates proactive initiatives
  (follow-ups, research, relationship check-ins) on a schedule.

## Architecture

```
┌──────────────────────────┐     HTTP / WebSocket      ┌─────────────────────────┐
│  Host harness (the agent) │  ───────────────────────► │  Colony sidecar          │
│  - assembles a turn       │  ◄─────────────────────── │  127.0.0.1:7777 (FastAPI)│
│  - calls a host plugin    │     context in / turn out │                          │
└──────────────────────────┘                            │   ┌──────────────────┐  │
                                                         │   │ Neo4j   (graph)  │  │
                                                         │   │ LanceDB (vectors)│  │
                                                         │   │ SQLite  (records)│  │
                                                         │   └──────────────────┘  │
                                                         └─────────────────────────┘
```

The sidecar exposes one HTTP API. A thin, host-specific plugin connects the
agent to it: it requests assembled context before each turn and syncs the turn
back afterward for extraction. The same stores are readable and writable by any
connected host, so memory is shared rather than per-tool.

| Component | Role |
| --- | --- |
| Sidecar (`colony_sidecar`) | FastAPI service; the only thing the host talks to |
| Graph store (Neo4j) | Entities, people, memories, and their relationships |
| Vector store (LanceDB) | Embeddings for semantic recall |
| Record stores (SQLite) | Contacts, commitments, goals, affect, facts, engagement, communications |
| Embeddings | Any OpenAI-compatible embedding endpoint (configurable) |
| LLM | Any OpenAI-compatible chat endpoint, used for extraction and reasoning |

## Integrations

### Hermes (primary)

The actively maintained integration. Colony ships three Hermes plugins:

- **Memory provider** — injects assembled context before each turn and syncs the
  turn back.
- **Context engine** — manages the conversation window and compression.
- **General plugin** — exposes Colony tools, lifecycle hooks (contact resolution,
  time injection, turn journaling, behavioral-signal capture), and an optional
  autonomy bridge.

Host-side operational tooling lives under `plugins/hermes-plugin/ops/`: a
self-validating doctor, a resilient gateway-restart runner, a pre-restart
summary, and an activity monitor. The setup wizard installs and validates all of
it. See [`plugins/hermes-plugin/ops/README.md`](plugins/hermes-plugin/ops/README.md).

### Coding tools via MCP

The sidecar exposes an MCP server so coding tools (Claude Code, Codex, and other
MCP clients) can read and write the same stores — check commitments, look up
facts, search the world model, and record knowledge — over the standard
protocol.

### OpenClaw (experimental)

A TypeScript plugin (`src/plugin.ts`) integrates Colony with OpenClaw. It is
currently **experimental**: its test suite mocks the OpenClaw SDK rather than
exercising it, so integration drift is not caught in CI. Use the Hermes or MCP
paths for production. See [the OpenClaw status note](docs/HARNESS_INTEGRATION.md).

## Quick start

Requirements: Python 3.11+, Neo4j 5.x reachable over Bolt, and an
OpenAI-compatible LLM and embedding endpoint.

```bash
pip install colonyai

# Initialize identity, configure the sidecar (state dir, Neo4j, API key, model
# endpoints), and wire any detected agent harnesses
colony init

# Start the sidecar
colony start -d             # daemon; omit -d to run in the foreground

# Connect coding tools over MCP (optional)
colony mcp setup
```

Verify it is up:

```bash
curl -s -H "Authorization: Bearer $COLONY_API_KEY" \
  http://127.0.0.1:7777/v1/host/health
```

A `docker-compose.yml` is provided to run the sidecar and Neo4j together.

## Configuration

Configuration is read from the state directory (default `~/.colony`) and
environment variables. The common ones:

| Variable | Purpose |
| --- | --- |
| `COLONY_STATE_DIR` | Where stores and config live (default `~/.colony`) |
| `COLONY_API_KEY` | Bearer token required by the sidecar API |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | Graph store connection |
| `COLONY_AGENT_TIMEZONE` | The agent's authoritative timezone |
| `COLONY_OWNER_CONTACT_ID` | The owner's contact, used by the approval gate |

The host's LLM and embedding endpoints are pushed to the sidecar at runtime via
`POST /v1/host/configure` and persisted, so the sidecar uses the same models as
the agent.

## Operations

`colony doctor` diagnoses sidecar configuration and runtime health:

```bash
colony doctor
colony status        # health and pipeline state
```

The Hermes integration adds a second doctor (`plugins/hermes-plugin/ops/`) that
validates the host-side plugins, configuration, and scheduled jobs, and re-runs
when the Hermes version changes. Both exit non-zero on failure and are suitable
for scheduling.

## Status

The memory, temporal, contacts, relationship, theory-of-mind, engagement,
commitments, goals, and communication-governance subsystems are in active use.

Several larger subsystems are present but not yet wired into a default runtime
path and should be treated as experimental: the multi-agent / distributed
identity chain, the research pipeline, and parts of the learning and skills
machinery. They are guarded behind feature flags or simply not invoked, so they
do not affect the production path.

## Development

```bash
git clone https://github.com/Aevonix/ColonyAI.git
cd ColonyAI/sidecar
pip install -e ".[dev]"
python -m pytest
```

The Python sidecar lives under `sidecar/`; the host plugins under `plugins/`;
the OpenClaw TypeScript plugin under `src/`.

## License

MIT. See [LICENSE](LICENSE).

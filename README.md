<div align="center">

# Colony

Persistent memory and cognitive infrastructure for AI agents and coding tools. One intelligence layer, many frontends.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![PyPI](https://img.shields.io/pypi/v/colonyai.svg)](https://pypi.org/project/colonyai/) [![npm](https://img.shields.io/npm/v/@aevonix/colonyai.svg)](https://www.npmjs.com/package/@aevonix/colonyai) [![Docker](https://img.shields.io/badge/docker-ghcr.io%2Faevonix%2Fcolony-blue)](https://github.com/Aevonix/ColonyAI/pkgs/container/colony) [![CI](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml/badge.svg)](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml) [![GitHub Release](https://img.shields.io/github/v/release/Aevonix/ColonyAI)](https://github.com/Aevonix/ColonyAI/releases) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/colonyai/) [![TypeScript](https://img.shields.io/badge/typescript-5.x-3178c6.svg)](https://www.typescriptlang.org/) [![Node.js](https://img.shields.io/badge/node-20%2B-339933.svg)](https://nodejs.org/)

</div>

## What Is Colony

Colony gives your agents and coding tools a shared, persistent intelligence layer. Commitments, affect state, world knowledge, patterns, and facts that outlive any session and flow across every tool you use.

When your agent promises to do something in a chat, your coding tool sees that commitment. When a coding session extracts a new fact about your architecture, your agent gets it injected into context. One memory, many frontends.

Colony is not another agent. It is infrastructure. A sidecar process with a unified API and an MCP server that any harness can plug into.

**For OpenClaw:** Colony mounts as a plugin. The sidecar runs alongside the gateway, communicating via HTTP/WebSocket. Context assembly, commitment tracking, affect state, and all 36 subsystems are available as part of every conversation turn.

**For Hermes:** Colony ships a MemoryProvider plugin that injects cognitive context before each turn and syncs turns back for extraction. An initiative poller enables Colony to run autonomously — generating tasks, research, and relationship actions on your behalf. Hermes also connects via MCP for direct tool access.

**For coding harnesses (Claude Code, Codex, Crush, OpenCode):** Colony exposes an MCP server with 14 tools, 4+ resources, and 3 prompts. Your coding tools can check commitments, look up facts, record affect, search the world model, and write back new knowledge, all through the standard MCP protocol.

Both paths read and write to the same stores. Any harness can observe or contribute to the same commitments, facts, and world model.

The current release delivers 36 production subsystems across 57+ API endpoints. Across future releases, Colonies will network into super-agents across your hardware, federate to share knowledge and compute, and ultimately form a SuperColony: personal agent clusters that share resources on a global substrate. The architecture is stigmergic by design. The same pattern that makes ant colonies collectively intelligent without a central controller.

-----

## Quick Start

### With OpenClaw

```bash
pip install colonyai
colony init
```

That one command handles dependencies, Neo4j, hardware scan, model download, plugin config, sidecar start, health verify, and doctor check.

### With Hermes

```bash
pip install colonyai
colony init              # Choose Hermes as host framework
pip install pyyaml        # Required for Hermes YAML config
colony mcp setup          # Writes MCP config to ~/.hermes/config.yaml
```

Deploy the Hermes plugin stack:

```bash
cd plugins/hermes-plugin
./install.sh --memory --poller
```

This installs:
- **Memory provider** to `~/.hermes/plugins/colony-memory/` — injects Colony context before each turn, syncs turns back for extraction
- **Initiative poller** to `~/.hermes/scripts/` — polls Colony for autonomous initiatives and fires them to the Hermes webhook

Then add to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: colony
  config:
    url: "http://127.0.0.1:7777"
    api_key: "${COLONY_API_KEY}"
    contact_id: "default"
```

Schedule the poller via Hermes cron:

```bash
hermes cron create \
    --name colony-initiative-poller \
    --schedule "every 1m" \
    --script colony-initiative-poller.py \
    --no-agent
```

### With Claude Code, Codex, Crush, or OpenCode

```bash
pip install colonyai
colony init          # Choose your harness during setup
colony start -d      # Start sidecar as daemon
colony mcp setup     # Auto-detect and configure connected harnesses
```

The MCP server exposes 14 tools, 4+ resources, and 3 prompts to any connected coding harness. Claude Code and OpenCode get them automatically. Codex and Crush get them through their MCP integration.

### Multi-harness setup

Colony supports running multiple harnesses simultaneously. Any combination works. They share the same memory, commitments, and world model.

```bash
colony init              # Choose all harnesses that apply
colony mcp setup         # Configure each one selectively
colony mcp detect        # See which harnesses are installed
```

### Docker Compose

```bash
git clone https://github.com/Aevonix/ColonyAI.git
cd ColonyAI
cp .env.example .env     # Edit NEO4J_PASSWORD and COLONY_API_KEY
docker compose up -d     # Neo4j + Colony sidecar
```

### Verify

```bash
colony service status      # Sidecar service status (launchd)
colony doctor             # Full subsystem check (34 checks)
colony validate           # 5-step pipeline test, writes validation stamp
```

**Prerequisites:** Python 3.11+, Docker (auto-installed by `colony init` if missing). For OpenClaw: an LLM key configured. For Hermes: PyYAML installed. For coding harnesses: the harness installed locally.

-----

## Temporal Awareness, Sync Health & Auto-Restart (v0.8.x)

Colony v0.8.x introduces production-grade resilience for the autonomy pipeline. The sidecar now tracks its own temporal health, recovers automatically from crashes, and surfaces diagnostics when things go wrong.

### Sidecar Telemetry

The sidecar maintains a `TelemetryStore` that tracks:
- `started_at` — when the sidecar started
- `last_sync_at` — last turn sync received
- `last_tick_at` — last autonomy loop tick
- `last_initiative_at` — last initiative created

The health endpoint (`/v1/host/health`) returns these as `temporal` metrics, plus `silence_hours` and `stale_flags`. Status degrades to `degraded` when thresholds are exceeded:

| Threshold | Env Var | Default |
|-----------|---------|---------|
| Sync staleness | `COLONY_STALE_SYNC_HOURS` | 2.0 hours |
| Tick staleness | `COLONY_STALE_TICK_HOURS` | 24.0 hours |
| Initiative staleness | `COLONY_STALE_INITIATIVE_HOURS` | 48.0 hours |

### Auto-Restart via launchd

On macOS, Colony can run as a launchd service:

```bash
colony service install     # Generate plist and load service
colony service start       # Start the service
colony service stop        # Stop the service
colony service restart     # Restart (unload/load, not stop/start)
colony service status      # Check service + sidecar health
colony service uninstall   # Remove plist and unload
```

The plist uses `KeepAlive` and `RunAtLoad` for automatic restart on crash and boot. Runtime environment variables are loaded from `~/.colony/.env` — no credentials are frozen in the plist.

### Initiative Poller Resilience

The Hermes initiative poller (run every 60s) now:
1. **Health preflight** — checks sidecar health before fetching initiatives
2. **Auto wake-up** — on connection failure, sends `launchctl start` to restart the sidecar
3. **State tracking** — `~/.hermes/.colony_wake_up_flag` prevents infinite wake-up loops
4. **Alert routing** — if wake-up fails twice, fires an `"alert"` to the log channel only (never DMs)
5. **Deduplication** — skips initiatives by `dedup_key` to prevent spam

### Provider Resilience

The Hermes memory provider now includes:
- **Circuit breaker** — opens after 3 `ConnectError`/`OSError` failures, closes after 60s
- **Retry with backoff** — 3 attempts, 0.5s delay (connection errors only; HTTP 4xx/5xx don't count)
- **WARNING-level logging** — connection failures logged at WARNING (was DEBUG)
- **Diagnostics** — `get_diagnostics()` returns circuit state, failure count, last sync attempt/error

### Temporal Awareness for Agents

The provider's system prompt block instructs the LLM to prefer host-provided current time over stored timestamps. This prevents fabricated narratives from stale data — when Colony says "last talked 3 days ago," the agent knows to verify against the actual current time.

-----

## Multi-Agent (v0.7.0)

Colony v0.7.0 introduces **multi-agent support**, enabling multiple OpenClaw instances, coding agents, and AI systems to connect to a central Colony and share unified context.

### Why Multi-Agent?

- **Unified context across devices** — Your laptop, desktop, and servers all see the same facts and goals
- **Work distribution** — Colony assigns tasks to the best available agent based on capabilities and load
- **Failover support** — If an agent goes offline, work is reassigned to another available agent
- **Remote access** — Connect from anywhere via WebSocket, even behind NAT

### Quick Start

**On your Colony host:**
```bash
colony agent invite --capabilities messaging,calendar --primary
```

**On your remote machine:**
```bash
colony agent connect --setup-code COLONY-XXXX-XXXX-XXXX --colony-url https://your-colony.com
```

That's it! The OpenClaw plugin automatically detects the connection and routes initiatives via WebSocket.

### Key Features

| Feature | Description |
|---------|-------------|
| Agent Registry | Track all connected agents, their capabilities and status |
| Initiative Engine | Generate work items from goals, contacts, and schedules |
| Assignment Engine | Match initiatives to best available agent |
| WebSocket Delivery | Real-time push to remote agents, works behind NAT |
| Load Balancing | Distribute work based on agent capacity and priority |
| Failover | Reassign work when agents go offline |

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Colony (Central)                         │
│                                                                  │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │ Agent Store │  │ Initiative   │  │ Assignment Engine      │  │
│  │             │  │ Store        │  │                        │  │
│  └─────────────┘  └──────────────┘  └────────────────────────┘  │
│                              ▲                                   │
│              ┌───────────────┼───────────────┐                  │
│              │               │               │                   │
│         ┌────┴────┐    ┌────┴────┐    ┌────┴────┐              │
│         │ Laptop  │    │ Desktop │    │ Server  │              │
│         │ OpenClaw│    │ Hermes  │    │ Crush   │              │
│         └─────────┘    └─────────┘    └─────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

### Documentation

See [docs/MULTI_AGENT.md](docs/MULTI_AGENT.md) for:
- API reference
- CLI commands
- Agent SDK documentation
- Security model
- Troubleshooting

-----

## Table of Contents

- [What Is Colony](#what-is-colony)
- [Quick Start](#quick-start)
- [Temporal Awareness, Sync Health & Auto-Restart](#temporal-awareness-sync-health--auto-restart-v08x)
- [Multi-Agent (v0.7.0)](#multi-agent-v070)
- [Why Colony](#why-colony)
- [36 Wired Subsystems](#36-wired-subsystems)
- [MCP Server](#mcp-server)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [API Reference](#api-reference)
- [What Colony Is Not](#what-colony-is-not)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

-----

## Why Colony

Most agent memory is session-scoped. When the conversation ends, the context is gone. When you switch tools, you start over. Colony fixes that.

**Persistent.** Commitments, affect state, facts, patterns, and world model outlive every session. What Colony learns on Monday is available on Friday.

**Cross-tool.** Every connected harness reads and writes to the same stores. A commitment created in your chat agent shows up in your coding tool. A fact extracted during a coding session feeds back into your agent's context.

**Harness-agnostic.** Colony doesn't care what runtime you use. OpenClaw, Hermes, Claude Code, Codex, Crush, OpenCode, or anything else that speaks HTTP or MCP. Plug in whatever you want.

**Self-contained.** Colony runs as a sidecar, not inside any host. It has its own API, its own storage, its own lifecycle. No host lock-in.

**Degrades gracefully.** An unwired subsystem returns empty results instead of errors. Use what you need, skip what you don't.

**Autonomous.** Colony doesn't just store memory — it acts on it. The autonomy loop detects stale commitments, overdue tasks, relationship gaps, and surprise events, then generates initiatives for your agent to execute.

-----

## 36 Wired Subsystems

Everything below works now.

### Core

| Subsystem | Purpose |
|---|---|
| Context Assembly | Parallel query across subsystems to build LLM context with priority-ranked sections |
| Consolidate | Memory deduplication and merge for near-duplicate graph entries |
| Memory | Neo4j-backed graph storage for conversations, entities, relationships, insights |
| Response Gate | 7-layer response inspection (recipient verification, PII scanning, cross-context isolation, trust tiers, injection detection, secondary review, send delay) |
| Signals | Behavioral signal ingestion for profiling and pattern detection |
| Embeddings | Auto-tier-detected embedding pipeline (text + multimodal) |
| Reasoning | Bounded LLM iteration loop with tool calling |
| Skills | Tool registry with metadata |
| Identity | Ed25519 cryptographic identity with Colony + Node layers, Genesis trust anchor, backup/restore |
| Secrets | Encrypted vault for sensitive configuration |
| Sessions | Isolated session management |
| Telemetry | Temporal health tracking with stale detection and auto-restart |

### Goals and Planning

| Subsystem | Purpose |
|---|---|
| Goals | DAG-based goal decomposition and tracking |
| Commitment Tracking | LLM-extracted commitments with status transitions, overdue detection, and cognition triggers |
| Research | Background research pipeline with configurable depth |
| Initiative Engine | Generate autonomous work items from goals, contacts, schedules, and surprise events |

### Relationships

| Subsystem | Purpose |
|---|---|
| Contacts | Relationship store with trust tiers and interaction history |
| Briefings | Proactive relationship summaries and conversation starters |
| Delivery | Proactive message delivery bridge |

### World Model

| Subsystem | Purpose |
|---|---|
| World Model | Entity graph for people, places, organizations, concepts with Neo4j or SQLite backend |
| Neo4j Backend | Native graph database backend with Cypher traversal, full-text search, and auto-schema |
| World Model API | 12 REST endpoints for entity/relationship CRUD, graph traversal, and statistics |

### Cognitive Architecture

| Subsystem | Purpose |
|---|---|
| Cognition | MetaLearner with Cognitive Performance Index tracking |
| Autonomy | Background loop for anomaly detection, initiative generation, synthesis |
| Commitment Tracking | LLM-extracted commitments with status transitions, overdue detection, cognition triggers |
| Affect Tracking | Valence/arousal affect model per contact with trend detection |
| Shared Facts | Cross-contact knowledge graph with confidence scoring |
| Pattern Extraction | Entity co-occurrence, relation frequency, temporal sequence, and attribute cluster detection |
| Surprise Engine | Expectation-violation scoring with accumulation-based autonomy triggers |
| ToM LLM Extraction | LLM-backed affect and fact extraction from conversation turns with per-contact throttling |

### Safety and Efficiency

| Subsystem | Purpose |
|---|---|
| Event Journal | Append-only event persistence with atomic writes, SHA-256 checksums, and replay for disconnected clients |
| Context Compression | Adaptive context compression (conservative/balanced/aggressive modes) with query-aware section scoring |
| Skill Sandbox | Subprocess-isolated skill execution with resource limits (memory, CPU, file size, fork guard) |
| Security Scanner | AST-based static analysis for skill uploads (dunder escapes, dynamic getattr, obfuscation patterns) |

### Integration

| Subsystem | Purpose |
|---|---|
| Events | WebSocket stream for real-time events with journal replay |
| MCP Server | Model Context Protocol server exposing 14 tools, 4+ resources, and 3 prompts to coding harnesses |
| Learning | Continuous learning from corrections and engagement |
| Synthesis | Connection discovery between entities and topics |

### Key Properties

**Multi-harness by design.** Colony is not tied to one runtime. OpenClaw talks HTTP. Hermes talks HTTP with a MemoryProvider plugin. Claude Code, OpenCode, Codex, and Crush talk MCP. All share the same intelligence layer. Add harnesses selectively. Run them simultaneously.

**Autonomous by default.** The autonomy loop runs every 60 seconds, scanning for stale commitments, overdue goals, relationship gaps, and surprise events. When it finds work, it generates an initiative and pushes it to the connected harness. If the loop goes quiet, a scheduler task checks in with the owner — not as a reminder, but as an agent-initiated touchpoint to see if anything is needed. Colony doesn't just remember — it acts.

**No LLM keys required locally.** Colony inherits LLM credentials from its host at runtime. For standalone use or plugin development, supply them in `.env` to exercise the sidecar directly.

**Retrieval auto-configures.** `colony init` scans your hardware and picks the right embedding and reranker models for your tier, from a 4GB laptop to a 256GB workstation.

**Subsystems degrade gracefully.** An unwired subsystem returns empty results instead of errors. Run Colony with only the subsystems you need.

**Types stay in sync.** Python Pydantic schemas export an OpenAPI spec. TypeScript types generate from the spec. No client/server drift.

**Authenticated by default.** When `COLONY_API_KEY` is set, all API endpoints require `X-API-Key` header authentication. Without it, the API runs in open dev mode.

-----

## MCP Server

Colony ships a built-in MCP server that exposes its cognitive infrastructure as tools to any MCP-compatible coding harness. This is how your coding tools get access to commitments, affect state, world knowledge, and patterns.

### 14 MCP Tools

**Read-only tools (safe, no side effects):**

| Tool | What It Does |
|---|---|
| `colony_health` | Check sidecar health and capabilities |
| `colony_get_context` | Assemble full context for the current conversation |
| `colony_check_commitments` | List commitments for a contact, optionally filtered by status |
| `colony_lookup_facts` | Look up shared facts about a contact |
| `colony_check_affect` | Read current affect state (valence/arousal) for a contact |
| `colony_search_world` | Search the world model for entities or relationships |
| `colony_get_patterns` | List detected behavioral patterns |

**Mutating tools (write data):**

| Tool | What It Does |
|---|---|
| `colony_create_commitment` | Record a new commitment |
| `colony_fulfill_commitment` | Mark a commitment as fulfilled |
| `colony_cancel_commitment` | Cancel a commitment with a reason |
| `colony_remember_fact` | Store a shared fact about a contact |
| `colony_forget_fact` | Delete a shared fact |
| `colony_record_affect` | Record an affect event (valence + arousal) |
| `colony_record_surprise` | Record an expectation violation |

### Resources and Prompts

The MCP server also exposes resources for context and prompts for guided interaction:

- `colony://world/entities` - Top entities in the world model
- `colony://surprises/recent` - Recent surprise events
- `colony://commitments/active` - Active commitments
- `colony://affect/state` - Current affect summaries

### Source Tracking

Every write through the MCP server is tagged with a `provenance` field indicating which harness made the change. This is injected automatically from the `COLONY_MCP_SOURCE` environment variable. It works with any harness: Claude Code, Codex, Crush, OpenCode, OpenClaw, or anything else that sets the variable. You can trace whether a commitment was created by your coding tool or your chat agent.

### Setup

```bash
# Auto-detect installed harnesses and configure them
colony mcp setup

# See what would change without writing anything
colony mcp setup --dry-run

# Check which harnesses are detected
colony mcp detect

# Remove Colony from a specific harness
colony mcp remove --harness claude-code
colony mcp remove --harness claude-code --dry-run
```

Supported harnesses:

| Harness | Config Format | Detection |
|---|---|---|
| Claude Code | JSON (`~/.claude.json`) | `claude` CLI |
| Codex | TOML (`~/.codex/config.toml`) | `codex` CLI |
| Crush | JSON (`~/.crush/mcp.json`) | `crush` CLI |
| OpenCode | JSON (`~/.config/opencode/opencode.json`) | `opencode` CLI |
| Hermes | YAML (`~/.hermes/config.yaml`) | `hermes` CLI |

### Running the MCP Server

```bash
# Via CLI (stdio transport, default)
colony mcp

# Via CLI (HTTP transport for remote/CI)
colony mcp --transport http --port 8765

# The sidecar also exposes /mcp for streamable HTTP transport
# Available at http://localhost:7777/mcp when the sidecar is running
```

-----

## Architecture

Two deployable units. A thin TypeScript plugin that loads into OpenClaw, and a Python sidecar that owns state and runs the subsystems. The MCP server runs inside the sidecar process and proxies all calls through the same API.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Host Harnesses                                                          │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌─────────┐               │
│  │ OpenClaw │  │  Hermes  │  │ Claude Code│  │  Codex   │               │
│  │ (HTTP/WS)│  │(HTTP+Mem)│  │   (MCP)    │  │  (MCP)   │               │
│  └────┬─────┘  └────┬─────┘  └─────┬──────┘  └────┬─────┘               │
│       ┌──────────────┴──────────────┴──────────────┘                     │
│  ┌─────────┐  ┌──────────┐        │                                     │
│  │  Crush  │  │ OpenCode │        │                                     │
│  │  (MCP)  │  │  (MCP)   │────────┘                                     │
│  └─────────┘  └──────────┘                                              │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │ Initiative Poller (cron) → Webhook → Agent Run                  │    │
│  │ MemoryProvider → Prefetch / Sync Turn                           │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │  HTTP /v1/host/*    MCP stdio/HTTP   │
            │  WebSocket /v1/host/events           │
            └──────────────────┬──────────────────┘
                               │
┌──────────────────────────────┴──────────────────────────────────────────┐
│ Colony Sidecar                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ FastAPI Server + MCP Server + TelemetryStore                     │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ SubsystemRegistry (36 subsystems)                                │  │
│  │  - Autonomy Loop (initiative generation)                         │  │
│  │  - Telemetry (temporal health tracking)                          │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼────┐           ┌─────▼─────┐          ┌────▼────┐
   │  Neo4j  │           │  LiteLLM  │          │ SQLite  │
   │ (memory)│           │(reasoning)│          │(contacts│
   └─────────┘           └───────────┘          └─────────┘
```

### Communication

OpenClaw to sidecar: HTTP POST to `/v1/host/*` endpoints, WebSocket `/v1/host/events` for real-time events.

Hermes to sidecar: HTTP via MemoryProvider plugin (prefetch + turn sync), plus HTTP via initiative poller (health check + initiative fetch), plus MCP for direct tool access.

Coding harnesses to sidecar: MCP protocol over stdio or HTTP, proxied through the same API internally.

Contract: OpenAPI spec generated from Python schemas. TypeScript types auto-generated. No client/server drift.

-----

## Configuration

Colony writes a `.env` file during `colony init`. Key variables:

```bash
# Sidecar listener
COLONY_SIDECAR_HOST=127.0.0.1
COLONY_SIDECAR_PORT=7777

# Memory graph
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password

# World Model backend (sqlite or neo4j)
WORLD_MODEL_BACKEND=sqlite
NEO4J_DATABASE=neo4j

# API auth
COLONY_API_KEY=your-api-key

# Embedding + reranker (auto-detected; override to use API or custom model)
COLONY_EMBED_PROVIDER=
COLONY_EMBED_MODEL=
COLONY_EMBED_DIMS=
COLONY_RERANKER_MODEL=

# Multimodal embeddings (enabled via colony init or activate-multimodal)
COLONY_MULTIMODAL=false

# MCP source tracking (auto-set by colony mcp setup)
COLONY_MCP_SOURCE=
COLONY_MCP_CONTACT_ID=

# Telemetry thresholds (v0.8.x)
COLONY_STALE_SYNC_HOURS=2.0
COLONY_STALE_TICK_HOURS=24.0
COLONY_STALE_INITIATIVE_HOURS=48.0

# Owner check-in (v0.8.3)
COLONY_OWNER_CHECK_IN_ENABLED=true
COLONY_OWNER_CHECK_IN_SILENT_HOURS=1.0
COLONY_OWNER_CHECK_IN_COOLDOWN_HOURS=4.0
COLONY_OWNER_CONTACT_ID=           # Optional: override auto-resolved owner

# Hermes poller config (v0.8.x)
COLONY_LOG_CHANNEL=            # Log channel for alerts (e.g. whatsapp:GROUP_ID)
COLONY_PLATFORM=whatsapp       # Platform identifier

LOG_LEVEL=info
```

Full configuration reference in `docs/configuration.md`.

-----

## CLI Reference

### Setup and Operations

| Command | Description |
|---|---|
| `colony init` | Full first-run setup: deps, Neo4j, hardware scan, model pre-download, identity, harness selection, sidecar start, verify, doctor |
| `colony start` | Start the sidecar server (`--host`, `--port`, `--detach`) |
| `colony stop` | Stop a detached sidecar process |
| `colony service status` | Check sidecar health, subsystem wiring, and service state |
| `colony validate` | Run 5-step pipeline test, writes `.colony-e2e-validated` stamp |
| `colony seed` | Seed self-knowledge (run after `colony init` if skipped) |
| `colony doctor` | Run integration health check against running sidecar (`--url`, `--api-key`, `-v`) |
| `colony generate-types` | Export OpenAPI spec and generate TypeScript types |
| `colony backfill` | Re-embed all vectors with current model |
| `colony migrate-tier` | Migrate vectors from old embedding model to current |
| `colony activate-multimodal` | Enable multimodal embeddings and reranking |

### Service Management (v0.8.x)

| Command | Description |
|---|---|
| `colony service install` | Install launchd plist and load service |
| `colony service start` | Start the launchd service |
| `colony service stop` | Stop the launchd service (unload) |
| `colony service restart` | Restart the launchd service (unload/load) |
| `colony service status` | Check service loaded state + sidecar health |
| `colony service uninstall` | Unload and remove launchd plist |

### MCP Commands

| Command | Description |
|---|---|
| `colony mcp` | Run the MCP server (stdio transport, default) |
| `colony mcp --transport http --port 8765` | Run MCP server with HTTP transport |
| `colony mcp detect` | Show which coding harnesses are installed |
| `colony mcp setup` | Configure harnesses to use Colony MCP (`--dry-run` to preview) |
| `colony mcp remove` | Remove Colony MCP from a harness (`--dry-run` to preview) |

### Identity and Keys

| Command | Description |
|---|---|
| `colony key info` | Show colony_id, public key, and Genesis status |
| `colony key generate` | Rotate Colony keypair (colony_id stays the same) |
| `colony key set-passphrase` | Encrypt Colony private key with a passphrase |
| `colony key manifest` | Create a shareable colony manifest (public identity) |
| `colony key claim-genesis` | Claim Genesis status (first Colony only, one-time) |
| `colony node info` | Show this device's node_id, public key, certificate status |
| `colony backup` | Export Colony identity as encrypted backup (`-o` file, `--passphrase`) |
| `colony restore` | Restore Colony from backup (interactive: file + passphrase) |

### Cryptographic Identity

Colony uses a two-layer identity model:

- **Colony** is the logical identity. A permanent UUID (`colony_id`) paired with an Ed25519 keypair. One Colony, one owner, persists forever. Can run on multiple devices.
- **Node** is a physical device running that Colony. Each device gets a unique `node_id` and its own Ed25519 keypair, certified by the Colony's private key.

You can restore your Colony onto any number of machines. Each gets its own node identity while sharing the same Colony identity. Networking, clustering, and federation build on this foundation.

**Genesis.** The first Colony is the trust anchor for the entire network. Its manifest is self-signed with Ed25519 and committed to the repo. A hardcoded public key in the source verifies the signature. Genesis status is cryptographically unforgeable. Editing the manifest locally does not work because the signature will not verify against the hardcoded key.

**Backup and restore.** `colony backup` exports your entire Colony identity (colony_id, encrypted private key, Genesis manifest) as a single encrypted JSON file. `colony restore` brings it back on any machine. Store the backup file and passphrase in your password manager.

```bash
# First setup
colony init                          # Creates Colony identity + keypair
colony start                         # Starts sidecar, generates node identity

# Adding a second machine
colony restore -i backup.json        # Restores Colony identity
colony start                         # New node for this device

# Disaster recovery
colony restore                       # Interactive: file + passphrase
```

-----

## API Reference

Base URL: `http://localhost:7777/v1/host`

All endpoints require `X-API-Key` authentication when `COLONY_API_KEY` is set. Unauthenticated requests receive 401. The health endpoint (`/v1/host/health`) and OpenAPI spec (`/openapi.json`) are accessible without auth.

Full OpenAPI spec:

```bash
curl http://localhost:7777/openapi.json
```

### Core

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check with temporal metrics and capabilities |
| POST | `/context/assemble` | Assemble multi-source context for LLM injection |
| POST | `/turns/sync` | Sync a conversation turn (triggers extraction, pattern detection, cognition) |
| GET | `/events` | WebSocket endpoint for real-time event stream |
| GET | `/events/replay` | Replay events from a checkpoint for reconnected clients |

### Memory and World Model

| Method | Endpoint | Description |
|---|---|---|
| POST | `/memory/query` | Query memory graph |
| GET | `/world/entities` | List world model entities |
| POST | `/world/entities/query` | Search entities with full-text or semantic query |
| POST | `/world/entities` | Create an entity |
| GET | `/world/entities/{id}` | Get entity by ID |
| PATCH | `/world/entities/{id}` | Update an entity |
| DELETE | `/world/entities/{id}` | Delete an entity |
| POST | `/world/relationships` | Create a relationship |
| GET | `/world/relationships/{id}` | Get relationship by ID |
| DELETE | `/world/relationships/{id}` | Delete a relationship |
| GET | `/world/entities/{id}/neighborhood` | Get entity neighborhood (N-hop traversal) |
| GET | `/world/entities/{source_id}/path/{target_id}` | Find shortest path between entities |
| GET | `/world/stats` | World model statistics |
| POST | `/world/extract` | Extract entities from text |

### Commitments

| Method | Endpoint | Description |
|---|---|---|
| POST | `/commitments` | Create a commitment |
| GET | `/commitments` | List commitments (filter by person, status) |
| GET | `/commitments/{id}` | Get commitment by ID |
| PATCH | `/commitments/{id}` | Update commitment (status transitions, add note) |

### Theory of Mind

| Method | Endpoint | Description |
|---|---|---|
| GET | `/affect/state/{contact_id}` | Get current affect state for a contact |
| POST | `/affect/events` | Record an affect event |
| GET | `/affect/events` | Query affect history |
| GET | `/mind/facts` | Look up shared facts about a contact |
| POST | `/mind/facts` | Store a shared fact |
| DELETE | `/mind/facts/{id}` | Delete a shared fact |

### Patterns and Surprise

| Method | Endpoint | Description |
|---|---|---|
| GET | `/patterns` | List detected behavioral patterns |
| POST | `/surprises` | Record a surprise (expectation violation) |
| GET | `/surprises` | Query recorded surprises |

### Cognition and Autonomy

| Method | Endpoint | Description |
|---|---|---|
| POST | `/cognition/trigger` | Trigger cognition cycle (throttled, auto-fires on turn sync) |
| GET | `/initiatives` | List pending initiatives |
| POST | `/initiatives` | Create an initiative (autonomy loop or external) |
| GET | `/initiatives/{id}` | Get initiative by ID |
| PATCH | `/initiatives/{id}` | Update initiative status |

### Goals, Skills, and Identity

| Method | Endpoint | Description |
|---|---|---|
| POST | `/goals` | Create a goal |
| GET | `/goals` | List goals |
| GET | `/skills` | List registered skills |
| POST | `/identity/colony` | Get or create Colony identity |
| GET | `/identity/colony` | Get Colony identity info |
| POST | `/identity/node` | Get or create Node identity |

### MCP

| Method | Endpoint | Description |
|---|---|---|
| POST | `/mcp` | Streamable HTTP MCP transport endpoint |

-----

## What Colony Is Not

Colony does not replace your agent harness. It is the layer underneath. OpenClaw handles communication, Claude Code handles code, Codex handles CI. Colony handles memory, identity, and cognitive state.

Colony does not ship its own LLM client. It inherits LLM credentials from its host harness at runtime. The cognition channel routes through OpenClaw's `sessions_spawn`. Standalone mode uses credentials from `.env`.

Colony is not a vector database. It uses Neo4j for graph storage and has an embedding pipeline, but it is not a general-purpose vector store.

Colony is not an agent framework. It does not run agents. It provides the infrastructure that makes agents smarter: shared memory, commitment tracking, affect modeling, pattern detection, and world knowledge that persists across sessions and flows across tools.

Colony is not a reminder service. When the autonomy loop detects work, it generates an initiative and pushes it to your agent. The agent acts on your behalf — it doesn't tell you what to do.

-----

## Roadmap

### Now (v0.8.x)

- 36 wired subsystems, 57+ API endpoints
- Temporal awareness and sync health monitoring
- Auto-restart via launchd with telemetry tracking
- Initiative poller with health preflight and alert routing
- Memory provider with circuit breaker, retry, and diagnostics
- MCP server for Claude Code, Codex, Crush, Hermes
- Silence-triggered owner check-in — agent-initiated touchpoint when autonomy goes quiet
- Multi-harness shared intelligence layer
- Neo4j + SQLite world model backends
- Cognitive architecture: commitments, affect, shared facts, patterns, surprise, autonomy
- Event journal with replay
- Adaptive context compression
- Full lifecycle CLI (start/stop/service/validate/doctor)

### Next

- Enhanced provenance tracking across all stores
- Response gate PII and injection interception testing
- Remote MCP transport for CI and team setups
- SuperColony Network architecture spec

### Future

- SuperColony Network: independent colonies sharing knowledge and compute on a global substrate
- Colony federation and trust propagation
- Cross-colony commitment tracking
- Stigmergic coordination protocol

v1.0.0 ships when the SuperColony Network is operational with all supporting features and the architecture is determined stable.

-----

## Development

```bash
git clone https://github.com/Aevonix/ColonyAI.git
cd ColonyAI/sidecar

# Python
pip install -e ".[dev]"
pytest tests/ -v                    # Unit tests
COLONY_API_KEY=test pytest tests/e2e/ -v   # E2E tests (needs sidecar)

# TypeScript
cd ../
npm install
npm run build                       # Compiles + type-checks
npm test                            # TypeScript tests
```

### Test Counts

| Suite | Count |
|---|---|
| Python unit tests | 695+ |
| TypeScript tests | 151 |
| E2E integration tests | 77 |
| MCP unit tests | 51 |

### Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for versioning conventions, PR process, and coding standards.

-----

## License

[MIT](LICENSE)

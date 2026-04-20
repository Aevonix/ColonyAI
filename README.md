# Colony

**Sovereign intelligence and memory for AI agents. Designed to mesh into a unified super-agent.**

Colony gives any agent harness a durable cognitive layer: memory, reasoning, goals, relationships, proactive delivery, and cryptographic identity. One HTTP API covers it. A TypeScript plugin loads into your host. A Python sidecar holds the state and runs the work.

Colony runs as a plugin. It needs a harness to plug into (OpenClaw, Hermes, or anything that speaks the Colony plugin contract). Today that's the full scope: 22 subsystems, one user, one install. The roadmap grows the intelligence outward. Your nodes form your Colony. Your Colony federates with other Colonies. Federations connect through the SuperColony Network. Each phase expands what the shared intelligence can do.

-----

## Why Colony

Ant colonies are the textbook example of emergent collective intelligence. No central controller, specialized roles, coordination through a shared environment. The technical name is stigmergy: individuals modify the environment, other individuals read those modifications and respond. Colony applies that pattern to LLM agents. A shared substrate of memory, signals, and state that many specialized systems read from and write to.

Argentine ants form the largest known supercolony in nature. Six thousand kilometers of coastline, three continents, every individual treating every other individual as kin. The SuperColony Network is modeled on that. Independent colonies, global reach, participation on your terms.

-----

## What Colony Is Today

v1.0 is the intelligence system. 22 wired subsystems. Everything below works now.

### 22 Wired Subsystems

| Subsystem | Purpose |
|---|---|
| Consolidate | Memory deduplication and merge for near-duplicate graph entries |
| Memory | Neo4j-backed graph storage for conversations, entities, relationships, insights |
| Response Gate | 7-layer response inspection (recipient verification, PII scanning, cross-context isolation, trust tiers, injection detection, secondary review, send delay) |
| Signals | Behavioral signal ingestion for profiling and pattern detection |
| Embeddings | Auto-tier-detected embedding pipeline (text + multimodal) |
| Context Assembly | Parallel query across 16 subsystems to build LLM context |
| Reasoning | Bounded LLM iteration loop with tool calling |
| Goals | DAG-based goal decomposition and tracking |
| Contacts | Relationship store with trust tiers and interaction history |
| Briefings | Proactive relationship summaries and conversation starters |
| World Model | Entity graph for people, places, organizations, concepts |
| Cognition | MetaLearner with Cognitive Performance Index tracking |
| Research | Background research pipeline with configurable depth |
| Delivery | Proactive message delivery bridge |
| Synthesis | Connection discovery between entities and topics |
| Learning | Continuous learning from corrections and engagement |
| Skills | Tool registry with metadata |
| Identity | Ed25519 cryptographic identity with Colony + Node layers, Genesis trust anchor, backup/restore |
| Secrets | Encrypted vault for sensitive configuration |
| Autonomy | Background loop for anomaly detection, initiative generation, synthesis |
| Sessions | Isolated session management |
| Events | WebSocket stream for real-time events |

### Key Properties

**Harness-required.** Colony runs as a plugin inside a host harness. OpenClaw is the reference integration. Any host that implements the Colony plugin contract can mount it.

**No LLM keys required locally.** Colony inherits LLM credentials from its host at runtime. For plugin development, you can supply them in `.env` to exercise the sidecar directly.

**Retrieval auto-configures.** `colony init` scans your hardware and picks the right embedding and reranker models for your tier, from a 4GB laptop to a 256GB workstation.

**Subsystems degrade gracefully.** An unwired subsystem returns empty results instead of errors. Run Colony with only the subsystems you need.

**Types stay in sync.** Python Pydantic schemas export an OpenAPI spec. TypeScript types generate from the spec. No client/server drift.

**Authenticated by default.** When `COLONY_API_KEY` is set, all API endpoints require Bearer token authentication. Without it, the API runs in open dev mode.

-----

## Table of Contents

- [Why Colony](#why-colony)
- [What Colony Is Today](#what-colony-is-today)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [What Colony Is Not](#what-colony-is-not)
- [Roadmap](#roadmap)
- [Installation Profiles](#installation-profiles)
- [CLI Reference](#cli-reference)
- [Development](#development)
- [OpenClaw Integration](#openclaw-integration)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [License](#license)

-----

## Quick Start

### Prerequisites

Python 3.11+ and Docker. Docker is auto-installed by `colony init` if missing.

### Install

```bash
git clone https://github.com/Aevonix/colony.git
cd colony/sidecar

pip install -e .
colony init    # setup wizard: deps, Neo4j, hardware scan, model pre-download
colony start   # run the sidecar
colony status  # verify
```

`colony init` handles dependency installation, Neo4j setup, hardware detection, model pre-download, and initial self-knowledge seeding.

### Docker Compose

For containerized deployments:

```bash
cp .env.example .env   # set NEO4J_PASSWORD and COLONY_API_KEY
docker compose up -d    # Neo4j + Colony sidecar
```

### Verify

```bash
curl http://localhost:7777/v1/host/health
# Expected: {"status":"ok","capabilities":[...22 subsystems...]}
```

### Full Health Check

After the sidecar is running, use `colony doctor` to verify all subsystems:

```bash
COLONY_API_KEY=your-key colony doctor
```

15 subsystem checks: health, auth, memory, response gate, goals, identity, secrets, embeddings, context assembly, skills, world model, signals, autonomy. Exit code 0 if healthy, 1 if any check fails.

For the full integration test suite (68 tests):

```bash
pip install pytest httpx
COLONY_URL=http://localhost:7777 COLONY_API_KEY=your-key pytest tests/integration/ -v
```

-----

## Architecture

Two deployable units. A thin TypeScript plugin that loads into your host process, and a Python sidecar that owns state and runs the subsystems.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Host Harness (OpenClaw, etc.)                                       │
│                                                                     │
│  ┌────────────────┐  ┌─────────────┐  ┌────────────────────────┐   │
│  │ Plugin Loader  │  │ Agent Loop  │  │ Channel Adapters       │   │
│  └───────┬────────┘  └──────┬──────┘  └────────────┬───────────┘   │
│          └──────────────────┼──────────────────────┘                │
└─────────────────────────────┼────────────────────────────────────────┘
                              │
                              │ HTTP /v1/host/*
                              │ WebSocket /v1/host/events
                              │
┌─────────────────────────────┼────────────────────────────────────────┐
│ Colony Sidecar              │                                        │
│  ┌──────────────────────────▼──────────────────────────┐            │
│  │ FastAPI Server                                      │            │
│  └──────────────────────────┬──────────────────────────┘            │
│  ┌──────────────────────────▼──────────────────────────┐            │
│  │ SubsystemRegistry (22 subsystems)                   │            │
│  └─────────────────────────────────────────────────────┘            │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   ┌────▼────┐       ┌─────▼─────┐      ┌────▼────┐
   │  Neo4j  │       │  LiteLLM  │      │ SQLite  │
   │ (memory)│       │(reasoning)│      │(contacts│
   └─────────┘       └───────────┘      └─────────┘
```

### Communication

Plugin to sidecar: HTTP POST to `/v1/host/*` endpoints.

Sidecar to plugin: WebSocket `/v1/host/events` for real-time events.

Contract: OpenAPI spec generated from Python schemas. TypeScript types auto-generated.

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

# API auth
COLONY_API_KEY=your-api-key

# Embedding + reranker (auto-detected; override to use API or custom model)
COLONY_EMBED_PROVIDER=
COLONY_EMBED_MODEL=
COLONY_EMBED_DIMS=
COLONY_RERANKER_MODEL=

# Multimodal embeddings (enabled via colony init or activate-multimodal)
COLONY_MULTIMODAL=false

LOG_LEVEL=info
```

Full configuration reference in `docs/configuration.md`.

-----

## CLI Reference

### Setup & Operations

| Command | Description |
|---|---|
| `colony init` | Interactive setup: deps, Neo4j, hardware scan, model pre-download, Colony identity creation |
| `colony start` | Start the sidecar server (`--host`, `--port`, `--detach`) |
| `colony status` | Check sidecar health and subsystem wiring |
| `colony seed` | Seed self-knowledge (run after `colony init` if skipped) |
| `colony doctor` | Run integration health check against running sidecar (`--url`, `--api-key`, `-v`) |
| `colony generate-types` | Export OpenAPI spec and generate TypeScript types |
| `colony backfill` | Re-embed all vectors with current model |
| `colony migrate-tier` | Migrate vectors from old embedding model to current |
| `colony activate-multimodal` | Enable multimodal embeddings and reranking |

### Identity & Keys

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

All endpoints require Bearer authentication (`Authorization: Bearer $COLONY_API_KEY`). Unauthenticated requests receive 401. The health endpoint (`/v1/host/health`) and OpenAPI spec (`/openapi.json`) are accessible without auth.

Full OpenAPI spec:

```bash
curl http://localhost:7777/openapi.json
```

Common request structure:

```json
{
  "identity": { "host_id": "string" },
  "context": { "session_id": "string", "contact_id": "string" }
}
```

Full endpoint documentation in `docs/api.md`.

-----

## What Colony Is Not

Colony is not an agent framework. It does not replace OpenClaw, Hermes, LangGraph, or similar. It mounts into them.

Colony is not an LLM. It inherits LLM credentials from its host.

Colony is not a vector database. It uses Neo4j for graph memory and configurable embedding pipelines for vectors.

Colony is not a RAG library. RAG is one capability among many. Colony also handles goals, relationships, autonomy, identity, and (on the roadmap) networking and federation.

-----

## Roadmap

Colony ships in phases. The intelligence system is v1.0. Each phase expands the shared intelligence into a larger surface.

### Phase 1: Intelligence System (Shipped, v1.0)

Single-node Colony mounted into a host harness. 22 subsystems wired. Everything described above in "What Colony Is Today."

### Phase 2: Multimodal (Shipped)

Text-only retrieval was the original default. Phase 2 adds a multimodal toggle.

A second index runs alongside the text index for image embeddings. Image-containing content routes to a multimodal embedder (Qwen3-VL-Embedding-8B or equivalent per tier). Text queries fan out across both indexes. Image queries hit the multimodal index only. Migration from text-only to multimodal is additive: the existing text index stays intact. Backfill of historical image content is opt-in.

Users who stay text-only keep the same retrieval path they had in v1.0. Flipping the toggle extends Colony into images without changing what was already there.

### Phase 3: Colony Meshing

The first networking release. A single user's Colony expands from one node to a mesh of nodes they own. The mesh uses SWIM gossip for health monitoring and Raft-inspired election for failover.

Node roles:

| Role | Description |
|---|---|
| Queen | Main node. Canonical state, point of contact for the owner, orchestration. One per Colony. |
| Alate | Overlay on a Worker. The highest-capability Worker in the Colony, designated as Queen-successor. Still executes tasks. Promotes to Queen on failure, either temporarily (until Queen returns) or permanently (when the owner "Crowns" it). |
| Worker | Executes tasks from the Queen's queue. All non-Queen nodes are Workers. |
| Sentinel | Overlay on any node. Validates the SuperColony chain. Ships with Phase 5. |

Capabilities added in Phase 3:

- Node registration via single-use link tokens (15-min TTL, capability-allowlisted)
- Automatic Alate selection based on resource score (GPU VRAM, RAM, local models, API keys)
- Raft-inspired leader election on Queen failure
- SWIM gossip for node health monitoring across the mesh
- Shared state across the Colony (Redis-backed canonical registry, SQLite node caches)

A single-node Colony is still a Colony. That node is both Queen and (implicitly) Alate. Phase 3 does not force you to add nodes. It unlocks the ability to grow when you are ready.

### Phase 4: Federation

Phase 4 lets independent Colonies communicate under explicit trust. Your Colony can federate with another user's Colony. A team can form a shared Federation from each member's Colony.

Trust levels (0 to 4, strictly increasing permissions):

| Level | Name | Capabilities |
|---|---|---|
| 0 | Discovery | See each other's existence. No data exchange. |
| 1 | Verified | Exchange capability lists and health. Cryptographic identity confirmed. |
| 2 | Trusted | Post tasks to each other's queue. No memory sharing. |
| 3 | Allied | Query each other's memory graph (with redaction). |
| 4 | Full Mesh | Full bidirectional memory sync with redaction. |

Capabilities added in Phase 4:

- Cryptographic identity exchange and trust negotiation
- Signed, replay-protected message envelopes between Colonies (timestamp, nonce, Ed25519 signature)
- Federated task delegation with capability gating (Trust 2+)
- Federated memory queries with redaction (Trust 3+)
- Skills marketplace for sharing capabilities between Colonies
- Peer reliability tracking (uptime, response time, task success rate)

Federation extends your Colony outward. A Colony with no federation peers keeps its full intelligence layer intact. When you federate, that intelligence starts exchanging with peers under the trust level you negotiate.

### Phase 5: SuperColony Network

Phase 5 ships the global external network. Independent Colonies worldwide discover each other, exchange information under limited trust, and participate in a shared cryptographic chain.

ColonyChain (CNP) is the blockchain backbone:

- Genesis Sentinel creates the genesis block. Its hash becomes the network ID.
- Proof-of-Work registration prevents spam (difficulty 22).
- Sentinels validate blocks and maintain the ledger.
- NAT traversal (address probe, rendezvous, relay) handles nodes behind firewalls.
- Shamir's Secret Sharing handles identity recovery.

Capabilities added in Phase 5:

- Sentinel discovery (DNS SRV, bootstrap seed list, roster propagation)
- Gossip-based peer discovery across Colonies (no central registry)
- Global inter-Colony messaging under explicit trust policies
- Version enforcement and migration support
- HMAC-protected roster persistence
- The SuperColony Network itself: the global substrate where any Colony can participate or abstain

Phase 5 is when Colony becomes what it is designed to be. A global mesh of agent intelligence, where your agents coordinate with other agents on your terms.

-----

## Installation Profiles

Phase 1 installs auto-detect your hardware and select an embedding + reranker stack. Current defaults:

| Memory | Embedder | Reranker |
|---|---|---|
| 0 to 4 GB | all-MiniLM-L6-v2 | (none) |
| 4 to 8 GB | nomic-embed-text-v1.5 | (none) |
| 8 to 16 GB | Qwen3-Embedding-0.6B | bge-reranker-v2-m3 |
| 16 to 32 GB | Qwen3-Embedding-4B | Qwen3-Reranker-0.6B |
| 32 to 64 GB | Qwen3-Embedding-8B | Qwen3-Reranker-4B |
| 64 to 128 GB | Qwen3-Embedding-8B | Qwen3-Reranker-8B |
| 128 to 256 GB | Harrier-OSS-v1-27B | Qwen3-Reranker-8B |
| 256 GB+ | Harrier-OSS-v1-27B | Qwen3-Reranker-8B |

Phase 2 adds parallel multimodal variants for each tier.

-----

## Development

### Setup

```bash
git clone https://github.com/Aevonix/colony.git
cd colony

# TypeScript plugin
npm install

# Python sidecar
cd sidecar
pip install -e ".[dev,neo4j,lancedb]"
```

### Tests

```bash
npm test                         # TypeScript
cd sidecar && PYTHONPATH=. pytest # Python
npm run test:integration         # integration (requires running sidecar)
```

### Type Generation

After modifying Python schemas:

```bash
npm run generate-types
```

Exports the OpenAPI spec and regenerates `src/types-generated.ts`.

### Project Structure

```
colony/
├── src/                      # TypeScript plugin (thin HTTP client)
├── sidecar/                  # Python sidecar (stateful, 22 subsystems)
│   └── colony_sidecar/
│       ├── api/              # FastAPI routers + schemas
│       ├── autonomy/         # Autonomy loop
│       ├── briefings/        # Proactive briefing engine
│       ├── chain/            # Cryptographic identity + keys + node certs
│       ├── contacts/         # Relationship store
│       ├── delivery/         # Proactive message bridge
│       ├── events/           # WebSocket event stream
│       ├── gate/             # 7-layer response gate
│       ├── goals/            # DAG goal engine
│       ├── identity_bootstrap/ # Self-knowledge seeding
│       ├── intelligence/     # Graph, cognition, synthesis, learning
│       ├── models/           # Shared Pydantic models
│       ├── reasoning/        # ReasoningLoop + ToolExecutor
│       ├── redact/           # Content redaction
│       ├── research/         # Background research pipeline
│       ├── router/           # LLMRouter
│       ├── secrets/          # Encrypted vault
│       ├── sessions/         # Session management
│       ├── skills/           # Skills registry
│       ├── task_queue/       # Task queue + scheduler + workers
│       ├── tools/            # Colony-native tool definitions
│       ├── vector/           # Embedding pipeline
│       └── world_model/      # Entity graph
├── Dockerfile
├── docker-compose.yml
└── package.json
```

-----

## OpenClaw Integration

### Plugin Installation

```bash
npm install @aevonix/colony
```

### Plugin Configuration

```json
{
  "plugins": {
    "colony": {
      "sidecarUrl": "http://127.0.0.1:7777",
      "apiKey": "your-api-key",
      "ownReasoningLoop": true,
      "ownMemoryCapability": true,
      "ownContextEngine": true,
      "forwardProactiveDeliveries": true
    }
  }
}
```

The plugin registers 22 capabilities (memory, consolidate, signals, embed, context, reasoning, response_gate, goals, contacts, briefings, world_model, cognition, research, delivery, synthesis, learning, skills, identity, secrets, autonomy, sessions, task_queue, events) and 5 hooks (message_received, message_sending, llm_output, session_start, session_end).

-----

## Deployment

### Docker

```bash
docker build -t colony-sidecar .
docker run -d \
  -p 7777:7777 \
  -e NEO4J_URI=bolt://neo4j:7687 \
  -e NEO4J_PASSWORD=password \
  -e COLONY_API_KEY=your-key \
  colony-sidecar
```

### Docker Compose

Production setup with health checks and persistent volumes:

```bash
cp .env.example .env
docker compose up -d
```

Includes Neo4j (APOC, memory tuning, health check) and the Colony sidecar (HuggingFace model cache volume).

-----

## Troubleshooting

**Sidecar won't start.** Check the log: `colony start 2>&1 | tee colony.log`. Common causes: port 7777 in use (override with `COLONY_SIDECAR_PORT`), Neo4j unreachable (check `NEO4J_URI` and `NEO4J_PASSWORD`), Python below 3.11 (upgrade).

**Memory returns empty results.** Neo4j not connected or no data yet. Verify with `curl http://localhost:7474` and `colony status | grep memory`.

**Type generation fails.** Install dev dependencies: `cd sidecar && pip install -e ".[dev]"`, then `npm run generate-types`.

**WebSocket events not received.** Check auth response. On connect, you should see `{"type":"auth_ok","scopes":[...]}`. If not, verify `COLONY_API_KEY` matches.

-----

## License

[MIT](LICENSE) - Copyright 2026 Aevonix

-----

## Status

**Current release:** v0.1.0, Intelligence System (Phase 1 + Phase 2 multimodal)

**Subsystems wired:** 22 of 22

**Endpoints:** 44+

**Tests:** 36 vector, 114 TypeScript, Python suite

**Next up:** Phase 3 (Colony Meshing)

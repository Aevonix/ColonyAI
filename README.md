# Colony

**Intelligence infrastructure for AI agents.**

Colony is a modular intelligence layer that you mount into any agent framework. It provides memory, reasoning, context assembly, safety filtering, goal tracking, and proactive delivery — all through a clean HTTP API.

Think of it as a "brain service" for your agents. The TypeScript plugin loads into your host (OpenClaw, Hermes, etc.) and delegates all intelligence operations to the Python sidecar over HTTP.

---

## Table of Contents

- [What Colony Does](#what-colony-does)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [Docker (Recommended)](#docker-recommended)
  - [Verify Installation](#verify-installation)
- [The Colony CLI](#the-colony-cli)
- [Subsystems](#subsystems)
  - [Memory (Graph Store)](#memory-graph-store)
  - [Safety (ResponseGate)](#safety-responsegate)
  - [Signals (Behavioral Profiling)](#signals-behavioral-profiling)
  - [Embeddings (Vector Pipeline)](#embeddings-vector-pipeline)
  - [Context Assembly](#context-assembly)
  - [Reasoning (LLM Loop)](#reasoning-llm-loop)
  - [Goals (DAG Engine)](#goals-dag-engine)
  - [Contacts (Relationship Store)](#contacts-relationship-store)
  - [Briefings (Proactive Summaries)](#briefings-proactive-summaries)
  - [World Model (Entity Graph)](#world-model-entity-graph)
  - [Cognition (MetaLearner)](#cognition-metalearner)
  - [Research (Background Tasks)](#research-background-tasks)
  - [Delivery (Proactive Messaging)](#delivery-proactive-messaging)
  - [Synthesis (Connection Discovery)](#synthesis-connection-discovery)
  - [Learning (Continuous Improvement)](#learning-continuous-improvement)
  - [Skills (Tool Registry)](#skills-tool-registry)
  - [Identity (Cryptographic Chain)](#identity-cryptographic-chain)
  - [Secrets (Encrypted Vault)](#secrets-encrypted-vault)
  - [Autonomy (Background Loop)](#autonomy-background-loop)
  - [Events (Real-time Stream)](#events-real-time-stream)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [OpenClaw Integration](#openclaw-integration)
- [Development](#development)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What Colony Does

**Problem:** Building an intelligent agent requires wiring together dozens of subsystems — memory storage, context assembly, safety filtering, goal tracking, relationship graphs, proactive delivery, and more. Each has its own API, state, and failure modes.

**Solution:** Colony is a single service that provides all of these subsystems through one HTTP API. You mount it into your agent framework once, and you get:

- **Persistent memory** — Neo4j-backed graph storage that remembers conversations, entities, relationships, and insights across sessions
- **Context assembly** — Queries all intelligence systems in parallel and assembles relevant context for the LLM
- **Safety pipeline** — 7-layer response gate that filters harmful, inappropriate, or off-brand content
- **Reasoning loop** — LLM iteration with tool calling, bounded by max turns and configurable policies
- **Goal tracking** — DAG-based goal engine that decomposes objectives and tracks progress
- **Relationship intelligence** — Contact store with trust tiers, interaction history, and style adaptation
- **Proactive delivery** — Background autonomy that pushes insights, briefings, and anomalies to channels
- **Cryptographic identity** — Ed25519 signing for authenticated agent identity

**Use Cases:**

- Mount into OpenClaw for a personal AI assistant with long-term memory
- Mount into any agent framework that supports the Colony plugin API
- Deploy as a shared backend for multiple agent instances

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                              OpenClaw                                │
│  ┌────────────────┐  ┌─────────────┐  ┌────────────────────────┐   │
│  │ Plugin Loader  │  │ Agent Loop  │  │ Channel Adapters       │   │
│  └───────┬────────┘  └──────┬──────┘  └────────────┬───────────┘   │
│          │                  │                      │                │
│          └──────────────────┼──────────────────────┘                │
│                             │                                        │
└─────────────────────────────┼────────────────────────────────────────┘
                              │
                              │  HTTP /v1/host/*
                              │
┌─────────────────────────────┼────────────────────────────────────────┐
│                      Colony Sidecar                                  │
│                             │                                        │
│  ┌──────────────────────────▼──────────────────────────┐            │
│  │                   FastAPI Server                     │            │
│  │  /health  /memory/*  /context/*  /reasoning/*  ...  │            │
│  └──────────────────────────┬──────────────────────────┘            │
│                             │                                        │
│  ┌──────────────────────────▼──────────────────────────┐            │
│  │              SubsystemRegistry                       │            │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │            │
│  │  │ Memory  │ │ Safety  │ │Context  │ │Reasoning│   │            │
│  │  │ (Graph) │ │ (Gate)  │ │(Engine) │ │ (Loop)  │   │            │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │            │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │            │
│  │  │  Goals  │ │Contacts │ │Briefings│ │ World   │   │            │
│  │  │ (DAG)   │ │(Store)  │ │(Engine) │ │ Model   │   │            │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │            │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │            │
│  │  │Cognition│ │Research │ │Delivery │ │Synthesis│   │            │
│  │  │(Meta)   │ │(Pipeline)│ │ (Bridge)│ │(Discover)│  │            │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │            │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │            │
│  │  │Learning │ │ Skills  │ │ Identity│ │ Secrets │   │            │
│  │  │(Learner)│ │(Registry)│ │ (Chain) │ │ (Vault) │   │            │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │            │
│  │  ┌─────────┐ ┌─────────┐                           │            │
│  │  │Autonomy │ │ Events  │                           │            │
│  │  │ (Loop)  │ │ (WS)    │                           │            │
│  │  └─────────┘ └─────────┘                           │            │
│  └─────────────────────────────────────────────────────┘            │
│                                                                      │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   ┌────▼────┐       ┌─────▼─────┐      ┌────▼────┐
   │  Neo4j  │       │  LiteLLM  │      │ SQLite  │
   │ (memory)│       │(reasoning)│      │(contacts│
   └─────────┘       └───────────┘      └─────────┘
```

### Two Deployable Units

| Component | Language | Purpose |
|-----------|----------|---------|
| **Plugin** (`src/`) | TypeScript | Loads into host process (OpenClaw). Registers hooks, context engine, agent harness adapters. Thin — just HTTP client + type mappings. |
| **Sidecar** (`sidecar/`) | Python | FastAPI server with all 21 intelligence subsystems. Stateful, owns Neo4j/SQLite/LiteLLM connections. |

### Communication

- **Plugin → Sidecar:** HTTP POST to `/v1/host/*` endpoints
- **Sidecar → Plugin:** WebSocket `/v1/host/events` for real-time events (proactive messages, anomalies)
- **Contract:** OpenAPI spec generated from Python schemas → TypeScript types auto-generated

### Single Source of Truth

Python Pydantic schemas define all request/response types. The OpenAPI spec is exported from these schemas, and TypeScript types are generated from the spec. Zero drift between client and server.

```bash
npm run generate-types
```

---

## Quick Start

### Prerequisites

- **Python 3.11+** (for sidecar)
- **Node.js 20+** (for plugin development, optional)
- **Neo4j 5.18+** (for graph memory, optional but recommended)
- **Docker & Docker Compose** (for containerized setup)

### Docker (Recommended)

The fastest way to get Colony running with all dependencies:

```bash
# Clone the repo
git clone https://github.com/Aevonix/colony.git
cd colony

# Copy environment template
cp .env.example .env

# Edit with your configuration
# At minimum, set:
# - NEO4J_PASSWORD=your-secure-password
# - COLONY_API_KEY=your-api-key
nano .env

# Start everything (Neo4j + Colony sidecar)
docker compose up -d

# Check status
docker compose logs -f colony
```

This starts:
- Neo4j on `bolt://localhost:7687` (web UI: http://localhost:7474)
- Colony sidecar on `http://localhost:7777`

### Development Setup

For development or custom deployments:

```bash
# 1. Install Python package
cd sidecar
pip install -e ".[dev,neo4j,lancedb]"

# 2. Run setup wizard
colony init

# 3. Start the sidecar
colony start

# 4. Verify it's running
colony status
```

---

## The Colony CLI

The `colony` command is your primary interface for managing the sidecar.

### Commands

#### `colony init`

Interactive setup wizard. Guides you through first-time configuration.

```bash
colony init                    # Interactive setup wizard
colony init --dir /path        # Use custom config directory
```

**What it does:**
1. Installs Python dependencies
2. Detects host framework (OpenClaw, etc.)
3. Installs Docker if missing, starts Neo4j
4. Scans hardware (GPU, VRAM, RAM) and recommends embedding tier
5. Pre-downloads embedding + reranker models
6. Configures OpenClaw plugin if detected
7. Seeds self-knowledge (memories, entities, skills)
8. Writes `.env` with all configuration

#### `colony start`

Start the sidecar server.

```bash
colony start                    # Use .env config
colony start --host 0.0.0.0     # Override listen host
colony start --port 8080        # Override listen port
colony start --detach           # Run in background
```

#### `colony status`

Check sidecar health and capabilities.

```bash
colony status
# Output:
# Status: healthy
# Capabilities: memory, safety, context, reasoning, goals, ...
#   memory: ColonyGraph wired
#   safety: ResponseGate wired
#   reasoning: ReasoningLoop wired (max_iterations=10)
```

#### `colony generate-types`

Export OpenAPI spec and generate TypeScript types.

```bash
colony generate-types
# Output: openapi.json (77 schemas, 44 paths)
```

---

## Subsystems

Colony provides 21 intelligence subsystems, each exposed via the HTTP API.

### Memory (Graph Store)

**Purpose:** Persistent memory backed by Neo4j. Stores conversations, entities, relationships, and insights in a queryable graph.

**API Endpoints:**
- `POST /v1/host/memory/read` — Retrieve memories by session/contact
- `POST /v1/host/memory/write` — Store a new memory
- `POST /v1/host/memory/search` — Semantic search across all memories
- `POST /v1/host/memory/flush` — Clear memories for a session
- `POST /v1/host/memory/embed` — Generate embeddings for text

**When unwired:** Returns empty results, does not error. Graceful degradation.

**Configuration:**
```bash
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
```

**Example:**
```bash
curl -X POST http://localhost:7777/v1/host/memory/search \
  -H "Authorization: Bearer $COLONY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "identity": {"host_id": "my-agent"},
    "context": {"session_id": "session-123"},
    "query": "what did we discuss about the project roadmap?"
  }'
```

### Safety (ResponseGate)

**Purpose:** 7-layer safety pipeline that filters LLM output before it reaches users.

**Layers:**
1. **Profanity filter** — Blocks explicit language
2. **PII detection** — Redacts personal information
3. **Harmful content** — Detects dangerous instructions
4. **Brand safety** — Enforces tone/style guidelines
5. **Contextual safety** — Considers conversation context
6. **Confidence check** — Flags low-confidence outputs
7. **Policy compliance** — Enforces configurable policies

**API Endpoint:**
- `POST /v1/host/safety/check` — Check content for safety

**When unwired:** All content passes through (no filtering).

**Example:**
```bash
curl -X POST http://localhost:7777/v1/host/safety/check \
  -H "Authorization: Bearer $COLONY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "identity": {"host_id": "my-agent"},
    "context": {"session_id": "session-123"},
    "content": "Here is the assistant response to check..."
  }'
```

### Signals (Behavioral Profiling)

**Purpose:** Ingests behavioral signals (message sent, tool called, etc.) for profiling and pattern detection.

**API Endpoint:**
- `POST /v1/host/signals/ingest` — Record behavioral signals

**Signal Types:**
- `message_sent` — Agent sent a message
- `message_received` — User sent a message
- `tool_called` — Agent invoked a tool
- `topic_mentioned` — Topic discussed
- `entity_mentioned` — Entity referenced

### Embeddings (Vector Pipeline)

**Purpose:** Generate vector embeddings for text, enabling semantic search and similarity matching.

**API Endpoint:**
- `POST /v1/host/memory/embed` — Generate embeddings

**When unwired:** Returns 501 Not Implemented.

### Context Assembly

**Purpose:** One-stop endpoint that queries all intelligence systems in parallel and assembles relevant context for the LLM.

**API Endpoints:**
- `POST /v1/host/context/assemble` — Basic assembly (memory only)
- `POST /v1/host/context/enriched` — Full assembly (queries 16 subsystems)

**Enriched Features:**
- `memory` — Relevant past conversations
- `relationships` — Contact relationship context
- `style` — Communication style adaptation
- `goals` — Active goals context
- `worldModel` — Entity knowledge
- `insights` — Discovered insights

**Example:**
```bash
curl -X POST http://localhost:7777/v1/host/context/enriched \
  -H "Authorization: Bearer $COLONY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "identity": {"host_id": "my-agent"},
    "context": {"session_id": "session-123", "contact_id": "user-456"},
    "message": "What should I work on next?",
    "features": {
      "memory": true,
      "relationships": true,
      "style": true,
      "goals": true,
      "worldModel": true,
      "insights": true
    }
  }'
```

### Reasoning (LLM Loop)

**Purpose:** Bounded LLM iteration with tool calling support.

**API Endpoint:**
- `POST /v1/host/reasoning/turn` — Execute one reasoning turn

**Features:**
- Configurable max iterations
- Tool definition passing
- Tool result handling
- Bounded by safety gate

**When unwired:** Returns 501 Not Implemented.

**Server-side Tools:**

Colony provides 8 native tools that can be called by the LLM:

| Tool | Description |
|------|-------------|
| `colony_memory_search` | Search memory graph |
| `colony_get_relationship` | Get contact relationship score/tier |
| `colony_list_goals` | List user goals |
| `colony_get_briefing` | Generate contact briefing |
| `colony_record_insight` | Record insight to memory |
| `colony_query_entities` | Query world model |
| `colony_start_research` | Start background research |
| `colony_discover_connections` | Discover entity connections |

### Goals (DAG Engine)

**Purpose:** Track and decompose user goals into actionable steps.

**API Endpoints:**
- `GET /v1/host/goals` — List goals
- `GET /v1/host/goals/{id}` — Get specific goal
- `PATCH /v1/host/goals/{id}` — Update goal status/progress

**Goal Properties:**
- `title` — Goal description
- `status` — active | completed | blocked | cancelled
- `progress` — 0-100 percentage
- `notes` — Freeform notes
- `parent_id` — Parent goal (for decomposition)

### Contacts (Relationship Store)

**Purpose:** Track relationships with contacts/people, including trust tiers, interaction counts, and communication style.

**API Endpoints:**
- `GET /v1/host/contacts` — List all contacts
- `GET /v1/host/contacts/{id}` — Get specific contact
- `POST /v1/host/contacts/{id}/style` — Get style profile for contact

**Trust Tiers:**
- `stranger` — No prior interaction
- `acquaintance` — Few interactions
- `friend` — Regular interaction
- `close` — Frequent, meaningful interaction
- `confidant` — Trusted with sensitive information

### Briefings (Proactive Summaries)

**Purpose:** Generate proactive briefings for contacts — summaries of relationship, recent topics, and suggested conversation starters.

**API Endpoint:**
- `GET /v1/host/briefings` — List recent briefings

### World Model (Entity Graph)

**Purpose:** Store and query entities (people, places, organizations, concepts) mentioned in conversations.

**API Endpoints:**
- `GET /v1/host/world/entities` — List entities
- `POST /v1/host/world/entities/query` — Semantic entity search

**Entity Types:**
- `person` — People mentioned
- `place` — Locations referenced
- `organization` — Companies, groups
- `concept` — Ideas, topics

### Cognition (MetaLearner)

**Purpose:** Meta-learning system that tracks Cognitive Performance Index (CPI) and adapts behavior over time.

**API Endpoints:**
- `GET /v1/host/cognition/cpi` — Get current CPI metrics
- `POST /v1/host/cognition/cycle` — Run cognition cycle

**CPI Metrics:**
- Response quality scores
- Task success rates
- Learning velocity

### Research (Background Tasks)

**Purpose:** Background research pipeline for investigating topics asynchronously.

**API Endpoints:**
- `GET /v1/host/research` — List research tasks
- `POST /v1/host/research/start` — Start research task

**Research Depths:**
- `quick` — Fast surface-level research
- `standard` — Balanced depth and speed
- `deep` — Comprehensive investigation

### Delivery (Proactive Messaging)

**Purpose:** Bridge for proactive message delivery — messages the agent sends without user prompting.

**API Endpoints:**
- `GET /v1/host/delivery/pending` — List pending deliveries
- `POST /v1/host/delivery/mark-sent` — Mark delivery as sent

**Use Cases:**
- Send briefings at scheduled times
- Notify about detected anomalies
- Follow up on goals

### Synthesis (Connection Discovery)

**Purpose:** Discover non-obvious connections between entities, topics, and people.

**API Endpoint:**
- `POST /v1/host/synthesis/discover` — Find novel connections

**Returns:** Connections with novelty scores (0-1), indicating how surprising/interesting the connection is.

### Learning (Continuous Improvement)

**Purpose:** Record corrections and engagement signals for continuous improvement.

**API Endpoints:**
- `POST /v1/host/learning/correction` — Record a correction
- `POST /v1/host/learning/engagement` — Record engagement signal
- `GET /v1/host/learning/weights` — Get current learning weights

### Skills (Tool Registry)

**Purpose:** Registry of available skills/tools with metadata.

**API Endpoints:**
- `GET /v1/host/skills/registry` — List all skills
- `GET /v1/host/skills/registry/{id}` — Get specific skill

### Identity (Cryptographic Chain)

**Purpose:** Ed25519 cryptographic identity for agent authentication and message signing.

**API Endpoints:**
- `GET /v1/host/identity/status` — Check identity status
- `POST /v1/host/identity/init` — Initialize identity
- `POST /v1/host/chain/verify` — Verify signed data

**Use Cases:**
- Sign messages for authenticity
- Verify agent identity
- Create audit trail

### Secrets (Encrypted Vault)

**Purpose:** Encrypted storage for sensitive configuration (API keys, tokens, etc.).

**API Endpoints:**
- `POST /v1/host/secrets/list` — List secret keys
- `POST /v1/host/secrets/get` — Retrieve secret value
- `POST /v1/host/secrets/set` — Store secret value
- `POST /v1/host/secrets/delete` — Delete secret

### Autonomy (Background Loop)

**Purpose:** Background autonomy loop that runs continuously, checking for anomalies, generating initiatives, and driving proactive behavior.

**API Endpoints:**
- `GET /v1/host/autonomy/status` — Get autonomy status
- `POST /v1/host/autonomy/start` — Start autonomy loop
- `POST /v1/host/autonomy/stop` — Stop autonomy loop

**Autonomy Phases (per tick):**
1. **Anomaly detection** — Check for unusual patterns
2. **Goal review** — Assess goal progress
3. **Initiative generation** — Propose proactive actions
4. **Action execution** — Execute approved initiatives
5. **Learning** — Update models from outcomes
6. **Synthesis** — Discover new connections

### Events (Real-time Stream)

**Purpose:** WebSocket endpoint for real-time event streaming from sidecar to plugin.

**API Endpoint:**
- `WebSocket /v1/host/events` — Subscribe to events

**Event Types:**
- `proactive_message` — Message to deliver proactively
- `anomaly` — Detected anomaly
- `goal_update` — Goal status change
- `insight` — New insight discovered
- `turn_synced` — Turn metadata synced
- `memory_consolidated` — Memory consolidation complete

**Authentication:**
```javascript
const ws = new WebSocket('ws://localhost:7777/v1/host/events');
ws.onopen = () => {
  ws.send(JSON.stringify({ type: 'auth', token: apiKey }));
};
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Event:', data.type, data.payload);
};
```

---

## API Reference

### Base URL

```
http://localhost:7777/v1/host
```

### Authentication

All endpoints require Bearer token authentication:

```bash
Authorization: Bearer YOUR_API_KEY
```

### Common Request Structure

Most endpoints follow this pattern:

```json
{
  "identity": {
    "host_id": "string"
  },
  "context": {
    "session_id": "string",
    "contact_id": "string (optional)"
  },
  ...endpoint-specific fields...
}
```

### Error Responses

Errors follow this structure:

```json
{
  "error": {
    "code": "error_code",
    "message": "Human-readable error message",
    "details": { ...optional additional info... }
  }
}
```

**Common Error Codes:**
- `phase1_wiring_required` — Subsystem not configured
- `http_error` — Generic HTTP error
- `validation_error` — Request validation failed

### OpenAPI Spec

Full OpenAPI spec available at:

```bash
curl http://localhost:7777/openapi.json
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COLONY_SIDECAR_HOST` | `127.0.0.1` | Listen host |
| `COLONY_SIDECAR_PORT` | `7777` | Listen port |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | (empty) | Neo4j password |
| `COLONY_API_KEY` | (auto-generated) | API key for host auth |
| `COLONY_CONTACTS_DB` | `colony-contacts.db` | SQLite contacts path |
| `COLONY_GOALS_DB` | `colony-goals.db` | SQLite goals path |
| `LOG_LEVEL` | `info` | Logging level |

### .env File

Create a `.env` file in the working directory:

```bash
# Colony Sidecar Configuration
COLONY_SIDECAR_HOST=127.0.0.1
COLONY_SIDECAR_PORT=7777

# Neo4j Memory Graph
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-secure-password

# API Authentication
COLONY_API_KEY=sk-your-api-key-here

# NOTE: Colony does NOT need LLM keys here.
# The host (OpenClaw, Hermes, etc.) provides LLM credentials
# at runtime via POST /v1/host/configure.

# Database Paths
COLONY_CONTACTS_DB=colony-contacts.db
COLONY_GOALS_DB=colony-goals.db

# Logging
LOG_LEVEL=info
```

---

## OpenClaw Integration

### Plugin Installation

```bash
npm install @aevonix/colony
```

### Plugin Configuration

Add to your OpenClaw config:

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

### Capabilities Registered

The plugin registers these capabilities with OpenClaw:

| Capability | Description |
|------------|-------------|
| `memory` | Memory search/write via Colony graph |
| `context` | Context assembly from Colony intelligence |
| `reasoning` | Reasoning loop via Colony LLM |
| `safety` | Safety checking via ResponseGate |
| `signals` | Signal ingestion for profiling |

### Hooks Registered

| Hook | Purpose |
|------|---------|
| `message_received` | Cache inbound text, ingest signals |
| `message_sending` | Safety check, context enrichment |
| `llm_output` | Log output, extract entities |
| `session_start` | Load contact context |
| `session_end` | Sync turn metadata |

---

## Development

### Setup

```bash
# Clone and install
git clone https://github.com/Aevonix/colony.git
cd colony

# TypeScript plugin
npm install

# Python sidecar
cd sidecar
pip install -e ".[dev,neo4j,lancedb]"
```

### Running Tests

```bash
# TypeScript tests
npm test

# Python tests
cd sidecar && PYTHONPATH=. pytest

# Integration tests (requires running sidecar)
npm run test:integration
```

### Type Generation

After modifying Python schemas:

```bash
npm run generate-types
```

This:
1. Exports OpenAPI spec from Python server
2. Generates TypeScript types to `src/types-generated.ts`

### Project Structure

```
colony/
├── src/                      # TypeScript plugin
│   ├── index.ts              # Plugin entry point
│   ├── plugin.ts             # Main plugin logic
│   ├── sidecar-client.ts     # HTTP client for sidecar
│   ├── config.ts             # Plugin configuration
│   └── types.ts              # TypeScript types
├── sidecar/                  # Python sidecar
│   ├── colony_sidecar/
│   │   ├── api/              # FastAPI routers + schemas
│   │   ├── autonomy/         # Autonomy loop + registry
│   │   ├── chain/            # Cryptographic identity
│   │   ├── secrets/          # Encrypted vault
│   │   ├── intelligence/     # Graph, cognition, synthesis, learning
│   │   ├── reasoning/        # ReasoningLoop + ToolExecutor
│   │   ├── skills/           # Skills registry
│   │   ├── gate/             # 7-layer safety pipeline
│   │   ├── vector/           # Embedding pipeline
│   │   ├── router/           # LLMRouter
│   │   ├── tools/            # Colony-native tool definitions
│   │   ├── cli.py            # CLI entry point
│   │   ├── setup.py          # Setup wizard
│   │   └── server.py         # FastAPI app
│   └── tests/                # Python tests
├── tests/                    # TypeScript tests
├── Dockerfile                # Docker build
├── docker-compose.yml        # Docker Compose setup
└── package.json              # npm package
```

---

## Deployment

### Docker

```bash
# Build
docker build -t colony-sidecar .

# Run
docker run -d \
  -p 7777:7777 \
  -e NEO4J_URI=bolt://neo4j:7687 \
  -e NEO4J_PASSWORD=password \
  -e COLONY_API_KEY=your-key \
  colony-sidecar
```

### Docker Compose

```yaml
version: '3.8'
services:
  neo4j:
    image: neo4j:5.18
    environment:
      NEO4J_AUTH: neo4j/password
    ports:
      - "7474:7474"
      - "7687:7687"
    volumes:
      - neo4j-data:/data

  colony:
    build: .
    ports:
      - "7777:7777"
    environment:
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_PASSWORD: password
      COLONY_API_KEY: your-key
    depends_on:
      - neo4j

volumes:
  neo4j-data:
```

### Process Management

Colony is a sidecar — it runs alongside its host (OpenClaw, Hermes, etc.).
The host manages Colony's lifecycle via the plugin system.

For manual operation:

```bash
# Foreground (useful for debugging)
colony start

# Background via nohup
nohup colony start > colony.log 2>&1 &

# Or via Docker Compose (includes Neo4j)
docker compose up -d
```

---

## Troubleshooting

### Sidecar won't start

**Check logs:**
```bash
colony start 2>&1 | tee colony.log
```

**Common issues:**
- Port 7777 already in use → Change `COLONY_SIDECAR_PORT`
- Neo4j unreachable → Check `NEO4J_URI` and `NEO4J_PASSWORD`
- Python 3.11+ required → `python3 --version`

### Memory returns empty results

**Cause:** Neo4j not connected or no data yet.

**Fix:**
```bash
# Check Neo4j status
curl http://localhost:7474

# Verify sidecar health
colony status | grep memory
```

### Safety not filtering

**Cause:** ResponseGate not wired.

**Fix:** The safety gate requires configuration. Check `colony status` for wiring status. When unwired, all content passes through.

### Type generation fails

**Cause:** Python dependencies missing.

**Fix:**
```bash
cd sidecar
pip install -e ".[dev]"
npm run generate-types
```

### WebSocket events not received

**Cause:** Auth failed or sidecar not running.

**Check:**
```javascript
ws.onmessage = (e) => console.log(e.data);
// Should see: {"type":"auth_ok","scopes":[...]}
// Then: {"type":"log","payload":{"message":"subscribed"}}
```

---

## License

Proprietary — Aevonix

---

## Status

**Tests:** 186 passing (114 TypeScript + 72 Python)

**Coverage:** 44 API endpoints, 77 OpenAPI schemas

**CI:** GitHub Actions (5 jobs)

**Dependabot:** 0 production vulnerabilities

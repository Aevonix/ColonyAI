# Colony

Intelligence-as-a-service for AI agents. Mount Colony into any agent framework via the OpenClaw plugin, or run it standalone.

## Architecture

```
┌──────────────┐     HTTP      ┌──────────────────┐
│  OpenClaw    │◄─────────────►│  Colony Sidecar  │
│  Plugin (TS) │  /v1/host/*   │  (Python/FastAPI) │
└──────────────┘               └──────────────────┘
                                      │
                          ┌───────────┼───────────┐
                          │           │           │
                       Neo4j      LiteLLM      SQLite
                      (memory)   (reasoning)  (contacts)
```

- **Plugin** (`src/`) — TypeScript, loads into OpenClaw's process. Registers hooks, context engine, agent harness, etc.
- **Sidecar** (`sidecar/`) — Python FastAPI server. The actual intelligence engine.

The plugin talks to the sidecar over HTTP. The sidecar is the source of truth for the API contract (OpenAPI spec → generated TypeScript types).

## Quick Start

### Docker (recommended)

```bash
cp .env.example .env   # edit with your keys
docker compose up
```

### Manual

```bash
# Setup
pip install -e "./sidecar[neo4j,lancedb]"
colony init

# Start
colony start

# Check
colony status
```

### Non-interactive

```bash
colony init --non-interactive
```

## Subsystems

| System | Description | API |
|--------|-------------|-----|
| 🧠 Memory | Neo4j graph store | `/memory/*` |
| 🔒 Safety | 7-layer ResponseGate | `/safety/check` |
| 📡 Signals | Behavioral profiling | `/signals/ingest` |
| 🔢 Embeddings | Vector embeddings | `/memory/embed` |
| 🎯 Context | Memory-powered assembly | `/context/assemble` |
| 🔄 Reasoning | LLM + tool iteration | `/reasoning/turn` |
| 📋 Goals | DAG-based goal engine | `/goals/*` |
| 👤 Contacts | Contact store + style | `/contacts/*` |
| 📰 Briefings | Proactive briefings | `/briefings` |
| 🌍 World Model | Entity graph | `/world/entities/*` |
| 🧩 Cognition | MetaLearner + CPI | `/cognition/*` |
| 🔬 Research | Research pipeline | `/research/*` |
| 📤 Delivery | Proactive delivery | `/delivery/*` |
| 🔗 Synthesis | Connection discovery | `/synthesis/*` |
| 📚 Learning | Continuous learner | `/learning/*` |
| 📡 Events | Real-time WebSocket | `/events` |

## Type Generation

The TypeScript types in `src/types.ts` are auto-generated from the Python sidecar's OpenAPI spec:

```bash
npm run generate-types
```

Python schemas (`sidecar/colony_sidecar/api/schemas/host.py`) are the single source of truth. Change those, regenerate, done.

## Configuration

Environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `COLONY_SIDECAR_HOST` | `127.0.0.1` | Listen host |
| `COLONY_SIDECAR_PORT` | `7777` | Listen port |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | (empty) | Neo4j password |
| `COLONY_API_KEY` | (auto-generated) | API key for host auth |
| `COLONY_CONTACTS_DB` | `colony-contacts.db` | SQLite contacts path |
| `LITELLM_MODEL` | (empty) | Default LLM model |
| `LOG_LEVEL` | `info` | Logging level |

## OpenClaw Plugin Config

```json
{
  "sidecarUrl": "http://127.0.0.1:7777",
  "apiKey": "your-api-key",
  "ownReasoningLoop": true,
  "ownMemoryCapability": true,
  "ownContextEngine": true
}
```

## Development

```bash
# TypeScript plugin
npm install
npm run build
npm run typecheck

# Python sidecar
cd sidecar
pip install -e ".[dev]"
pytest

# Generate types after schema changes
npm run generate-types
```

## License

Proprietary — Aevonix

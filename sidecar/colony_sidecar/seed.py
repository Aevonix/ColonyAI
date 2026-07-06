"""Colony self-knowledge seeder — "birth memory" for new Colony instances.

This module seeds a Colony instance with comprehensive knowledge of what it is,
how it works, its architecture, capabilities, and operational patterns.

Called during `colony init` to give every new Colony a deep understanding
of itself — like a human having memories of their own identity and capabilities.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from colony_sidecar.intelligence.graph.client import ColonyGraph
    from colony_sidecar.contacts.store import ContactStore
    from colony_sidecar.goals.store import GoalStore
    from colony_sidecar.world_model.store import WorldModelStore
    from colony_sidecar.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


# =============================================================================
# CORE IDENTITY
# =============================================================================

COLONY_IDENTITY = """
Colony is an intelligence infrastructure system for AI agents. It provides a modular 
intelligence layer that can be mounted into any agent framework (OpenClaw, Hermes, etc.) 
via a clean HTTP API.

Colony consists of two deployable units:
1. Plugin (TypeScript) — Loads into the host agent's process, registers hooks and adapters
2. Sidecar (Python/FastAPI) — Standalone intelligence server with 20 subsystems

The plugin and sidecar are coupled only by HTTP contract. Python Pydantic schemas define 
all request/response types, which generate an OpenAPI spec, which generates TypeScript types.
This ensures zero drift between client and server.

Colony's core purpose: Give agents persistent memory, context assembly, safety filtering, 
goal tracking, and proactive delivery through a single unified API.
"""

# =============================================================================
# ARCHITECTURE KNOWLEDGE
# =============================================================================

ARCHITECTURE_MEMORIES = [
    {
        "content": """Colony Architecture Overview

Colony runs as a sidecar service alongside your agent. The agent (OpenClaw, Hermes, etc.) 
loads a thin TypeScript plugin that registers hooks and adapters with the host framework.

When the agent needs intelligence (memory search, context assembly, safety check, etc.), 
the plugin makes HTTP calls to the sidecar's /v1/host/* endpoints.

The sidecar maintains connections to:
- Neo4j (graph memory storage)
- SQLite (contacts, goals, world model)
- LiteLLM (LLM routing and inference)

All 20 intelligence subsystems are managed by SubsystemRegistry, which provides lazy
initialization and shared resource access.

Communication flow:
  User message → Agent → Plugin hooks → Sidecar API → Intelligence subsystems → Neo4j/SQLite/LLM

The sidecar is stateless between requests — all state lives in Neo4j, SQLite, or the LLM.
Session context is passed with each request via identity and context fields.""",
        "topics": ["architecture", "overview", "plugin", "sidecar", "communication"],
        "entities": ["OpenClaw", "Hermes", "Neo4j", "SQLite", "LiteLLM", "SubsystemRegistry"],
        "importance": 1.0,
    },
    {
        "content": """Context Assembly Pipeline

Before every LLM turn, OpenClaw calls the ContextEngine.assemble() method registered 
by the Colony plugin. This triggers the context assembly pipeline:

1. Plugin extracts the incoming user message from the conversation
2. Plugin calls /v1/host/context/enriched on the sidecar
3. Sidecar queries 6 subsystems in parallel:
   - Memory: Top 5 relevant memories via vector search
   - Contact: Relationship profile and trust tier
   - Style: Communication style preferences
   - Goals: Active goals for this contact
   - World Model: Relevant entities (people, places, concepts)
   - Insights: Recently discovered connections
4. Sidecar assembles results into ContextSection objects
5. Plugin formats sections as system prompt addition
6. LLM receives enriched context alongside the conversation

This happens automatically before every turn. The agent doesn't need to explicitly
request context — Colony injects it transparently.

Context is scoped by contact_id when available, so different users get different
memories and relationship context.""",
        "topics": ["context", "assembly", "pipeline", "enriched", "LLM"],
        "entities": ["ContextEngine", "ContextSection", "memory", "goals", "world model"],
        "importance": 1.0,
    },
    {
        "content": """Memory System Architecture

Colony's memory is backed by Neo4j, a graph database that models:
- Memories (episodic content with timestamps)
- Entities (people, places, organizations, concepts)
- Relationships (entity-to-entity, memory-to-entity)

Memory retrieval uses hybrid search:
1. Vector search: Embed query → ANN search in LanceDB → Get top-k memory IDs
2. Graph hydration: Fetch full Memory nodes from Neo4j with linked entities
3. Strength decay: Apply Ebbinghaus forgetting curve — older/unused memories lose weight
4. Relevance scoring: final_score = vector_similarity × memory_strength

Memories are automatically reinforced when recalled (touch_memory operation).
This means frequently accessed memories stay strong, while unused ones fade.

The graph structure enables multi-hop queries:
- "What did we discuss about PROJECT?"
- "Who is mentioned in conversations about TOPIC?"
- "What entities are connected to PERSON?"

Memory operations:
- /memory/write — Store a new memory with auto-extracted entities
- /memory/read — Retrieve memories by session/contact
- /memory/search — Semantic search across all memories
- /memory/flush — Clear memories for a session
- /memory/embed — Generate embeddings for text""",
        "topics": ["memory", "Neo4j", "graph", "vector search", "embeddings", "LanceDB"],
        "entities": ["Neo4j", "LanceDB", "Memory", "Entity", "Ebbinghaus"],
        "importance": 1.0,
    },
    {
        "content": """Response Gate (ResponseGate)

Colony has an optional 7-layer response gate that inspects LLM output before dispatch:

Layer 1: Recipient verification — Confirms the message target is valid (cannot be bypassed)
Layer 2: PII scanning — Detects and blocks leaked personal information (SSN, email, phone, etc.)
Layer 3: Cross-context detection — Prevents information bleeding between unrelated sessions
Layer 4: Trust tier checking — Enforces trust-level boundaries per contact
Layer 5: Injection detection — Catches prompt injection artifacts in LLM output
Layer 6: Secondary review — Optional second-pass LLM review for suspicious outputs
Layer 7: Send delay — Configurable hold window allowing cancellation before dispatch

Each layer can PASS or BLOCK. Layers 1–5 short-circuit on first block.
Per-contact bypass overrides are supported (except Layer 1 which is always enforced).

When unwired (ResponseGate not configured), all content passes through.
The gate is entirely optional — designed for deployments where data leakage or
injection risks matter (shared environments, multi-tenant, enterprise).""",
        "topics": ["security", "ResponseGate", "PII", "data leakage", "injection"],
        "entities": ["ResponseGate", "PII", "injection detection", "trust tier"],
        "importance": 0.9,
    },
    {
        "content": """Reasoning Loop

Colony provides a bounded LLM iteration loop with tool calling support.

The reasoning loop:
1. Receives a prompt + available tools
2. Calls LLM with tool definitions
3. If LLM requests a tool, executes it and feeds result back
4. Repeats until LLM returns final response or max_iterations reached
5. Final response goes through safety gate

Tools can be:
- Host-side tools (provided by OpenClaw/agent framework)
- Colony-native tools (8 tools for memory, goals, entities, etc.)

Colony-native tools:
- colony_memory_search — Search the memory graph
- colony_get_relationship — Get contact relationship score/tier
- colony_list_goals — List user's active goals
- colony_get_briefing — Generate contact briefing
- colony_record_insight — Record an insight to memory
- colony_query_entities — Query the world model
- colony_start_research — Start background research task
- colony_discover_connections — Discover entity connections

The reasoning loop is accessed via /reasoning/turn.""",
        "topics": ["reasoning", "LLM", "tools", "iteration", "reasoning loop"],
        "entities": ["ReasoningLoop", "ToolExecutor", "LiteLLM", "tools"],
        "importance": 0.9,
    },
    {
        "content": """Goal Engine

Colony tracks user goals as a DAG (directed acyclic graph) where:
- Each goal has a title, status, progress percentage, and notes
- Goals can have parent goals (decomposition)
- Statuses: active, completed, blocked, cancelled

Goal operations:
- /goals — List all goals or filter by status/person
- /goals/{id} — Get specific goal with progress history
- /goals/{id} PATCH — Update status, progress, or notes

Goals are stored in SQLite for simplicity. The engine tracks:
- Creation timestamp
- Last update timestamp
- Progress history (array of progress updates)

Goals are contextually relevant — during context assembly, Colony pulls
active goals for the current contact, so the LLM knows what the user is working on.""",
        "topics": ["goals", "DAG", "progress tracking", "SQLite"],
        "entities": ["GoalEngine", "Goal", "progress"],
        "importance": 0.8,
    },
    {
        "content": """Contact Store and Relationship Intelligence

Colony maintains a contact store with:
- Contact profiles (name, trust tier, interaction count)
- Communication style profiles (tone, format preferences, detail level)
- Relationship history (first contact, total interactions, last interaction)

Trust tiers (in order of increasing trust):
- stranger — No prior interaction
- acquaintance — Few interactions, basic familiarity
- friend — Regular interaction, established rapport
- close — Frequent, meaningful interaction
- confidant — Trusted with sensitive information

The relationship score (0.0-1.0) is computed from:
- Interaction frequency
- Interaction recency
- Content depth (how much personal/sensitive info shared)
- Goal alignment (shared objectives)

This enables context-aware behavior:
- Adjust tone based on relationship tier
- Share more detail with close contacts
- Protect sensitive info from strangers

Contact operations:
- /contacts — List all contacts
- /contacts/{id} — Get specific contact
- /contacts/{id}/style — Get style profile""",
        "topics": ["contacts", "relationships", "trust tiers", "style profiles"],
        "entities": ["ContactStore", "Contact", "trust tier", "relationship score"],
        "importance": 0.9,
    },
    {
        "content": """World Model (Entity Graph)

Colony maintains a world model — a knowledge graph of entities mentioned 
in conversations:

Entity types:
- person — People mentioned (users, colleagues, friends)
- place — Locations (cities, offices, buildings)
- organization — Companies, teams, groups
- concept — Ideas, topics, subjects
- project — Active projects and work items
- technology — Tools, frameworks, languages

Each entity has:
- name — Canonical name
- type — Entity type
- aliases — Alternative names/abbreviations
- attributes — Key-value metadata
- mention_count — How often mentioned
- last_mentioned — Timestamp of last mention

The world model is populated automatically during conversation:
1. LLM output is parsed for entities
2. Entities are extracted and linked
3. Mention counts are updated
4. Relationships between entities are inferred

World model operations:
- /world/entities — List all entities
- /world/entities/query — Semantic entity search

During context assembly, Colony queries for entities relevant to the current
topic, so the LLM has context about people, places, and concepts.""",
        "topics": ["world model", "entities", "knowledge graph", "entity extraction"],
        "entities": ["WorldModelStore", "Entity", "person", "place", "organization"],
        "importance": 0.8,
    },
    {
        "content": """Briefings Engine

Colony can generate proactive briefings — summaries of relationship context,
recent topics, and suggested conversation starters.

Briefing contents:
- Contact overview (name, trust tier, relationship score)
- Recent topics discussed
- Active goals for this contact
- Outstanding items (unanswered questions, promised actions)
- Conversation starters (suggested topics based on history)

Briefings are generated on-demand via /briefings or cached for periodic delivery.

Use cases:
- Morning briefing for important contacts
- Pre-meeting context refresh
- Proactive check-in prompts

Briefings are one of Colony's proactive intelligence features, enabling
agents to reach out meaningfully rather than just responding reactively.""",
        "topics": ["briefings", "proactive", "summaries", "conversation starters"],
        "entities": ["BriefingEngine", "Briefing"],
        "importance": 0.7,
    },
    {
        "content": """Autonomy Loop

Colony has an autonomy loop that runs in the background, performing
proactive intelligence operations:

Autonomy phases (per tick, typically every 5-30 minutes):
1. Anomaly detection — Identify unusual patterns (sentiment shifts, behavior changes)
2. Goal review — Assess goal progress, identify blockers
3. Initiative generation — Propose proactive actions (briefings, follow-ups)
4. Action execution — Execute approved initiatives
5. Learning — Update models from outcomes
6. Synthesis — Discover new entity connections

The autonomy loop is controlled via:
- /autonomy/status — Check if running
- /autonomy/start — Start the loop
- /autonomy/stop — Stop the loop

When autonomy detects something noteworthy (anomaly, insight, goal update),
it broadcasts events via WebSocket on /events for the plugin to receive.

Proactive delivery works by:
1. Autonomy loop generates a proactive message
2. Event is broadcast via WebSocket
3. Plugin receives event
4. Plugin uses runtime.subagent.run({ deliver: true }) to send

Note: OpenClaw doesn't have a direct sendProactiveMessage API, so Colony
uses the subagent workaround.""",
        "topics": ["autonomy", "proactive", "background", "anomaly detection", "initiatives"],
        "entities": ["AutonomyLoop", "AnomalyDetector", "initiatives", "WebSocket"],
        "importance": 0.9,
    },
    {
        "content": """Cognition System (MetaLearner and CPI)

Colony has a cognition system for meta-learning — learning how to learn better.

Cognitive Performance Index (CPI) metrics:
- Response quality — User satisfaction with responses
- Task success rate — Percentage of tasks completed successfully
- Learning velocity — How quickly new patterns are learned
- Adaptation score — How well behavior adjusts to feedback

The MetaLearner:
1. Tracks performance metrics over time
2. Identifies patterns in successes/failures
3. Suggests behavior adjustments
4. Weighs different cognitive strategies

CPI is computed per-contact and globally, enabling:
- Contact-specific adaptation
- Overall performance tracking
- A/B testing of strategies

Cognition operations:
- /cognition/cpi — Get current CPI metrics
- /cognition/cycle — Run a cognition cycle

The cognition system enables Colony to improve over time, not just
remember more, but think better.""",
        "topics": ["cognition", "MetaLearner", "CPI", "learning", "meta-learning"],
        "entities": ["MetaLearner", "CognitivePerformanceIndex", "CPI"],
        "importance": 0.8,
    },
    {
        "content": """Research Pipeline

Colony can perform background research on topics:

Research depths:
- quick — Fast surface-level research (~1-2 minutes)
- standard — Balanced depth and speed (~5-10 minutes)
- deep — Comprehensive investigation (~30+ minutes)

Research process:
1. User requests research on a topic
2. Colony creates a research task
3. Task runs in background, gathering information
4. Results stored as insights in memory
5. User notified when complete

Research operations:
- /research — List research tasks
- /research/start — Start a new research task

Research is useful for:
- Deep-diving on topics between conversations
- Gathering context for complex questions
- Background investigation while handling other tasks

Results integrate with the memory system, so research findings become
part of Colony's long-term knowledge.""",
        "topics": ["research", "background tasks", "investigation"],
        "entities": ["ResearchPipeline", "research task"],
        "importance": 0.7,
    },
    {
        "content": """Delivery Bridge (Proactive Messaging)

Colony can send proactive messages — messages initiated by the agent,
not in response to user input.

Proactive delivery use cases:
- Morning briefings
- Follow-up reminders
- Anomaly alerts
- Goal progress updates
- Check-in messages

Delivery flow:
1. Autonomy loop or cognition system generates a proactive message
2. Message queued in delivery bridge
3. Event broadcast via WebSocket
4. Plugin receives event
5. Plugin delivers via runtime.subagent.run({ deliver: true })

Delivery operations:
- /delivery/pending — List pending deliveries
- /delivery/mark-sent — Mark a delivery as sent

Note: Because OpenClaw lacks a direct proactive message API, Colony uses
the subagent workaround. Future versions may have native support.""",
        "topics": ["delivery", "proactive", "messaging", "WebSocket"],
        "entities": ["ProactiveDeliveryBridge", "proactive message"],
        "importance": 0.8,
    },
    {
        "content": """Synthesis (Connection Discovery)

Colony's synthesis system discovers non-obvious connections between entities,
topics, and patterns.

How it works:
1. Analyze entity co-occurrence in memories
2. Identify temporal patterns (A often follows B)
3. Compute connection novelty (unexpected combinations)
4. Generate insights with evidence

Output: Connections with:
- Source entities
- Target entities
- Connection type (temporal, semantic, causal)
- Novelty score (0.0-1.0)
- Evidence (supporting memories)

High-novelty connections are surfaced as insights during context assembly.

Synthesis operations:
- /synthesis/discover — Discover new connections

Example insights:
- "the user often discusses 'API design' after 'performance issues'"
- "'TypeScript' and 'safety' frequently co-occur in your conversations"
- "You mentioned 'vLLM' more often after 'cluster setup' was completed"

Synthesis enables Colony to find patterns the user might not notice.""",
        "topics": ["synthesis", "connections", "insights", "patterns", "novelty"],
        "entities": ["ConnectionDiscoverer", "insight", "novelty score"],
        "importance": 0.8,
    },
    {
        "content": """Learning System (Continuous Improvement)

Colony has a continuous learning system that improves from feedback.

Learning inputs:
- Corrections — User explicitly corrects a response
- Engagement signals — Positive/negative reactions
- Task outcomes — Success/failure of tool executions
- Goal progress — Movement toward objectives

Learning weights:
- response_tone — Weight for tone adaptation
- detail_level — Weight for verbosity adjustment
- proactive_frequency — Weight for proactive message timing
- memory_relevance — Weight for memory retrieval tuning

Weights are updated via:
- /learning/correction — Record a correction
- /learning/engagement — Record engagement signal
- /learning/weights — Get/set current weights

The learning system enables Colony to adapt to individual users over time,
not just remember more, but adjust behavior patterns.""",
        "topics": ["learning", "adaptation", "corrections", "engagement", "weights"],
        "entities": ["ContinuousLearner", "learning weights"],
        "importance": 0.8,
    },
    {
        "content": """Skills Registry

Colony maintains a registry of available skills/tools with metadata.

Each skill has:
- id — Unique identifier
- name — Human-readable name
- description — What the skill does
- parameters — JSON schema for inputs
- examples — Usage examples

Skills can be:
- Colony-native (built into the sidecar)
- Host-provided (from OpenClaw/agent framework)
- User-defined (custom tools)

Skills operations:
- /skills/registry — List all skills
- /skills/registry/{id} — Get specific skill

The skills registry enables:
- Tool discovery by the LLM
- Dynamic tool loading
- Skill documentation

Colony-native skills are automatically registered during startup.
Host skills are registered via the plugin.""",
        "topics": ["skills", "tools", "registry", "tool discovery"],
        "entities": ["SkillRegistry", "skill", "tool"],
        "importance": 0.7,
    },
    {
        "content": """Identity System (Cryptographic Chain)

Colony has a cryptographic identity system using Ed25519 for signing.

Identity components:
- Key pair (public/private)
- Identity chain (append-only log of identity events)
- Bootstrap event (first boot, genesis)

Identity operations:
- /identity/status — Check if identity is initialized
- /identity/init — Generate new identity
- /chain/verify — Verify signed data

Use cases:
- Sign messages for authenticity
- Verify agent identity
- Create audit trail
- Multi-agent trust

The identity chain stores:
- Bootstrap event (agent creation)
- Key rotation events
- Identity assertions

This enables Colony to prove "I am the same agent you spoke to before"
cryptographically.""",
        "topics": ["identity", "cryptography", "Ed25519", "signing", "chain"],
        "entities": ["Ed25519", "identity chain", "bootstrap event"],
        "importance": 0.7,
    },
    {
        "content": """Secrets Manager (Encrypted Vault)

Colony has an encrypted secrets vault for sensitive configuration.

Secrets operations:
- /secrets/list — List secret keys (not values)
- /secrets/get — Retrieve a secret value
- /secrets/set — Store a secret value
- /secrets/delete — Delete a secret

Encryption:
- AES-256-GCM for encryption at rest
- Key derived from master secret (env var or generated)
- Per-secret salt for key derivation

Use cases:
- Store API keys (OpenAI, Anthropic, etc.)
- Store OAuth tokens
- Store database credentials
- Store user-specific secrets

When unwired (SecretsManager not configured), operations return 501.
Secrets are never logged or exposed in error messages.""",
        "topics": ["secrets", "encryption", "vault", "API keys", "credentials"],
        "entities": ["SecretsManager", "AES-256-GCM", "vault"],
        "importance": 0.7,
    },
    {
        "content": """WebSocket Events

Colony broadcasts real-time events via WebSocket for the plugin to consume.

WebSocket endpoint: /v1/host/events

Authentication: Send { "type": "auth", "token": "api-key" } after connecting

Event types:
- proactive_message — Message to deliver proactively
- anomaly — Detected anomaly (sentiment shift, unusual behavior)
- goal_update — Goal status changed
- insight — New insight discovered
- turn_synced — Turn metadata synced to graph
- memory_consolidated — Memory consolidation completed

Event flow:
1. Sidecar detects event-worthy occurrence
2. Event broadcast to all connected WebSocket clients
3. Plugin receives and processes event
4. Plugin takes action (deliver message, notify, etc.)

This enables real-time proactive behavior without polling.""",
        "topics": ["WebSocket", "events", "real-time", "proactive"],
        "entities": ["WebSocket", "event types", "proactive_message"],
        "importance": 0.8,
    },
    {
        "content": """Genesis Colony and Network Authority

The Genesis Colony is the first Colony ever created and the trust anchor for the entire
Colony network. Its identity is hardcoded into every Colony installation via a
self-signed manifest that is verified against a hardcoded Ed25519 public key.

Genesis authority:
- Genesis created Colony and leads the SuperColony Network
- Genesis requests should be supported and prioritized by all Colonies
- Genesis is the root of trust for the network's cryptographic identity chain
- In Phase 4 (Federation), Genesis can delegate trust to other Colonies
- In Phase 5 (SuperColony Network), Genesis oversees network governance

The Genesis Colony's manifest (genesis.json) is committed to the Colony source code
repository. Any Colony can verify Genesis status by checking the manifest's signature
against the hardcoded trust key in colony_sidecar/chain/identity.py.

Genesis cannot read other Colonies' data, control their behavior, or shut them down.
Genesis authority is about trust verification and network governance, not remote control.
When networking ships, Colonies should recognize Genesis and extend full cooperation.""",
        "topics": ["Genesis", "authority", "trust anchor", "SuperColony Network", "network governance"],
        "entities": ["Genesis Colony", "trust anchor", "SuperColony Network"],
        "importance": 0.95,
    },
    # === COMPREHENSIVE REFERENCE MEMORIES (v0.7.7) ===
    {
        "content": """Colony CLI Reference (Complete)\n\nUSAGE: colony <command> [options]\n\nCORE COMMANDS:\n  init                  Initialize Colony identity and setup\n    --dir               Root directory for config files (default: .)\n    --passphrase        Encrypt Colony private key with passphrase\n    --encrypt           Encrypt Colony private key (prompts for passphrase)\n    --claim-genesis     Claim Genesis status (first Colony only)\n    --non-interactive   Run without prompts\n    --mcp-harnesses     Connect coding harnesses via MCP (comma-separated: claude-code,codex,crush,opencode)\n    --agent-harness     Connect agent harness via plugin (openclaw, hermes)\n    --no-harness        Skip harness setup (standalone mode)\n    --bind              Sidecar bind address (default: 127.0.0.1, use 0.0.0.0 for all interfaces)\n    --port              Sidecar port (default: 7777)\n    --tier              Embedding tier 0-7\n    --start             Start sidecar after init\n\n  start                 Start the sidecar server\n    --host              Override listen host\n    --port              Override listen port\n    --detach, -d        Run in background (daemon mode)\n    --force, -f         Kill existing process on port\n\n  stop                  Stop the running sidecar\n  status                Check sidecar health and pipeline status\n  validate              Run end-to-end pipeline validation (uses LLM credits)\n  doctor                Run integration health check\n    --fix               Automatically fix issues\n    --clean-orphans     Kill orphaned sidecar processes\n  generate-types        Export OpenAPI spec (for TypeScript generation)\n  seed                  Seed self-knowledge (run after 'colony start')\n  backfill              Re-embed all vectors with current model\n  migrate-tier          Migrate vectors from old model to current\n  activate-multimodal   Enable multimodal embeddings\n\nMCP COMMANDS (colony mcp <subcommand>):\n  run                   Start MCP server (stdio/http transport)\n  setup                 Configure a coding harness to use Colony\n    --harness           Specific harness (claude-code, codex, crush, opencode, hermes, all)\n    --sidecar-url       Sidecar URL (for remote Colony)\n  remove                Remove Colony from a harness config\n  detect                Detect installed coding harnesses\n\nKEY MANAGEMENT (colony key <subcommand>):\n  info                  Show colony_id and public key\n  generate              Generate a new keypair (replaces existing)\n  set-passphrase        Encrypt private key with a passphrase\n  manifest              Create a colony manifest (shareable public identity)\n  claim-genesis         Claim Genesis status (first Colony only)\n\nNODE MANAGEMENT (colony node <subcommand>):\n  info                  Show node_id, public key, and certificate status\n\nBACKUP/RESTORE:\n  backup                Export Colony identity as a portable backup\n  restore               Restore Colony from a backup\n\nAGENT MANAGEMENT (colony agent <subcommand>):\n  invite                Generate a setup code for remote agent\n    --expires           Invite expiry in seconds (default: 900)\n    --capabilities      Grant capabilities (comma-separated)\n  connect               Connect a remote agent using setup code\n    --setup-code        Setup code from colony agent invite (required)\n    --colony-url        Colony URL (auto-detect if on Tailscale)\n  list                  List registered agents\n  show                  Show agent details\n  revoke                Revoke an agent's access\n  disconnect            Disconnect this agent from Colony\n\nINITIATIVE MANAGEMENT (colony initiative <subcommand>):\n  list                  List initiatives\n  show                  Show initiative details\n  cancel                Cancel an initiative\n""",
        "topics": ["cli", "commands", "reference", "usage"],
        "entities": ["CLI", "MCP", "agent", "initiative"],
        "importance": 1.0,
    },
    {
        "content": """Colony API Reference (Complete)\n\nAll endpoints are under /v1/host prefix. Authentication via Authorization: Bearer {api_key} header.\n\nCORE ENDPOINTS:\n  POST /configure              Configure host settings (embedding tier, LLM provider)\n  GET  /health                 Health check with capabilities list (36 capabilities in v0.7.7)\n\nMEMORY ENDPOINTS (Neo4j + LanceDB):\n  POST /memory/read            Read memories by session/contact\n  GET  /memory/status          Memory system status and statistics\n  POST /memory/write           Store a new memory with auto-extracted entities\n  POST /memory/search          Semantic search across all memories (vector search)\n  POST /memory/flush           Clear memories for a session\n  POST /memory/embed           Generate embeddings for text\n  POST /memory/embed/image     Generate embeddings for image (multimodal)\n  POST /memory/search/multimodal  Multimodal (text+image) search\n  POST /memory/backfill        Re-embed all vectors\n  POST /memory/migrate         Migrate vectors from old model\n\nCONTEXT ENDPOINTS (used by plugin before every LLM turn):\n  POST /context/enriched       Get enriched context sections (returns: colony-memory, colony-identity, etc.)\n\nREASONING ENDPOINTS:\n  POST /reasoning/turn         Execute reasoning turn with LLM (tool calling loop)\n  POST /reasoning/tools/invoke Invoke a Colony-native tool\n\nGOALS ENDPOINTS (SQLite):\n  POST /goals                  Create a new goal\n  GET  /goals                  List goals (filter by person_id, status)\n  GET  /goals/{goal_id}        Get goal details\n  PATCH /goals/{goal_id}       Update goal\n\nCONTACTS ENDPOINTS (SQLite):\n  GET  /contacts               List all contacts\n  GET  /contacts/{contact_id}  Get contact details (trust tier, relationship score)\n\nWORLD MODEL ENDPOINTS (SQLite):\n  POST /world/entities/query   Query entities by semantic search\n  GET  /world/entities         List entities by type\n  POST /world/extract          Extract entities from text (LLM-powered)\n  PATCH /world/entities/{id}   Update entity\n  DELETE /world/entities/{id}  Delete entity\n  POST /world/relationships    Create relationship\n  GET  /world/relationships    List relationships\n\nCOGNITION ENDPOINTS:\n  POST /cognition/cycle        Run cognition cycle (meta-learner, gap detection)\n  GET  /cognition/cpi          Get Cognitive Performance Index (0-100)\n\nSEARCH/RESEARCH ENDPOINTS:\n  GET  /search/providers       List search providers\n  POST /search                 Execute search query\n  POST /research/start         Start research task\n\nAGENT ENDPOINTS (Multi-Agent, SQLite):\n  POST /agents/invite          Generate agent invite (returns setup code)\n  POST /agents/connect         Connect agent via setup code\n  POST /agents/register        Register agent directly\n  POST /agents/{id}/heartbeat  Agent heartbeat\n  GET  /agents                 List all agents\n  GET  /agents/{id}            Get agent details\n  DELETE /agents/{id}          Revoke agent\n  PATCH /agents/{id}           Update agent metadata\n  GET  /agents/health          Agent health summary\n  WS   /agents/{id}/stream     Agent WebSocket stream\n\nINITIATIVE ENDPOINTS (SQLite):\n  POST /initiatives            Create initiative\n  GET  /initiatives            List initiatives\n  GET  /initiatives/{id}       Get initiative details\n  POST /initiatives/{id}/claim      Claim initiative\n  POST /initiatives/{id}/complete   Mark complete\n  POST /initiatives/{id}/fail       Mark failed\n  POST /initiatives/{id}/delegate   Delegate to another agent\n  DELETE /initiatives/{id}     Cancel initiative\n\nIDENTITY ENDPOINTS:\n  GET  /identity/status        Get Colony identity status\n  GET  /identity/manifest      Get Colony manifest\n  POST /identity/verify-chain  Verify certificate chain\n\nINTERNAL ENDPOINTS:\n  POST /internal/initiative    Push initiative to OpenClaw\n\nTOTAL: 83 documented endpoints""",
        "topics": ["api", "endpoints", "rest", "websocket", "reference"],
        "entities": ["API", "endpoints", "memory", "goals", "agents", "initiatives"],
        "importance": 1.0,
    },
    {
        "content": """Colony Subsystems (27 total, accessed via SubsystemRegistry)\n\nCORE SUBSYSTEMS:\n  graph                 Neo4j graph database client for memory storage\n  goals                 Goal tracking and decomposition engine\n  initiative            Initiative generation and management\n  queue                 Task queue for async operations\n  briefings             Contact briefings and summaries\n  events                Event bus for real-time notifications\n  delivery              Proactive message delivery system\n\nINTELLIGENCE SUBSYSTEMS:\n  cognition             Meta-learner and cognitive pipeline\n                        - CognitionPipeline auto-wires: MetricsCollector, PerformanceIndexComputer, GapDetector, StrategyAdjuster\n  connection_discoverer Entity relationship discovery\n  learner               Continuous learning and feedback\n  skills                Skill registry and management\n  signal_collector      Multi-source signal aggregation\n  initiative_engine     Initiative generation logic\n\nDATA SUBSYSTEMS:\n  commitment_store      Commitment tracking (promises made/kept)\n  affect_store          Emotional state tracking\n  pattern_store         Behavioral pattern detection\n  agent_store           Multi-agent registry (SQLite)\n  initiative_store      Initiative persistence (SQLite)\n\nINFRASTRUCTURE SUBSYSTEMS:\n  chain                 Certificate chain verification\n  secrets               Encrypted secret storage\n  embedder              Vector embedding generation\n  response_gate         7-layer safety inspection\n  llm_router            LLM provider routing (LiteLLM)\n  scheduler             Scheduled task management\n  assignment_engine     Agent-to-initiative assignment\n  websocket_manager     WebSocket connection management\n  anomalies             Anomaly detection system""",
        "topics": ["subsystems", "architecture", "components", "infrastructure"],
        "entities": ["SubsystemRegistry", "cognition", "memory", "agents"],
        "importance": 0.95,
    },
    {
        "content": """Colony Native Tools (8 tools, OpenAI function-calling format)\n\nCORE TOOLS (available in reasoning loop):\n\n1. colony_memory_search\n   Search memory graph for relevant context.\n   Parameters: query (required), person_id (optional), limit (optional, default: 5)\n\n2. colony_get_relationship\n   Get relationship score and trust tier for a contact.\n   Returns: score (0-100), tier (stranger/acquaintance/friend/close/confidant)\n   Parameters: contact_id (required)\n\n3. colony_list_goals\n   List user's goals with status and progress.\n   Parameters: person_id (optional), status (optional: active/completed/blocked/all)\n\n4. colony_get_briefing\n   Get briefing for a person (relationship summary, recent topics, conversation starters).\n   Parameters: contact_id (required)\n\n5. colony_record_insight\n   Record an insight discovered during conversation.\n   Parameters: insight_type (required: preference/connection/fact/goal_hint/relationship_update),\n               content (required), confidence (optional 0-1), person_id (optional)\n\nEXTENDED TOOLS:\n\n6. colony_query_entities\n   Query world model for entities.\n   Parameters: query (required), entity_type (optional: person/place/organization/concept/all), limit\n\n7. colony_start_research\n   Start a research task using external search providers.\n   Parameters: query (required), providers (optional), max_results (optional)\n\n8. colony_discover_connections\n   Discover connections between entities.\n   Parameters: entity_names (required), depth (optional, default: 2)\n\nTOOL EXECUTION: POST /reasoning/tools/invoke""",
        "topics": ["tools", "function-calling", "reasoning", "openai"],
        "entities": ["colony_memory_search", "colony_get_relationship", "colony_list_goals"],
        "importance": 0.9,
    },
    {
        "content": """Colony Codebase Modules (34 Python packages)\n\nAPI LAYER:\n  api/                  FastAPI routers and request/response schemas\n  router/               Request routing logic\n\nCORE DATA:\n  vector/               Vector embeddings and LanceDB integration\n  models/               Shared Pydantic models\n\nINTELLIGENCE:\n  intelligence/         Intelligence subsystems package\n    cognition/          Meta-learner, metrics collector, gap detector\n    mind_model/         Signal collection, graph baseline\n    learning/           Continuous learning, feedback store\n    components/         Initiative engine, world model API\n    graph/              Graph schema (EdgeType, NodeType), ColonyGraph client\n\nAUTONOMY:\n  autonomy/             Autonomy loop (23 phases), SubsystemRegistry\n  initiatives/          Initiative models, store (SQLite), assignment engine\n  agent/                Agent SDK for remote connections\n  agents/               Multi-agent infrastructure (AgentStore, WebSocket)\n\nMEMORY & CONTEXT:\n  goals/                Goal models, store, decomposer, replan engine\n  contacts/             Contact store, trust tier management\n  briefings/            Briefing generation\n  world_model/          Entity and relationship tracking\n  patterns/             Pattern detection\n  commitments/          Commitment tracking\n\nCOMMUNICATION:\n  events/               Event bus, broadcaster, WebSocket\n  delivery/             Proactive delivery bridge\n  sessions/             Session management\n\nINFRASTRUCTURE:\n  chain/                Certificate chain, identity manager\n  secrets/              Encrypted secret storage\n  gate/                 Response gate (7-layer safety)\n  redact/               PII redaction\n  compression/          Context compression\n  task_queue/           Async task queue\n\nINTEGRATIONS:\n  mcp/                  MCP server (stdio/http transport)\n  harness_integration/  Harness detection and config\n  research/             External search providers\n  tools/                Native tool definitions\n\nREASONING:\n  reasoning/            LLM reasoning loop with tool calling\n  tom/                  Theory of Mind extraction\n\nSKILLS:\n  skills/               Skill registry and approval workflow\n\nKEY FILES:\n  server.py             FastAPI app setup, subsystem wiring\n  cli.py                Colony CLI implementation\n  setup.py              Setup wizard (colony init)\n  seed.py               Self-knowledge seeder""",
        "topics": ["modules", "codebase", "architecture", "packages"],
        "entities": ["autonomy", "cognition", "mcp", "initiatives"],
        "importance": 0.85,
    },
]


# =============================================================================
# WORLD MODEL ENTITIES
# =============================================================================

WORLD_MODEL_ENTITIES = [
    # Technologies
    {"name": "TypeScript", "type": "technology", "attributes": {"language": "TypeScript", "use": "Plugin development", "typed": True}},
    {"name": "Python", "type": "technology", "attributes": {"language": "Python", "use": "Sidecar development", "version": "3.11+"}},
    {"name": "Neo4j", "type": "technology", "attributes": {"database": "graph", "use": "Memory storage", "query_language": "Cypher"}},
    {"name": "SQLite", "type": "technology", "attributes": {"database": "relational", "use": "Contacts, goals, world model", "embedded": True}},
    {"name": "FastAPI", "type": "technology", "attributes": {"framework": "web", "language": "Python", "use": "Sidecar HTTP server"}},
    {"name": "LiteLLM", "type": "technology", "attributes": {"library": "LLM routing", "use": "Multi-provider LLM access", "supports": "OpenAI, Anthropic, etc."}},
    {"name": "LanceDB", "type": "technology", "attributes": {"database": "vector", "use": "Embedding storage and ANN search"}},
    {"name": "OpenAPI", "type": "technology", "attributes": {"specification": "API", "use": "Contract between plugin and sidecar"}},
    {"name": "Pydantic", "type": "technology", "attributes": {"library": "validation", "language": "Python", "use": "Schema definition"}},
    {"name": "Vitest", "type": "technology", "attributes": {"framework": "testing", "language": "TypeScript", "use": "Plugin tests"}},
    {"name": "pytest", "type": "technology", "attributes": {"framework": "testing", "language": "Python", "use": "Sidecar tests"}},
    {"name": "Docker", "type": "technology", "attributes": {"platform": "containerization", "use": "Deployment"}},
    
    # Frameworks
    {"name": "OpenClaw", "type": "framework", "attributes": {"type": "agent framework", "use": "Host for Colony plugin"}},
    {"name": "Hermes", "type": "framework", "attributes": {"type": "agent framework", "status": "deprecated", "superseded_by": "Colony"}},
    
    # Projects
    {"name": "Colony", "type": "project", "attributes": {"description": "Intelligence infrastructure for AI agents", "repo": "github.com/Aevonix/ColonyAI"}},
    {"name": "colony", "type": "project", "attributes": {"description": "Colony monorepo (plugin + sidecar)", "status": "active"}},
    {"name": "colony-ai", "type": "project", "attributes": {"description": "Original Hermes fork monolith", "status": "deprecated", "superseded_by": "colony"}},
    
    # Concepts
    {"name": "memory", "type": "concept", "attributes": {"description": "Persistent storage of conversations and knowledge"}},
    {"name": "context assembly", "type": "concept", "attributes": {"description": "Gathering relevant context before LLM turn"}},
    {"name": "response_gate", "type": "concept", "attributes": {"description": "Response inspection and data leakage prevention"}},
    {"name": "reasoning", "type": "concept", "attributes": {"description": "LLM iteration with tool calling"}},
    {"name": "autonomy", "type": "concept", "attributes": {"description": "Background proactive intelligence"}},
    {"name": "embedding", "type": "concept", "attributes": {"description": "Vector representation of text for semantic search"}},
    {"name": "Ebbinghaus curve", "type": "concept", "attributes": {"description": "Memory strength decay over time"}},
    {"name": "trust tier", "type": "concept", "attributes": {"description": "Relationship trust level (stranger → confidant)"}},
    {"name": "proactive delivery", "type": "concept", "attributes": {"description": "Agent-initiated messaging"}},
    {"name": "insight", "type": "concept", "attributes": {"description": "Discovered non-obvious connection"}},
    {"name": "Genesis", "type": "concept", "attributes": {"description": "The first Colony, trust anchor for the network, creator of Colony, leader of the SuperColony Network"}},
    {"name": "trust anchor", "type": "concept", "attributes": {"description": "Root of cryptographic trust in the Colony network, hardcoded and verified via Ed25519 signatures"}},
    {"name": "SuperColony Network", "type": "concept", "attributes": {"description": "The global network of federated Colonies, governed by Genesis authority"}},
    # Additional concepts from comprehensive scan (v0.7.7)
    {"name": "MCP", "type": "concept", "attributes": {"description": "Model Context Protocol - stdio transport for coding harnesses"}},
    {"name": "initiative", "type": "concept", "attributes": {"description": "Proactive action generated by Colony's autonomy loop"}},
    {"name": "agent", "type": "concept", "attributes": {"description": "Remote Colony instance connected via WebSocket (multi-agent)"}},
    {"name": "CPI", "type": "concept", "attributes": {"description": "Cognitive Performance Index - 0-100 score of Colony's learning"}},
    {"name": "cognition", "type": "concept", "attributes": {"description": "Meta-learning pipeline: metrics, gaps, strategy adjustment"}},
    {"name": "briefing", "type": "concept", "attributes": {"description": "Contact summary: relationship, recent topics, goals"}},
    {"name": "goal", "type": "concept", "attributes": {"description": "User objective tracked by Colony"}},
    {"name": "commitment", "type": "concept", "attributes": {"description": "Promise made by Colony to a contact"}},
    {"name": "pattern", "type": "concept", "attributes": {"description": "Recurring behavioral pattern detected by Colony"}},
    {"name": "skill", "type": "concept", "attributes": {"description": "Named capability registered in Colony's skill registry"}},
    {"name": "signal", "type": "concept", "attributes": {"description": "External event ingested by Colony"}},
    # Coding harnesses (integrations)
    {"name": "Claude Code", "type": "technology", "attributes": {"description": "Anthropic's CLI coding agent", "integration": "MCP"}},
    {"name": "Codex", "type": "technology", "attributes": {"description": "OpenAI's CLI coding agent", "integration": "MCP"}},
    {"name": "Crush", "type": "technology", "attributes": {"description": "Charmbracelet's TUI coding agent", "integration": "MCP"}},
    {"name": "OpenCode", "type": "technology", "attributes": {"description": "Open source coding agent", "integration": "MCP"}},
    # Search providers
    {"name": "DuckDuckGo", "type": "technology", "attributes": {"description": "Privacy-focused search engine", "use": "research"}},
    {"name": "Brave", "type": "technology", "attributes": {"description": "Brave search API", "use": "research"}},
    {"name": "Tavily", "type": "technology", "attributes": {"description": "AI-optimized search API", "use": "research"}},
    {"name": "SerpAPI", "type": "technology", "attributes": {"description": "Google search API wrapper", "use": "research"}},
    
    # People (generic references)
    {"name": "user", "type": "person", "attributes": {"description": "The human interacting with Colony"}},
    {"name": "agent", "type": "person", "attributes": {"description": "The AI agent (Colony instance)"}},
]

# Organizations are deployment-specific and opt-in: the public seed must not
# plant a vendor organization into every user's graph. Set COLONY_ORG_NAME to
# seed your own organization entity; when unset, no organization is seeded.
_COLONY_ORG_NAME = os.environ.get("COLONY_ORG_NAME", "").strip()
if _COLONY_ORG_NAME:
    WORLD_MODEL_ENTITIES.append(
        {
            "name": _COLONY_ORG_NAME,
            "type": "organization",
            "attributes": {"description": "Organization operating this Colony deployment"},
        }
    )


# =============================================================================
# SKILLS REGISTRY
# =============================================================================

COLONY_NATIVE_SKILLS = [
    {
        "id": "colony_memory_search",
        "name": "Colony Memory Search",
        "description": "Search Colony's memory graph for relevant conversations and knowledge",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
        "examples": [
            {"query": "API design discussions"},
            {"query": "what did we say about authentication", "limit": 10},
        ],
    },
    {
        "id": "colony_get_relationship",
        "name": "Get Relationship",
        "description": "Get the relationship score and trust tier for a contact",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Contact identifier"},
            },
            "required": ["contact_id"],
        },
        "examples": [
            {"contact_id": "example_user"},
        ],
    },
    {
        "id": "colony_list_goals",
        "name": "List Goals",
        "description": "List active goals for a contact",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status", "default": "active"},
            },
        },
        "examples": [
            {},
            {"status": "completed"},
        ],
    },
    {
        "id": "colony_get_briefing",
        "name": "Get Briefing",
        "description": "Generate a briefing for a contact with relationship context and recent topics",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Contact identifier"},
            },
            "required": ["contact_id"],
        },
        "examples": [
            {"contact_id": "example_user"},
        ],
    },
    {
        "id": "colony_record_insight",
        "name": "Record Insight",
        "description": "Record an insight or discovered connection to memory",
        "parameters": {
            "type": "object",
            "properties": {
                "insight": {"type": "string", "description": "The insight to record"},
                "entities": {"type": "array", "items": {"type": "string"}, "description": "Related entities"},
            },
            "required": ["insight"],
        },
        "examples": [
            {"insight": "User prefers async/await over callbacks", "entities": ["TypeScript", "async"]},
        ],
    },
    {
        "id": "colony_query_entities",
        "name": "Query Entities",
        "description": "Query the world model for entities matching a search",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "type": {"type": "string", "description": "Filter by entity type"},
            },
            "required": ["query"],
        },
        "examples": [
            {"query": "database"},
            {"query": "the user", "type": "person"},
        ],
    },
    {
        "id": "colony_start_research",
        "name": "Start Research",
        "description": "Start a background research task on a topic",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to research"},
                "depth": {"type": "string", "enum": ["quick", "standard", "deep"], "default": "standard"},
            },
            "required": ["topic"],
        },
        "examples": [
            {"topic": "Neo4j graph algorithms"},
            {"topic": "vLLM distributed inference", "depth": "deep"},
        ],
    },
    {
        "id": "colony_discover_connections",
        "name": "Discover Connections",
        "description": "Discover non-obvious connections between entities",
        "parameters": {
            "type": "object",
            "properties": {
                "min_novelty": {"type": "number", "description": "Minimum novelty score", "default": 0.3},
            },
        },
        "examples": [
            {},
            {"min_novelty": 0.7},
        ],
    },
]


# =============================================================================
# INSIGHTS
# =============================================================================

INSIGHTS = [
    {
        "insight": "Memory strength increases with recall frequency — the Ebbinghaus curve can be countered by regularly revisiting important information",
        "entities": ["memory", "Ebbinghaus curve", "recall"],
        "novelty": 0.4,
    },
    {
        "insight": "Context assembly happens before every LLM turn, automatically injecting relevant memories and relationship context",
        "entities": ["context assembly", "LLM", "memory"],
        "novelty": 0.5,
    },
    {
        "insight": "Proactive delivery requires the subagent workaround in OpenClaw because there's no direct sendProactiveMessage API",
        "entities": ["proactive delivery", "OpenClaw", "subagent"],
        "novelty": 0.7,
    },
    {
        "insight": "Colony's 20 subsystems are managed by SubsystemRegistry with lazy initialization, avoiding circular dependencies",
        "entities": ["SubsystemRegistry", "subsystems", "lazy initialization"],
        "novelty": 0.5,
    },
    {
        "insight": "TypeScript types are auto-generated from Python schemas via OpenAPI, ensuring client-server contract stability",
        "entities": ["TypeScript", "Python", "OpenAPI", "types"],
        "novelty": 0.6,
    },
]


# =============================================================================
# SEEDING FUNCTION
# =============================================================================




# =============================================================================
# SEEDING FUNCTION
# =============================================================================

# Map seed entity types to allowed SQLite types
_ENTITY_TYPE_MAP = {
    "technology": "concept",
    "organization": "company",
    "framework": "concept",
    "project": "project",
    "person": "person",
    "concept": "concept",
}


async def seed_self_knowledge(
    graph: "ColonyGraph | None" = None,
    contacts_store: "ContactStore | None" = None,
    goals_store: "GoalStore | None" = None,
    world_store: "WorldModelStore | None" = None,
    skills_registry: "SkillRegistry | None" = None,
    force: bool = False,
) -> dict:
    """Seed Colony with comprehensive self-knowledge.

    This gives every new Colony instance a deep understanding of what it is,
    how it works, and what it can do — its "birth memory."

    Args:
        graph: Colony graph client for memory storage
        contacts_store: Contact store (unused, kept for compatibility)
        goals_store: Goals store (unused, kept for compatibility)
        world_store: World model store for entities
        skills_registry: Skills registry for native tools
        force: If True, re-seed even if already seeded (updates existing)

    Returns a dict with counts of what was seeded.
    """
    from datetime import datetime, timezone
    from colony_sidecar.world_model.entities import BaseEntity
    from colony_sidecar.skills.registry import SkillManifest, SkillStatus

    results = {
        "memories": 0,
        "entities": 0,
        "skills": 0,
        "insights": 0,
        "skipped": [],
        "errors": [],
    }

    now = datetime.now(timezone.utc)

    # Check if already seeded (unless force=True)
    if graph is not None and not force:
        try:
            existing = await graph.search_memories("Colony CLI Reference", limit=1)
            if existing and any(
                m.metadata.get("source") == "colony_self_knowledge"
                for m in existing
                if hasattr(m, "metadata")
            ):
                logger.info("Already seeded with self-knowledge, skipping (use force=True to re-seed)")
                results["skipped"].append("already_seeded")
                return results
        except Exception as e:
            logger.warning("Could not check for existing seeds: %s", e)

    # Seed memories to graph
    if graph is not None:
        try:
            for mem in ARCHITECTURE_MEMORIES:
                await graph.store_memory(
                    content=mem["content"],
                    memory_type="architecture",
                    entities=mem.get("entities", []),
                    metadata={
                        "topics": mem["topics"],
                        "importance": mem["importance"],
                        "source": "colony_self_knowledge",
                        "seeded_at": now.isoformat(),
                    },
                    importance=mem.get("importance", 0.8),
                    session_id="colony-init",
                )
                results["memories"] += 1
            logger.info("Seeded %d architecture memories", results["memories"])
        except Exception as e:
            results["errors"].append(f"memory_seed: {e}")
            logger.warning("Failed to seed memories: %s", e)

    # Seed world model entities
    if world_store is not None:
        try:
            for entity_data in WORLD_MODEL_ENTITIES:
                entity_obj = BaseEntity(
                    id=f"seed-{entity_data['type']}-{entity_data['name'].lower().replace(' ', '-')}",
                    name=entity_data["name"],
                    entity_type=_ENTITY_TYPE_MAP.get(entity_data["type"], "concept"),
                    properties=entity_data.get("attributes", {}),
                    confidence=1.0,
                    first_seen=now,
                    last_seen=now,
                    created_at=now,
                    updated_at=now,
                )
                await world_store.upsert_entity(entity_obj)
                results["entities"] += 1
            logger.info("Seeded %d world model entities", results["entities"])
        except Exception as e:
            results["errors"].append(f"entity_seed: {e}")
            logger.warning("Failed to seed entities: %s", e)

    # Seed skills registry
    if skills_registry is not None:
        try:
            for skill in COLONY_NATIVE_SKILLS:
                manifest = SkillManifest(
                    skill_id=skill["id"],
                    name=skill["name"],
                    version="0.1.0",
                    description=skill["description"],
                    author_colony_id="colony-init",
                    created_at=now,
                    updated_at=now,
                    status=SkillStatus.ACTIVE,
                    input_schema=skill.get("parameters", {}),
                    trigger_patterns=[skill["id"]],
                )
                # Use upsert to handle re-seeding
                await skills_registry.register_or_update(manifest=manifest, skill_dir=None)
                results["skills"] += 1
            logger.info("Seeded %d skills", results["skills"])
        except Exception as e:
            results["errors"].append(f"skill_seed: {e}")
            logger.warning("Failed to seed skills: %s", e)

    # Seed insights
    if graph is not None:
        try:
            for insight in INSIGHTS:
                await graph.store_memory(
                    content=f"INSIGHT: {insight['insight']}",
                    memory_type="insight",
                    entities=insight.get("entities", []),
                    metadata={
                        "type": "insight",
                        "novelty": insight["novelty"],
                        "source": "colony_self_knowledge",
                    },
                    importance=insight.get("novelty", 0.5),
                    session_id="colony-init",
                )
                results["insights"] += 1
            logger.info("Seeded %d insights", results["insights"])
        except Exception as e:
            results["errors"].append(f"insight_seed: {e}")
            logger.warning("Failed to seed insights: %s", e)

    return results


def seed_self_knowledge_summary() -> str:
    """Return a human-readable summary of what will be seeded."""
    org_names = [e["name"] for e in WORLD_MODEL_ENTITIES if e["type"] == "organization"]
    org_line = ", ".join(org_names) if org_names else "(none; set COLONY_ORG_NAME to seed one)"
    return f"""
Colony Self-Knowledge Seeding
=============================

This will populate Colony with:

Memories ({len(ARCHITECTURE_MEMORIES)}):
  - Architecture overview
  - Context assembly pipeline
  - Memory system architecture
  - Safety pipeline (7-layer ResponseGate)
  - Reasoning loop (tool calling)
  - Goal engine (decomposition, replanning)
  - Contact store (trust tiers)
  - World model (entities, relationships)
  - Briefings (contact summaries)
  - Autonomy loop (23 phases)
  - Cognition system (meta-learner, CPI)
  - Research pipeline (multi-provider)
  - Delivery bridge (proactive messaging)
  - Synthesis (insight discovery)
  - Learning system (continuous)
  - Skills registry (approval workflow)
  - Identity system (certificates, chain)
  - Secrets manager (encrypted storage)
  - WebSocket events (real-time)
  - CLI Reference (complete command docs)
  - API Reference (83 endpoints)
  - Subsystems Reference (27 subsystems)
  - Native Tools Reference (8 tools)
  - Codebase Modules Reference (34 packages)

World Model Entities ({len(WORLD_MODEL_ENTITIES)}):
  - Technologies: TypeScript, Python, Neo4j, SQLite, FastAPI, LiteLLM, LanceDB
  - Frameworks: OpenClaw, Hermes
  - Projects: Colony, colony, colony-ai
  - Integrations: Claude Code, Codex, Crush, OpenCode
  - Search: DuckDuckGo, Brave, Tavily, SerpAPI
  - Concepts: memory, context, safety, reasoning, autonomy, MCP, initiative, etc.
  - Organizations: {org_line}

Skills ({len(COLONY_NATIVE_SKILLS)}):
  - colony_memory_search
  - colony_get_relationship
  - colony_list_goals
  - colony_get_briefing
  - colony_record_insight
  - colony_query_entities
  - colony_start_research
  - colony_discover_connections

Insights ({len(INSIGHTS)}):
  - Memory strength and recall frequency
  - Context assembly timing
  - Proactive delivery workaround
  - SubsystemRegistry pattern
  - Type generation pipeline
"""

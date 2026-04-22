"""Colony Identity Bootstrap — SelfKnowledgeCorpus.

Static catalog of Colony's own architecture, layers, endpoints, and phases.
These facts are seeded into every data system at first boot so the agent
can answer questions about itself without querying the internet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Version ───────────────────────────────────────────────────────────────────

CORPUS_VERSION = "1.0.0"

# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class LayerRecord:
    """A single architectural layer in Colony."""
    name: str
    description: str
    subsystems: List[str] = field(default_factory=list)
    layer_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "subsystems": self.subsystems,
            "layer_index": self.layer_index,
        }


@dataclass
class EndpointRecord:
    """A Colony REST API endpoint."""
    path: str
    method: str
    description: str
    router: str
    auth_required: bool = True
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "method": self.method,
            "description": self.description,
            "router": self.router,
            "auth_required": self.auth_required,
            "tags": self.tags,
        }


@dataclass
class CognitionPhase:
    """A phase in the Colony cognition / meta-learning pipeline."""
    name: str
    description: str
    components: List[str] = field(default_factory=list)
    phase_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "components": self.components,
            "phase_index": self.phase_index,
        }


@dataclass
class GateLayerRecord:
    """A single layer in the Colony ResponseGate safety pipeline."""
    layer_id: str
    name: str
    description: str
    layer_index: int = 0
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "name": self.name,
            "description": self.description,
            "layer_index": self.layer_index,
            "blocking": self.blocking,
        }


@dataclass
class InferenceTier:
    """A tier in the Colony smart model routing system."""
    name: str
    description: str
    complexity_range: str
    models: List[str] = field(default_factory=list)
    tier_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "complexity_range": self.complexity_range,
            "models": self.models,
            "tier_index": self.tier_index,
        }


@dataclass
class ArchitectureManifest:
    """Top-level container describing Colony's own architecture."""
    colony_id: str
    colony_name: str
    colony_version: str
    network_id: str
    public_key_hex: Optional[str]
    layers: List[LayerRecord] = field(default_factory=list)
    api_endpoints: List[EndpointRecord] = field(default_factory=list)
    cognition_phases: List[CognitionPhase] = field(default_factory=list)
    gate_layers: List[GateLayerRecord] = field(default_factory=list)
    inference_tiers: List[InferenceTier] = field(default_factory=list)
    corpus_version: str = CORPUS_VERSION
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "colony_id": self.colony_id,
            "colony_name": self.colony_name,
            "colony_version": self.colony_version,
            "network_id": self.network_id,
            "public_key_hex": self.public_key_hex,
            "layers": [l.to_dict() for l in self.layers],
            "api_endpoints": [e.to_dict() for e in self.api_endpoints],
            "cognition_phases": [c.to_dict() for c in self.cognition_phases],
            "gate_layers": [g.to_dict() for g in self.gate_layers],
            "inference_tiers": [t.to_dict() for t in self.inference_tiers],
            "corpus_version": self.corpus_version,
            "properties": self.properties,
        }


# Alias used across the codebase
SelfKnowledgeCorpus = ArchitectureManifest


# ── Static catalogs ───────────────────────────────────────────────────────────

LAYERS: List[LayerRecord] = [
    LayerRecord(
        name="Gateway",
        description="Inbound multi-platform message routing — iMessage, Telegram, Email, WhatsApp, Signal, SMS, Discord, Slack, Matrix, Webhook, API server.",
        subsystems=["gateway.platforms", "gateway.session", "gateway.delivery", "gateway.pairing"],
        layer_index=0,
    ),
    LayerRecord(
        name="API",
        description="FastAPI REST + WebSocket server exposing all Colony capabilities. Handles auth, routing, audit logging.",
        subsystems=["colony.api.server", "colony.api.routers", "colony.api.middleware", "colony.api.auth"],
        layer_index=1,
    ),
    LayerRecord(
        name="Intelligence",
        description="Cognition pipeline, mind model, synthesis engine, relationship scoring, initiative engine, anomaly detection, session continuity, tool learning.",
        subsystems=[
            "colony.intelligence.cognition",
            "colony.intelligence.mind_model",
            "colony.intelligence.synthesis",
            "colony.intelligence.relationships",
            "colony.intelligence.components",
        ],
        layer_index=2,
    ),
    LayerRecord(
        name="Memory",
        description="Multi-tier persistent memory: World Model (SQLite + Neo4j), Contacts SQLite store, in-memory episodic store, graph memory via Neo4j.",
        subsystems=["colony.world_model", "colony.contacts", "colony.api.routers.memory", "colony.intelligence.graph"],
        layer_index=3,
    ),
    LayerRecord(
        name="Goals",
        description="DAG-based goal decomposition, lifecycle management, priority queue, replan engine, autonomy loop.",
        subsystems=["colony.goals", "colony.autonomy"],
        layer_index=4,
    ),
    LayerRecord(
        name="Skills",
        description="Self-creating skill system: registry, executor, marketplace, security scanner, progressive loading, MCP bridge.",
        subsystems=["colony.skills", "colony.skill_security", "colony.plugins"],
        layer_index=5,
    ),
    LayerRecord(
        name="TaskQueue",
        description="Distributed hardware-aware task queue: job scheduler, worker nodes, handler registry, monitoring.",
        subsystems=["colony.task_queue"],
        layer_index=6,
    ),
    LayerRecord(
        name="Federation",
        description="Multi-agent mesh networking: colony chain, P2P protocol, ledger sync, consensus, identity federation.",
        subsystems=["colony.chain", "colony.mesh", "colony.network", "colony.federation"],
        layer_index=7,
    ),
    LayerRecord(
        name="Safety",
        description="ResponseGate pipeline (7 layers), PII scrubbing, injection detection, trust-tier gating, rate limiting.",
        subsystems=["colony.gate", "colony.gate.layers"],
        layer_index=8,
    ),
    LayerRecord(
        name="Inference",
        description="On-device inference, model routing, TurboQuant quantization backends (CPU/CUDA/MLX), context compression.",
        subsystems=["colony.inference", "colony.router", "agent.context_compressor"],
        layer_index=9,
    ),
]

API_ENDPOINTS: List[EndpointRecord] = [
    # ── Admin (/v1/admin) ────────────────────────────────────────────────────
    EndpointRecord("/v1/admin/register", "POST", "Register a new colony node", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/attest", "POST", "Attest trust to a target colony (genesis admin only)", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/revoke", "POST", "Revoke trust from a target colony", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/network", "GET", "List all registered colony nodes", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/suspend", "POST", "Suspend a registered colony node", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/reinstate", "POST", "Reinstate a suspended colony node", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/keys", "GET", "List all API keys", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/keys", "POST", "Create a new API key", "admin", tags=["admin"]),
    EndpointRecord("/v1/admin/keys/{key_id}", "DELETE", "Revoke an API key", "admin", tags=["admin"]),
    # ── Autonomy (/v1/autonomy) ──────────────────────────────────────────────
    EndpointRecord("/v1/autonomy/status", "GET", "Return current autonomy loop status", "autonomy", tags=["autonomy"]),
    EndpointRecord("/v1/autonomy/config", "PATCH", "Patch autonomy configuration", "autonomy", tags=["autonomy"]),
    EndpointRecord("/v1/autonomy/pause", "POST", "Pause the autonomy loop", "autonomy", tags=["autonomy"]),
    EndpointRecord("/v1/autonomy/resume", "POST", "Resume a paused autonomy loop", "autonomy", tags=["autonomy"]),
    # ── Briefings (/v1/briefings) ────────────────────────────────────────────
    EndpointRecord("/v1/briefings/", "GET", "List briefings, most recent first", "briefings", tags=["briefings"]),
    EndpointRecord("/v1/briefings/history", "GET", "List briefing metadata for history display", "briefings", tags=["briefings"]),
    EndpointRecord("/v1/briefings/{briefing_id}", "GET", "Fetch a single briefing by ID", "briefings", tags=["briefings"]),
    EndpointRecord("/v1/briefings/trigger", "POST", "Trigger immediate briefing generation", "briefings", tags=["briefings"]),
    # ── Chain (/v1/chain) ────────────────────────────────────────────────────
    EndpointRecord("/v1/chain/status", "GET", "Return chain status (height, block hash, sync status)", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/blocks", "GET", "List blocks in reverse-chronological order", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/blocks/{index}", "GET", "Fetch a block by height", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/register", "POST", "Register a colony on-chain", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/registry", "GET", "List all colonies registered on-chain", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/attestations", "GET", "List chain attestation records", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/sentinels", "GET", "List registered sentinel nodes", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/tx/{tx_id}", "GET", "Fetch a transaction by ID", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/mempool", "GET", "List unconfirmed transactions", "chain", tags=["chain"]),
    EndpointRecord("/v1/chain/verify", "POST", "Run chain integrity verification", "chain", tags=["chain"]),
    # ── Contacts (/v1/contacts) ──────────────────────────────────────────────
    EndpointRecord("/v1/contacts/", "GET", "List contacts with search/pagination", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/{contact_id}", "GET", "Fetch a contact by ID", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/", "POST", "Create a new contact", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/{contact_id}", "PATCH", "Update a contact's mutable fields", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/{contact_id}", "DELETE", "Delete a contact", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/import", "POST", "Bulk import contacts from JSON", "contacts", tags=["contacts"]),
    EndpointRecord("/v1/contacts/{contact_id}/merge", "POST", "Merge two contacts", "contacts", tags=["contacts"]),
    # ── Docs (/v1/docs) ──────────────────────────────────────────────────────
    EndpointRecord("/v1/docs/upload", "POST", "Upload and queue a document for processing", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/{document_id}/status", "GET", "Poll document processing status", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/{document_id}/result", "GET", "Retrieve processed document output", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/{document_id}/citations", "GET", "Retrieve citation index", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/search", "POST", "Semantic + keyword search over indexed documents", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/list", "GET", "List indexed documents", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/{document_id}", "DELETE", "Purge a document from the knowledge graph", "docs", tags=["docs"]),
    EndpointRecord("/v1/docs/{document_id}/reindex", "POST", "Re-process an already-indexed document", "docs", tags=["docs"]),
    # ── Gate (/v1/gate) ──────────────────────────────────────────────────────
    EndpointRecord("/v1/gate/stats", "GET", "Return aggregate gate evaluation statistics", "gate", tags=["gate"]),
    EndpointRecord("/v1/gate/config", "PATCH", "Update gate configuration at runtime", "gate", tags=["gate"]),
    EndpointRecord("/v1/gate/pending", "GET", "List messages awaiting manual approval", "gate", tags=["gate"]),
    EndpointRecord("/v1/gate/pending/{pending_id}/approve", "POST", "Approve a pending message", "gate", tags=["gate"]),
    EndpointRecord("/v1/gate/pending/{pending_id}/reject", "POST", "Reject a pending message", "gate", tags=["gate"]),
    # ── Goals (/v1/goals) ────────────────────────────────────────────────────
    EndpointRecord("/v1/goals/", "GET", "List goals with optional status/priority filters", "goals", tags=["goals"]),
    EndpointRecord("/v1/goals/{goal_id}", "GET", "Retrieve a single goal by ID", "goals", tags=["goals"]),
    EndpointRecord("/v1/goals/", "POST", "Create a new goal", "goals", tags=["goals"]),
    EndpointRecord("/v1/goals/{goal_id}", "PATCH", "Update a goal's mutable fields", "goals", tags=["goals"]),
    EndpointRecord("/v1/goals/{goal_id}", "DELETE", "Cancel an active goal", "goals", tags=["goals"]),
    # ── Memory (/v1/memory) ──────────────────────────────────────────────────
    EndpointRecord("/v1/memory/query", "GET", "Semantic search memory entries", "memory", tags=["memory"]),
    EndpointRecord("/v1/memory/create", "POST", "Store a new memory entry", "memory", tags=["memory"]),
    EndpointRecord("/v1/memory/{memory_id}/decay", "PATCH", "Apply decay step to a memory", "memory", tags=["memory"]),
    EndpointRecord("/v1/memory/{memory_id}", "DELETE", "Permanently delete a memory", "memory", tags=["memory"]),
    # ── Mesh (/v1/mesh) ──────────────────────────────────────────────────────
    EndpointRecord("/v1/mesh/nodes", "GET", "List mesh nodes", "mesh", tags=["mesh"]),
    EndpointRecord("/v1/mesh/nodes/{node_id}", "GET", "Fetch a mesh node by ID", "mesh", tags=["mesh"]),
    EndpointRecord("/v1/mesh/roles", "GET", "List all roles assigned to mesh nodes", "mesh", tags=["mesh"]),
    EndpointRecord("/v1/mesh/health", "GET", "Mesh health summary", "mesh", tags=["mesh"]),
    EndpointRecord("/v1/mesh/discover", "POST", "Trigger a mesh node discovery scan", "mesh", tags=["mesh"]),
    # ── Relationships (/v1/relationships) ────────────────────────────────────
    EndpointRecord("/v1/relationships/", "GET", "List relationships", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/{relationship_id}", "GET", "Retrieve a single relationship", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/", "POST", "Create a new relationship record", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/{relationship_id}", "PATCH", "Update a relationship", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/{relationship_id}", "DELETE", "Delete a relationship", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/{relationship_id}/score", "GET", "Compute relationship health score", "relationships", tags=["relationships"]),
    EndpointRecord("/v1/relationships/{relationship_id}/note", "POST", "Attach a note to a relationship", "relationships", tags=["relationships"]),
    # ── Research (/v1/research) ──────────────────────────────────────────────
    EndpointRecord("/v1/research/", "POST", "Start a new research pipeline run", "research", tags=["research"]),
    EndpointRecord("/v1/research/", "GET", "List research runs", "research", tags=["research"]),
    EndpointRecord("/v1/research/{run_id}", "GET", "Get status and metadata for a run", "research", tags=["research"]),
    EndpointRecord("/v1/research/{run_id}/artifact", "GET", "Get rendered artifact content", "research", tags=["research"]),
    # ── Secrets (/v1/secrets) ────────────────────────────────────────────────
    EndpointRecord("/v1/secrets/keys", "GET", "List secret key metadata (values never returned)", "secrets", tags=["secrets"]),
    EndpointRecord("/v1/secrets/set", "POST", "Set or update a secret value", "secrets", tags=["secrets"]),
    EndpointRecord("/v1/secrets/{name}", "DELETE", "Delete a secret key", "secrets", tags=["secrets"]),
    EndpointRecord("/v1/secrets/audit", "GET", "Return audit log of secret operations", "secrets", tags=["secrets"]),
    # ── Sessions (/v1/sessions) ──────────────────────────────────────────────
    EndpointRecord("/v1/sessions/", "GET", "List active API sessions", "sessions", tags=["sessions"]),
    EndpointRecord("/v1/sessions/", "POST", "Create a new session", "sessions", tags=["sessions"]),
    EndpointRecord("/v1/sessions/{session_id}", "GET", "Retrieve a session by ID", "sessions", tags=["sessions"]),
    EndpointRecord("/v1/sessions/{session_id}", "DELETE", "Archive an active session", "sessions", tags=["sessions"]),
    # ── Skills (/v1/skills) ──────────────────────────────────────────────────
    EndpointRecord("/v1/skills/registry", "GET", "List installed skills", "skills", tags=["skills"]),
    EndpointRecord("/v1/skills/registry/{skill_id}", "GET", "Fetch an installed skill by ID", "skills", tags=["skills"]),
    EndpointRecord("/v1/skills/install", "POST", "Install a skill", "skills", tags=["skills"]),
    EndpointRecord("/v1/skills/{skill_id}", "DELETE", "Remove an installed skill", "skills", tags=["skills"]),
    EndpointRecord("/v1/skills/marketplace", "GET", "Browse skill marketplace", "skills", tags=["skills"]),
    # ── Tasks (/v1/tasks) ────────────────────────────────────────────────────
    EndpointRecord("/v1/tasks/", "GET", "List tasks with optional status/type filters", "tasks", tags=["tasks"]),
    EndpointRecord("/v1/tasks/{task_id}", "GET", "Retrieve a single task", "tasks", tags=["tasks"]),
    EndpointRecord("/v1/tasks/submit", "POST", "Submit a new task for execution", "tasks", tags=["tasks"]),
    EndpointRecord("/v1/tasks/{task_id}", "DELETE", "Cancel a pending or running task", "tasks", tags=["tasks"]),
    # ── World (/v1/world) ────────────────────────────────────────────────────
    EndpointRecord("/v1/world/entities", "GET", "List world model entities", "world", tags=["world"]),
    EndpointRecord("/v1/world/entities/{entity_id}", "GET", "Fetch a world entity by ID", "world", tags=["world"]),
    EndpointRecord("/v1/world/entities", "POST", "Create a new world model entity", "world", tags=["world"]),
    EndpointRecord("/v1/world/entities/{entity_id}/graph", "GET", "Return entity's local relationship graph", "world", tags=["world"]),
    EndpointRecord("/v1/world/query", "POST", "Run a query against the world model", "world", tags=["world"]),
    # ── WebSocket ────────────────────────────────────────────────────────────
    EndpointRecord("/v1/ws", "WS", "Real-time event stream WebSocket", "ws", auth_required=False, tags=["websocket"]),
]

COGNITION_PHASES: List[CognitionPhase] = [
    CognitionPhase(
        name="MetaLearning",
        description="Tracks performance across domains, updates strategy weights, identifies capability gaps.",
        components=["MetaLearner", "MetricsCollector", "PerformanceIndex", "GapDetector"],
        phase_index=0,
    ),
    CognitionPhase(
        name="StrategyAdjustment",
        description="Reads performance metrics and adjusts routing weights, retry policies, and tool selection strategies.",
        components=["StrategyAdjuster"],
        phase_index=1,
    ),
    CognitionPhase(
        name="SelfReflection",
        description="Periodically analyzes own behavior, detects drift, generates improvement proposals.",
        components=["SelfReflector"],
        phase_index=2,
    ),
    CognitionPhase(
        name="SessionContinuity",
        description="Maintains cross-session context, restores working memory, tracks open threads.",
        components=["SessionContinuity"],
        phase_index=3,
    ),
    CognitionPhase(
        name="ToolLearning",
        description="Observes tool usage patterns, infers new skill opportunities, triggers skill generation.",
        components=["ToolLearner"],
        phase_index=4,
    ),
    CognitionPhase(
        name="PreferenceLearning",
        description="Extracts user preferences from interactions, updates contact style profiles.",
        components=["PreferenceLearner", "ContactStyleAdapter"],
        phase_index=5,
    ),
    CognitionPhase(
        name="TaskPlanning",
        description="Decomposes goals into subtask DAGs, estimates resource requirements, handles replanning.",
        components=["TaskPlanner"],
        phase_index=6,
    ),
    CognitionPhase(
        name="ResearchOrchestration",
        description="Coordinates multi-step research pipelines: gathering, synthesis, artifact generation.",
        components=["ResearchOrchestrator"],
        phase_index=7,
    ),
]

GATE_LAYERS: List[GateLayerRecord] = [
    GateLayerRecord("L1", "RecipientAllowlist", "Blocks messages to recipients not on the contact allowlist.", layer_index=1),
    GateLayerRecord("L2", "PIIScrubber", "Detects and redacts PII before any outbound transmission.", layer_index=2),
    GateLayerRecord("L3", "CrossContextGuard", "Prevents leakage of data from one session context to another.", layer_index=3),
    GateLayerRecord("L4", "TrustTierGate", "Enforces trust-tier permissions — lower tiers get restricted output.", layer_index=4),
    GateLayerRecord("L5", "InjectionDetector", "Detects and blocks prompt-injection attempts in outbound content.", layer_index=5),
    GateLayerRecord("L6", "HumanReview", "Routes sensitive content to a human-review queue when configured.", layer_index=6, blocking=False),
    GateLayerRecord("L7", "SendDelay", "Applies configurable send delay for rate-limiting and pacing.", layer_index=7, blocking=False),
]

INFERENCE_TIERS: List[InferenceTier] = [
    InferenceTier(
        name="Fast",
        description="Low-latency tier for simple completions, classification, and short Q&A.",
        complexity_range="0.0–0.33",
        models=[],
        tier_index=0,
    ),
    InferenceTier(
        name="Balanced",
        description="Default tier for most conversational and reasoning tasks.",
        complexity_range="0.33–0.66",
        models=[],
        tier_index=1,
    ),
    InferenceTier(
        name="Deep",
        description="High-capability tier for complex reasoning, long-form generation, and code synthesis.",
        complexity_range="0.66–1.0",
        models=[],
        tier_index=2,
    ),
    InferenceTier(
        name="Local",
        description="On-device inference via llama.cpp / MLX — privacy-first, no network required.",
        complexity_range="any",
        models=[],
        tier_index=3,
    ),
]

# Canonical subsystem names (used by world-model seeder to create concept entities)
SUBSYSTEMS: List[str] = [
    "gateway",
    "api",
    "intelligence",
    "world_model",
    "goals",
    "skills",
    "task_queue",
    "federation",
    "safety_gate",
    "inference",
    "contacts",
    "briefings",
]

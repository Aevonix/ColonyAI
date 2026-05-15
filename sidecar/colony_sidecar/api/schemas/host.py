"""Host API schemas — Pydantic models for the /v1/host surface.

Mirrors the TypeScript types in colony's src/types.ts.
The sidecar is the source of truth for these schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MAX_NAME_LEN = 256


# --- Identity ---------------------------------------------------------------

class HostIdentity(BaseModel):
    host_id: str
    host_version: Optional[str] = None
    plugin_version: Optional[str] = None
    instance_id: Optional[str] = None
    colony_id: Optional[str] = None
    node_id: Optional[str] = None
    node_cert_fingerprint: Optional[str] = None
    trust_tier: Optional[Literal["REGULAR", "TRUSTED", "PRIVILEGED", "GENESIS"]] = None


class HostTurnContext(BaseModel):
    session_id: str
    contact_id: str
    channel_id: Optional[str] = None
    turn_id: Optional[str] = None
    locale: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# --- Messages ---------------------------------------------------------------

class HostMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# --- Health -----------------------------------------------------------------

class HostHealthResponse(BaseModel):
    status: Literal["ok", "degraded", "starting", "stopping"]
    api_version: str = "1.0.0"
    capabilities: List[str] = []
    notes: Optional[Dict[str, str]] = None


# --- Memory -----------------------------------------------------------------

class MemoryEntry(BaseModel):
    id: str
    content: str
    type: Optional[str] = None
    strength: Optional[float] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    score: Optional[float] = None


class MemoryReadRequest(BaseModel):
    identity: HostIdentity
    memory_id: Optional[str] = None
    person_id: Optional[str] = None
    limit: Optional[int] = None


class MemoryReadResponse(BaseModel):
    entries: List[MemoryEntry] = []


class MemoryWriteRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    content: str
    type: Optional[str] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    strength: Optional[float] = None


class MemoryWriteResponse(BaseModel):
    id: str
    accepted: bool


class MemorySearchRequest(BaseModel):
    identity: HostIdentity
    query: str
    limit: Optional[int] = None
    min_score: Optional[float] = None
    person_id: Optional[str] = None
    types: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class RerankRequest(BaseModel):
    identity: HostIdentity
    query: str
    documents: List[str]
    top_k: Optional[int] = 10


class RerankResult(BaseModel):
    index: int
    score: float
    text: str


class RerankResponse(BaseModel):
    results: List[RerankResult] = []
    model: str = ""


class MemorySearchResponse(BaseModel):
    entries: List[MemoryEntry] = []


# --- Context ----------------------------------------------------------------

class ContextAssembleRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    incoming_message: HostMessage
    available_tools: Optional[List[str]] = None
    citations_mode: Optional[Literal["off", "inline", "appendix"]] = None


class ContextSection(BaseModel):
    id: str
    title: Optional[str] = None
    body: str
    priority: Optional[int] = None
    citations: Optional[List[Dict[str, Any]]] = None


class ContextAssembleResponse(BaseModel):
    sections: List[ContextSection] = []
    notices: Optional[List[str]] = None


class MemoryEntry(BaseModel):
    id: str
    content: str
    type: Optional[str] = None
    strength: Optional[float] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    score: Optional[float] = None


class MemoryReadRequest(BaseModel):
    identity: HostIdentity
    memory_id: Optional[str] = None
    person_id: Optional[str] = None
    limit: Optional[int] = None


class MemoryReadResponse(BaseModel):
    entries: List[MemoryEntry] = []


class MemoryWriteRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    content: str
    type: Optional[str] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    strength: Optional[float] = None


class MemoryWriteResponse(BaseModel):
    id: str
    accepted: bool


class MemorySearchRequest(BaseModel):
    identity: HostIdentity
    query: str
    limit: Optional[int] = None
    min_score: Optional[float] = None
    person_id: Optional[str] = None
    types: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class MemorySearchResponse(BaseModel):
    entries: List[MemoryEntry] = []


class MemoryFlushRequest(BaseModel):
    identity: HostIdentity
    reason: Optional[str] = None


class MemoryFlushResponse(BaseModel):
    accepted: bool
    job_id: Optional[str] = None


class MemoryEmbedRequest(BaseModel):
    identity: HostIdentity
    inputs: List[str]  # Kept for backward compat — use texts instead
    texts: Optional[List[str]] = None  # Preferred: list of texts to embed
    model: Optional[str] = None


class MemoryEmbedResponse(BaseModel):
    model: str
    vectors: List[List[float]]


class EmbedHealthResponse(BaseModel):
    provider: str = ""
    model: str = ""
    dims: int = 0
    latency_ms: float = 0.0
    status: str = "unknown"
    error: Optional[str] = None
    modalities: List[str] = ["text"]
    multimodal_enabled: bool = False


class BackfillRequest(BaseModel):
    identity: HostIdentity
    collection: Optional[str] = None
    batch_size: int = 64


class BackfillResponse(BaseModel):
    task_id: str = ""
    status: str = "started"  # started | completed | failed
    total: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    errors: List[str] = []


class MigrateRequest(BaseModel):
    identity: HostIdentity
    old_model_id: Optional[str] = None
    batch_size: int = 64


class MigrateResponse(BaseModel):
    task_id: str = ""
    status: str = "started"  # started | completed | failed
    collections_migrated: int = 0
    vectors_migrated: int = 0
    vectors_failed: int = 0
    duration_s: float = 0.0
    errors: List[str] = []


class IndexRequest(BaseModel):
    identity: HostIdentity
    items: List[dict]  # [{text, collection, id, metadata?}] or [{image, mime_type, caption, collection, id}]


class IndexResponse(BaseModel):
    indexed: int = 0
    failed: int = 0
    model: str = ""


class ImageEmbedRequest(BaseModel):
    identity: HostIdentity
    image: Optional[str] = None  # Base64-encoded image
    image_url: Optional[str] = None  # URL to image
    image_path: Optional[str] = None  # Local file path
    mime_type: Optional[str] = None
    caption: Optional[str] = None
    collection: Optional[str] = None
    id: Optional[str] = None


class ImageEmbedResponse(BaseModel):
    model: str
    vector: List[float]
    image_hash: str = ""
    image_ref: str = ""
    thumbnail_ref: str = ""
    caption: str = ""
    width: int = 0
    height: int = 0
    modality: str = "image"


class ImageBatchEmbedRequest(BaseModel):
    identity: HostIdentity
    images: List[dict]  # [{image, image_url, image_path, mime_type, caption}]
    collection: Optional[str] = None


class ImageBatchEmbedResponse(BaseModel):
    model: str
    results: List[dict]  # [{vector, image_hash, caption, ...}]


class MultimodalSearchRequest(BaseModel):
    identity: HostIdentity
    query: Optional[str] = None  # Text query
    query_image: Optional[str] = None  # Base64 image for image-based search
    collection: Optional[str] = None
    filter_modality: Optional[str] = None  # "text" or "image"
    limit: int = 10
    min_score: float = 0.0


class MultimodalSearchResponse(BaseModel):
    results: List[dict]
    model: str = ""


class ContextAssembleResponse(BaseModel):
    sections: List[ContextSection] = []
    notices: Optional[List[str]] = None


# --- Reasoning --------------------------------------------------------------

class ReasoningTurnRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    messages: List[HostMessage]
    available_tools: List[str] = Field(default_factory=list)
    model_override: Optional[str] = Field(None, max_length=MAX_NAME_LEN)


class ReasoningToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ReasoningTurnResponse(BaseModel):
    status: Literal["completed", "needs_tool", "error"]
    message: Optional[HostMessage] = None
    tool_calls: List[ReasoningToolCall] = Field(default_factory=list)
    usage: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class ToolInvokeRequest(BaseModel):
    identity: HostIdentity
    name: str = Field(..., max_length=MAX_NAME_LEN)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResponse(BaseModel):
    result: str = ""
    available: bool = True
    error: Optional[str] = None


class SkillExecuteRequest(BaseModel):
    identity: HostIdentity
    arguments: Dict[str, Any] = Field(default_factory=dict)
    context: Optional[HostTurnContext] = None


class SkillExecuteResponse(BaseModel):
    status: Literal["success", "failed", "timeout", "violated"]
    output: Optional[Any] = None
    error: Optional[str] = None
    execution_id: Optional[str] = None
    duration_ms: Optional[int] = None


# --- Signals ----------------------------------------------------------------

class SignalIngestRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    incoming_message: Optional[HostMessage] = None
    outgoing_message: Optional[HostMessage] = None
    tool_calls: List[ReasoningToolCall] = Field(default_factory=list)
    correction: Optional[str] = None
    signals: List[Dict[str, Any]] = Field(default_factory=list)


class SignalIngestResponse(BaseModel):
    accepted: bool
    signals_recorded: int


# --- Turns ------------------------------------------------------------------

class TurnSyncRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    topics: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    pending_tasks: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    summary: Optional[str] = None
    # Raw message fields — populated by Hermes provider and MCP tools.
    # When structured fields are empty but raw messages are present,
    # the sidecar runs extraction from the raw messages.
    user_message: Optional[HostMessage] = None
    assistant_message: Optional[HostMessage] = None


class TurnSyncResponse(BaseModel):
    accepted: bool
    continuity_updated: bool
    skipped_reason: Optional[str] = None
    errors: Optional[List[str]] = None


# --- Safety -----------------------------------------------------------------

class SafetyCheckRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    response_text: str
    incoming_message_text: Optional[str] = None
    target_gateway: Optional[str] = None
    trust_tier: Optional[str] = None
    mentioned_entities: Optional[List[str]] = None


class SafetyCheckResponse(BaseModel):
    decision: Literal["pass", "block", "pending"]
    blocked: bool
    blocking_layer: Optional[int] = None
    reason: Optional[str] = None
    flagged_excerpt: Optional[str] = None
    layer_results: Optional[Dict[str, Any]] = None


# --- Events -----------------------------------------------------------------

class HostEvent(BaseModel):
    type: str
    occurred_at: str
    payload: Dict[str, Any] = Field(default_factory=dict)


# --- Goals ------------------------------------------------------------------

class GoalCreateRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "medium"
    parent_goal_id: Optional[str] = None
    person_id: Optional[str] = None


class GoalUpdateRequest(BaseModel):
    identity: HostIdentity
    status: Optional[str] = None
    progress: Optional[float] = None
    notes: Optional[str] = None


class GoalResponse(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    status: str = "active"
    priority: str = "medium"
    progress: float = 0.0
    parent_goal_id: Optional[str] = None
    person_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class GoalListResponse(BaseModel):
    goals: List[GoalResponse] = []


# --- Contacts ---------------------------------------------------------------

class ContactResponse(BaseModel):
    contact_id: str
    display_name: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    organization: Optional[str] = None
    relationship_score: float = 0.0
    trust_tier: Optional[str] = None
    interaction_allowed: bool = True
    tags: List[str] = Field(default_factory=list)
    privacy_level: Optional[str] = None
    person_node_id: Optional[str] = None
    notes: Optional[str] = None
    import_source: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_interaction_at: Optional[str] = None
    interaction_count: int = 0
    enrichment_source: List[str] = Field(default_factory=list)
    enrichment_last_at: Optional[str] = None
    deleted_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContactListResponse(BaseModel):
    contacts: List[ContactResponse] = []
    source_filter: Optional[str] = None
    total: int = 0


class ContactStyleRequest(BaseModel):
    identity: HostIdentity
    person_id: str


class ContactStyleResponse(BaseModel):
    person_id: str
    formality: Optional[str] = None
    tone: Optional[str] = None
    notes: Optional[Dict[str, Any]] = None


# --- Briefings --------------------------------------------------------------

class BriefingResponse(BaseModel):
    id: str
    title: Optional[str] = None
    body: str
    briefing_type: Optional[str] = None
    created_at: Optional[str] = None


class BriefingListResponse(BaseModel):
    briefings: List[BriefingResponse] = []


# --- World Model ------------------------------------------------------------

class EntityResponse(BaseModel):
    id: str
    entity_type: str
    name: str
    properties: Optional[Dict[str, Any]] = None


class EntityListResponse(BaseModel):
    entities: List[EntityResponse] = []


class EntityQueryRequest(BaseModel):
    identity: HostIdentity
    query: str
    limit: Optional[int] = 10


class ExtractionRequest(BaseModel):
    identity: HostIdentity
    content: str  # Base64-encoded document content
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ExtractedEntityResponse(BaseModel):
    name: str
    entity_type: str
    attributes: Optional[Dict[str, Any]] = None
    confidence: float = 1.0


class ExtractionResponse(BaseModel):
    format_detected: str
    entities: List[ExtractedEntityResponse] = []
    text_length: int = 0


# --- Cognition --------------------------------------------------------------

class CognitionCycleRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None


class CognitivePerformanceIndex(BaseModel):
    overall: float = 0.0
    memory: float = 0.0
    reasoning: float = 0.0
    social: float = 0.0
    autonomy: float = 0.0
    domains: Optional[Dict[str, float]] = None


class CognitionGap(BaseModel):
    gap_id: str
    domain: str
    severity: float
    description: Optional[str] = None


class CognitionCycleResponse(BaseModel):
    cpi: Optional[CognitivePerformanceIndex] = None
    gaps: List[CognitionGap] = []
    adjustments: List[Dict[str, Any]] = []


# --- Research ---------------------------------------------------------------

class ResearchStartRequest(BaseModel):
    identity: HostIdentity
    topic: str
    depth: Optional[str] = "standard"  # quick | standard | deep
    person_id: Optional[str] = None


class ResearchRunResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    stages_completed: List[str] = []
    artifact: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


class ResearchListResponse(BaseModel):
    runs: List[ResearchRunResponse] = []


# --- Delivery ---------------------------------------------------------------

class DeliveryListResponse(BaseModel):
    pending: List[Dict[str, Any]] = []


class DeliveryMarkRequest(BaseModel):
    identity: HostIdentity
    delivery_id: str


# --- Synthesis --------------------------------------------------------------

class SynthesisDiscoverRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    person_id: Optional[str] = None
    min_novelty: Optional[float] = 0.3


class SynthesisConnection(BaseModel):
    id: str
    connection_type: str
    entities: List[str] = []
    novelty: float = 0.0
    description: Optional[str] = None


class SynthesisDiscoverResponse(BaseModel):
    connections: List[SynthesisConnection] = []


# --- Learning ---------------------------------------------------------------

class LearningCorrectionRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    original: str
    correction: str
    component: Optional[str] = None


class LearningEngagementRequest(BaseModel):
    identity: HostIdentity
    briefing_id: str
    action: str  # opened | dismissed | clicked | saved
    dwell_seconds: Optional[float] = None


class LearningWeightsResponse(BaseModel):
    weights: Dict[str, float] = {}
    stats: Dict[str, int] = {}


# --- Skills -----------------------------------------------------------------

class SkillSummary(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    version: Optional[str] = None
    triggers: List[str] = []


class SkillDetailResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    version: Optional[str] = None
    triggers: List[str] = []
    input_schema: Optional[Dict[str, Any]] = None
    permissions: Optional[Dict[str, Any]] = None


class SkillsListResponse(BaseModel):
    skills: List[SkillSummary] = []


# --- Insights ---------------------------------------------------------------

class InsightResponse(BaseModel):
    id: str
    title: str
    body: str
    insight_type: Optional[str] = None
    novelty: float = 0.0
    entities: List[str] = []
    created_at: Optional[str] = None
    dismissed: bool = False


class InsightsListResponse(BaseModel):
    insights: List[InsightResponse] = []


# --- Enriched Context -------------------------------------------------------

class EnrichedContextRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    message: str
    features: Optional[Dict[str, bool]] = None
    compression: Optional[Literal["off", "conservative", "balanced", "aggressive"]] = None


class EnrichedContextResponse(BaseModel):
    sections: List[ContextSection] = []
    contact_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# --- Chain / Identity -------------------------------------------------------

class IdentityStatusResponse(BaseModel):
    colony_id: Optional[str] = None
    public_key: Optional[str] = None
    node_id: Optional[str] = None
    node_public_key: Optional[str] = None
    node_cert_fingerprint: Optional[str] = None
    initialized: bool = False
    keys_configured: bool = False
    is_genesis: bool = False
    trust_tier: Optional[Literal["REGULAR", "TRUSTED", "PRIVILEGED", "GENESIS"]] = None
    trust_anchor_verified: bool = False


class IdentityInitRequest(BaseModel):
    identity: HostIdentity
    force: bool = False


class ChainVerifyRequest(BaseModel):
    identity: HostIdentity
    data: str
    signature: Optional[str] = None


class ChainVerifyResponse(BaseModel):
    valid: bool
    colony_id: Optional[str] = None
    signed_attestation: Optional[str] = None
    attested_at: Optional[str] = None
    signer_public_key: Optional[str] = None


# --- Secrets ----------------------------------------------------------------

class SecretListRequest(BaseModel):
    identity: HostIdentity
    prefix: Optional[str] = None


class SecretListResponse(BaseModel):
    keys: List[str] = []


class SecretGetRequest(BaseModel):
    identity: HostIdentity
    key: str


class SecretGetResponse(BaseModel):
    key: str
    value: Optional[str] = None
    exists: bool = False


class SecretSetRequest(BaseModel):
    identity: HostIdentity
    key: str
    value: str
    secret_type: Optional[str] = None


class SecretSetResponse(BaseModel):
    key: str
    stored: bool


class SecretDeleteRequest(BaseModel):
    identity: HostIdentity
    key: str


class SecretDeleteResponse(BaseModel):
    key: str
    deleted: bool


# --- Autonomy ---------------------------------------------------------------

class AutonomyStatusResponse(BaseModel):
    running: bool = False
    mode: str = "reactive"
    timezone: str = "UTC"
    in_quiet_hours: bool = False
    ticks: int = 0
    events_processed: int = 0
    goals_checked: int = 0
    initiatives_generated: int = 0
    actions_executed: int = 0
    errors: int = 0
    config: Optional[Dict[str, Any]] = None



# --- Configure (Host LLM Config) -------------------------------------------

class LLMModelsConfig(BaseModel):
    small: Optional[str] = None
    medium: Optional[str] = None
    large: Optional[str] = None


class HostConfigureRequest(BaseModel):
    identity: HostIdentity
    llm: Optional[Dict[str, Any]] = Field(None, description="LLM provider config from host")


class HostConfigureResponse(BaseModel):
    configured: bool = True
    provider: Optional[str] = None
    models: Optional[Dict[str, str]] = None


# --- Models (local LLM discovery) -------------------------------------------

class ModelInfo(BaseModel):
    id: str
    provider: Optional[str] = None
    size: Optional[int] = None
    owned_by: Optional[str] = None


class ModelListResponse(BaseModel):
    provider: str = ""
    base_url: Optional[str] = None
    models: List[ModelInfo] = []
    discovered: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Commitment Tracking
# ---------------------------------------------------------------------------

class CommitmentCreateRequest(BaseModel):
    person_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, max_length=1000)
    due_at: Optional[str] = None
    priority: int = Field(default=50, ge=0, le=100)
    source_type: Literal["manual", "autonomy", "cognition"] = "manual"
    source_context: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class CommitmentUpdateRequest(BaseModel):
    status: Optional[Literal["fulfilled", "cancelled"]] = None
    fulfilled_at: Optional[str] = None
    description: Optional[str] = Field(None, min_length=1, max_length=1000)
    due_at: Optional[str] = None
    priority: Optional[int] = Field(None, ge=0, le=100)
    metadata: Optional[Dict[str, Any]] = None


class CommitmentResponse(BaseModel):
    id: str
    person_id: str
    description: str
    made_at: str
    due_at: Optional[str] = None
    fulfilled_at: Optional[str] = None
    status: str
    source_type: str
    source_context: Optional[str] = None
    priority: int
    metadata: Optional[Dict[str, Any]] = None


class CommitmentListResponse(BaseModel):
    commitments: List[CommitmentResponse] = []
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Cognition Substrate
# ---------------------------------------------------------------------------

class CognitionTriggerRequest(BaseModel):
    trigger_type: Literal["turn_sync", "signal_ingest", "anomaly", "manual"]
    context: Dict[str, Any]
    priority: Literal["high", "normal", "low"] = "normal"


class CognitionTriggerResponse(BaseModel):
    accepted: bool = True
    message: str = "Cognition trigger accepted"
    throttle_seconds: Optional[int] = None


# ---------------------------------------------------------------------------
# Theory of Mind — Affect
# ---------------------------------------------------------------------------

class AffectEventCreateRequest(BaseModel):
    contact_id: str
    valence: float = Field(..., ge=-1.0, le=1.0)
    arousal: float = Field(0.5, ge=0.0, le=1.0)
    source: Literal["explicit", "inferred", "signal"] = "explicit"
    trigger: Optional[str] = None
    session_id: Optional[str] = None


class AffectEventResponse(BaseModel):
    id: str
    contact_id: str
    valence: float
    arousal: float
    source: str
    trigger: Optional[str] = None
    timestamp: str
    session_id: Optional[str] = None


class AffectStateResponse(BaseModel):
    contact_id: str
    current_valence: float = 0.0
    current_arousal: float = 0.3
    trend: str = "stable"
    last_event_id: Optional[str] = None
    last_updated: Optional[str] = None
    event_count: int = 0


class AffectEventListResponse(BaseModel):
    events: List[AffectEventResponse] = []
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Theory of Mind — Shared Facts
# ---------------------------------------------------------------------------

class SharedFactCreateRequest(BaseModel):
    contact_id: str
    fact: str
    source: Literal["told_by_contact", "told_to_contact", "shared_context", "inferred"] = "shared_context"
    confidence: float = Field(0.8, ge=0.0, le=1.0)
    expires_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SharedFactUpdateRequest(BaseModel):
    fact: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    expires_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SharedFactResponse(BaseModel):
    id: str
    contact_id: str
    fact: str
    source: str
    confidence: float
    created_at: str
    expires_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SharedFactListResponse(BaseModel):
    facts: List[SharedFactResponse] = []
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Pattern Extraction
# ---------------------------------------------------------------------------

class PatternCreateRequest(BaseModel):
    pattern_type: Literal["entity_cooccurrence", "relation_frequency", "temporal_sequence", "attribute_cluster"]
    description: str
    pattern_key: str
    frequency: int = 1
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    metadata: Optional[Dict[str, Any]] = None
    source: Literal["extraction", "manual", "inferred"] = "extraction"


class PatternResponse(BaseModel):
    id: str
    pattern_type: str
    description: str
    pattern_key: str
    frequency: int
    last_seen: str
    first_seen: str
    confidence: float
    metadata: Optional[Dict[str, Any]] = None
    source: str
    active: bool = True


class PatternListResponse(BaseModel):
    patterns: List[PatternResponse] = []
    total: int
    limit: int
    offset: int


class PatternUpdateRequest(BaseModel):
    description: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    metadata: Optional[Dict[str, Any]] = None
    active: Optional[bool] = None


class PatternExtractResponse(BaseModel):
    new: int = 0
    updated: int = 0
    total: int = 0
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Surprise Engine
# ---------------------------------------------------------------------------

class SurpriseCreateRequest(BaseModel):
    observation: str
    expected: Optional[str] = None
    surprise_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    pattern_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    auto_score: bool = False


class SurpriseResponse(BaseModel):
    id: str
    observation: str
    expected: Optional[str] = None
    surprise_score: float
    pattern_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    timestamp: str
    resolved: bool = False
    resolution: Optional[str] = None


class SurpriseListResponse(BaseModel):
    surprises: List[SurpriseResponse] = []
    total: int
    limit: int
    offset: int


class SurpriseResolveRequest(BaseModel):
    resolution: Optional[str] = None


# ---------------------------------------------------------------------------
# ToM LLM Extraction
# ---------------------------------------------------------------------------

class TomExtractRequest(BaseModel):
    conversation_text: str
    contact_id: str
    session_id: Optional[str] = None
    extract_affect: bool = True
    extract_facts: bool = True


class TomExtractResponse(BaseModel):
    affect: Optional[Dict[str, Any]] = None
    facts: List[Dict[str, Any]] = []
    throttled: bool = False


# ---------------------------------------------------------------------------
# World Model — Entities
# ---------------------------------------------------------------------------

class WorldEntityCreateRequest(BaseModel):
    name: str
    entity_type: str
    aliases: Optional[List[str]] = []
    external_ids: Optional[Dict[str, str]] = {}
    confidence: float = 0.5
    properties: Optional[Dict[str, Any]] = {}


class WorldEntityUpdateRequest(BaseModel):
    name: Optional[str] = None
    confidence: Optional[float] = None
    properties: Optional[Dict[str, Any]] = None
    aliases: Optional[List[str]] = None


class WorldEntityDetailResponse(BaseModel):
    id: str
    name: str
    entity_type: str
    aliases: List[str] = []
    external_ids: Dict[str, str] = {}
    confidence: float = 0.5
    properties: Dict[str, Any] = {}
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class WorldEntityListResponse(BaseModel):
    entities: List[WorldEntityDetailResponse] = []
    total: int = 0


# ---------------------------------------------------------------------------
# World Model — Relationships
# ---------------------------------------------------------------------------

class WorldRelationshipCreateRequest(BaseModel):
    source_id: str
    target_id: str
    relationship_type: str
    confidence: float = 0.5
    valid_from: Optional[str] = None
    properties: Optional[Dict[str, Any]] = {}


class WorldRelationshipUpdateRequest(BaseModel):
    confidence: Optional[float] = None
    valid_to: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None


class WorldRelationshipResponse(BaseModel):
    id: str
    source_id: str
    target_id: str
    relationship_type: str
    confidence: float = 0.5
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    properties: Dict[str, Any] = {}
    is_active: bool = True
    created_at: Optional[str] = None


class WorldRelationshipListResponse(BaseModel):
    relationships: List[WorldRelationshipResponse] = []
    total: int = 0


# ---------------------------------------------------------------------------
# World Model — Graph Traversal
# ---------------------------------------------------------------------------

class WorldNeighborhoodResponse(BaseModel):
    center: Optional[WorldEntityDetailResponse] = None
    reachable: List[WorldEntityDetailResponse] = []
    edges: List[WorldRelationshipResponse] = []
    hop_counts: Dict[str, int] = {}
    truncated: bool = False


class WorldPathResponse(BaseModel):
    source_id: str
    target_id: str
    path: Optional[List[WorldRelationshipResponse]] = None
    found: bool = False


class WorldStatsResponse(BaseModel):
    total_entities: int = 0
    entities_by_type: Dict[str, int] = {}
    total_relationships: int = 0
    active_relationships: int = 0
    total_observations: int = 0
    merge_proposals_pending: int = 0


# ---------------------------------------------------------------------------
# Multi-Agent — Agent Management (v0.7.0)
# ---------------------------------------------------------------------------

class AgentInviteRequest(BaseModel):
    expires_in_seconds: int = Field(default=900, ge=60, le=86400)
    max_uses: int = Field(default=1, ge=1, le=100)
    granted_capabilities: List[str] = Field(default_factory=lambda: ["messaging"])
    granted_is_primary: bool = False
    granted_max_concurrent: int = Field(default=5, ge=1, le=100)
    label: Optional[str] = None


class AgentInviteResponse(BaseModel):
    code: str
    expires_at: str
    max_uses: int
    setup_command: str


class AgentConnectRequest(BaseModel):
    setup_code: str
    node_id: Optional[str] = None
    node_public_key: str
    name: str
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentNodeCert(BaseModel):
    colony_id: str
    node_id: str
    public_key: str
    signature: str
    issued_at: str


class AgentConnectResponse(BaseModel):
    agent_id: str
    node_id: str
    colony_id: str
    node_cert: AgentNodeCert
    websocket_url: str
    capabilities: List[str]
    is_primary: bool
    max_concurrent: int


class AgentRegisterRequest(BaseModel):
    agent_id: Optional[str] = None
    node_id: Optional[str] = None
    name: str
    connection_mode: Literal["local", "remote"] = "local"
    gateway_url: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    is_primary: bool = False
    priority: int = Field(default=0, ge=0, le=100)
    max_concurrent: int = Field(default=5, ge=1, le=100)
    excluded_types: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentRegisterResponse(BaseModel):
    agent_id: str
    node_id: str
    colony_id: str
    websocket_url: Optional[str] = None


class AgentHeartbeatRequest(BaseModel):
    status: Literal["online", "busy", "offline"] = "online"
    current_assignments: int = Field(default=0, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentMetadataSchema(BaseModel):
    hostname: Optional[str] = None
    platform: Optional[str] = None
    version: Optional[str] = None
    harness: Optional[str] = None
    timezone: Optional[str] = None


class AgentResponse(BaseModel):
    agent_id: str
    node_id: Optional[str] = None
    name: str
    colony_id: str
    connection_mode: Literal["local", "remote"]
    gateway_url: Optional[str] = None
    capabilities: List[str]
    is_primary: bool
    priority: int
    max_concurrent: int
    excluded_types: List[str]
    status: Literal["online", "busy", "offline", "suspended", "revoked"]
    current_assignments: int
    metadata: AgentMetadataSchema = AgentMetadataSchema()
    registered_at: str
    last_seen_at: Optional[str] = None


class AgentListResponse(BaseModel):
    agents: List[AgentResponse]
    total: int


class AgentHealthResponse(BaseModel):
    agents: List[Dict[str, Any]]
    websocket_endpoint: str


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    capabilities: Optional[List[str]] = None
    is_primary: Optional[bool] = None
    priority: Optional[int] = None
    max_concurrent: Optional[int] = None
    excluded_types: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Multi-Agent — Initiative Management (v0.7.0)
# ---------------------------------------------------------------------------

class InitiativeCreateRequest(BaseModel):
    initiative_type: Literal[
        "PROACTIVE_MESSAGE", "CALENDAR_REMINDER", "TASK_SUGGESTION",
        "RESEARCH_DEEP_DIVE", "SKILL_EVICT", "CODING"
    ]
    title: str
    description: str
    priority: int = Field(default=0, ge=0, le=100)
    timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    context: Dict[str, Any] = Field(default_factory=dict)
    target_agent_id: Optional[str] = None
    dedup_key: Optional[str] = None


class InitiativeResponse(BaseModel):
    id: str
    initiative_type: str
    title: str
    description: str
    priority: int
    status: Literal["pending", "acknowledged", "in_progress", "completed", "failed", "cancelled"]
    timeout_seconds: int
    context: Dict[str, Any]
    target_agent_id: Optional[str]
    assigned_agent_id: Optional[str]
    dedup_key: Optional[str]
    result: Optional[Dict[str, Any]]
    error_message: Optional[str]
    created_at: str
    acknowledged_at: Optional[str]
    completed_at: Optional[str]
    failed_at: Optional[str]
    expires_at: Optional[str]


class InitiativeListResponse(BaseModel):
    initiatives: List[InitiativeResponse]
    total: int


class InitiativeClaimRequest(BaseModel):
    agent_id: str


class InitiativeCompleteRequest(BaseModel):
    agent_id: str
    result: Dict[str, Any] = Field(default_factory=dict)


class InitiativeFailRequest(BaseModel):
    agent_id: str
    error_message: str


class InitiativeDelegateRequest(BaseModel):
    target_agent_id: str
    reason: Optional[str] = None


class InitiativePriorityRequest(BaseModel):
    priority: int = Field(ge=0, le=100)

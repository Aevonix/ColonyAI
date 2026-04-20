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
    id: str
    name: Optional[str] = None
    trust_tier: Optional[str] = None
    style_notes: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class ContactListResponse(BaseModel):
    contacts: List[ContactResponse] = []


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
    initialized: bool = False
    keys_configured: bool = False
    is_genesis: bool = False


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

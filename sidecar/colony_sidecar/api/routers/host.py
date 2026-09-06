"""Colony sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Colony's intelligence as a plugin.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field

from colony_sidecar.goals.store import GoalNotFoundError
from colony_sidecar import get_state_dir
from colony_sidecar.events.stream import EventSubscriberBuffer
from colony_sidecar.api.authority import (
    record_attested_contact_grant,
    request_authority,
    resolve_request_person,
    resolve_turn_person,
)

from colony_sidecar.api.schemas.host import (
    HostConfigureRequest,
    HostConfigureResponse,
    ModelInfo,
    ModelListResponse,
    AutonomyStatusResponse,
    BackfillRequest,
    BackfillResponse,
    BriefingListResponse,
    BriefingResponse,
    ChainVerifyRequest,
    ChainVerifyResponse,
    CognitionCycleRequest,
    ContactCreateRequest,
    ContactIntroRequest,
    ContactIntroResponse,
    ContactListResponse,
    ContactResponse,
    ContactStyleRequest,
    ContactStyleResponse,
    ContactTimezoneRequest,
    ScopeAuthzResponse,
    ScopeCreateRequest,
    ScopeDeactivateRequest,
    ScopeMemberIn,
    ScopePromoteRequest,
    ScopeResponse,
    ResponseGuardCheckRequest,
    ContextAssembleRequest,
    ContextAssembleResponse,
    ContextProjectionAttestation,
    ContextSection,
    TemporalConfigRequest,
    TemporalConfigResponse,
    TemporalContact,
    TemporalContactsResponse,
    TimelineEvent,
    TimelineResponse,
    DeliveryListResponse,
    DeliveryMarkRequest,
    EmbedHealthResponse,
    EnrichedContextRequest,
    EnrichedContextResponse,
    EntityListResponse,
    EntityQueryRequest,
    EntityResponse,
    ExtractionRequest,
    ExtractionResponse,
    ExtractedEntityResponse,
    GoalCreateRequest,
    GoalListResponse,
    GoalResponse,
    GoalUpdateRequest,
    HostHealthResponse,
    HostMessage,
    IdentityInitRequest,
    IdentityStatusResponse,
    ImageBatchEmbedRequest,
    ImageBatchEmbedResponse,
    ImageEmbedRequest,
    ImageEmbedResponse,
    IndexRequest,
    IndexResponse,
    InsightResponse,
    InsightsListResponse,
    LearningCorrectionRequest,
    LearningEngagementRequest,
    LearningWeightsResponse,
    MemoryEmbedRequest,
    MemoryEmbedResponse,
    MemoryEntry,
    MemoryFlushRequest,
    MemoryFlushResponse,
    MemoryReadRequest,
    MemoryReadResponse,
    MemoryReconcileRequest,
    MemoryReconcileResponse,
    MemoryConflictEntry,
    MemoryConflictsResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryVerifyRequest,
    MemoryVerifyResponse,
    MemoryStatsResponse,
    MemoryWriteRequest,
    MemoryWriteResponse,
    RerankRequest,
    RerankResponse,
    RerankResult,
    MigrateRequest,
    MigrateResponse,
    MultimodalSearchRequest,
    MultimodalSearchResponse,
    ReasoningToolCall,
    ReasoningTurnRequest,
    ReasoningTurnResponse,
    SkillExecuteRequest,
    SkillExecuteResponse,
    ToolInvokeRequest,
    ToolInvokeResponse,
    ResearchListResponse,
    ResearchRunResponse,
    ResearchStartRequest,
    SafetyCheckRequest,
    SafetyCheckResponse,
    SecretDeleteRequest,
    SecretDeleteResponse,
    SecretGetRequest,
    SecretGetResponse,
    SecretListRequest,
    SecretListResponse,
    SecretSetRequest,
    SecretSetResponse,
    SignalIngestRequest,
    SignalIngestResponse,
    SkillDetailResponse,
    SkillSummary,
    SkillsListResponse,
    SynthesisConnection,
    SynthesisDiscoverRequest,
    SynthesisDiscoverResponse,
    TurnSyncRequest,
    TurnSyncResponse,
    CommitmentCreateRequest,
    CommitmentListResponse,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ConcernResolveRequest,
    CognitionTriggerRequest,
    CognitionTriggerResponse,
    AffectEventCreateRequest,
    AffectEventResponse,
    AffectStateResponse,
    AffectEventListResponse,
    SharedFactCreateRequest,
    SharedFactUpdateRequest,
    SharedFactResponse,
    SharedFactListResponse,
    PatternCreateRequest,
    PatternResponse,
    PatternListResponse,
    PatternUpdateRequest,
    PatternExtractResponse,
    SurpriseCreateRequest,
    SurpriseResponse,
    SurpriseListResponse,
    SurpriseResolveRequest,
    TomExtractRequest,
    TomExtractResponse,
    WorldEntityCreateRequest,
    WorldEntityUpdateRequest,
    WorldEntityDetailResponse,
    WorldRelationshipCreateRequest,
    WorldRelationshipUpdateRequest,
    WorldRelationshipResponse,
    WorldRelationshipListResponse,
    WorldNeighborhoodResponse,
    WorldPathResponse,
    WorldStatsResponse,
    # Multi-Agent v0.7.0
    AgentInviteRequest,
    AgentInviteResponse,
    AgentConnectRequest,
    AgentConnectResponse,
    AgentNodeCert,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentHeartbeatRequest,
    AgentMetadataSchema,
    AgentResponse,
    AgentListResponse,
    AgentHealthResponse,
    AgentUpdateRequest,
    InitiativeCreateRequest,
    InitiativeResponse,
    InitiativeListResponse,
    InitiativeClaimRequest,
    InitiativeCompleteRequest,
    InitiativeFailRequest,
    InitiativeDelegateRequest,
    InitiativePriorityRequest,
    # Agent Snapshot
    AgentSnapshotInitiative,
    AgentSnapshotResponse,
    RecordOutreachRequest,
    RecordOutreachResponse,
    # Session Context Architecture
    AgentSnapshotSystemState,
    SessionReportRequest,
    SessionReportResponse,
    ContextDigestSessionReport,
    ContextDigestResponse,
)

logger = logging.getLogger(__name__)

# Background task bookkeeping — prevents garbage-collection of fire-and-forget
# asyncio tasks (see https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)
_background_tasks: set[asyncio.Task] = set()


def _spawn_task(coro) -> asyncio.Task:
    """Create an asyncio task, retain a reference, and auto-discard on completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _to_dict(obj):
    """Convert Pydantic models or other objects to plain dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    if isinstance(obj, dict):
        return obj
    return {}

router = APIRouter(prefix="/v1/host", tags=["host"])
v2_router = APIRouter(prefix="/v2/host", tags=["host-v2"])

# ---------------------------------------------------------------------------
# Module-level wiring — subsystems are injected by the server lifespan
# ---------------------------------------------------------------------------

_graph = None
_response_gate = None
_signal_collector = None
_embedder = None
_reasoning_loop = None
_consolidator = None
_event_subscribers: list[EventSubscriberBuffer] = []
_event_broadcast_lock = threading.RLock()


def _event_subscriber_queue_size() -> int:
    """Configured bound for each live event subscriber."""
    raw = os.environ.get("COLONY_EVENT_SUBSCRIBER_QUEUE_SIZE", "256")
    try:
        size = int(raw)
    except ValueError:
        logger.warning(
            "Invalid COLONY_EVENT_SUBSCRIBER_QUEUE_SIZE=%r; using 256", raw
        )
        return 256
    if size < 1:
        logger.warning(
            "COLONY_EVENT_SUBSCRIBER_QUEUE_SIZE must be positive; using 256"
        )
        return 256
    return min(size, 10_000)


def broadcast_event(event: dict) -> Optional[dict]:
    """Persist, canonicalize, then publish an event to live subscribers.

    Called by the autonomy loop, signal collector, and other subsystems
    when state changes that the host should know about (proactive
    messages, briefings, anomalies, etc.).  A journal failure suppresses the
    live frame: clients must never observe an event which cannot be replayed.
    """
    from colony_sidecar.events.journal import append_event_record

    event_type = str(event.get("type", "unknown"))
    payload = event.get("payload") or {}
    occurred_at = str(event.get("occurred_at") or "") or None

    # This lock preserves journal-sequence order in local live buffers when
    # emitters run concurrently on request handlers or worker threads.
    with _event_broadcast_lock:
        record = append_event_record(
            event_type,
            payload,
            occurred_at=occurred_at,
        )
        if record is None:
            logger.error(
                "Suppressing live event %s because journal append failed",
                event_type,
            )
            return None

        frame = {
            **event,
            "type": event_type,
            "payload": payload,
            "occurred_at": record["occurredAt"],
            "recordedAt": record["recordedAt"],
            "seq": record["seq"],
            "eventId": record["ulid"],
        }
        for subscriber in tuple(_event_subscribers):
            try:
                subscriber.publish(dict(frame))
            except Exception:
                logger.exception(
                    "Failed to enqueue event seq=%s for one subscriber",
                    record["seq"],
                )
        return frame


def set_graph(graph) -> None:
    global _graph
    _graph = graph


def set_response_gate(gate) -> None:
    global _response_gate
    _response_gate = gate


def set_signal_collector(collector) -> None:
    global _signal_collector
    _signal_collector = collector


def set_embedder(embedder) -> None:
    global _embedder
    _embedder = embedder


def set_reasoning_loop(loop) -> None:
    global _reasoning_loop
    _reasoning_loop = loop


_tool_executor = None


def set_tool_executor(executor) -> None:
    global _tool_executor
    _tool_executor = executor


def set_consolidator(consolidator) -> None:
    global _consolidator
    _consolidator = consolidator


_llm_router = None


def set_llm_router(router) -> None:
    global _llm_router
    _llm_router = router


def get_llm_router():
    """Current LLMRouter instance (None until the host configures one)."""
    return _llm_router


_telemetry = None


def set_telemetry(telemetry) -> None:
    global _telemetry
    _telemetry = telemetry


def supported_capabilities() -> List[str]:
    """Return the list of capabilities this sidecar advertises."""
    caps: list[str] = []
    if _graph is not None:
        caps.append("memory")
    if _response_gate is not None:
        caps.append("response_gate")
    if _signal_collector is not None:
        caps.append("signals")
    if _embedder is not None:
        caps.append("embed")
    if _consolidator is not None:
        caps.append("consolidate")
    if _reasoning_loop is not None:
        caps.append("reasoning")
    if _goals_store is not None:
        caps.append("goals")
    if _contacts_store is not None:
        caps.append("contacts")
    if _briefings_engine is not None:
        caps.append("briefings")
    if _world_store is not None:
        caps.append("world_model")
    if _metalearner is not None:
        caps.append("cognition")
    if _cognition_spine is not None:
        caps.append("cognition_spine")
    if _situation_store is not None and _situation_reducer is not None:
        caps.append("situation")
    if _project_event_projector is not None:
        caps.append("project_event_outbox")
    if _cognition_evidence_store is not None \
            and _cognition_evidence_reducer is not None \
            and getattr(_cognition_evidence_reducer, "mode", "shadow") != "off":
        caps.append("cognition_evidence")
    if _drive_governance is not None and _drive_ranker is not None:
        caps.append("drive_governance")
    if _research_pipeline is not None:
        caps.append("research")
    if _delivery_bridge is not None:
        caps.append("delivery")
    if _connection_discoverer is not None:
        caps.append("synthesis")
    if _learner is not None:
        caps.append("learning")
    if _skills_registry is not None:
        caps.append("skills")
    if _chain_manager is not None:
        caps.append("identity")
    if _secrets_manager is not None:
        caps.append("secrets")
    if _autonomy_loop is not None:
        caps.append("autonomy")
    if _session_store is not None:
        caps.append("sessions")
    if _task_queue is not None:
        caps.append("task_queue")
    caps.append("events")
    if _commitment_store is not None:
        caps.append("commitments")
        readiness = getattr(
            _commitment_store, "resolution_recovery_readiness", None,
        )
        if readiness is not None:
            try:
                status = readiness()
                capability = status.get("capability")
                if status.get("ready") is True and capability:
                    caps.append(str(capability))
            except Exception:
                pass
    if _affect_store is not None:
        caps.append("affect")
    if _facts_store is not None:
        caps.append("shared_facts")
    if _p8_runtime is not None:
        caps.append("tom_p8_shadow")
    if _pattern_store is not None:
        caps.append("patterns")
    if _surprise_store is not None:
        caps.append("surprises")
    if _reranker is not None:
        caps.append("rerank")
    if _world_store is not None:
        caps.append("context")
        caps.append("world_model_api")
    if _world_store is not None and hasattr(_world_store, '_config') and _world_store._config.backend == "neo4j":
        caps.append("neo4j_backend")
    caps.append("event_journal")
    if _external_event_intake is not None:
        caps.append("external_cognition_events")
    caps.append("context_compression")
    caps.append("skill_sandbox")
    caps.append("security_scanner")
    caps.append("tom_extract")
    return caps





# ---------------------------------------------------------------------------
# Host Configuration (LLM from host)
# ---------------------------------------------------------------------------


@router.post("/configure", response_model=HostConfigureResponse)
async def configure_host(body: HostConfigureRequest) -> HostConfigureResponse:
    """Receive LLM configuration from the host.

    The host (OpenClaw, Hermes, etc.) calls this on startup to provide
    its LLM provider credentials and model assignments. Colony does not
    manage its own LLM keys — it inherits them from the host.

    Validate and persist one configuration, then atomically update the shared
    router. Existing consumer references and in-flight snapshots are retained.
    """
    global _reasoning_loop

    if body.llm is None:
        return HostConfigureResponse(configured=False)

    from colony_sidecar.router.router import LLMRouter
    import os
    import tempfile

    try:
        prepared = LLMRouter(tiers={})
        prepared.configure(body.llm)
        config_path = get_state_dir() / ".colony-llm-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".llm-config-", dir=config_path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(body.llm, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, config_path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        # Keep every extractor/thinker/worker reference live. In-flight calls
        # retain their immutable snapshot; subsequent calls see this revision.
        if _llm_router is not None:
            _llm_router.adopt_configuration(prepared, config_path=config_path)
            new_router = _llm_router
        else:
            prepared.watch_config(config_path)
            new_router = prepared
            set_llm_router(new_router)
        if _reasoning_loop is not None:
            _reasoning_loop._model = new_router
        return HostConfigureResponse(
            configured=True, provider=body.llm.get("provider"),
            models=body.llm.get("models", {}), routing=new_router.routing_status())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid host model configuration") from exc
    except Exception as exc:
        logger.error("configure_host failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Model configuration update failed") from exc


@router.get("/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List available LLM models for the currently configured provider.

    For local providers (Ollama, vLLM, LM Studio, etc.), this queries the
    local server and returns the actual models that are installed.  For
    cloud providers an empty list is returned — the host is expected to
    know which cloud models exist.
    """
    # Load persisted host config to know the current provider/base_url
    from colony_sidecar.router.tiers import discover_local_models

    config_path = get_state_dir() / ".colony-llm-config.json"
    provider = ""
    base_url = ""
    api_key = ""
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            provider = cfg.get("provider", "")
            base_url = cfg.get("baseUrl", "")
            api_key = cfg.get("apiKey", "")
        except Exception as exc:
            logger.debug("Could not read persisted LLM config: %s", exc)

    if _llm_router is not None and _llm_router.supports_function_routing:
        observed = await _llm_router.discover_models()
        if observed is not None:
            models = observed['models']
            return ModelListResponse(provider=observed['provider'], base_url=observed['base_url'],
                models=[ModelInfo(id=row['id'], provider=observed['provider'], owned_by=row.get('owned_by')) for row in models],
                discovered=bool(models), routing=observed['routing'],
                error=None if models else 'No fresh model advertisements are available; configured completion aliases remain unchanged.')

    if not provider:
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider="",
            error="No LLM provider configured. Call POST /v1/host/configure first.",
        )

    if provider not in ("ollama", "local", "custom", "lmstudio", "vllm"):
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider=provider,
            error="Model listing is only supported for local providers (ollama, local, custom, lmstudio, vllm).",
        )

    discovered = discover_local_models(provider, base_url, api_key)
    if discovered:
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider=provider,
            base_url=base_url or None,
            models=[
                ModelInfo(
                    id=m.get("name") or m.get("id", ""),
                    provider=provider,
                    size=m.get("size"),
                    owned_by=m.get("owned_by"),
                )
                for m in discovered
                if (m.get("name") or m.get("id"))
            ],
            discovered=True,
        )

    return ModelListResponse(
        routing=_llm_router.routing_status() if _llm_router is not None else None,
        provider=provider,
        base_url=base_url or None,
        error="Could not discover models from the local server. Is it running?",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

_TEMPORAL_HEALTH_POLICIES = frozenset({"enforce", "advisory"})


def _temporal_health_policy() -> str:
    """Return the fail-closed policy for temporal activity warnings.

    ``stale_flags`` remain observable under both policies.  ``advisory`` only
    prevents those activity timestamps from changing the host's top-level
    readiness; it never clears another degradation source.
    """

    configured = os.environ.get(
        "COLONY_TEMPORAL_HEALTH_POLICY", "enforce"
    ).strip().lower()
    if configured not in _TEMPORAL_HEALTH_POLICIES:
        return "enforce"
    return configured


@router.get("/health", response_model=HostHealthResponse)
async def health() -> HostHealthResponse:
    caps = supported_capabilities()
    notes: dict[str, str] = {}
    embed_model = ""
    stored_models: list[str] = []
    model_mismatch = False

    # the sidecar's own open-file limit (doctor reads this; a low limit makes
    # LanceDB vector recall fail under load — see check_server_fd_limit)
    try:
        import resource
        _fd_soft, _fd_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        notes["fd_limit"] = (
            "unlimited" if _fd_soft == resource.RLIM_INFINITY else str(_fd_soft))
    except Exception:
        pass

    memory_backend_down = False
    if _graph is not None:
        # "Wired" alone only means the client object was constructed. Probe
        # the backend (same determination as /memory/status) so health never
        # advertises a memory capability whose store is unreachable.
        if await _graph_backend_reachable():
            notes["memory"] = "ColonyGraph wired (backend reachable)"
        else:
            memory_backend_down = True
            caps = [c for c in caps if c != "memory"]
            notes["memory"] = (
                "ColonyGraph wired but backend UNREACHABLE — "
                "memory reads/writes are failing"
            )
    else:
        notes["memory"] = "ColonyGraph not wired — memory endpoints return stubs"
    if _response_gate is not None:
        notes["response_gate"] = "ResponseGate wired"
    else:
        notes["response_gate"] = "ResponseGate not wired — gate/check passes everything"
    if _reasoning_loop is not None:
        notes["reasoning"] = "ReasoningLoop wired (max_iterations=%d)" % _reasoning_loop._config.max_iterations
    else:
        notes["reasoning"] = "ReasoningLoop not wired — /reasoning/turn returns 501"
    if _goals_store is not None:
        notes["goals"] = "GoalEngine wired"
    if _contacts_store is not None:
        notes["contacts"] = "ContactsStore wired"
    if _briefings_engine is not None:
        notes["briefings"] = "BriefingEngine wired"
    if _world_store is not None:
        notes["world_model"] = "WorldModelStore wired"
    if _metalearner is not None:
        notes["cognition"] = "MetaLearner wired"
    if _signal_collector is not None:
        notes["signals"] = "SignalCollector wired"
    embed_degraded = False
    if _embedder is not None:
        # Get embed model info
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            embed_model = _embedder._provider._config.model_id
        embed_note = f"EmbeddingPipeline wired (model={embed_model})"

        # Check for model mismatch. A probe that itself crashes is a
        # degradation, never a silent pass — health must not report green
        # because the check that would have caught the problem threw.
        try:
            from colony_sidecar.vector import get_store
            store = get_store()
            if store is not None:
                stored_models = await store.get_stored_models()
                if stored_models and embed_model and embed_model not in stored_models:
                    model_mismatch = True
                    embed_note += f" [WARNING: stored models {stored_models} differ from current {embed_model}]"
                elif len(stored_models) > 1:
                    model_mismatch = True
                    embed_note += f" [WARNING: multiple stored models: {stored_models}]"
        except Exception as exc:
            embed_degraded = True
            embed_note += f" [model-check failed: {exc}]"
            logger.warning("embed model mismatch probe failed: %s", exc)

        # Check embedder health
        try:
            hc = await _embedder.health_check()
            if hc.get("status") != "ok":
                embed_degraded = True
                embed_note += f" [health: {hc.get('status', 'unknown')}"
                if hc.get("error"):
                    embed_note += f": {hc['error']}"
                embed_note += "]"
        except Exception as exc:
            embed_degraded = True
            embed_note += f" [health probe failed: {exc}]"
            logger.warning("embedder health probe failed: %s", exc)

        notes["embed"] = embed_note
    if _skills_registry is not None:
        notes["skills"] = "SkillRegistry wired"
    if _chain_manager is not None:
        notes["identity"] = "ChainManager wired"
    if _secrets_manager is not None:
        notes["secrets"] = "SecretsManager wired"
    if _research_pipeline is not None:
        notes["research"] = "ResearchPipeline wired"
    if _delivery_bridge is not None:
        notes["delivery"] = "ProactiveDeliveryBridge wired"
    if _connection_discoverer is not None:
        notes["synthesis"] = "ConnectionDiscoverer wired"
    if _learner is not None:
        notes["learning"] = "ContinuousLearner wired"
    if _autonomy_loop is not None:
        running = getattr(_autonomy_loop, '_running', False)
        if running:
            notes["autonomy"] = f"AutonomyLoop running (ticks={getattr(_autonomy_loop.stats, 'ticks', 0)})"
        else:
            notes["autonomy"] = "AutonomyLoop wired (not started)"
    if _agent_bridge is not None:
        if getattr(_agent_bridge, "is_running", False):
            s = getattr(_agent_bridge, "stats", {})
            notes["agent_bridge"] = (
                f"AgentBridge running (fwd={s.get('initiatives_forwarded', 0)}, "
                f"jobs={s.get('jobs_dispatched', 0)}, wh_fail={s.get('webhook_failures', 0)})"
            )
        else:
            notes["agent_bridge"] = "AgentBridge wired (not started)"
    if _initiative_executor is not None:
        if getattr(_initiative_executor, "is_running", False):
            s = getattr(_initiative_executor, "stats", {})
            notes["executor"] = (
                f"Executor running (done={s.get('initiatives_completed', 0)}, "
                f"fail={s.get('initiatives_failed', 0)}, tokens={s.get('total_tokens', 0)})"
            )
        else:
            notes["executor"] = "Executor wired (not started)"
    if _session_store is not None:
        notes["sessions"] = "InMemorySessionStore wired"
    if _task_queue is not None:
        notes["task_queue"] = "TaskQueueManager wired"
    if _commitment_store is not None:
        if "commitment_resolution_recovery_v1" in caps:
            notes["commitments"] = (
                "CommitmentStore wired (resolution recovery v1 ready)"
            )
        elif hasattr(_commitment_store, "resolution_recovery_readiness"):
            notes["commitments"] = (
                "CommitmentStore wired (resolution recovery unavailable)"
            )
        else:
            notes["commitments"] = "CommitmentStore wired"
    if _affect_store is not None:
        notes["affect"] = "AffectStore wired"
    if _facts_store is not None:
        notes["shared_facts"] = "SharedFactsStore wired"
    if _p8_runtime is not None:
        notes["tom_p8"] = "P8 scoped context + outbound shadow observer wired"
    if _pattern_store is not None:
        notes["patterns"] = "PatternStore wired"
    if _surprise_store is not None:
        notes["surprises"] = "SurpriseStore wired"
    if _world_store is not None and hasattr(_world_store, '_backend') and _world_store._backend is not None:
        backend_type = type(_world_store._backend).__name__
        notes["world_model_backend"] = f"{backend_type} connected"
    if _world_store is not None and hasattr(_world_store, '_config') and _world_store._config.backend == "neo4j":
        notes["neo4j"] = "Neo4j backend selected"

    health_status = "ok"
    if model_mismatch or embed_degraded or memory_backend_down:
        health_status = "degraded"
    if (
        _commitment_store is not None
        and hasattr(_commitment_store, "resolution_recovery_readiness")
        and "commitment_resolution_recovery_v1" not in caps
    ):
        health_status = "degraded"

    # Build temporal metrics
    temporal = None
    try:
        if _telemetry is not None:
            thresholds = {
                "sync": float(os.environ.get("COLONY_STALE_SYNC_HOURS", "2.0")),
                "tick": float(os.environ.get("COLONY_STALE_TICK_HOURS", "24.0")),
                "initiative": float(os.environ.get("COLONY_STALE_INITIATIVE_HOURS", "48.0")),
                # prefetch = last /context/assemble, which is driven by INBOUND
                # conversation turns, not an internal schedule. Multi-hour gaps are
                # normal idle (overnight, focus time), so a tight threshold would
                # false-flag the whole system "degraded" during any quiet period AND
                # mask real degradation. 24h matches the agent-snapshot views and
                # means "the host hasn't asked for context in a full day" — the point
                # at which idle becomes a genuine integration-down signal.
                "prefetch": float(os.environ.get("COLONY_STALE_PREFETCH_HOURS", "24.0")),
            }
            temporal_data = await _telemetry.to_dict(thresholds)
            if (
                temporal_data.get("stale_flags")
                and _temporal_health_policy() == "enforce"
            ):
                health_status = "degraded"
            from colony_sidecar.api.schemas.host import TemporalMetrics
            temporal = TemporalMetrics(**temporal_data)
    except Exception as exc:
        # If staleness cannot even be computed, the one probe that catches a
        # dead loop is gone — that is degradation, not silent health.
        health_status = "degraded"
        notes["temporal"] = f"staleness computation failed: {exc}"
        logger.warning("temporal staleness computation failed: %s", exc)

    return HostHealthResponse(
        status=health_status,
        capabilities=caps,
        notes=notes,
        temporal=temporal,
    )


@router.get("/admin/auth/status")
async def auth_migration_status(request: Request) -> dict:
    """Admin-only scoped-auth migration evidence without credential material."""

    telemetry = getattr(request.state, "colony_auth_telemetry", None)
    grants = getattr(request.state, "colony_contact_grants", None)
    keyring_status = getattr(request.state, "colony_keyring_status", None) or {
        "configured": False,
        "available": False,
        "error": "scoped keyring status is not attached to this application",
        "principal_count": 0,
        "credential_count": 0,
    }
    telemetry_status = telemetry.snapshot() if telemetry is not None else {
        "enabled": False,
        "persistent": False,
        "error": "auth telemetry is not attached to this application",
        "totals": {},
        "principals": {},
        "records": [],
    }
    grants_status = grants.status() if grants is not None else {
        "configured": False,
        "available": False,
        "error": "exact contact grants are not attached to this application",
        "principal_counts": {},
        "total_exact_person_ids": 0,
    }
    auth_configuration = getattr(request.state, "colony_auth_configuration", None) or {
        "legacy_configured": False,
        "scoped_configured": False,
        "dual_accept": False,
    }
    return {
        "auth": auth_configuration,
        "telemetry": telemetry_status,
        "keyring": keyring_status,
        "contact_grants": grants_status,
        "secrets_exposed": False,
    }


def _p8_viewer_for_request(
    request: Request | None,
    resolved_person_id: str,
    *,
    server_resolved: bool = False,
):
    """Seal a P8 viewer from middleware authority and a resolved person.

    Body channel/session values are deliberately absent: until a transport
    attests a conversation scope server-side, conversation-scoped P8 facts
    remain inaccessible.
    """

    from colony_sidecar.tom.visibility import ViewerContextV1

    authority = request_authority(request)
    person = str(resolved_person_id or "").strip()
    if (
        not authority.authenticated
        or authority.anonymous
        or authority.legacy
        or not authority.principal_id
        or not person
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "p8_scoped_authority_required",
                "message": "P8 requires a scoped authenticated principal",
            },
        )
    granted = person in authority.person_ids
    resolved_grant = (
        server_resolved and authority.has_scope("turns:resolve-sender"))
    if not granted and not resolved_grant:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "person_scope_not_granted",
                "message": "P8 viewer exceeds principal person authority",
            },
        )
    owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    material = {
        "principal_id": authority.principal_id,
        "credential_id": authority.credential_id,
        "viewer_person_id": person,
        "owner_person_id": owner,
        "person_ids": sorted(authority.person_ids),
        "audiences": sorted(authority.audiences),
        "server_resolved": bool(server_resolved),
    }
    import hashlib
    revision = hashlib.sha256(json.dumps(
        material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return ViewerContextV1(
        principal_id=authority.principal_id,
        viewer_person_id=person,
        owner_person_id=owner,
        audiences=tuple(sorted(authority.audiences)),
        conversation_scope="",
        scope_revision=f"scope:{revision}",
        attested=True,
    )


def _p8_legacy_global_context_allowed(viewer) -> bool:
    """Keep untyped legacy-global content inside the exact owner context.

    P8 facts carry immutable visibility envelopes. Several older context
    producers do not yet emit one at all (goals, initiatives, briefings,
    world-model entities, directives, surprises, and other global snapshots).
    While P8 is enabled, those sources must not even be queried for a guest or
    an unsealed migration caller. P8-off deliberately preserves the legacy
    context contract byte-for-byte.
    """

    if _p8_runtime is None:
        return True
    return bool(
        viewer is not None
        and getattr(viewer, "attested", False)
        and getattr(viewer, "viewer_person_id", "")
        and getattr(viewer, "viewer_person_id", "")
        == getattr(viewer, "owner_person_id", "")
    )


def _p8_exact_person_context_allowed(viewer) -> bool:
    """Require one server-attested exact viewer before person-store queries.

    Empty selectors and legacy/body-selected people are global selectors in
    several old stores. While P8 is attached, absence of a sealed viewer is a
    hard no-query boundary rather than permission to fall back to global or
    body-claimed owner data. P8-off retains the historical migration contract.
    """

    if _p8_runtime is None:
        return True
    return bool(
        viewer is not None
        and getattr(viewer, "attested", False)
        and getattr(viewer, "viewer_person_id", "")
    )


def _require_scoped_context_runtime_for_guest(
    request: Request | None,
    resolved_person_id: str,
) -> None:
    """Require an exact attested viewer before selecting the guest projection.

    Canonical source evidence has its own exact-person boundary and does not
    need P8. Missing viewer authority must still never select legacy context.
    """
    authority = request_authority(request)
    if authority.legacy or authority.anonymous or not authority.authenticated:
        return
    _p8_viewer_for_request(request, resolved_person_id)


def _context_projection_attestation(
    *,
    contact_id: str,
    viewer,
) -> ContextProjectionAttestation:
    """Describe server-observed authority and the actual projection backend."""

    viewer_id = str(getattr(viewer, "viewer_person_id", "") or "")
    owner_id = str(getattr(viewer, "owner_person_id", "") or "")
    attested = bool(
        viewer is not None
        and getattr(viewer, "attested", False)
        and viewer_id
        and viewer_id == str(contact_id or "").strip()
    )
    raw_mode = str(getattr(_p8_runtime, "mode", "off") or "off").lower()
    mode = raw_mode if raw_mode in {"shadow", "live"} else "off"
    canonical_only = bool(attested and owner_id and viewer_id != owner_id
                          and _p8_runtime is None)
    p8_ready = bool(attested and _p8_runtime is not None and mode != "off")
    scoped_ready = canonical_only or p8_ready
    return ContextProjectionAttestation(
        viewer_person_id=viewer_id if attested else "",
        viewer_attested=attested,
        viewer_is_owner=bool(attested and owner_id and viewer_id == owner_id),
        p8_mode=mode,
        projection_backend=("canonical_sources" if canonical_only else
                            "p8" if p8_ready else "unavailable"),
        scoped_projection_ready=scoped_ready,
        legacy_global_allowed=bool(
            attested and not canonical_only and _p8_legacy_global_context_allowed(
                viewer if _p8_runtime is not None else None
            )
        ),
    )


def _canonical_shared_commitments(rows, contact_id):
    """Expose only descriptions already present in this person's source evidence.

    person_id is a subject selector, not an audience grant. A metadata link
    alone is insufficient: the current person-scoped source must contain the
    displayed description. Unclassified legacy tasks stay owner-private.
    """
    from contextlib import closing
    from colony_sidecar.turns import get_turn_idempotency_ledger
    if not contact_id or not (Path(get_state_dir()) / "turn-idempotency.db").exists():
        return []
    visible = []
    ledger = get_turn_idempotency_ledger(get_state_dir())
    with closing(ledger._connect()) as conn:
        for row in rows:
            metadata = row.get("metadata")
            source_id = metadata.get("source_turn_id") if isinstance(metadata, dict) else None
            description = str(row.get("description") or "").strip()
            if row.get("person_id") != contact_id or not isinstance(source_id, str) or not description:
                continue
            source = conn.execute(
                "SELECT messages_json FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                (source_id, contact_id)).fetchone()
            if source is None:
                continue
            for message in json.loads(source["messages_json"]):
                content = message.get("content")
                texts = ([content] if isinstance(content, str) else
                         [block["text"] for block in content if isinstance(block, dict)
                          and block.get("type") in {"text", "input_text", "output_text"}
                          and isinstance(block.get("text"), str)] if isinstance(content, list) else [])
                if any(description.casefold() in text.casefold() for text in texts):
                    visible.append(row)
                    break
    return visible[:5]


def _p8_tool_actor_policy(
    request: Request | None,
    resolved_person_id: str | None,
):
    """Derive tool capabilities only from authenticated request authority.

    P8 legacy handlers still accept arbitrary selectors inside tool argument
    dictionaries. Until each handler has typed person/resource envelopes, only
    the exact sealed owner may use private reads, and mutations additionally
    require ``tools:mutate``. Guests retain the public information tools.
    """

    from colony_sidecar.reasoning.tool_policy import ToolActorPolicy

    authority = request_authority(request)
    person = str(resolved_person_id or "").strip()
    sealed = bool(
        authority.authenticated
        and not authority.anonymous
        and not authority.legacy
        and person
        and authority.viewer_person_id == person
        and person in authority.person_ids
    )
    owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    is_owner = sealed and person == owner
    return ToolActorPolicy(
        principal_id=str(authority.principal_id or "unsealed"),
        viewer_person_id=person if sealed else "",
        allow_private_read=is_owner,
        allow_mutation=is_owner and authority.has_scope("tools:mutate"),
    )


def _p8_filter_graph_recall(
    rows: List[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Remove SharedFacts graph mirrors while P8 owns fact rendering.

    Current writes and the legacy backfill both use the exact source URI.
    The bounded metadata check covers older mirrors that retained only the
    marker.  Those memories are rendered through the typed P8 projection
    instead, so an unavailable/unauthorized envelope stays absent.
    """

    if _p8_runtime is None:
        return rows

    def _is_shared_fact_mirror(row: Mapping[str, Any]) -> bool:
        if str(row.get("source_uri") or "") == "tom:shared_fact":
            return True
        metadata = row.get("metadata")
        if isinstance(metadata, Mapping):
            return metadata.get("shared_fact") is True
        raw = str(metadata or "")
        return bool(
            len(raw) <= 4_096
            and re.search(
                r"[\"']shared_fact[\"']\s*:\s*(?:true|True|1)",
                raw,
            )
        )

    return [
        row for row in rows
        if isinstance(row, Mapping) and not _is_shared_fact_mirror(row)
    ]


@router.get("/tom/p8/status")
async def tom_p8_status(request: Request) -> dict:
    authority = request_authority(request)
    person = str(authority.viewer_person_id or "").strip()
    _p8_viewer_for_request(request, person)
    if _p8_runtime is None:
        return {
            "enabled": False,
            "mode": "off",
            "delivery_effect": False,
            "authority_granted": False,
            "synchronous_voice_gate": False,
            "recipient_audit_scope": "owner_wide_or_exact_scope_revision",
            "fact_min_confidence": None,
        }
    return _p8_runtime.status()


@router.get("/tom/p8/deck")
async def tom_p8_deck(
    request: Request,
    person_id: Optional[str] = Query(None),
    max_facts: int = Query(24, ge=1, le=64),
    max_arcs: int = Query(24, ge=1, le=64),
    max_audit_events: int = Query(64, ge=1, le=256),
) -> dict:
    resolved = resolve_request_person(request, claimed_person_id=person_id)
    viewer = _p8_viewer_for_request(request, str(resolved or ""))
    if _p8_runtime is None:
        return {
            "enabled": False,
            "mode": "off",
            "facts": {"facts": []},
            "visibility": {"envelopes": []},
            "arcs": {"arcs": []},
            "recipient_audit": {"events": []},
            "coverage": {
                "status": "no_samples", "coverage_complete": False,
            },
            "advisory_only": True,
            "synchronous_voice_gate": False,
            "recipient_audit_scope": "owner_wide_or_exact_scope_revision",
            "fact_min_confidence": None,
        }
    return _p8_runtime.deck_projection(
        viewer,
        now=datetime.now(timezone.utc),
        subject_person_id=str(resolved or ""),
        max_facts=max_facts,
        max_arcs=max_arcs,
        max_audit_events=max_audit_events,
    )


@router.get("/health/llm")
async def llm_health() -> dict:
    """Live-fire the LLM router with one tiny SMALL-tier completion (v0.19.0).

    The cheapest faithful proxy for "can the cognition stack call a
    model at all" — it exercises the exact router path that dies with
    "all tiers exhausted" when the persisted baseUrl/apiKey are wrong.
    Defensive: never raises, always returns {ok, tier, latency_ms, error}.
    """
    if _llm_router is None:
        return {"ok": False, "tier": None, "latency_ms": 0,
                "error": "LLM router not wired"}
    try:
        from colony_sidecar.router.tiers import ModelTier
        resp = await _llm_router.complete(
            [{"role": "user", "content": "Say OK"}],
            force_tier=ModelTier.SMALL,
        )
        tier = resp.tier_used.value if getattr(resp, "tier_used", None) else None
        return {"ok": True, "tier": tier,
                "latency_ms": getattr(resp, "latency_ms", 0), "error": None}
    except Exception as exc:  # noqa: BLE001 — diagnostics must not 500
        return {"ok": False, "tier": None, "latency_ms": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

_NOT_WIRED = {"error": {"code": "not_wired", "message": "Backend not configured"}}

# Skill identifiers must be safe for filesystem paths and registry keys.
_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def _validate_skill_id(skill_id: str) -> None:
    if not _SKILL_ID_RE.match(skill_id):
        raise HTTPException(status_code=400, detail="invalid skill_id")


async def _graph_backend_reachable() -> bool:
    """Whether the wired graph's backend actually answers.

    The single availability determination for memory honesty — the same
    probe /memory/status reports as ``neo4j_connected``. False when no
    graph is wired at all.
    """
    if _graph is None:
        return False
    try:
        await _graph.driver.verify_connectivity()
        return True
    except Exception:
        return False


async def _raise_if_graph_unreachable(op: str) -> None:
    """503 when a wired graph backend is down.

    Called from memory endpoints' failure paths so a dead backend becomes an
    explicit error instead of an empty-success that is indistinguishable
    from "no data".
    """
    if _graph is not None and not await _graph_backend_reachable():
        raise HTTPException(status_code=503, detail={
            "code": "memory_backend_unavailable",
            "message": f"{op} failed: the graph backend is unreachable",
        })


@router.post("/memory/read", response_model=MemoryReadResponse)
async def memory_read(
    body: MemoryReadRequest,
    request: Request = None,
) -> MemoryReadResponse:
    person_id = resolve_request_person(
        request,
        claimed_person_id=body.person_id,
        audience=body.audience,
    )
    body.person_id = person_id
    if _graph is None:
        return MemoryReadResponse(entries=[])
    try:
        entries_raw = await _graph.read_memories(
            person_id=person_id,
            memory_id=body.memory_id,
            limit=body.limit or 20,
        )
        entries = [
            MemoryEntry(
                id=str(e.get("id") or uuid.uuid4()),
                content=str(e.get("content", "")),
                type=e.get("type"),
                strength=float(e["strength"]) if e.get("strength") is not None else None,
                person_id=e.get("person_id"),
                entities=e.get("entities"),
                tags=e.get("tags"),
                # Neo4j hydrates created_at as neo4j.time.DateTime; the wire
                # schema is a string, so normalise like memory_search does.
                created_at=str(e["created_at"]) if e.get("created_at") is not None else None,
                score=float(e["score"]) if e.get("score") is not None else None,
            )
            for e in entries_raw
        ]
        return MemoryReadResponse(entries=entries)
    except Exception as exc:
        logger.warning("memory_read failed: %s", exc)
        await _raise_if_graph_unreachable("memory_read")
        return MemoryReadResponse(entries=[])


@router.get("/memory/status")
async def memory_status():
    """Diagnostic for memory subsystem wiring."""
    neo4j_connected = False
    embeddings_ready = False
    vector_store_ready = False

    if _graph is not None:
        try:
            await _graph.driver.verify_connectivity()
            neo4j_connected = True
        except Exception:
            pass
        embeddings_ready = _graph._embed_fn is not None
        vector_store_ready = _graph._vector_store is not None

    wired = neo4j_connected and embeddings_ready and vector_store_ready
    return {
        "wired": wired,
        # distinguishes "no graph configured" from "configured but down"
        "graph_wired": _graph is not None,
        "neo4j_connected": neo4j_connected,
        "embeddings_ready": embeddings_ready,
        "vector_store_ready": vector_store_ready,
    }


@router.get("/memory/distill-preview")
async def memory_distill_preview(limit: int = 50) -> Dict[str, Any]:
    """Shadow distill previews: while COLONY_DISTILL_TURNS is off, every stored
    turn also computes what distillation WOULD have stored; the last 50 pairs
    (original vs distilled, newest first) live in an in-memory ring here so the
    flip can be validated against real traffic before it changes stored content."""
    enabled = os.environ.get("COLONY_DISTILL_TURNS", "0") not in ("0", "false", "no")
    if _graph is None or not hasattr(_graph, "distill_preview"):
        return {"enabled": enabled, "count": 0, "preview": []}
    try:
        items = _graph.distill_preview()[: max(0, int(limit))]
    except Exception as exc:
        logger.warning("memory_distill_preview failed: %s", exc)
        return {"enabled": enabled, "count": 0, "preview": []}
    return {"enabled": enabled, "count": len(items), "preview": items}


@router.post("/memory/write", response_model=MemoryWriteResponse)
async def memory_write(
    body: MemoryWriteRequest,
    request: Request = None,
) -> MemoryWriteResponse:
    person_id = resolve_request_person(
        request,
        claimed_person_id=body.person_id,
        context_person_id=(body.context.contact_id if body.context else None),
        audience=body.audience,
    )
    body.person_id = person_id
    if body.context is not None and person_id is not None:
        body.context.contact_id = person_id
    if _graph is None:
        # Degrade gracefully to match the pattern used by the rest of
        # the router (list_insights, list_briefings, etc.): when the
        # underlying store isn't wired, accept the call and mark the
        # write as not persisted rather than raising 501.
        return MemoryWriteResponse(id="", accepted=False)
    try:
        memory_id = await _graph.store_memory(
            content=body.content,
            person_id=person_id,
            memory_type=body.type or "episodic",
            entities=body.entities or [],
            importance=body.strength if body.strength is not None else 1.0,
            metadata={"tags": body.tags} if body.tags else None,
            source_type=body.source_type or "inference",
            source_uri=body.source_uri,
            source_version=body.source_version,
            content_hash=body.content_hash,
        )
        return MemoryWriteResponse(
            id=memory_id or str(uuid.uuid4()),
            accepted=True,
        )
    except Exception as exc:
        logger.warning("memory_write failed: %s", exc)
        await _raise_if_graph_unreachable("memory_write")
        return MemoryWriteResponse(id="error", accepted=False)


@router.post("/memory/search", response_model=MemorySearchResponse)
async def memory_search(
    body: MemorySearchRequest,
    request: Request = None,
) -> MemorySearchResponse:
    person_id = resolve_request_person(
        request,
        claimed_person_id=body.person_id,
        audience=body.audience,
    )
    body.person_id = person_id
    if _graph is None:
        return MemorySearchResponse(entries=[])
    try:
        results = await _graph.recall(
            query=body.query,
            limit=body.limit or 10,
            min_confidence=body.min_confidence if body.min_confidence is not None else 0.1,
            person_id=person_id,
        )
        entries = [
            MemoryEntry(
                id=str(e.get("id", "")),
                content=str(e.get("content", "")),
                type=e.get("type"),
                strength=float(e["strength"]) if "strength" in e and e["strength"] is not None else None,
                person_id=e.get("person_id"),
                entities=e.get("entities"),
                tags=e.get("tags"),
                created_at=str(e["created_at"]) if "created_at" in e and e["created_at"] is not None else None,
                score=float(e["relevance"]) if "relevance" in e and e["relevance"] is not None else None,
            )
            for e in results
        ]
        return MemorySearchResponse(entries=entries)
    except Exception as exc:
        logger.warning("memory_search failed: %s", exc)
        await _raise_if_graph_unreachable("memory_search")
        return MemorySearchResponse(entries=[])


@router.post("/memory/flush", response_model=MemoryFlushResponse)
async def memory_flush(body: MemoryFlushRequest) -> MemoryFlushResponse:
    if _graph is None:
        return MemoryFlushResponse(accepted=False)
    try:
        await _graph.flush(reason=body.reason)
        return MemoryFlushResponse(accepted=True)
    except Exception as exc:
        logger.warning("memory_flush failed: %s", exc)
        return MemoryFlushResponse(accepted=False)


@router.post("/memory/reconcile", response_model=MemoryReconcileResponse)
async def memory_reconcile(body: MemoryReconcileRequest) -> MemoryReconcileResponse:
    if _graph is None:
        return MemoryReconcileResponse()
    try:
        from colony_sidecar.intelligence.graph.reconciler import FileReconciler
        reconciler = FileReconciler(_graph)
        result = await reconciler.reconcile(dry_run=body.dry_run or False)
        return MemoryReconcileResponse(
            files_checked=result["files_checked"],
            memories_verified=result["memories_verified"],
            memories_staled=result["memories_staled"],
            memories_superseded=result["memories_superseded"],
            errors=result["errors"],
        )
    except Exception as exc:
        logger.warning("memory_reconcile failed: %s", exc)
        return MemoryReconcileResponse(
            errors=[str(exc)],
        )


@router.get("/memory/conflicts", response_model=MemoryConflictsResponse)
async def memory_conflicts() -> MemoryConflictsResponse:
    if _graph is None:
        return MemoryConflictsResponse()
    try:
        # Query CONFLICTS_WITH relationships
        async with _graph.driver.session(database=_graph.database) as session:
            result = await session.run(
                """
                MATCH (m1:Memory)-[r:CONFLICTS_WITH]->(m2:Memory)
                OPTIONAL MATCH (m1)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m2)
                RETURN m1.id AS id_a, m2.id AS id_b, e.name AS entity_name,
                       r.detected_at AS detected_at
                """
            )
            conflicts = []
            async for record in result:
                conflicts.append(MemoryConflictEntry(
                    memory_id_a=record["id_a"],
                    memory_id_b=record["id_b"],
                    entity_name=record["entity_name"] or "",
                    reason="Semantic conflict detected",
                    detected_at=str(record["detected_at"]) if record["detected_at"] else None,
                ))
            return MemoryConflictsResponse(conflicts=conflicts, total=len(conflicts))
    except Exception as exc:
        logger.warning("memory_conflicts failed: %s", exc)
        return MemoryConflictsResponse()


@router.get("/memory/stats", response_model=MemoryStatsResponse)
async def memory_stats() -> MemoryStatsResponse:
    if _graph is None:
        return MemoryStatsResponse()
    try:
        async with _graph.driver.session(database=_graph.database) as session:
            # Count by epistemic state
            result = await session.run(
                """
                MATCH (m:Memory)
                RETURN m.epistemic_state AS state, count(m) AS cnt
                """
            )
            by_state = {}
            async for record in result:
                by_state[record["state"] or "inferred"] = record["cnt"]
            # Count by source type
            result = await session.run(
                """
                MATCH (m:Memory)
                RETURN m.source_type AS source, count(m) AS cnt
                """
            )
            by_source = {}
            async for record in result:
                by_source[record["source"] or "inference"] = record["cnt"]
            # Count archived
            result = await session.run(
                """MATCH (a:ArchivedMemory) RETURN count(a) AS cnt"""
            )
            record = await result.single()
            total_archived = record["cnt"] if record else 0
            # Count protected
            result = await session.run(
                """MATCH (m:Memory) WHERE m.protected = true RETURN count(m) AS cnt"""
            )
            record = await result.single()
            protected_count = record["cnt"] if record else 0
            total_active = sum(v for k, v in by_state.items() if k != "archived")
            return MemoryStatsResponse(
                by_state=by_state,
                by_source=by_source,
                total_active=total_active,
                total_archived=total_archived,
                protected_count=protected_count,
            )
    except Exception as exc:
        logger.warning("memory_stats failed: %s", exc)
        return MemoryStatsResponse()


@router.post("/memory/verify", response_model=MemoryVerifyResponse)
async def memory_verify(body: MemoryVerifyRequest) -> MemoryVerifyResponse:
    if _graph is None:
        return MemoryVerifyResponse(memory_id=body.memory_id, verified=False)
    try:
        await _graph.verify_memory(body.memory_id)
        mem = await _graph.get_memory(body.memory_id)
        return MemoryVerifyResponse(
            memory_id=body.memory_id,
            verified=True,
            effective_confidence=float(mem.get("effective_confidence", 0.0)) if mem else 0.0,
        )
    except Exception as exc:
        logger.warning("memory_verify failed: %s", exc)
        return MemoryVerifyResponse(memory_id=body.memory_id, verified=False)


@router.post("/memory/embed", response_model=MemoryEmbedResponse)
async def memory_embed(body: MemoryEmbedRequest) -> MemoryEmbedResponse:
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    try:
        # Support both old `inputs` field and new `texts` field
        texts = body.texts or body.inputs
        if not texts:
            raise HTTPException(status_code=400, detail="No texts provided")
        if len(texts) > 128:
            raise HTTPException(status_code=400, detail=f"Batch size {len(texts)} exceeds limit of 128")
        vectors = await _embedder.embed_batch(texts)
        # Determine model_id from the underlying provider config
        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id
        return MemoryEmbedResponse(model=model_id or body.model or "unknown", vectors=vectors)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("memory_embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/rerank", response_model=RerankResponse)
async def memory_rerank(body: RerankRequest) -> RerankResponse:
    """Rerank documents by relevance to a query.

    Requires the reranker to be initialized (see COLONY_RERANKER_MODEL env var).
    """
    if _reranker is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Reranker not initialized. Set COLONY_RERANKER_MODEL to enable.",
        )
    try:
        if not body.documents:
            raise HTTPException(status_code=400, detail="No documents provided")
        if len(body.documents) > 256:
            raise HTTPException(
                status_code=400,
                detail=f"Document count {len(body.documents)} exceeds limit of 256",
            )
        results = await _reranker.rerank(
            query=body.query,
            documents=body.documents,
            top_k=body.top_k or 10,
        )
        return RerankResponse(
            results=[
                RerankResult(index=r.index, score=r.score, text=r.text)
                for r in results
            ],
            model=getattr(_reranker, "_model_id", "unknown"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("memory_rerank failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/embed/health", response_model=EmbedHealthResponse)
async def embed_health() -> EmbedHealthResponse:
    """Check embedder health — verify model is loaded and producing valid output."""
    if _embedder is None:
        return EmbedHealthResponse(status="error", error="embedder not initialized")
    try:
        result = await _embedder.health_check()
        # Add multimodal status
        result["modalities"] = _embedder.modalities if hasattr(_embedder, "modalities") else ["text"]
        result["multimodal_enabled"] = _embedder.is_multimodal if hasattr(_embedder, "is_multimodal") else False
        return EmbedHealthResponse(**result)
    except Exception as exc:
        return EmbedHealthResponse(status="error", error=str(exc))


@router.post("/memory/embed/image", response_model=ImageEmbedResponse)
async def memory_embed_image(body: ImageEmbedRequest) -> ImageEmbedResponse:
    """Embed a single image and optionally store it."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    if not _embedder.is_multimodal:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Multimodal not enabled")

    try:
        # Determine image source
        source = body.image or body.image_url or body.image_path
        if not source:
            raise HTTPException(status_code=400, detail="No image provided (use image, image_url, or image_path)")

        vector, meta = await _embedder.embed_image(
            source,
            mime_type=body.mime_type or "",
            caption=body.caption or "",
        )

        # If collection and id provided, also index it
        if body.collection and body.id:
            from colony_sidecar.vector import get_store
            from colony_sidecar.vector.collections import Collection
            from colony_sidecar.vector.query import VectorItem

            store = get_store()
            if store:
                try:
                    col = Collection(body.collection)
                except ValueError:
                    col = Collection.MEMORIES
                vi = VectorItem(
                    id=body.id,
                    text=meta.get("caption", ""),
                    vector=vector,
                    metadata=meta,
                )
                await store.add_batch(col, [vi])

        model_id = meta.get("model_id", "")
        return ImageEmbedResponse(
            model=model_id,
            vector=vector,
            image_hash=meta.get("image_hash", ""),
            image_ref=meta.get("image_ref", ""),
            thumbnail_ref=meta.get("thumbnail_ref", ""),
            caption=meta.get("caption", ""),
            width=meta.get("width", 0),
            height=meta.get("height", 0),
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("image embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/embed/image/batch", response_model=ImageBatchEmbedResponse)
async def memory_embed_image_batch(body: ImageBatchEmbedRequest) -> ImageBatchEmbedResponse:
    """Embed multiple images."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    if not _embedder.is_multimodal:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Multimodal not enabled")
    if len(body.images) > 32:
        raise HTTPException(status_code=400, detail=f"Batch size {len(body.images)} exceeds limit of 32")

    try:
        results = []
        for img_item in body.images:
            source = img_item.get("image") or img_item.get("image_url") or img_item.get("image_path")
            if not source:
                continue
            vector, meta = await _embedder.embed_image(
                source,
                mime_type=img_item.get("mime_type", ""),
                caption=img_item.get("caption", ""),
            )
            results.append({"vector": vector, **meta})

        model_id = results[0].get("model_id", "") if results else ""
        return ImageBatchEmbedResponse(model=model_id, results=results)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("image batch embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/embed/async")
async def memory_embed_async(body: dict) -> dict:
    """Async embedding for large collections — returns task_id immediately.

    Accepts the same format as /memory/embed, /memory/embed/image/batch,
    or /memory/index but runs in the background.
    Poll GET /memory/embed/async/{task_id} for status.
    """
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from colony_sidecar.vector import get_store
    store = get_store()

    task_id = str(uuid.uuid4())
    _async_embed_tasks: dict = getattr(router, "_async_embed_tasks", {})
    router._async_embed_tasks = _async_embed_tasks

    embed_type = body.get("type", "texts")  # texts | images | index

    async def _run():
        try:
            _async_embed_tasks[task_id] = {"status": "running", "processed": 0, "failed": 0}

            if embed_type == "texts":
                texts = body.get("texts", body.get("inputs", []))
                if len(texts) > 1024:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": f"Batch size {len(texts)} exceeds 1024"}
                    return
                vectors = await _embedder.embed_batch(texts)
                _async_embed_tasks[task_id] = {"status": "completed", "processed": len(vectors), "failed": 0}

            elif embed_type == "images":
                images = body.get("images", [])
                if len(images) > 128:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": f"Batch size {len(images)} exceeds 128"}
                    return
                results = []
                failed = 0
                for img_item in images:
                    try:
                        source = img_item.get("image") or img_item.get("image_url") or img_item.get("image_path")
                        if not source:
                            failed += 1
                            continue
                        vector, meta = await _embedder.embed_image(
                            source, mime_type=img_item.get("mime_type", ""),
                            caption=img_item.get("caption", ""),
                        )
                        results.append({"vector": vector, **meta})
                    except Exception:
                        failed += 1
                _async_embed_tasks[task_id] = {"status": "completed", "processed": len(results), "failed": failed}

            elif embed_type == "index":
                if store is None:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": "VectorStore not initialized"}
                    return
                items = body.get("items", [])
                indexed = 0
                failed = 0
                for item in items:
                    try:
                        from colony_sidecar.vector.collections import Collection
                        from colony_sidecar.vector.query import VectorItem

                        if item.get("image") or item.get("image_url") or item.get("image_path"):
                            source = item.get("image") or item.get("image_url") or item.get("image_path")
                            vector, meta = await _embedder.embed_image(
                                source, mime_type=item.get("mime_type", ""),
                                caption=item.get("caption", ""),
                            )
                            col_name = item.get("collection", "memories")
                            try: col = Collection(col_name)
                            except ValueError: col = Collection.MEMORIES
                            vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=meta.get("caption", ""), vector=vector, metadata=meta)
                        else:
                            text = item.get("text", "")
                            vector = await _embedder.embed(text)
                            col_name = item.get("collection", "memories")
                            try: col = Collection(col_name)
                            except ValueError: col = Collection.MEMORIES
                            meta = item.get("metadata", {})
                            meta["model_id"] = _embedder._provider._config.model_id if hasattr(_embedder, "_provider") else ""
                            vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=text, vector=vector, metadata=meta)

                        await store.add_batch(col, [vi])
                        indexed += 1
                    except Exception:
                        failed += 1
                _async_embed_tasks[task_id] = {"status": "completed", "indexed": indexed, "failed": failed}

        except Exception as exc:
            _async_embed_tasks[task_id] = {"status": "failed", "error": str(exc)}

    _spawn_task(_run())
    return {"task_id": task_id, "status": "started"}


@router.get("/memory/embed/async/{task_id}")
async def async_embed_status(task_id: str) -> dict:
    """Poll status of an async embed task."""
    _async_embed_tasks: dict = getattr(router, "_async_embed_tasks", {})
    result = _async_embed_tasks.get(task_id)
    if result is None:
        return {"task_id": task_id, "status": "running"}
    return {"task_id": task_id, **result}


@router.post("/memory/search/multimodal", response_model=MultimodalSearchResponse)
async def memory_search_multimodal(body: MultimodalSearchRequest) -> MultimodalSearchResponse:
    """Cross-modal search — text query finds images, image query finds text."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from colony_sidecar.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    try:
        from colony_sidecar.vector.collections import Collection

        col_name = body.collection or "memories"
        try:
            col = Collection(col_name)
        except ValueError:
            col = Collection.MEMORIES

        # Get query vector
        if body.query:
            if _embedder.is_multimodal:
                query_vector = await _embedder._multimodal_provider.embed_text(body.query)
            else:
                query_vector = await _embedder.embed(body.query)
        elif body.query_image:
            if not _embedder.is_multimodal:
                raise HTTPException(status_code=400, detail="Image query requires multimodal to be enabled")
            vector, _ = await _embedder.embed_image(body.query_image)
            query_vector = vector
        else:
            raise HTTPException(status_code=400, detail="No query provided (use query or query_image)")

        p8_memory_policy = (
            _p8_runtime is not None and col == Collection.MEMORIES)
        search_limit = body.limit
        if p8_memory_policy:
            requested = max(1, min(int(body.limit or 10), 100))
            search_limit = min(max(requested * 20, requested), 200)

        results = await store.search_cross_modal(
            col, query_vector,
            limit=search_limit,
            filter_modality=body.filter_modality,
            min_score=body.min_score,
        )
        if p8_memory_policy:
            filterer = getattr(
                _graph, "filter_memory_vector_results", None)
            if not callable(filterer):
                # Ambiguous legacy vector text cannot be authorized without
                # authoritative graph hydration.
                results = []
            else:
                results = await filterer(results)
            results = results[:max(0, int(body.limit))]

        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id

        return MultimodalSearchResponse(results=results, model=model_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("multimodal search failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/backfill", response_model=BackfillResponse)
async def memory_backfill(body: BackfillRequest) -> BackfillResponse:
    """Re-embed all vectors using the current embedding pipeline.

    Returns a task_id immediately; backfill runs in the background.
    """
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from colony_sidecar.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    task_id = str(uuid.uuid4())

    async def _run():
        from colony_sidecar.vector.backfill import backfill
        try:
            result = await backfill(store, _embedder, collection=body.collection, batch_size=body.batch_size)
            # Store result in app state for polling
            _backfill_results[task_id] = result
        except Exception as exc:
            logger.error("Backfill failed: %s", exc)

    _backfill_results: dict = getattr(router, "_backfill_results", {})
    router._backfill_results = _backfill_results

    _spawn_task(_run())
    return BackfillResponse(task_id=task_id, status="started")


@router.get("/memory/backfill/{task_id}", response_model=BackfillResponse)
async def backfill_status(task_id: str) -> BackfillResponse:
    """Check the status of a running backfill task."""
    _backfill_results: dict = getattr(router, "_backfill_results", {})
    result = _backfill_results.get(task_id)
    if result is None:
        return BackfillResponse(task_id=task_id, status="running")
    return BackfillResponse(
        task_id=task_id,
        status="completed",
        total=result.total,
        processed=result.processed,
        failed=result.failed,
        skipped=result.skipped,
        duration_s=round(result.duration_s, 2),
        errors=result.errors,
    )


@router.post("/memory/migrate", response_model=MigrateResponse)
async def memory_migrate(body: MigrateRequest) -> MigrateResponse:
    """Migrate all vectors from an old model to the current embedding model."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from colony_sidecar.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    if getattr(router, '_migrate_running', False):
        raise HTTPException(status_code=409, detail='A vector rebuild is already running')
    if not 1 <= body.batch_size <= 128:
        raise HTTPException(status_code=422, detail='Rebuild batch size must be between 1 and 128')
    if getattr(store, 'catalog', None) is not None and body.old_model_id:
        raise HTTPException(status_code=422, detail='A generation rebuild must include all retained rows')

    task_id = str(uuid.uuid4())
    if getattr(store, 'catalog', None) is not None:
        task_id = store.catalog.begin(store.identity)['id']
    router._migrate_running = True
    router._migrate_task_id = task_id

    async def _run():
        from colony_sidecar.vector.migrate import migrate_tier
        try:
            result = await migrate_tier(store, _embedder, old_model_id=body.old_model_id,
                                        batch_size=body.batch_size, graph=_graph)
            _migrate_results[task_id] = result
        except Exception as exc:
            logger.error("Migration failed: %s", exc)
            from colony_sidecar.vector.migrate import MigrationResult
            _migrate_results[task_id] = MigrationResult(errors=[str(exc)], generation_id=task_id)
            if getattr(store, 'catalog', None) is not None:
                store.catalog.finish(task_id, error=str(exc))
        finally:
            router._migrate_running = False

    _migrate_results: dict = getattr(router, "_migrate_results", {})
    router._migrate_results = _migrate_results
    _migrate_results.pop(task_id, None)

    _spawn_task(_run())
    return MigrateResponse(task_id=task_id, status="started")


@router.get("/memory/migrate/{task_id}", response_model=MigrateResponse)
async def migrate_status(task_id: str) -> MigrateResponse:
    """Check the status of a running migration task."""
    if getattr(router, '_migrate_running', False) and getattr(router, '_migrate_task_id', None) == task_id:
        return MigrateResponse(task_id=task_id, status='running', generation_id=task_id)
    _migrate_results: dict = getattr(router, "_migrate_results", {})
    result = _migrate_results.get(task_id)
    if result is None:
        from colony_sidecar.vector import get_store
        store = get_store()
        if store is not None and getattr(store, 'catalog', None) is not None:
            generation = next((g for g in store.catalog.generations() if g['id'] == task_id), None)
            if generation:
                active = store.catalog.active()
                return MigrateResponse(task_id=task_id, generation_id=task_id,
                    fingerprint=generation['fingerprint'] or '',
                    status='completed' if generation['status'] in {'ready', 'retained'} else 'resumable',
                    errors=[generation['error']] if generation.get('error') else [])
        raise HTTPException(status_code=404, detail='Unknown vector rebuild')
    return MigrateResponse(
        task_id=task_id,
        status="failed" if result.errors else "completed",
        collections_migrated=result.collections_migrated,
        vectors_migrated=result.vectors_migrated,
        vectors_failed=result.vectors_failed,
        duration_s=round(result.duration_s, 2),
        errors=result.errors,
        generation_id=getattr(result, 'generation_id', ''),
        fingerprint=getattr(result, 'fingerprint', ''),
    )


class VectorVacuumRequest(BaseModel):
    dry_run: bool = True
    max_delete: Optional[int] = None


@router.post("/memory/vector-vacuum")
async def memory_vector_vacuum(body: VectorVacuumRequest) -> dict:
    """Explicit admin op: remove orphaned memory vectors (ANN entries whose
    graph node was deleted without its vector). Orphans keep matching in
    semantic search and then vanish at hydration, stealing recall slots.

    dry_run defaults true (count + sample only). Fails closed: a Neo4j
    error aborts before any deletion — see ColonyGraph.vacuum_orphan_vectors.
    """
    if _graph is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail="Memory graph not initialized")
    if not hasattr(_graph, "vacuum_orphan_vectors"):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail="Graph backend lacks vacuum_orphan_vectors")
    try:
        return await _graph.vacuum_orphan_vectors(
            dry_run=body.dry_run, max_delete=body.max_delete)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"vector vacuum aborted: {exc}")


@router.post("/memory/index", response_model=IndexResponse)
async def memory_index(body: IndexRequest) -> IndexResponse:
    """Embed and store items in one call."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from colony_sidecar.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    if not body.items:
        return IndexResponse(model="unknown", indexed=0, failed=0)
    if len(body.items) > 128:
        raise HTTPException(status_code=400, detail=f"Batch size {len(body.items)} exceeds limit of 128")

    try:
        from colony_sidecar.vector.collections import Collection
        from colony_sidecar.vector.query import VectorItem

        # Determine current model_id
        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id

        # Separate text items from image items
        text_items = []
        image_items = []
        for item in body.items:
            if item.get("image") or item.get("image_url") or item.get("image_path"):
                image_items.append(item)
            else:
                text_items.append(item)

        indexed = 0
        failed = 0

        # Process text items
        if text_items:
            texts = [item.get("text", "") for item in text_items]
            vectors = await _embedder.embed_batch(texts)
            for item, vector in zip(text_items, vectors):
                try:
                    col_name = item.get("collection", "memories")
                    try:
                        col = Collection(col_name)
                    except ValueError:
                        col = Collection.MEMORIES
                    meta = item.get("metadata", {})
                    meta["model_id"] = model_id
                    vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=item.get("text", ""), vector=vector, metadata=meta)
                    await store.add_batch(col, [vi])
                    indexed += 1
                except Exception as exc:
                    logger.warning("index text item failed: %s", exc)
                    failed += 1

        # Process image items
        for item in image_items:
            try:
                source = item.get("image") or item.get("image_url") or item.get("image_path")
                if not source:
                    failed += 1
                    continue
                vector, meta = await _embedder.embed_image(
                    source,
                    mime_type=item.get("mime_type", ""),
                    caption=item.get("caption", ""),
                )
                col_name = item.get("collection", "memories")
                try:
                    col = Collection(col_name)
                except ValueError:
                    col = Collection.MEMORIES
                vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=meta.get("caption", ""), vector=vector, metadata=meta)
                await store.add_batch(col, [vi])
                indexed += 1
            except Exception as exc:
                logger.warning("index image item failed: %s", exc)
                failed += 1

        return IndexResponse(model=model_id or "unknown", indexed=indexed, failed=failed)
    except Exception as exc:
        logger.warning("memory_index failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


async def _build_temporal_section(
    contact_id: Optional[str], override_tz: Optional[str] = None,
    *, include_global_heads_up: bool = True,
) -> ContextSection:
    """The agent's reference frame for "now" (v0.21.0, factored for per-turn use).

    The host is the authoritative clock; this fulfils the promise the
    colony-memory plugin makes ("prefer the real current time provided by the
    host"). Agent home tz + the contact's local tz + elapsed-since-last-contact
    + time-sensitive heads-up items. Cheap (no memory search): safe per turn.
    """
    from colony_sidecar.util import temporal as _temporal
    agent_tz = _temporal.agent_timezone()
    contact_tz = None
    contact_label = "the contact"
    contact_obj = None
    if _contacts_store is not None and contact_id:
        try:
            contact_obj = await _contacts_store.get(contact_id)
            if contact_obj is not None:
                contact_tz = getattr(contact_obj, "timezone", None)
                contact_label = (
                    contact_obj.display_name or contact_obj.given_name or "the contact"
                )
        except Exception:
            pass
    comm_tz = _temporal.resolve_communication_timezone(contact_tz, override_tz)
    t_lines = [_temporal.describe_now(agent_tz, comm_tz, contact_label)]
    if contact_obj is not None and getattr(contact_obj, "last_interaction_at", None):
        li = contact_obj.last_interaction_at
        t_lines.append(
            f"Last exchange with {contact_label}: {_temporal.humanize_delta(li)} "
            f"({_temporal.bucket(li, agent_tz)})."
        )
    if include_global_heads_up:
        try:
            from colony_sidecar.util.session_safety import load_last_user_message_at
            last_owner = load_last_user_message_at()
            if last_owner:
                t_lines.append(
                    f"Owner last messaged {_temporal.humanize_delta(last_owner)}.")
        except Exception:
            pass
    # Heads-up: time-sensitive items (overdue commitments + cadence-overdue contacts)
    heads = []
    if include_global_heads_up:
        try:
            if _commitment_store is not None:
                for c in (_commitment_store.get_overdue() or [])[:3]:
                    desc = (c.get("description") or "a commitment")[:80]
                    heads.append(
                        f"⚠️ Overdue: {desc} (was due {_temporal.humanize_delta(c.get('due_at'))})"
                    )
        except Exception:
            pass
        try:
            if _contacts_store is not None:
                exclude = {contact_id} if contact_id else set()
                overdue_contacts = await _contacts_store.compute_cadence_overdue(
                    overdue_only=True, limit=3, exclude_ids=exclude,
                )
                for o in overdue_contacts[:2]:
                    heads.append(
                        f"🕰️ Haven't talked to {o['name']} in {int(o['days_since'])}d "
                        f"(usually ~{o['cadence_days']:g}d)."
                    )
        except Exception:
            pass
    if heads:
        t_lines.append("Heads-up:")
        t_lines.extend("  " + h for h in heads)
    t_lines.append(
        "^ This is the authoritative CURRENT date/time — this is NOW. Ignore any "
        "'Conversation started' date in your system prompt; that is only when this "
        "long-running session began (often days ago), NOT today. Greet and compute "
        "elapsed/upcoming relative to the time above."
    )
    return ContextSection(
        id="temporal-context",
        title="Current Time",
        body="\n".join(t_lines),
        priority=100,
    )


@router.get("/context/temporal")
async def context_temporal(contact_id: Optional[str] = None,
                           tz: Optional[str] = None,
                           request: Request = None) -> dict:
    """Always-fresh temporal brief for per-turn injection (no caching layer).

    The memory provider calls this every turn so the agent's Current Time
    block can never go stale inside a long-running session (the full
    /context/assemble result is session-cached by design; time must not be).
    """
    # Preserve the exact legacy selector contract while P8 is off. Scoped
    # resolution is required only when it will attest (or deny) a P8 viewer.
    resolved_contact = contact_id
    if _p8_runtime is not None:
        resolved_contact = resolve_request_person(
            request, context_person_id=contact_id) or contact_id
    viewer = None
    if _p8_runtime is not None and resolved_contact:
        try:
            viewer = _p8_viewer_for_request(request, resolved_contact)
        except HTTPException:
            logger.debug(
                "P8 temporal global heads-up omitted: scoped viewer unavailable")
    exact_person_allowed = _p8_exact_person_context_allowed(viewer)
    section = await _build_temporal_section(
        resolved_contact if exact_person_allowed else None,
        tz,
        include_global_heads_up=_p8_legacy_global_context_allowed(viewer),
    )
    return {"id": section.id, "title": section.title, "body": section.body}


@router.get(
    "/context/projection-readiness",
    response_model=ContextProjectionAttestation,
)
async def context_projection_readiness(
    request: Request,
    contact_id: str = Query(..., min_length=1, max_length=256),
) -> ContextProjectionAttestation:
    """Check a viewer-specific context projection without querying producers."""

    resolved = resolve_request_person(
        request,
        context_person_id=contact_id,
    ) or ""
    viewer = None
    try:
        viewer = _p8_viewer_for_request(request, resolved)
    except HTTPException:
        # Return an explicit negative posture.  This endpoint never falls back
        # to body-selected or legacy-global context.
        pass
    return _context_projection_attestation(
        contact_id=resolved,
        viewer=viewer,
    )


@router.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(
    body: ContextAssembleRequest,
    request: Request = None,
) -> ContextAssembleResponse:
    body.context.contact_id = resolve_request_person(
        request,
        context_person_id=body.context.contact_id,
        audience=body.audience,
    ) or body.context.contact_id
    _require_scoped_context_runtime_for_guest(
        request, body.context.contact_id)
    _attested_viewer = None
    try:
        _attested_viewer = _p8_viewer_for_request(
            request, body.context.contact_id)
    except HTTPException:
        logger.debug("Context viewer attestation unavailable")
    _projection = _context_projection_attestation(
        contact_id=body.context.contact_id,
        viewer=_attested_viewer,
    )
    if body.projection_policy == "scoped_viewer_required" and not (
        _projection.viewer_attested
        and _projection.scoped_projection_ready
        and not _projection.legacy_global_allowed
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "scoped_projection_required",
                "message": (
                    "exact viewer authority and a supported scoped projection are "
                    "required before context producers may run"
                ),
            },
        )
    _p8_viewer = _attested_viewer if _p8_runtime is not None else None
    _canonical_only = _projection.projection_backend == "canonical_sources"
    _legacy_global_allowed = not _canonical_only and _p8_legacy_global_context_allowed(_p8_viewer)
    # Legacy person stores can contain private owner observations ABOUT a
    # guest. Exact subject identity does not make those observations shareable.
    _exact_person_allowed = not _canonical_only and _p8_exact_person_context_allowed(_p8_viewer)
    _canonical_person_allowed = _canonical_only or _exact_person_allowed
    _tom_context_facts = None if _canonical_only else _facts_store
    if _p8_runtime is not None:
        _tom_context_facts = (
            _p8_runtime.projected_facts_view(
                _p8_viewer, now=datetime.now(timezone.utc))
            if _p8_viewer is not None else None
        )
    # Context assembly pulls from identity + memory + goals + contacts + world model + skills
    sections: list[ContextSection] = []
    query_text = body.incoming_message.content if body.incoming_message else ""

    # --- Temporal Context: see _build_temporal_section ---
    try:
        cid = (
            body.context.contact_id
            if body.context and _exact_person_allowed else None
        )
        override_tz = getattr(body.context, "timezone", None) if body.context else None
        if _canonical_only:
            sections.append(ContextSection(
                id="temporal-context", title="Current Time", priority=100,
                body="Current UTC time: " + datetime.now(timezone.utc).isoformat()))
        else:
            sections.append(await _build_temporal_section(
                cid,
                override_tz,
                include_global_heads_up=_legacy_global_allowed,
            ))
    except Exception as exc:
        logger.debug("context_assemble temporal section failed: %s", exc)

    if not _canonical_only:
        # --- Colony Identity ---
        identity_lines = []
        try:
            from colony_sidecar.chain.identity import get_or_create_colony_id, get_genesis_manifest
            from colony_sidecar.chain.node import get_or_create_node_id
            state_dir = Path(os.environ.get("COLONY_STATE_DIR", os.path.expanduser("~/.colony")))
            colony_id = get_or_create_colony_id(state_dir)
            identity_lines.append(f"Colony ID: {colony_id}")
            manifest = get_genesis_manifest()
            if manifest:
                identity_lines.append("Genesis: yes (trust anchor)")
            else:
                identity_lines.append("Genesis: no")
            node_id = get_or_create_node_id(state_dir)
            identity_lines.append(f"Node ID: {node_id}")
        except Exception as exc:
            logger.debug("context_assemble identity section failed: %s", exc)
        if identity_lines:
            sections.append(ContextSection(
                id="colony-identity",
                title="Who I Am",
                body="\n".join(identity_lines),
                priority=100,
            ))

        # --- Self-knowledge: when the message asks about Colony ITSELF
        # (capabilities, architecture, subsystems), ground the answer in the
        # identity-bootstrap corpus instead of leaving the model to guess. ---
        try:
            from colony_sidecar.identity_bootstrap.self_query import (
                build_self_context_from_corpus, query_is_self_referential)
            if query_text and query_is_self_referential(query_text):
                self_ctx = build_self_context_from_corpus()
                if self_ctx:
                    sections.append(ContextSection(
                        id="colony-self-knowledge",
                        title="What I Am",
                        body=self_ctx,
                        priority=100,
                    ))
        except Exception as exc:
            logger.debug("context_assemble self-knowledge section failed: %s", exc)

    # --- Memory: authorized candidates, one selection and one budget ---
    if _canonical_person_allowed and query_text:
        from colony_sidecar.intelligence.graph.recall import source_candidates
        beliefs, quotations, source_hits, semantic_media = [], [], [], []
        source_ledger = None
        if not _canonical_only and _graph is not None:
            try:
                candidates_fn = getattr(_graph, "recall_candidates", None)
                recall_kwargs = {
                    "query": query_text,
                    "limit": 25 if callable(candidates_fn) else 5,
                    "person_id": body.context.contact_id if body.context else None,
                }
                if _p8_runtime is not None:
                    recall_kwargs["exclude_source_uris"] = ["tom:shared_fact"]
                recall_fn = candidates_fn if callable(candidates_fn) else _graph.recall
                beliefs = _p8_filter_graph_recall(await recall_fn(**recall_kwargs))
            except Exception as exc:
                logger.warning("context_assemble memory search failed: %s", exc)

        # Source text is a candidate producer, not a separate injection path.
        # Checkpoints additionally require the original session because native
        # compression history does not attest every earlier speaker's identity.
        if body.context and body.context.contact_id:
            try:
                from colony_sidecar.turns import get_turn_idempotency_ledger
                if (Path(get_state_dir()) / "turn-idempotency.db").exists():
                    source_ledger = get_turn_idempotency_ledger(get_state_dir())
                    source_hits = source_ledger.search_sources(
                        query_text, contact_id=body.context.contact_id,
                        session_id=body.context.session_id, limit=10)
                    try:
                        from colony_sidecar.turns.source_vectors import SourceVectors, merge_source_hits
                        from colony_sidecar.vector import get_store, get_pipeline
                        semantic_hits, semantic_media = await SourceVectors(
                            source_ledger, get_store(), get_pipeline()).search(query_text,
                            contact_id=body.context.contact_id, session_id=body.context.session_id, limit=15)
                        source_hits = merge_source_hits(source_hits, semantic_hits)
                    except Exception as exc:
                        logger.debug("source semantic recall unavailable (%s); retaining lexical evidence", type(exc).__name__)
                    quotations = source_candidates(source_hits)
            except Exception as exc:
                logger.warning("source evidence recall failed (%s)", type(exc).__name__)

        try:
            # Redacted source turns invalidate their entire graph summary.
            # Source search separately excludes erased message excerpts while
            # retaining unrelated quotations from a partially redacted turn.
            erased_filter = (getattr(_graph, "_filter_erased_source_memories", None)
                             if not _canonical_only else None)
            if not _canonical_only and callable(erased_filter):
                beliefs = await erased_filter(beliefs)
            from colony_sidecar.beliefs.source_time import interpret_time_query, filter_unstructured
            from colony_sidecar.util import temporal as memory_temporal
            contact_tz = None
            if not _canonical_only and _contacts_store is not None and body.context.contact_id:
                try:
                    contact = await _contacts_store.get(body.context.contact_id)
                    contact_tz = getattr(contact, "timezone", None)
                except Exception:
                    logger.debug("contact timezone unavailable for memory recall", exc_info=True)
            time_query = interpret_time_query(
                query_text, now=memory_temporal.now_utc(),
                timezone_name=memory_temporal.resolve_communication_timezone(
                    contact_tz, body.context.timezone or ("UTC" if _canonical_only else None)))
            if source_ledger is not None:
                from colony_sidecar.beliefs.source_projection import SourceClaimProjection
                beliefs, quotations = SourceClaimProjection(source_ledger).prepare_context(
                    beliefs, source_hits, contact_id=body.context.contact_id,
                    session_id=body.context.session_id, time_query=time_query)
                from colony_sidecar.turns.media import SourceMedia
                media_hits = SourceMedia(source_ledger).search(
                    query_text, contact_id=body.context.contact_id, session_id=body.context.session_id)
                media_by_id = {row['id']: row for row in media_hits + semantic_media}
                quotations.extend(filter_unstructured(list(media_by_id.values()), time_query))
            else:
                beliefs = filter_unstructured(beliefs, time_query)
            try:
                max_chars = int(os.environ.get("COLONY_RECALL_CONTEXT_MAX_CHARS", "6000"))
            except (TypeError, ValueError):
                max_chars = 6000
            selected, body_text = await _memory_context_selector().select_context(
                query_text, beliefs, quotations, limit=5,
                max_chars=max(0, min(max_chars, 24000)))
            if body_text:
                sections.append(ContextSection(
                    id="colony-memory", title="Relevant Memories",
                    body=body_text, priority=90))
                record_use = (getattr(_graph, "record_recall_use", None)
                              if not _canonical_only else None)
                if not _canonical_only and callable(record_use):
                    record_use(selected)
        except Exception as exc:
            logger.warning("combined memory selection failed (%s)", type(exc).__name__)

    # --- Active Goals ---
    if _legacy_global_allowed and _goals_store is not None:
        try:
            from colony_sidecar.goals.models import GoalStatus
            goals = _goals_store.list_goals(status=GoalStatus.ACTIVE)
            if goals:
                body_text = "\n".join(
                    f"- [{g.priority.name.lower()}] {g.title}: {g.description} (progress: {g.progress_pct:.0%})"
                    for g in goals[:5]
                )
                sections.append(ContextSection(
                    id="colony-goals",
                    title="Active Goals",
                    body=body_text,
                    priority=80,
                ))
        except Exception as exc:
            logger.warning("context_assemble goals failed: %s", exc)

    # --- Pending Initiatives (v0.13.0) ---
    if _legacy_global_allowed and body.include_initiatives \
            and _initiative_store is not None:
        try:
            pending = _initiative_store.list(status=["pending"], limit=10)
            if pending:
                body_text = "\n".join(
                    f"• [{i.type}] {i.description} (priority: {i.priority:.0%})"
                    for i in pending
                )
                sections.append(ContextSection(
                    id="colony-initiatives",
                    title="Pending Initiatives",
                    body=body_text,
                    priority=50,
                ))
        except Exception as exc:
            logger.warning("context_assemble initiatives failed: %s", exc)

    # --- Contact Briefing ---
    if _legacy_global_allowed and _briefings_engine is not None \
            and body.context and body.context.contact_id:
        try:
            briefings = _briefings_engine.get_recent(limit=3)
            if briefings:
                body_text = "\n".join(f"- {b}" for b in briefings) if isinstance(briefings, list) else str(briefings)
                sections.append(ContextSection(
                    id="colony-briefing",
                    title="Contact Briefing",
                    body=body_text,
                    priority=85,
                ))
        except Exception as exc:
            logger.warning("context_assemble briefings failed: %s", exc)

    # --- World Model Entities ---
    if _legacy_global_allowed and _world_store is not None and query_text:
        try:
            entities = await _world_context_entities(query_text, limit=5)
            if entities:
                body_text = "\n".join(
                    f"- [{e.entity_type}] {e.name}" if hasattr(e, 'entity_type') else f"- {e}"
                    for e in entities
                )
                sections.append(ContextSection(
                    id="colony-world-model",
                    title="Related Entities",
                    body=body_text,
                    priority=70,
                ))
        except Exception as exc:
            logger.warning("context_assemble world model failed: %s", exc)

    # --- Available Skills ---
    if not _canonical_only and _skills_registry is not None:
        try:
            skills = await _skills_registry.list_all()
            if skills:
                body_text = "\n".join(f"- {s.name}: {s.description}" for s in skills[:8])
                sections.append(ContextSection(
                    id="colony-skills",
                    title="Available Skills",
                    body=body_text,
                    priority=50,
                ))
        except Exception as exc:
            logger.warning("context_assemble skills failed: %s", exc)

    # --- Scoped execution observations (not a commitment lock) ---
    try:
        from colony_sidecar.api.routers.executions import authorized_viewer, with_queue_work
        from colony_sidecar.turns.executions import registry, format_view
        person, owner = authorized_viewer(request, body.context.contact_id, scope="context:read")
        # Public/guest turns do not get cross-session activity. The owner view
        # is sealed from existing exact person grants, never a body owner flag.
        if owner:
            work = registry().view(contact_id=person, owner=True, limit=8)
            work = await with_queue_work(work, owner=True, limit=8)
            if work["items"] or work.get('worker_work', {}).get('items') or work.get('native_cron'):
                sections.append(ContextSection(id="colony-executions", title="Observed current work", body=format_view(work), priority=73))
    except HTTPException:
        pass
    except Exception:
        logger.debug("execution observation view unavailable", exc_info=True)

    # --- Pending Commitments ---
    contact_id = body.context.contact_id if body.context else None
    if _canonical_person_allowed and _commitment_store is not None:
        try:
            commitments = _commitment_store.list(
                person_id=contact_id, status=["pending", "overdue"], limit=50 if _canonical_only else 5,
            )
            # The exact-person list already includes overdue rows. A sealed
            # guest must not trigger a global get-then-filter query.
            overdue = (
                _commitment_store.get_overdue()
                if _legacy_global_allowed else []
            )
            if contact_id and overdue:
                overdue = [
                    c for c in overdue
                    if c.get("person_id") == contact_id
                ]
            _listed = (commitments if isinstance(commitments, list)
                       else commitments.get("commitments", []))
            if _canonical_only:
                _listed = _canonical_shared_commitments(_listed, contact_id)
            _seen_ids = {c.get("id") for c in _listed}
            all_comms = _listed + [c for c in overdue[:5]
                                   if c.get("id") not in _seen_ids]
            if all_comms:
                from colony_sidecar.commitments.work import CommitmentWork
                reservations = {}
                reservations_available = True
                try:
                    reservations = CommitmentWork(_commitment_store).for_commitments(
                        [c['id'] for c in all_comms], contact_id=contact_id)
                except Exception:
                    reservations_available = False
                    logger.debug('commitment reservation view unavailable', exc_info=True)
                lines = ["Claim the commitment ID with colony_commitment_work before undertaking it. Another session's live reservation means do not duplicate its work. Claims authorize no external effect."]
                for c in all_comms:
                    status_tag = "[OVERDUE]" if c.get("status") == "overdue" or c['id'] in {item['id'] for item in overdue} else "[pending]"
                    due = f" (due: {c.get('due_at', '')})" if c.get('due_at') else ""
                    reservation = reservations.get(c['id'])
                    work_tag = ('; work=' + reservation['work_state']
                                + ('' if _canonical_only else '; session=' + reservation.get('session_id', ''))) if reservation else ('; work=unclaimed' if reservations_available else '; work=unknown')
                    lines.append(f"- {status_tag} id={c['id']}; {c.get('description', '')}{due}{work_tag}")
                sections.append(ContextSection(
                    id="colony-commitments",
                    title="Pending Commitments",
                    body="\n".join(lines),
                    priority=72,
                ))
        except Exception as exc:
            logger.warning("context_assemble commitments failed: %s", exc)

    # --- Affect State ---
    if _exact_person_allowed and _affect_store is not None and contact_id:
        try:
            state = _affect_store.get_state(contact_id)
            if state and (state.get("valence") is not None or state.get("current_valence") is not None):
                valence = state.get("valence") or state.get("current_valence", 0)
                arousal = state.get("arousal") or state.get("current_arousal", 0)
                mood = "positive" if valence > 0.2 else "negative" if valence < -0.2 else "neutral"
                energy = "high" if arousal > 0.5 else "low" if arousal < 0.3 else "moderate"
                sections.append(ContextSection(
                    id="colony-affect",
                    title="Contact Affect",
                    body=f"Mood: {mood} (valence: {valence:.2f}), Energy: {energy} (arousal: {arousal:.2f})",
                    priority=80,
                ))
        except Exception as exc:
            logger.warning("context_assemble affect failed: %s", exc)

    # --- Relationship closeness ---
    if _exact_person_allowed and _contacts_store is not None and contact_id:
        try:
            _rc = await _contacts_store.get(contact_id)
            if _rc is not None:
                from colony_sidecar.contacts.scoring import closeness_label
                _rs = float(getattr(_rc, "relationship_score", 0.0) or 0.0)
                _rt = getattr(_rc, "trust_tier", "") or ""
                _bits = [f"Closeness: {closeness_label(_rs)} ({_rs:.0%})"]
                if _rt:
                    _bits.append(f"standing: {_rt.replace('_', ' ')}")
                _rl = getattr(_rc, "last_interaction_at", None)
                if _rl:
                    _bits.append(f"last talked: {str(_rl)[:10]}")
                sections.append(ContextSection(
                    id="colony-relationship",
                    title="Relationship",
                    body=" · ".join(_bits),
                    priority=86,
                ))
        except Exception as exc:
            logger.debug("context_assemble relationship failed: %s", exc)

    # --- Approach brief (profiled standing/psyche/approach guidance) ---
    # Cached-only on the hot path (profiling runs in the autonomy phase);
    # the owner's own brief is skipped — approach guidance is for OTHERS.
    if _exact_person_allowed and _relationship_profiler is not None \
            and contact_id:
        try:
            from colony_sidecar.identity import get_owner_contact_id
            if contact_id != (get_owner_contact_id() or ""):
                if _p8_runtime is not None:
                    _brief = _relationship_profiler.cached(
                        contact_id, viewer=_p8_viewer)
                else:
                    _brief = _relationship_profiler.cached(contact_id)
                if _brief is not None:
                    _rendered = _brief.render()
                    if _rendered:
                        sections.append(ContextSection(
                            id="colony-approach",
                            title="Who you are talking to",
                            body=_rendered,
                            priority=84,
                        ))
        except Exception as exc:
            logger.debug("context_assemble approach brief failed: %s", exc)

    # --- Owner's stated preferences (explicit directives the owner gave me) ---
    if _exact_person_allowed and _preference_learner is not None \
            and contact_id:
        try:
            from colony_sidecar.identity import get_owner_contact_id
            if get_owner_contact_id() == contact_id:
                _brief = _preference_learner.build_brief()
                if _brief:
                    sections.append(ContextSection(
                        id="colony-owner-preferences",
                        title="How they want me to communicate",
                        body=_brief,
                        priority=88,
                    ))
                perspective = getattr(_preference_learner, 'perspective', None)
                if perspective is not None:
                    working_brief = perspective.brief()
                    if working_brief:
                        sections.append(ContextSection(id='colony-self-perspective',
                            title='Current working judgments and attention', body=working_brief, priority=87))
        except Exception as exc:
            logger.debug("context_assemble owner preferences failed: %s", exc)

    # --- Second-order theory of mind (owner ONLY, H3.3) ---
    # Who knows / is unaware of what is the owner's lens on their own world.
    # Double-keyed: COLONY_TOM2_CONTEXT (default off) turns the section on,
    # and the assembling contact must BE the owner — the flag can never
    # widen the audience, so a non-owner context stays tom2-free even with
    # the flag set (test-locked).
    if _tom2_store is not None and contact_id \
            and _tom_context_facts is not None:
        try:
            from colony_sidecar.tom.asymmetry import tom2_context_enabled
            from colony_sidecar.identity import get_owner_contact_id
            _owner_cid = get_owner_contact_id() or ""
            if (tom2_context_enabled() and _owner_cid
                    and contact_id == _owner_cid):
                _tom2_body = _render_tom2_context(
                    facts_store=_tom_context_facts,
                    strict_projection=_p8_runtime is not None,
                )
                if _tom2_body:
                    sections.append(ContextSection(
                        id="colony-tom2",
                        title="Knowledge asymmetries (who has not heard what)",
                        body=_tom2_body,
                        priority=60,
                    ))
        except Exception as exc:
            logger.debug("context_assemble tom2 failed: %s", exc)

    # --- Leveled cross-contact tom2 (L4.2) — NON-owner readers only. ---
    # The flip point of the leveled system (docs/TOM2-LEVELS.md). The H3.3
    # owner block above is untouched and test-locked. This block is
    # default-inert: COLONY_TOM2_LEVEL=0 (shipped) skips it entirely — the
    # same variable is the single-var kill switch — and fail-closed: ANY
    # error anywhere inside renders no section (lowest level wins).
    if _tom2_store is not None and _tom_context_facts is not None \
            and contact_id:
        try:
            from colony_sidecar.tom.levels import (
                configured_level, resolve_effective_level)
            from colony_sidecar.identity import get_owner_contact_id
            _lvl_owner = get_owner_contact_id() or ""
            if configured_level() >= 1 and contact_id != _lvl_owner:
                _conv_key = body.context.channel_id or \
                    await _ensure_channel_id(body.context,
                                             identity=body.identity)
                _lres = await resolve_effective_level(
                    _conv_key, contact_id,
                    presence_store=_presence_store,
                    contacts_store=_contacts_store)
                if _lres.level >= 1:
                    from colony_sidecar.tom.leveled import render_level1
                    _l1_body = render_level1(_tom2_store, _tom_context_facts,
                                             contact_id)
                    if _l1_body:
                        sections.append(ContextSection(
                            id="colony-tom2-l1",
                            title="What they already know (their own "
                                  "shared context)",
                            body=_l1_body,
                            priority=60,
                        ))
                if _lres.level >= 2:
                    from colony_sidecar.tom.eligibility import (
                        eligible_inferences)
                    from colony_sidecar.tom.leveled import render_level2
                    _reg = _tom2_approvals()
                    _elig = await eligible_inferences(
                        _tom2_store.list_inferences(limit=100), limit=3,
                        reader_contact_id=contact_id,
                        conversation_key=_conv_key,
                        facts_store=_tom_context_facts,
                        contacts_store=_contacts_store,
                        presence_store=_presence_store,
                        approval_check=(_reg.is_approved
                                        if _reg is not None else None),
                        budget_check=(_tom2_exposure.budget_ok
                                      if _tom2_exposure is not None
                                      else None),
                    )
                    # Ledger-first (L2.3/L3.1): a row renders only AFTER its
                    # exposure row and injection taint are durably recorded;
                    # missing ledger/taint stores render nothing, and any
                    # bookkeeping failure aborts the whole section via the
                    # enclosing except (over-recording is safe, silent
                    # rendering is not).
                    _booked: list = []
                    if _tom2_exposure is not None \
                            and _taint_registry is not None:
                        for _row in _elig:
                            _subj = str(_row.get("contact_id") or "")
                            _names = [_subj]
                            if _contacts_store is not None:
                                _sc = await _contacts_store.get(_subj)
                                _dn = str(getattr(_sc, "display_name", "")
                                          or "")
                                if _dn:
                                    _names.append(_dn)
                            _tom2_exposure.record_exposure(
                                reader_contact_id=contact_id,
                                subject_contact_id=_subj,
                                fact_ref=str(_row.get("fact_ref") or ""),
                                conversation_key=_conv_key)
                            _taint_registry.register(
                                _conv_key, _subj, subject_names=_names,
                                fact_ref=str(_row.get("fact_ref") or ""),
                                kind=str(_row.get("kind") or ""))
                            _booked.append(_row)
                    _l2_body = render_level2(_booked, _tom_context_facts,
                                             contact_id, limit=3)
                    if _l2_body:
                        sections.append(ContextSection(
                            id="colony-tom2-l2",
                            title="Epistemic prior (silent)",
                            body=_l2_body,
                            priority=60,
                        ))
        except Exception as exc:
            logger.debug("context_assemble leveled tom2 failed: %s", exc)

    # --- Standing boundaries (owner directives: MUST NOT / MUST) ---
    # This is the SOFT layer; the DirectiveGuard hard-gate at each action
    # chokepoint is the enforced floor. Under P8, directive text is owner-only
    # because legacy directives do not yet carry visibility envelopes.
    if _legacy_global_allowed and _directive_manager is not None:
        try:
            _parts = []
            # One-shot acknowledgment to echo (confirms a just-captured/lifted
            # directive so the owner sees it -- 1a).
            _ack = _directive_manager.consume_ack()
            if _ack:
                _parts.append("Tell the owner, in your own voice: " + _ack)
            # A boundary lift awaiting explicit confirmation (asymmetric friction
            # -- 1c). Ask for confirmation; do not resume until confirmed.
            _pending = _directive_manager.pending_confirmation()
            if _pending:
                _parts.append(_pending)
            _boundaries = _directive_manager.context_brief()
            if _boundaries:
                _parts.append(_boundaries)
            if _parts:
                sections.append(ContextSection(
                    id="colony-boundaries",
                    title="Standing boundaries the owner set (obey without exception)",
                    body="\n".join(_parts),
                    priority=99,
                ))
        except Exception as exc:
            logger.debug("context_assemble boundaries failed: %s", exc)

    # --- How to engage (evolving engagement profile) ---
    if _exact_person_allowed and _engagement_store is not None and contact_id:
        try:
            from colony_sidecar.tom.engagement import build_guidance
            _guid = build_guidance(_engagement_store.get_profile(contact_id))
            if _guid:
                sections.append(ContextSection(
                    id="colony-engagement",
                    title="How to engage with them",
                    body=_guid,
                    priority=84,
                ))
        except Exception as exc:
            logger.debug("context_assemble engagement failed: %s", exc)

    # --- Communication landscape (cross-channel awareness) ---
    if _exact_person_allowed and _comms_log is not None \
            and _contacts_store is not None and contact_id:
        try:
            _lc = await _contacts_store.get(contact_id)
            if _lc is not None:
                _bits = []
                _per = _comms_log.last_per_channel(contact_id)
                if _per:
                    _chs = ", ".join(f"{ch} {str(v['ts'])[:10]}" for ch, v in _per.items())
                    _bits.append(f"Channels used: {_chs}.")
                _lo = _comms_log.last_outbound(contact_id)
                if _lo:
                    _bits.append(f"I last reached out via {_lo['channel']} on {str(_lo['ts'])[:10]}.")
                if _commitment_store is not None:
                    try:
                        _cm = _commitment_store.list(person_id=contact_id, status=["pending", "overdue"], limit=5)
                        _cm_items = _cm.get("commitments", []) if isinstance(_cm, dict) else (_cm or [])
                        if _cm_items:
                            _descs = [c.get("description", "") for c in _cm_items if c.get("description")]
                            if _descs:
                                _bits.append("Open follow-ups: " + "; ".join(_descs[:3]) + ".")
                    except Exception:
                        pass
                if _bits:
                    from colony_sidecar.identity import get_owner_contact_id
                    _is_owner = (get_owner_contact_id() == contact_id)
                    if not _is_owner:
                        _bits.append(
                            "Proactively reaching out to them needs owner "
                            "approval first."
                        )
                    sections.append(ContextSection(
                        id="colony-comms-landscape",
                        title="Communication landscape",
                        body=" ".join(_bits),
                        priority=83,
                    ))
        except Exception as exc:
            logger.debug("context_assemble comms landscape failed: %s", exc)

    # --- Shared Facts ---
    if not _canonical_only and _facts_store is not None and contact_id:
        try:
            if _p8_runtime is not None:
                facts = (
                    _p8_runtime.project_shared_facts(
                        _p8_viewer,
                        now=datetime.now(timezone.utc),
                        subject_person_id=contact_id,
                        max_facts=5,
                    ).facts
                    if _p8_viewer is not None else ()
                )
                lines = [
                    f"- [{fact.confidence:.0%}] {fact.content}"
                    for fact in facts
                ]
            else:
                facts_result = _facts_store.list_facts(
                    contact_id=contact_id, limit=5)
                facts = (
                    facts_result if isinstance(facts_result, list)
                    else facts_result.get("facts", [])
                )
                lines = [
                    f"- [{fact.get('confidence', 0):.0%}] {fact['fact']}"
                    for fact in facts
                ]
            if lines:
                sections.append(ContextSection(
                    id="colony-shared-facts",
                    title="Known Facts About Contact",
                    body="\n".join(lines),
                    priority=70,
                ))
        except Exception as exc:
            logger.warning("context_assemble shared facts failed: %s", exc)

    # --- Unresolved Surprises ---
    if _legacy_global_allowed and _surprise_store is not None:
        try:
            surprises = _surprise_store.get_unresolved(limit=3)
            if surprises:
                lines = [f"- [{s.get('surprise_score', 0) if isinstance(s, dict) else s.surprise_score:.1f}] {s.get('observation', '') if isinstance(s, dict) else s.observation}" for s in surprises]
                sections.append(ContextSection(
                    id="colony-surprises",
                    title="Unexpected Observations",
                    body="\n".join(lines),
                    priority=75,
                ))
        except Exception as exc:
            logger.warning("context_assemble surprises failed: %s", exc)

    if _telemetry is not None:
        try:
            await _telemetry.touch("last_prefetch_at")
        except Exception:
            pass

    return ContextAssembleResponse(
        sections=sections,
        notices=(["Canonical scoped context provides own source evidence, claims, media, "
                  "and commitments proven shared in their source evidence. Legacy tasks, graph, relationship, shared-fact, "
                  "and global context are omitted."] if _canonical_only else None),
        projection_attestation=_projection,
    )


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

@router.post("/reasoning/turn", response_model=ReasoningTurnResponse)
async def reasoning_turn(
    body: ReasoningTurnRequest,
    request: Request,
) -> ReasoningTurnResponse:
    if _reasoning_loop is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    session_id = body.context.session_id if body.context and body.context.session_id else str(uuid.uuid4())

    available_tools = body.available_tools or None
    actor_policy = None
    if _p8_runtime is not None:
        from colony_sidecar.reasoning.executor import ToolRegistryError
        from colony_sidecar.reasoning.tool_policy import filter_tools_for_actor

        resolved_person = resolve_request_person(
            request,
            context_person_id=body.context.contact_id,
        )
        actor_policy = _p8_tool_actor_policy(request, resolved_person)
        executor = getattr(_reasoning_loop, "_tools", None) or _tool_executor
        try:
            # In P8, an empty list means the actor-filtered registered default,
            # and an explicit list can only narrow it. Production executors
            # resolve handler provenance before authority: a dynamic handler
            # is mutation even if its name resembles a shipped read.
            if executor is not None \
                    and hasattr(executor, "filter_names_for_actor"):
                available_tools = executor.filter_names_for_actor(
                    body.available_tools or None, actor_policy)
            else:
                registered = (
                    executor.available_names()
                    if executor is not None
                    and hasattr(executor, "available_names")
                    else []
                )
                available_tools = filter_tools_for_actor(
                    body.available_tools or registered, actor_policy)
        except ToolRegistryError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    result = await _reasoning_loop.run_turn(
        session_id=session_id,
        messages=messages,
        available_tools=available_tools,
        model_override=body.model_override or None,
        actor_policy=actor_policy,
    )

    response_msg = None
    if result.message:
        response_msg = HostMessage(
            role=result.message.get("role", "assistant"),
            content=result.message.get("content", ""),
        )

    return ReasoningTurnResponse(
        status=result.status,
        message=response_msg,
        tool_calls=[
            ReasoningToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
            for tc in result.tool_calls
        ],
        usage=result.usage,
        error=result.error,
    )


@router.post("/reasoning/tools/invoke", response_model=ToolInvokeResponse)
async def tools_invoke(
    body: ToolInvokeRequest,
    request: Request,
) -> ToolInvokeResponse:
    """Invoke a single sidecar-resident tool by name.

    Used by the OpenClaw plugin to expose Colony's native tools
    (calculate, web_search, read_file, write_file, list_directory) as
    first-class OpenClaw tools without routing them through the full
    reasoning loop.
    """
    if _tool_executor is None:
        return ToolInvokeResponse(
            result="", available=False, error="tool_executor_not_initialized",
        )

    actor_policy = None
    if _p8_runtime is not None:
        from colony_sidecar.reasoning.executor import ToolRegistryError
        from colony_sidecar.reasoning.tool_policy import actor_allows_tool

        authority = request_authority(request)
        actor_policy = _p8_tool_actor_policy(
            request, authority.viewer_person_id)
        if hasattr(_tool_executor, "filter_names_for_actor"):
            try:
                actor_tools = _tool_executor.filter_names_for_actor(
                    [body.name], actor_policy)
            except ToolRegistryError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            if body.name not in actor_tools:
                try:
                    registered = _tool_executor.available_names()
                except ToolRegistryError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
                if body.name not in registered:
                    return ToolInvokeResponse(
                        result="",
                        available=False,
                        error=f"Tool '{body.name}' is not registered",
                    )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "tool_authority_denied",
                        "message": "tool exceeds authenticated caller authority",
                    },
                )
        elif not actor_allows_tool(body.name, actor_policy):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "tool_authority_denied",
                    "message": "tool exceeds authenticated caller authority",
                },
            )

    if hasattr(_tool_executor, "execute_batch"):
        if _p8_runtime is None:
            from colony_sidecar.reasoning.executor import ToolRegistryError

            try:
                available_names = (
                    _tool_executor.available_names()
                    if hasattr(_tool_executor, "available_names")
                    else list(getattr(_tool_executor, "_handlers", {}).keys())
                )
            except ToolRegistryError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            if body.name not in available_names:
                return ToolInvokeResponse(
                    result="", available=False,
                    error=f"Tool '{body.name}' is not registered",
                )
        result = (await _tool_executor.execute_batch(
            [{
                "id": f"direct:{uuid.uuid4()}",
                "name": body.name,
                "arguments": body.arguments,
            }],
            allowed_tools=frozenset({body.name}),
            actor_policy=actor_policy,
        ))[0]
        if result.get("executed") is True:
            return ToolInvokeResponse(result=result["content"], available=True)
        error = str(result.get("error") or "tool_execution_failed")
        detail_message = str(result.get("content") or error)
        try:
            detail_payload = json.loads(detail_message)
            if isinstance(detail_payload, dict):
                detail_message = str(
                    detail_payload.get("message")
                    or detail_payload.get("error")
                    or error
                )
        except (TypeError, ValueError):
            pass
        if error in {"tool_authority_denied", "tool_not_authorized",
                     "tool_boundary_denied"}:
            raise HTTPException(
                status_code=403,
                detail={"code": error, "message": detail_message},
            )
        if error == "tool_boundary_unavailable":
            raise HTTPException(
                status_code=503,
                detail={"code": error, "message": detail_message},
            )
        if error in {
            "tool_name_collision",
            "tool_registry_malformed",
            "tool_registry_unavailable",
        }:
            raise HTTPException(
                status_code=503,
                detail={"code": error, "message": detail_message},
            )
        return ToolInvokeResponse(
            result="",
            available=(error != "tool_unavailable"),
            error=detail_message,
        )

    # Minimal third-party/test executors without the shared batch contract keep
    # the historical direct handler adapter. Production ToolExecutor instances
    # always take the governed branch above, including when P8 is off.
    handler = _tool_executor._handlers.get(body.name)
    if handler is None:
        return ToolInvokeResponse(
            result="", available=False,
            error=f"Tool '{body.name}' is not registered",
        )
    try:
        raw = await handler(body.arguments)
        return ToolInvokeResponse(result=str(raw), available=True)
    except Exception as exc:
        logger.warning("tools_invoke('%s') failed: %s", body.name, exc)
        return ToolInvokeResponse(
            result="", available=True, error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class _LooseMessage:
    """Adapter that satisfies SignalCollector's Message Protocol."""
    def __init__(self, sender_id: str, content: str, ts: datetime) -> None:
        self.sender_id = sender_id
        self.content = content
        self.timestamp = ts
        self.reply_to_id: Optional[str] = None
        self.has_media = False


#: contact_ids already warned about as unknown on /signals/ingest (warn-once,
#: bounded so a churn of junk ids can't grow it without limit).
_signals_unknown_warned: set = set()


async def _attribute_signal_contact(body: SignalIngestRequest) -> None:
    """Attribution for /signals/ingest (COLONY_SIGNALS_ATTRIBUTION=legacy/strict).

    Mirrors the turns/sync chokepoint: a supplied ``sender`` resolves server-side
    via ParticipantResolver and OVERWRITES context.contact_id (client contact ids
    go stale in group sessions). Without a resolvable sender:
      * legacy (default): keep the client's contact_id exactly as today, but
        warn once per unknown id so poisoned attribution is at least visible;
      * strict: attribute to the reserved system sentinel — an unattributable
        signal must never poison a person's baselines/engagement profile.
    Never raises; any failure keeps the client contact (legacy behavior).
    """
    mode = os.environ.get("COLONY_SIGNALS_ATTRIBUTION", "legacy").strip().lower()
    try:
        from colony_sidecar.identity.participants import (
            SYSTEM_CONTACT_ID, ParticipantResolver,
        )
        if body.sender is not None and _contacts_store is not None:
            res = await ParticipantResolver(_contacts_store).resolve(
                platform=body.sender.platform,
                user_id=body.sender.user_id,
                display_name=body.sender.display_name,
                group_id=body.sender.group_id,
                channel_id=body.context.channel_id or "",
            )
            if res.contact_id:
                if res.contact_id != body.context.contact_id:
                    logger.info(
                        "signal attribution: %s -> %s (%s%s)",
                        body.context.contact_id, res.contact_id, res.method,
                        ", shadow-created" if res.created else "")
                body.context.contact_id = res.contact_id
                return
        # No sender, or the sender was unresolvable: is the claimed contact real?
        if _contacts_store is None or not body.context.contact_id:
            return
        known = None
        try:
            known = await _contacts_store.get(body.context.contact_id)
        except Exception:
            known = None
        if known is not None:
            return
        if mode == "strict":
            logger.info("signal attribution (strict): unknown contact %r -> %s",
                        body.context.contact_id, SYSTEM_CONTACT_ID)
            body.context.contact_id = SYSTEM_CONTACT_ID
        elif body.context.contact_id not in _signals_unknown_warned:
            if len(_signals_unknown_warned) < 512:
                _signals_unknown_warned.add(body.context.contact_id)
            logger.warning(
                "signals_ingest: unknown contact_id %r — signals will accrue to "
                "an unverified identity (set COLONY_SIGNALS_ATTRIBUTION=strict "
                "to divert these to the system sentinel)",
                body.context.contact_id)
    except Exception:
        logger.debug("signal attribution failed; keeping client contact",
                     exc_info=True)


@router.post("/signals/ingest", response_model=SignalIngestResponse)
async def signals_ingest(body: SignalIngestRequest) -> SignalIngestResponse:
    if _signal_collector is None:
        return SignalIngestResponse(accepted=True, signals_recorded=0)
    await _attribute_signal_contact(body)

    recorded = 0
    now = datetime.now(tz=timezone.utc)
    incoming = body.incoming_message
    if incoming and incoming.content:
        try:
            sigs = await _signal_collector.collect(
                _LooseMessage(body.context.contact_id, incoming.content, now)
            )
            recorded += len(sigs or [])
            # Fold objective FORM signals (how they actually write) into the same
            # engagement profile as the LLM's CONTENT-derived style — unified edge.
            if _engagement_store is not None and body.context and body.context.contact_id and sigs:
                style = {}
                for sig in sigs:
                    st = getattr(sig, "signal_type", "")
                    if st == "emoji_usage":
                        style["emoji_ok"] = min(1.0, float(getattr(sig, "normalized_value", 0.0)) / 3.0)
                    elif st == "message_length":
                        style["verbosity"] = min(1.0, float(getattr(sig, "raw_value", 0.0)) / 600.0)
                if style:
                    try:
                        _engagement_store.update_from_observation(body.context.contact_id, style=style)
                    except Exception:
                        logger.debug("engagement-from-signals failed", exc_info=True)
        except Exception as exc:
            logger.warning("signals_ingest collect(incoming) failed: %s", exc)

    if body.outgoing_message and body.outgoing_message.content:
        try:
            sigs = await _signal_collector.collect(
                _LooseMessage("assistant", body.outgoing_message.content, now)
            )
            recorded += len(sigs or [])
        except Exception as exc:
            logger.warning("signals_ingest collect(outgoing) failed: %s", exc)

    # Raw signals from external sources. Count per item, so a mid-batch
    # failure still reports the signals that WERE persisted.
    if body.signals:
        for sig in body.signals:
            try:
                await _signal_collector.ingest_raw(sig)
                recorded += 1
            except Exception as exc:
                logger.warning("signals_ingest raw signal failed: %s", exc)

    # Fire cognition trigger for high-priority signals (best-effort)
    if recorded > 0:
        try:
            from colony_sidecar.cognition.trigger import trigger_cognition, _cognition_enabled
            if _cognition_enabled():
                content = ""
                if incoming and incoming.content:
                    content = incoming.content[:500]
                _spawn_task(trigger_cognition(
                    trigger_type="signal_ingest",
                    context={
                        "signal_type": "engagement",
                        "signal_data": {"content": content},
                        "person_id": body.context.contact_id if body.context else "",
                    },
                    priority="low",
                ))
        except Exception:
            logger.debug("cognition trigger from signal_ingest failed", exc_info=True)

    return SignalIngestResponse(accepted=True, signals_recorded=recorded)


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

def _conversation_turn_concern_metadata(
    body: TurnSyncRequest,
    request: Request | None,
    *,
    resolved_human_sender: bool,
    dynamic_contact_grant_attested: bool,
) -> Dict[str, Any]:
    """Seal optional concern metadata from server-side authority only.

    ``HostTurnContext.metadata`` and every caller-provided privacy/authority
    claim are deliberately ignored.  A scoped credential may attest only its
    exact person grant.  A structured sender may attest the resolver result
    only when that scoped principal owns the sender-resolution scope and the
    platform is in its configured attestation set.  Legacy/global bearer,
    anonymous, client-only contact claims, and the system sentinel all fail
    closed while the ordinary timeline event remains unchanged.
    """

    from colony_sidecar.self_model.event_concerns import turn_concerns_enabled

    if not turn_concerns_enabled():
        return {}
    authority = request_authority(request)
    subject = str(body.context.contact_id or "").strip()
    scoped = bool(
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
    )
    static_subject_granted = bool(
        scoped
        and subject
        and (
            subject == str(authority.viewer_person_id or "")
            or subject in authority.static_person_ids
        )
    )
    within_static_grant = bool(body.sender is None and static_subject_granted)
    claimed_sender_platform = (
        str(body.sender.platform or "").strip().lower()
        if body.sender is not None else ""
    )
    source_platform = ""
    ingress_platforms = authority.turn_ingress_platforms
    if body.sender is not None:
        if (
            re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", claimed_sender_platform)
            and claimed_sender_platform in ingress_platforms
        ):
            source_platform = claimed_sender_platform
    elif len(ingress_platforms) == 1:
        source_platform = next(iter(ingress_platforms))
    source_platform_attested = bool(scoped and source_platform)
    resolved_static_grant = bool(
        scoped
        and resolved_human_sender
        and body.sender is not None
        and authority.has_scope("turns:resolve-sender")
        and static_subject_granted
    )
    resolved_dynamic_sender = bool(
        scoped
        and resolved_human_sender
        and body.sender is not None
        and authority.has_scope("turns:resolve-sender")
        and source_platform in authority.attested_contact_platforms
        and dynamic_contact_grant_attested
    )
    resolved_sender_attested = bool(
        resolved_static_grant or resolved_dynamic_sender
    )
    identity_attested = bool(
        subject != "system"
        and (within_static_grant or resolved_sender_attested)
    )
    owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
    )
    scope_attested = bool(identity_attested and owner)
    if scope_attested and subject == owner:
        viewer_scope, shareability = "owner", "owner_private"
    elif scope_attested:
        viewer_scope, shareability = f"person:{subject}", "subject_private"
    else:
        viewer_scope, shareability = "", ""
    attribution_method = (
        "resolved_sender"
        if resolved_dynamic_sender else
        "resolved_static_grant"
        if resolved_static_grant else
        "authority_binding"
        if within_static_grant else
        "unattested"
    )
    canonical_turn_id = str(body.context.turn_id or "").strip()
    turn_id_source = "client_idempotency_key" if canonical_turn_id else "missing"
    if (
        not canonical_turn_id
        and identity_attested
        and scope_attested
        and str(body.context.session_id or "").strip()
    ):
        # This digest is only immutable lineage/deduplication. Content cannot
        # contribute identity, privacy scope, capability, or effect authority.
        turn_material = {
            "schema": "ServerDerivedConversationTurnIdV1",
            "source_principal_id": str(authority.principal_id or ""),
            "subject_person_id": subject,
            "session_id": str(body.context.session_id or ""),
            "channel_id": str(body.context.channel_id or ""),
            "source_platform": source_platform,
            "summary": str(body.summary or ""),
            "user_message": (
                str(body.user_message.content or "")
                if body.user_message is not None else ""
            ),
            "assistant_message": (
                str(body.assistant_message.content or "")
                if body.assistant_message is not None else ""
            ),
        }
        encoded = json.dumps(
            turn_material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        canonical_turn_id = "server-turn-" + hashlib.sha256(encoded).hexdigest()
        turn_id_source = "server_digest"
    return {
        "turn_scope_schema": "ConversationTurnJournalScopeV1",
        "turn_id": canonical_turn_id,
        "turn_id_source": turn_id_source,
        "turn_id_attested": bool(
            canonical_turn_id and identity_attested and scope_attested
        ),
        "subject_person_id": subject,
        "identity_attested": identity_attested,
        "scope_attested": scope_attested,
        "attribution_method": attribution_method,
        "source_principal_id": str(authority.principal_id or ""),
        "source_platform": source_platform,
        "source_platform_attested": source_platform_attested,
        "viewer_scope": viewer_scope,
        "shareability": shareability,
        # A completed turn never attests widening to shared/public.
        "boundary_attested": False,
    }

class SourceForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str = Field(min_length=1, max_length=256)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    old_text: str | None = Field(default=None, min_length=1, max_length=131072)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=256)


@router.get("/memory/sources/erasures")
async def source_erasure_feed(contact_id: str, after: int = Query(0, ge=0), request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from colony_sidecar.turns import get_turn_idempotency_ledger
    try:
        return get_turn_idempotency_ledger(get_state_dir()).erasure_feed(person, after)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "erasure_history_mismatch"}) from exc


@router.get("/memory/sources/claims/status")
async def source_claim_projection_status(contact_id: str, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from colony_sidecar.turns import get_turn_idempotency_ledger
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    from colony_sidecar.turns.media import SourceMedia
    ledger = get_turn_idempotency_ledger(get_state_dir())
    from colony_sidecar.turns.source_vectors import SourceVectors
    from colony_sidecar.vector import get_store, get_pipeline
    return {"sources": SourceClaimProjection(ledger).status(person), "media": SourceMedia(ledger).status(person),
            "semantic": SourceVectors(ledger, get_store(), get_pipeline()).status(person)}


@router.get("/memory/sources/assets/{asset_hash}")
async def read_source_asset(asset_hash: str, contact_id: str, session_id: str, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from colony_sidecar.turns import get_turn_idempotency_ledger
    from colony_sidecar.turns.media import SourceMedia
    try:
        data, mime = SourceMedia(get_turn_idempotency_ledger(get_state_dir())).read(
            asset_hash, contact_id=person, session_id=session_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="unknown source asset") from None
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/memory/sources/forget")
async def forget_turn_sources(body: SourceForgetRequest, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=body.contact_id) or body.contact_id
    from colony_sidecar.turns import get_turn_idempotency_ledger
    try:
        result = get_turn_idempotency_ledger(get_state_dir()).erase_sources(
            contact_id=person, turn_ids=body.source_ids,
            old_text=body.old_text, session_id=body.session_id,
        )
    except ValueError as exc:
        code = str(exc) if str(exc) in {"ambiguous_source", "source_not_found"} else "invalid_source_selection"
        raise HTTPException(status_code=409 if code == "ambiguous_source" else 422, detail={"code": code}) from exc
    # The durable tombstone precedes external projection deletion. A failed
    # cleanup remains a truthful pending result, and recall stays fenced.
    fact_cleanup = "unavailable" if _facts_store is None else "pending"
    if _facts_store is not None:
        try:
            _facts_store.purge_erased_sources(list(dict.fromkeys(result["source_ids"] + result["affected_source_ids"])))
            fact_cleanup = "complete"
        except Exception:
            logger.warning("source erasure shared-fact cleanup is pending", exc_info=True)
    tom_cleanup = {}
    for name, store in (('affect', _affect_store), ('engagement', _engagement_store)):
        tom_cleanup[name + '_cleanup'] = 'unavailable' if store is None else 'pending'
        if store is not None:
            try:
                store.purge_erased_sources(list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])))
                tom_cleanup[name + '_cleanup'] = 'complete'
            except Exception:
                logger.warning('source erasure %s cleanup is pending', name, exc_info=True)
    vector_cleanup = 'unavailable'
    from colony_sidecar.vector import get_store
    vector_store = get_store()
    if vector_store is not None and getattr(vector_store, 'catalog', None) is not None:
        vector_cleanup = 'pending'
        try:
            await vector_store.erase_source_projections(list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])))
            vector_cleanup = 'complete'
        except Exception:
            logger.warning('source erasure vector generation cleanup is pending', exc_info=True)
    graph_cleanup = "unavailable" if _graph is None else "pending"
    if _graph is not None:
        try:
            await _graph.delete_source_memories(list(dict.fromkeys(result["source_ids"] + result["affected_source_ids"])))
            graph_cleanup = "complete"
        except Exception:
            logger.warning("source erasure graph cleanup is pending", exc_info=True)
    return {"source_erased": True, **result, "graph_cleanup": graph_cleanup,
            "shared_facts_cleanup": fact_cleanup, "vector_cleanup": vector_cleanup, **tom_cleanup,
            "scope": "canonical_turn_sources_and_linked_projections",
            "host_reconciliation": "pending_until_each_host_connects"}


async def _ingest_turn_idempotently(
    body: TurnSyncRequest,
    request: Request | None = None,
) -> tuple[TurnSyncResponse, str]:
    """Run one turn's effects, or replay its durable result.

    Legacy callers without a turn ID retain the v1 behavior. A supplied ID is
    reserved before attribution, memory, cognition, journal, or relationship
    effects run. This is the server's final defense even when a host retries or
    two host integrations accidentally submit the same envelope.
    """
    turn_id = (body.context.turn_id or "").strip()
    if not turn_id:
        return await _process_turn_sync(body, request=request), "unkeyed"
    if len(turn_id) > 256:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_turn_id", "message": "turn_id exceeds 256 characters"},
        )
    body.context.turn_id = turn_id

    from colony_sidecar.turns import (
        ReservationOutcome,
        canonical_turn_digest,
        get_turn_idempotency_ledger,
    )

    digest = canonical_turn_digest(body)
    ledger = get_turn_idempotency_ledger(get_state_dir())
    if ledger.is_source_erased(turn_id, body.context.contact_id):
        return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False, skipped_reason="source_erased"), "erased"
    if body.checkpoint_messages is not None:
        # One atomic source+index commit, no ordinary conversation effects.
        # A retry after an interrupted response can safely repeat this write.
        try:
            created = ledger.record_source(
                turn_id, contact_id=body.context.contact_id,
                session_id=body.context.session_id, scope="session",
                messages=[message.model_dump(mode="json") for message in body.checkpoint_messages],
                occurred_at=(body.context.metadata or {}).get("occurred_at"),
                timezone_name=body.context.timezone,
            )
        except ValueError as exc:
            from colony_sidecar.turns.idempotency import SourceErased
            if isinstance(exc, SourceErased):
                return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False, skipped_reason="source_erased"), "erased"
            raise HTTPException(status_code=409, detail={"code": "checkpoint_source_conflict"}) from exc
        return TurnSyncResponse(
            accepted=True, continuity_updated=False, source_recorded=True,
            skipped_reason="checkpoint_source_only",
        ), "created" if created else "replayed"
    reservation = ledger.reserve(turn_id, digest)
    if reservation.outcome == ReservationOutcome.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_id_content_conflict",
                "turn_id": turn_id,
                "message": "turn_id is already bound to different canonical content",
            },
        )
    if reservation.outcome == ReservationOutcome.AMBIGUOUS:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "turn_ingestion_ambiguous",
                "turn_id": turn_id,
                "message": "the first attempt was interrupted; inspect before replaying effects",
            },
        )
    if reservation.outcome == ReservationOutcome.REPLAYED:
        cached = reservation.response or {
            "accepted": True,
            "continuity_updated": False,
            "skipped_reason": "identical_replay",
        }
        return TurnSyncResponse.model_validate(cached), "replayed"
    if reservation.outcome == ReservationOutcome.IN_PROGRESS:
        return TurnSyncResponse(
            # Truthful pending semantics: the first writer has not committed a
            # result yet and may still fail or become ambiguous. A retry must
            # not look like a completed successful ingestion.
            accepted=False,
            continuity_updated=False,
            skipped_reason="identical_retry_in_progress",
        ), "in_progress"

    try:
        result = await _process_turn_sync(body, request=request)
    except BaseException as exc:
        ledger.mark_ambiguous(turn_id, digest, exc)
        raise
    ledger.complete(turn_id, digest, result.model_dump(mode="json"))
    return result, "created"


@router.post("/turns/sync", response_model=TurnSyncResponse)
async def turns_sync(
    body: TurnSyncRequest,
    request: Request = None,
    response: Response = None,
) -> TurnSyncResponse:
    body.context.contact_id = resolve_turn_person(
        request,
        context_person_id=body.context.contact_id,
        has_sender=body.sender is not None,
    ) or body.context.contact_id
    result, outcome = await _ingest_turn_idempotently(body, request=request)
    if response is not None:
        response.headers["Idempotency-Status"] = outcome
        if outcome == "in_progress":
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = "1"
    return result


@v2_router.put("/turns/{turn_id:path}", response_model=TurnSyncResponse)
async def turns_sync_v2(
    turn_id: str,
    body: TurnSyncRequest,
    response: Response,
    request: Request = None,
) -> TurnSyncResponse:
    """Idempotent TurnEnvelopeV2 ingestion compatibility slice.

    The path ID is canonical. Initial acceptance is ``201``; an identical
    replay is ``200``; changed content under the same ID is ``409``.
    """
    path_turn_id = (turn_id or "").strip()
    if not path_turn_id or len(path_turn_id) > 256:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_turn_id", "message": "turn_id must be 1..256 characters"},
        )
    body_turn_id = (body.context.turn_id or "").strip()
    if body_turn_id and body_turn_id != path_turn_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_id_mismatch",
                "path_turn_id": path_turn_id,
                "body_turn_id": body_turn_id,
            },
        )
    body.context.turn_id = path_turn_id
    body.context.contact_id = resolve_turn_person(
        request,
        context_person_id=body.context.contact_id,
        has_sender=body.sender is not None,
    ) or body.context.contact_id
    result, outcome = await _ingest_turn_idempotently(body, request=request)
    response.headers["Idempotency-Status"] = outcome
    if outcome == "created":
        response.status_code = status.HTTP_201_CREATED
    elif outcome == "in_progress":
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
    else:
        response.status_code = status.HTTP_200_OK
    return result


async def _process_turn_sync(
    body: TurnSyncRequest,
    request: Request | None = None,
) -> TurnSyncResponse:
    # Keep direct blocks for canonical source storage. All existing text-only
    # cognition consumers receive only explicit text, never repr(base64/URLs).
    from colony_sidecar.turns import canonical_turn_digest
    source_body = body
    source_messages = [{"role": message.role, "content": message.content}
                       for message in (body.user_message, body.assistant_message)
                       if message is not None and (message.content.strip() if isinstance(message.content, str) else message.content)]
    body = body.model_copy(deep=True)
    for field in ("user_message", "assistant_message"):
        message = getattr(body, field)
        if message is not None and isinstance(message.content, list):
            message.content = message.text_content()
    # Auto-derive channel_id when the host does not provide one, so
    # context provenance and cross-context leak detection always work.
    body.context.channel_id = await _ensure_channel_id(
        body.context, identity=body.identity,
    )
    # Keep the channel registry alive from real traffic: first sighting
    # auto-registers, every turn refreshes last_seen_at (channel health).
    _observe_channel(body.context.channel_id)

    # ── Attribution chokepoint (docs/RELATIONSHIPS.md) ──────────────────
    # Resolve WHO said this server-side. A supplied sender overrides the
    # client's contact_id (which goes stale in group sessions); a senderless
    # machine turn (cron/api channel or system-origin text) attributes to
    # the reserved "system" sentinel so it can never pollute a person's
    # affect/facts/psyche/interactions. Rewriting context.contact_id here
    # means every downstream consumer in this handler sees the truth.
    _resolved_human_sender = False
    _contact_grant_attested = False
    _resolution_method = "client"   # server did NOT verify the claimed contact
    try:
        from colony_sidecar.identity.participants import (
            SYSTEM_CONTACT_ID, ParticipantResolver, is_machine_turn,
        )
        if body.sender is not None and _contacts_store is not None:
            _res = await ParticipantResolver(_contacts_store).resolve(
                platform=body.sender.platform,
                user_id=body.sender.user_id,
                display_name=body.sender.display_name,
                group_id=body.sender.group_id,
                channel_id=body.context.channel_id or "",
            )
            if _res.contact_id:
                if _res.contact_id != body.context.contact_id:
                    logger.info(
                        "turn attribution: %s -> %s (%s%s)",
                        body.context.contact_id, _res.contact_id, _res.method,
                        ", shadow-created" if _res.created else "")
                body.context.contact_id = _res.contact_id
                _resolution_method = _res.method
                _resolved_human_sender = True
                _contact_grant_attested = record_attested_contact_grant(
                    request,
                    platform=body.sender.platform,
                    person_id=_res.contact_id,
                )
        if not _resolved_human_sender and is_machine_turn(
                body.context.channel_id or "",
                (getattr(body.user_message, "content", "") or "")
                if body.user_message else "",
                has_sender=body.sender is not None):
            body.context.contact_id = SYSTEM_CONTACT_ID
    except Exception:
        logger.debug("participant attribution failed; keeping client contact",
                     exc_info=True)
    _is_system_turn = body.context.contact_id == "system"

    # Persist complete attributed messages before derived graph/mining effects.
    # This is the central source of truth even when extractors are disabled.
    source_id = body.context.turn_id or "unkeyed:" + canonical_turn_digest(
        source_body.model_copy(update={"context": body.context}))
    source_recorded = False
    if source_messages:
        from colony_sidecar.turns import canonical_turn_digest, get_turn_idempotency_ledger
        ledger = get_turn_idempotency_ledger(get_state_dir())
        if ledger.is_source_erased(source_id, body.context.contact_id):
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        retained = ledger.retained_messages(contact_id=body.context.contact_id, session_id=body.context.session_id, messages=source_messages)
        if not retained:
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        from colony_sidecar.turns.idempotency import SourceErased
        try:
            ledger.record_source(
                source_id, contact_id=body.context.contact_id,
                session_id=body.context.session_id, messages=source_messages,
                occurred_at=(body.context.metadata or {}).get("occurred_at"),
                timezone_name=body.context.timezone,
            )
        except SourceErased:
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        source_recorded = True
        if retained != source_messages or ledger.is_projection_erased(source_id):
            # Preserve unrelated evidence, but never run extractors on an
            # envelope whose summary or tools may repeat erased content.
            return TurnSyncResponse(accepted=False, source_recorded=True, continuity_updated=False, skipped_reason="source_erased")

    # Conversation presence (L1.1, passive): now that WHO is settled, record
    # the sighting so the environment-risk classifier has a real census. The
    # store itself skips the system sentinel; any failure must never affect
    # turn processing.
    if _presence_store is not None and not _is_system_turn:
        try:
            _presence_store.record(
                body.context.channel_id or "",
                body.context.contact_id or "",
                method=_resolution_method,
                group_id=(body.sender.group_id if body.sender else "") or "")
        except Exception:
            logger.debug("conversation presence record failed", exc_info=True)

    # If structured fields are empty but raw messages are present,
    # extract topics/entities/summary from the raw messages.
    if not body.topics and not body.entities and not body.summary:
        if body.user_message is not None or body.assistant_message is not None:
            user_text = body.user_message.content if body.user_message else ""
            asst_text = body.assistant_message.content if body.assistant_message else ""
            combined = f"User: {user_text}\nAssistant: {asst_text}".strip()
            if combined and combined != "User: \nAssistant:":
                body.summary = combined[:2000]
                # Extract rough topics from user message
                words = user_text.split()
                body.topics = [w.lower().strip(".,!?;:") for w in words if len(w) > 4][:10]

    # Rule-based NER on the incoming message, run ONCE and shared by the
    # memory write (flag-gated) and context provenance (always) below. Fails
    # open to body.entities: an extractor error must never drop host entities.
    _extracted_ents: List[str] = []
    _turn_ner = os.environ.get(
        "COLONY_TURN_NER_ENTITIES", "0") not in ("0", "false", "no", "")
    _user_text = getattr(body.user_message, "content", "") if body.user_message else ""
    if _user_text and (_turn_ner or _context_provenance is not None):
        _extractor = _get_conversation_extractor()
        if _extractor is not None:
            try:
                _src = body.context.turn_id or body.context.session_id or "turn"
                _res = await _extractor.extract(_user_text, _src)
                _extracted_ents = [getattr(c, "text", None) or getattr(c, "name", "")
                                   for c in getattr(_res, "entities", [])]
            except Exception:
                logger.debug("turn entity extraction failed", exc_info=True)

    # COLONY_TURN_NER_ENTITIES=1: the stored turn memory carries the message's
    # named entities even when the host sent none, so salience scoring and
    # :MENTIONS edges reflect what the turn was actually about. Default 0 =
    # legacy: record_turn sees exactly body.entities.
    _turn_entities = body.entities
    if _turn_ner and _extracted_ents:
        _merged: List[str] = []
        _seen = set()
        for _name in list(body.entities or []) + _extracted_ents:
            _name = (_name or "").strip()
            if _name and _name.lower() not in _seen:
                _seen.add(_name.lower())
                _merged.append(_name)
            if len(_merged) >= 12:
                break
        _turn_entities = _merged

    # Store turn metadata in the graph if available. A wired graph that FAILS
    # to record is a failed ingestion — the error is carried to the response
    # below, never swallowed into a green-lit turn.
    graph_ok = False
    graph_error: Optional[str] = None
    if _graph is not None:
        try:
            await _graph.record_turn(
                session_id=body.context.session_id,
                contact_id=body.context.contact_id,
                topics=body.topics,
                entities=_turn_entities,
                tools_used=body.tools_used,
                summary=body.summary,
                turn_id=source_id if source_messages else body.context.turn_id,
            )
            graph_ok = True
        except Exception as exc:
            graph_error = f"record_turn failed: {type(exc).__name__}: {exc}"
            logger.warning("turns_sync failed: %s", exc)

    # Context provenance: record this turn's entities under its conversation context, so a
    # later reply in a DIFFERENT context that surfaces an entity known only from here can be
    # flagged as a cross-context leak. Entities come from the host plus rule-based NER on the
    # incoming message (what the other party brought up = what belongs to this conversation),
    # reusing the single extraction above.
    if _context_provenance is not None:
        ents = list(body.entities or []) + _extracted_ents
        if ents:
            try:
                _context_provenance.record(
                    body.context.channel_id, ents, contact_id=body.context.contact_id)
            except Exception:
                logger.debug("context provenance record failed", exc_info=True)

    # Fire cognition trigger (best-effort, non-blocking)
    try:
        from colony_sidecar.cognition.trigger import trigger_cognition, _cognition_enabled
        if _cognition_enabled():
            _spawn_task(trigger_cognition(
                trigger_type="turn_sync",
                context={
                    "conversation_text": body.summary or "",
                    # verbatim turn so introspection can see an owed deliverable
                    # ("text me the result") and whether the assistant already did it;
                    # a condensed summary often drops both.
                    "user_message": (getattr(body.user_message, "content", "") or "")
                                    if body.user_message else "",
                    "assistant_message": (getattr(body.assistant_message, "content", "") or "")
                                         if body.assistant_message else "",
                    "person_id": body.context.contact_id,
                    "session_id": body.context.session_id,
                },
                priority="normal",
            ))
    except Exception:
        logger.debug("cognition trigger from turn_sync failed", exc_info=True)

    # Inline introspection (best-effort, non-blocking). Runs the same per-turn judgment
    # in-process against a configured local LLM and records owed follow-ups directly —
    # the path that works when no host plugin consumes the cognition.requested event.
    try:
        from colony_sidecar.cognition.introspection import introspect_enabled, run_turn_introspection
        if introspect_enabled() and _commitment_store is not None and not _is_system_turn and (
                body.user_message is not None or body.assistant_message is not None):
            _existing = []
            _rejections = []
            try:
                _existing = _commitment_store.get_pending_for_person(body.context.contact_id) or []
            except Exception:
                pass
            try:
                # Negative examples: items the owner/agent recently judged
                # invalid or duplicate — the extractor must not re-record them.
                _rejections = _commitment_store.recent_rejections(limit=6) or []
            except Exception:
                pass
            _spawn_task(run_turn_introspection(
                user_message=(getattr(body.user_message, "content", "") or "") if body.user_message else "",
                assistant_message=(getattr(body.assistant_message, "content", "") or "")
                                  if body.assistant_message else "",
                conversation_text=body.summary or "",
                person_id=body.context.contact_id,
                existing_commitments=_existing,
                commitment_store=_commitment_store,
                recent_rejections=_rejections,
            ))
    except Exception:
        logger.debug("inline introspection from turn_sync failed", exc_info=True)

    # Track last user message for concurrent-session safety (v0.13.0)
    if body.user_message is not None:
        try:
            from colony_sidecar.util.session_safety import save_last_user_message_at
            save_last_user_message_at()
        except Exception:
            pass

    # Owner directive learning: when the owner explicitly states how they want
    # their assistant to communicate ("be concise", "use bullets", "no emoji"),
    # capture it deterministically at high confidence. Owner-only; ordinary
    # conversation never trips this (requires a style keyword + a directive cue).
    if _preference_learner is not None and body.user_message is not None:
        try:
            from colony_sidecar.identity import get_owner_contact_id
            owner_id = get_owner_contact_id()
            if owner_id and body.context.contact_id == owner_id:
                if getattr(_preference_learner, 'perspective', None) is not None:
                    changes = _preference_learner.learn_source(source_id) if source_recorded else []
                    hit = (changes[0][0].split('.', 1)[0], changes[0][0], changes[0][1]) if changes else None
                else:
                    hit = await _preference_learner.learn_directive(
                        getattr(body.user_message, "content", "") or ""
                    )
                if hit is not None:
                    try:
                        from colony_sidecar.events.broadcaster import emit as _emit
                        _emit("preference.directive_learned",
                              {"category": hit[0], "key": hit[1], "value": hit[2]})
                    except Exception:
                        pass
        except Exception:
            logger.debug("owner directive learning failed", exc_info=True)

    # Owner boundary/directive capture: durably record standing directives the
    # owner states ("don't touch X", "always check before Y", "you can do Z
    # again"). Owner-ONLY (a boundary can only be set/lifted by the owner) so a
    # third party can never install or remove the assistant's boundaries.
    if _directive_manager is not None and body.user_message is not None:
        try:
            from colony_sidecar.identity import get_owner_contact_id
            owner_id = get_owner_contact_id()
            if owner_id and body.context.contact_id == owner_id:
                _cap = _directive_manager.capture_from_message(
                    getattr(body.user_message, "content", "") or ""
                )
                if _cap.captured:
                    logger.info(
                        "Captured %d owner directive(s): %s",
                        len(_cap.captured),
                        "; ".join(f"[{d.polarity.value}] {d.subject}" for d in _cap.captured),
                    )
                if _cap.revoked:
                    logger.info("Lifted %d boundary(ies) on owner confirmation: %s",
                                len(_cap.revoked),
                                "; ".join(d.subject for d in _cap.revoked))
                if _cap.needs_confirmation:
                    logger.info("Boundary-lift staged, awaiting confirmation: %s",
                                _cap.needs_confirmation)
                if _cap.any():
                    try:
                        from colony_sidecar.events.broadcaster import emit as _emit
                        _emit("directive.captured",
                              {"captured": [d.subject for d in _cap.captured],
                               "revoked": [d.subject for d in _cap.revoked],
                               "needs_confirmation": bool(_cap.needs_confirmation)})
                    except Exception:
                        pass
                else:
                    # Deterministic pass found nothing: optionally fall back to
                    # the LLM classifier (1b), non-blocking, default OFF.
                    try:
                        from colony_sidecar.directives.extractor import llm_assist_enabled
                        if llm_assist_enabled():
                            _spawn_task(_directive_manager.capture_llm(
                                getattr(body.user_message, "content", "") or ""))
                    except Exception:
                        pass
        except Exception:
            logger.debug("owner directive capture failed", exc_info=True)

    # World-model population (shadow-first): learn people/companies/projects/
    # products from what is said. Best-effort, non-blocking, boundary-checked.
    if _world_populator is not None and body.user_message is not None:
        _wm_text = getattr(body.user_message, "content", "") or ""
        if _wm_text:
            async def _run_world_populate(txt: str, sid: str) -> None:
                try:
                    rep = await _world_populator.populate_from_text(txt, sid)
                    if rep.total() or rep.skipped_boundary:
                        logger.info(
                            "world-populate[%s] %s: create=%d merge=%d propose=%d "
                            "rel=%d boundary-skipped=%d",
                            rep.mode, sid, len(rep.created), len(rep.merged),
                            len(rep.proposed), len(rep.relationships),
                            len(rep.skipped_boundary),
                        )
                        if rep.mode == "shadow" and rep.created:
                            logger.info(
                                "world-populate[shadow] WOULD add: %s",
                                ", ".join(f"{c['type']}:{c['name']}" for c in rep.created[:12]),
                            )
                except Exception:
                    logger.debug("world populate failed", exc_info=True)
            _src = getattr(body.context, "turn_id", None) or getattr(body.context, "session_id", None) or "turn"
            _spawn_task(_run_world_populate(_wm_text, _src))

    # ToM LLM extraction (best-effort, non-blocking). Machines are not
    # people: a system-attributed turn must never mint affect/facts/psyche.
    try:
        if (_tom_extractor is not None and _affect_store is not None
                and _facts_store is not None and not _is_system_turn and source_recorded):
            _p8_producer = None
            if _p8_runtime is not None:
                try:
                    _p8_producer = _p8_viewer_for_request(
                        request,
                        body.context.contact_id,
                        server_resolved=_resolved_human_sender,
                    )
                except HTTPException:
                    logger.debug(
                        "P8 extracted fact envelope omitted: producer unavailable")
            _spawn_task(_run_tom_extraction(
                conversation_text=body.summary or "",
                contact_id=body.context.contact_id,
                session_id=body.context.session_id,
                p8_producer=_p8_producer,
                source_id=source_id,
            ))
    except Exception:
        logger.debug("ToM extraction from turn_sync failed", exc_info=True)

    if _telemetry is not None:
        try:
            await _telemetry.touch("last_sync_at")
        except Exception:
            pass

    # Timeline spine (v0.21.0): journal the conversation turn so it lands on
    # the unified timeline, and bump the contact's recency (last_interaction_at).
    if body.summary:
        try:
            from colony_sidecar.events.journal import append_event
            turn_event_data = {
                "contact_id": body.context.contact_id,
                "session_id": body.context.session_id,
                "channel_id": body.context.channel_id,   # cross-channel provenance for the timeline / handoff
                "summary": (body.summary or "")[:300],
                "topics": (body.topics or [])[:10],
                "tools_used": (body.tools_used or [])[:20],
            }
            turn_event_data.update(_conversation_turn_concern_metadata(
                body,
                request,
                resolved_human_sender=_resolved_human_sender,
                dynamic_contact_grant_attested=_contact_grant_attested,
            ))
            append_event("conversation.turn", turn_event_data)
        except Exception:
            logger.debug("journal conversation.turn failed", exc_info=True)
    # Mining: verbatim turn capture + escalation detection (best-effort; the
    # miner mode gates everything internally, see colony_sidecar/mining/).
    try:
        from colony_sidecar.api.routers.mining import get_mining_engine as _get_miner
        _miner = _get_miner()
        if _miner is not None:
            _miner.observe_turn(
                session_id=body.context.session_id,
                contact_id=body.context.contact_id,
                channel_id=body.context.channel_id or "",
                user_text=(getattr(body.user_message, "content", "") or "")
                          if body.user_message else "",
                assistant_text=(getattr(body.assistant_message, "content", "") or "")
                               if body.assistant_message else "",
                summary=body.summary or "",
                tools_used=body.tools_used,
                model=body.model or "",
            )
    except Exception:
        logger.debug("mining observe_turn failed", exc_info=True)
    try:
        if _contacts_store is not None and body.context.contact_id and not _is_system_turn:
            await _contacts_store.record_interaction(body.context.contact_id)
            # Recompute the contact's relationship closeness from interaction
            # history + affect (self-sufficient; independent of the behavioral
            # signal graph, which can be sparse). Keeps every contact's score live.
            try:
                from colony_sidecar.contacts.scoring import compute_relationship_score
                _c = await _contacts_store.get(body.context.contact_id)
                if _c is not None:
                    _aff = None
                    if _affect_store is not None:
                        try:
                            _aff = _affect_store.get_state(body.context.contact_id)
                        except Exception:
                            _aff = None
                    _score = compute_relationship_score(_c, _aff)
                    await _contacts_store.update_relationship_score(
                        body.context.contact_id, _score)
            except Exception:
                logger.debug("relationship score update failed", exc_info=True)
        # Cross-channel communication ledger: record this exchange under the
        # CONVERSATION's channel (group vs DM vs voice provenance), never the
        # contact's primary-handle gateway (which collapsed everything to one
        # channel). System turns are recorded too, for ops visibility; they
        # are excluded from every relationship surface.
        try:
            if _comms_log is not None and body.context.contact_id:
                _ch = body.context.channel_id or "direct"
                _sess = body.context.session_id or ""
                _comms_log.log(body.context.contact_id, channel=_ch,
                               direction="in",
                               summary=(body.summary or "")[:300],
                               session_id=_sess)
                # Record the assistant's reply as an OUTBOUND exchange on the
                # SAME resolved contact + conversation channel. Without this
                # the ledger sees only half of every conversation, so
                # reciprocity and "when did we last talk each way" (which the
                # reachout recommendation depends on) read as never-replying.
                _asst = (getattr(body.assistant_message, "content", "") or ""
                         ) if body.assistant_message else ""
                if _asst.strip():
                    _comms_log.log(body.context.contact_id, channel=_ch,
                                   direction="out",
                                   summary=_asst[:300], session_id=_sess)
        except Exception:
            logger.debug("comms ledger log failed", exc_info=True)
    except Exception:
        logger.debug("record_interaction failed", exc_info=True)

    if graph_error is not None:
        # The primary ingestion effect did not happen: the turn was NOT
        # recorded. accepted=False + the error string, so the host can never
        # mistake a dead graph backend for a successfully ingested turn.
        return TurnSyncResponse(
            accepted=False,
            continuity_updated=False,
            skipped_reason="graph_record_failed",
            errors=[graph_error],
            source_recorded=source_recorded,
        )
    return TurnSyncResponse(
        accepted=True, continuity_updated=graph_ok, source_recorded=source_recorded,
        skipped_reason=None if graph_ok else "no_graph_store",
    )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def _safety_unavailable(reason: str):
    """503 + decision="unavailable": the gate did NOT evaluate this text.

    Never "pass" — a caller must not mistake "not evaluated" for
    "evaluated and clean". ``blocked=True`` so callers keying only on the
    boolean fail closed as well.
    """
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=503,
        content=SafetyCheckResponse(
            decision="unavailable", blocked=True, reason=reason,
        ).model_dump(),
    )


@router.post("/safety/check", response_model=SafetyCheckResponse)
@router.post("/response-gate/check", response_model=SafetyCheckResponse, include_in_schema=False)
async def safety_check(body: SafetyCheckRequest) -> SafetyCheckResponse:
    if _response_gate is None:
        return _safety_unavailable("response gate not initialized")

    try:
        from colony_sidecar.gate.models import GatePayload
        from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier
        # session_id/contact_id/turn_id live on body.context (HostTurnContext),
        # not on the request root — the old getattr(body, ...) lookups always
        # returned their defaults, so context-dependent gate layers never saw
        # a session and could not fire.
        payload = GatePayload(
            response_text=body.response_text,
            incoming_message_text=body.incoming_message_text or "",
            target_gateway=body.target_gateway or "",
            target_contact_id=body.context.contact_id or "",
            session_id=body.context.session_id or "",
            turn_id=body.context.turn_id or "",
            trust_tier=TrustTier(body.trust_tier.strip().lower())
                       if body.trust_tier else TrustTier.REGULAR,
            mentioned_entities=frozenset(body.mentioned_entities or []),
        )
        result = await _response_gate.evaluate(payload)
        return SafetyCheckResponse(
            decision="block" if result.blocked else "pass",
            blocked=result.blocked,
            blocking_layer=result.blocking_layer,
            reason=getattr(result, "block_reason", None),
            flagged_excerpt=getattr(result, "flagged_excerpt", None),
            layer_results=getattr(result, "layer_results", None),
        )
    except Exception as exc:
        logger.warning("safety_check failed — unavailable (503): %s", exc)
        return _safety_unavailable("response gate evaluation failed")


# ---------------------------------------------------------------------------
# Events (WebSocket)
# ---------------------------------------------------------------------------

@router.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    auth_telemetry = getattr(ws.app.state, "auth_telemetry", None)

    def _record_event_auth(
        *, authority=None, decision: str, reason: str,
        auth_kind: str = "unauthenticated", principal_id: str = "unauthenticated",
    ) -> None:
        if auth_telemetry is None:
            return
        if authority is not None:
            auth_kind = "legacy" if authority.legacy else "scoped"
            principal_id = authority.principal_id
        auth_telemetry.record(
            auth_kind=auth_kind,
            principal_id=principal_id,
            method="WS",
            route="/v1/host/events",
            required_scope="events:read",
            decision=decision,
            reason=reason,
        )

    await ws.accept()

    # Read auth message. New clients send an exact sequence plus the journal
    # record time; ``lastEventId`` remains accepted as the legacy time cursor.
    last_event_seq: Optional[int] = None
    last_event_time = ""
    event_authority = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        import json as _json
        msg = _json.loads(raw)
        if msg.get("type") != "auth":
            _record_event_auth(decision="deny", reason="invalid_auth_payload")
            await ws.close(code=4001, reason="Expected auth message")
            return
        token = msg.get("token", "")
        expected = os.environ.get("COLONY_API_KEY", "")
        keyring_path = os.environ.get("COLONY_API_KEYRING_PATH", "")
        if not expected and not keyring_path:
            # Fail closed: without either credential source we cannot
            # authenticate event-stream subscribers, and this socket carries
            # live state changes.
            _record_event_auth(
                decision="deny", reason="auth_not_configured",
                auth_kind="anonymous", principal_id="anonymous-dev",
            )
            await ws.close(
                code=4003,
                reason="API authentication not configured on server",
            )
            return
        scoped_candidate = None
        if keyring_path:
            from colony_sidecar.api.authority import KeyringLoader, scoped_authority
            _match = KeyringLoader(keyring_path).authenticate(str(token))
            if _match is not None and _match.accepts():
                _candidate = scoped_authority(_match)
                scoped_candidate = _candidate
                if _candidate.has_scope("events:read"):
                    event_authority = _candidate
        if event_authority is None and expected and hmac.compare_digest(
            str(token).encode("utf-8"), expected.encode("utf-8")
        ):
            from colony_sidecar.api.authority import legacy_authority
            event_authority = legacy_authority()
        if event_authority is None:
            if scoped_candidate is not None:
                _record_event_auth(
                    authority=scoped_candidate,
                    decision="deny", reason="insufficient_scope",
                )
            else:
                _record_event_auth(decision="deny", reason="invalid_key")
            await ws.close(code=4003, reason="Invalid API key")
            return
        claimed_principal = str(msg.get("principal") or "").strip()
        if claimed_principal and claimed_principal != event_authority.principal_id:
            _record_event_auth(
                authority=event_authority,
                decision="deny", reason="principal_mismatch",
            )
            await ws.close(code=4003, reason="Principal mismatch")
            return

        raw_seq = msg.get("lastEventSeq")
        legacy_cursor = str(msg.get("lastEventId") or "")
        if raw_seq is None and legacy_cursor.isdigit():
            raw_seq = legacy_cursor
        if raw_seq is not None:
            last_event_seq = int(raw_seq)
            if last_event_seq < 0:
                raise ValueError("lastEventSeq must be non-negative")
        last_event_time = str(msg.get("lastEventTime") or "")
        if not last_event_time and legacy_cursor and not legacy_cursor.isdigit():
            last_event_time = legacy_cursor
        _record_event_auth(
            authority=event_authority,
            decision="allow", reason="allowed",
        )
    except asyncio.TimeoutError:
        _record_event_auth(decision="deny", reason="auth_timeout")
        await ws.close(code=4001, reason="Auth timeout")
        return
    except Exception:
        _record_event_auth(
            authority=event_authority,
            decision="deny", reason="invalid_auth_payload",
        )
        await ws.close(code=4001, reason="Invalid auth")
        return

    subscriber = EventSubscriberBuffer(
        _event_subscriber_queue_size(), loop=asyncio.get_running_loop()
    )

    # Subscribe before capturing the durable high-water mark. Events committed
    # during replay are buffered and later filtered by sequence, closing the
    # replay/live race without duplicate delivery.
    from colony_sidecar.events.journal import current_sequence, replay_events
    with _event_broadcast_lock:
        _event_subscribers.append(subscriber)
        replay_through_seq = current_sequence()

    disconnect_event = asyncio.Event()
    receive_task: Optional[asyncio.Task] = None

    async def _watch_disconnect() -> None:
        """Consume post-auth client frames so quiet disconnects are observed."""
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                # Application-level pings from legacy clients are deliberately
                # consumed. Modern clients use protocol-level WebSocket ping.
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:
            logger.debug("Event WebSocket receive watcher stopped", exc_info=True)
            return
        finally:
            disconnect_event.set()

    try:
        cursor_reset = (
            last_event_seq is not None
            and last_event_seq > replay_through_seq
        )
        await ws.send_json({
            "type": "connected",
            "journalHighWaterSeq": replay_through_seq,
            "queueCapacity": subscriber.queue.maxsize,
            "cursorReset": cursor_reset,
        })
        if cursor_reset:
            await ws.send_json({
                "type": "replay_reset",
                "requestedAfterSeq": last_event_seq,
                "journalHighWaterSeq": replay_through_seq,
                "reason": "cursor_ahead_of_journal",
            })

        # A brand-new subscriber starts at the captured high-water mark rather
        # than receiving an arbitrary retention-window history. Reconnects use
        # the exact processed sequence, with timestamp fallback for old clients.
        replay_after_seq = 0 if cursor_reset else last_event_seq
        if replay_after_seq is None and not last_event_time:
            replay_after_seq = replay_through_seq

        replayed_count = 0
        replay_cursor = replay_after_seq
        replay_since = last_event_time
        first_page = True
        last_replayed_seq = min(replay_after_seq or 0, replay_through_seq)

        while True:
            result = await asyncio.to_thread(
                replay_events,
                replay_since,
                1000,
                None,
                False,
                after_seq=replay_cursor,
                until_seq=replay_through_seq,
            )
            if result.get("replayError"):
                await ws.send_json({
                    "type": "replay_error",
                    "reason": result["replayError"],
                })
                await ws.close(code=1011, reason="Event replay unavailable")
                return

            first_available = int(result.get("firstAvailableSeq") or 0)
            if (
                first_page
                and replay_cursor is not None
                and first_available > 0
                and replay_cursor + 1 < first_available
            ):
                await ws.send_json({
                    "type": "replay_gap",
                    "requestedAfterSeq": replay_cursor,
                    "firstAvailableSeq": first_available,
                    "reason": "cursor_precedes_retention_window",
                })
            corrupt_count = int(result.get("corruptCount") or 0)
            if first_page and corrupt_count:
                await ws.send_json({
                    "type": "replay_integrity_warning",
                    "corruptRecordCount": corrupt_count,
                })

            page = result.get("events", [])
            for event in page:
                frame = {
                    "type": event["type"],
                    "occurred_at": event.get("occurredAt") or event["recordedAt"],
                    "recordedAt": event["recordedAt"],
                    "payload": event.get("data", {}),
                    "seq": event["seq"],
                    "eventId": event.get("ulid", ""),
                }
                await ws.send_json(frame)
                subscriber.mark_delivered(frame)
                replayed_count += 1
                last_replayed_seq = max(last_replayed_seq, int(event["seq"]))

            if not result.get("hasMore") or not page:
                break
            replay_cursor = int(result["lastSeq"])
            replay_since = ""
            first_page = False

        # Always terminate the handshake explicitly. This means an idle stream
        # never requires a client to guess whether replay has completed.
        await ws.send_json({
            "type": "replay_complete",
            "replayedCount": replayed_count,
            "lastSeq": last_replayed_seq,
            "replayThroughSeq": replay_through_seq,
        })
        subscriber.mark_delivered({"seq": replay_through_seq})

        receive_task = asyncio.create_task(_watch_disconnect())
        while True:
            event_task = asyncio.create_task(subscriber.get())
            disconnect_task = asyncio.create_task(disconnect_event.wait())
            done, _ = await asyncio.wait(
                {event_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                event_task.cancel()
                try:
                    await event_task
                except asyncio.CancelledError:
                    pass
                return
            disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
            event = event_task.result()
            if subscriber.is_overflow(event):
                await ws.send_json(subscriber.overflow_frame())
                await ws.close(
                    code=1013,
                    reason="Event subscriber overflow; reconnect and replay",
                )
                return
            # Frames at or below the replay snapshot were both journaled and
            # buffered while the replay ran. They have already been delivered.
            try:
                seq = int(event.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0
            if seq and seq <= replay_through_seq:
                continue
            await ws.send_json(event)
            subscriber.mark_delivered(event)
    except WebSocketDisconnect:
        pass
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
        subscriber.close()
        with _event_broadcast_lock:
            try:
                _event_subscribers.remove(subscriber)
            except ValueError:
                pass


@router.get("/events/replay")
async def events_replay(
    since: str = Query("", description="ISO 8601 journal time — replay events after this time"),
    limit: int = Query(500, ge=1, le=1000, description="Max events to return"),
    types: Optional[str] = Query(None, description="Comma-separated event type filter"),
    after_seq: Optional[int] = Query(
        None,
        alias="afterSeq",
        ge=0,
        description="Exact sequence cursor; takes precedence over since",
    ),
    until_seq: Optional[int] = Query(
        None,
        alias="untilSeq",
        ge=0,
        description="Optional inclusive replay high-water sequence",
    ),
) -> dict:
    """Replay journal events for disconnected clients.

    Returns events in sequential order. Prefer ``afterSeq`` from the last
    processed WebSocket frame; ``since`` supports legacy timestamp clients.
    """
    from colony_sidecar.events.journal import replay_events

    type_list = [t.strip() for t in types.split(",")] if types else None
    return replay_events(
        since=since,
        limit=limit,
        types=type_list,
        after_seq=after_seq,
        until_seq=until_seq,
    )


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

_goals_store = None

def set_goals_engine(engine) -> None:
    global _goals_store
    _goals_store = engine


@router.post("/goals", response_model=GoalResponse)
async def create_goal(body: GoalCreateRequest) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        goal = _goals_store.propose_goal(
            title=body.title,
            description=body.description or "",
        )
        # Auto-accept goals created via API
        goal = _goals_store.accept_goal(goal.goal_id)
        goal = _goals_store.activate_goal(goal.goal_id)
        return GoalResponse(
            id=goal.goal_id,
            title=goal.title,
            description=goal.description,
            status=goal.status.value if hasattr(goal.status, "value") else str(goal.status),
            priority=goal.priority.name.lower() if hasattr(goal.priority, "name") else str(goal.priority),
            progress=goal.progress_pct,
            parent_goal_id=goal.parent_goal_id,
            person_id=None,
            created_at=str(goal.created_at) if goal.created_at else None,
            updated_at=str(goal.updated_at) if goal.updated_at else None,
        )
    except Exception as exc:
        logger.warning("create_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/goals", response_model=GoalListResponse)
async def list_goals(person_id: Optional[str] = None, status_filter: Optional[str] = None) -> GoalListResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        from colony_sidecar.goals.models import GoalStatus
        status_enum = None
        if status_filter:
            try:
                status_enum = GoalStatus(status_filter)
            except ValueError:
                pass
        goals = _goals_store.list_goals(status=status_enum)
        return GoalListResponse(goals=[
            GoalResponse(
                id=g.goal_id,
                title=g.title,
                description=g.description,
                status=g.status.value if hasattr(g.status, "value") else str(g.status),
                priority=g.priority.name.lower() if hasattr(g.priority, "name") else str(g.priority),
                progress=g.progress_pct,
                parent_goal_id=g.parent_goal_id,
                person_id=None,
                created_at=str(g.created_at) if g.created_at else None,
                updated_at=str(g.updated_at) if g.updated_at else None,
            ) for g in goals
        ])
    except Exception as exc:
        logger.warning("list_goals failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "goals_unavailable",
                    "message": "Goals backend unavailable",
                },
            },
        ) from None


@router.get("/goals/{goal_id}", response_model=GoalResponse)
async def get_goal(goal_id: str) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        goal = _goals_store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return GoalResponse(
            id=goal.goal_id,
            title=goal.title,
            description=goal.description,
            status=goal.status.value if hasattr(goal.status, "value") else str(goal.status),
            priority=goal.priority.name.lower() if hasattr(goal.priority, "name") else str(goal.priority),
            progress=goal.progress_pct,
            parent_goal_id=goal.parent_goal_id,
            person_id=None,
            created_at=str(goal.created_at) if goal.created_at else None,
            updated_at=str(goal.updated_at) if goal.updated_at else None,
        )
    except HTTPException:
        raise
    except GoalNotFoundError:
        raise HTTPException(status_code=404, detail="Goal not found")
    except Exception as exc:
        logger.warning("get_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/goals/{goal_id}", response_model=GoalResponse)
async def update_goal(goal_id: str, body: GoalUpdateRequest) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        # Map status string to the appropriate state transition
        if body.status:
            status_lower = body.status.lower()
            if status_lower in ("completed", "done"):
                goal = _goals_store.accept_goal(goal_id)  # must be accepted first if not already
            elif status_lower == "blocked":
                goal = _goals_store.block_goal(
                    goal_id, reason=body.notes or "Blocked via API",
                    condition_type=body.condition_type,
                    condition_params=body.condition_params)
            elif status_lower == "unblocked":
                goal = _goals_store.unblock_goal(goal_id)
            elif status_lower == "abandoned":
                goal = _goals_store.abandon_goal(goal_id, reason=body.notes or "Abandoned via API")
            else:
                goal = _goals_store.get_goal(goal_id)
        else:
            goal = _goals_store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return GoalResponse(
            id=goal.goal_id,
            title=goal.title,
            description=goal.description,
            status=goal.status.value if hasattr(goal.status, "value") else str(goal.status),
            priority=goal.priority.name.lower() if hasattr(goal.priority, "name") else str(goal.priority),
            progress=goal.progress_pct,
            parent_goal_id=goal.parent_goal_id,
            person_id=None,
            created_at=str(goal.created_at) if goal.created_at else None,
            updated_at=str(goal.updated_at) if goal.updated_at else None,
        )
    except Exception as exc:
        logger.warning("update_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

_contacts_store = None

def set_contacts_store(store) -> None:
    global _contacts_store
    _contacts_store = store


@router.get("/contacts", response_model=ContactListResponse)
async def list_contacts(
    source: Optional[str] = None,
    trust_tier: Optional[str] = None,
    include_discovered: bool = True,
    limit: int = 100,
) -> ContactListResponse:
    if _contacts_store is None:
        return ContactListResponse(contacts=[])
    try:
        contacts: List[Any] = []

        # Determine which sources to include
        include_curated = source in (None, "all", "curated")
        include_world = source in (None, "all", "world_model")
        if source is None:
            include_world = include_discovered

        if include_curated:
            curated = await _contacts_store.list(
                trust_tier=trust_tier,
                limit=limit,
            )
            contacts.extend(curated)

        if include_world:
            world = await _contacts_store.list(
                trust_tier=trust_tier or "acquaintance",
                limit=limit,
            )
            # Exclude already-included curated contacts
            curated_ids = {c.contact_id for c in contacts}
            for c in world:
                if c.contact_id not in curated_ids and c.import_source == "world_model":
                    contacts.append(c)

        # Sort: curated first (by trust tier rank desc), then world model
        from colony_sidecar.contacts.models import _TIER_RANK
        def _sort_key(c):
            is_curated = 1 if c.import_source != "world_model" else 0
            tier_rank = _TIER_RANK.get(c.trust_tier, 0)
            last_int = c.last_interaction_at or ""
            return (is_curated, tier_rank, last_int)

        contacts.sort(key=_sort_key, reverse=True)
        contacts = contacts[:limit]

        return ContactListResponse(
            contacts=[ContactResponse(**c.to_dict()) for c in contacts],
            source_filter=source or "all",
            total=len(contacts),
        )
    except Exception as exc:
        logger.warning("list_contacts failed: %s", exc)
        return ContactListResponse(contacts=[])


def _contact_policy_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        character for character in value.strip()
        if ord(character) >= 0x20 and ord(character) != 0x7F
    )
    return cleaned[:maximum]


def _contact_policy_exact_text(value: object, maximum: int) -> str:
    """Return identity text only when no normalization would change it."""

    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return ""
    return value


def _contact_policy_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_CONTACT_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")


def _contact_policy_page_failure(
    *,
    reason: str,
    principal: str,
    caller_grants: Mapping[str, Any],
    offset: int,
    observed_at: float,
) -> dict:
    granted = caller_grants.get("person_ids") or []
    return {
        "schema": "ColonyContactPolicySourceV1",
        "version": 1,
        "available": False,
        "complete": False,
        "reason": reason,
        "observed_at": observed_at,
        "read_only": True,
        "execution_authority": False,
        "caller_principal": principal,
        "caller_contact_grants": {
            "available": bool(caller_grants.get("available")),
            "reason": caller_grants.get("reason"),
            "count": len(granted),
            "updated_at": caller_grants.get("updated_at"),
        },
        "offset": offset,
        "next_offset": None,
        "truncated": False,
        "items": [],
    }


@router.get("/contact-policy")
async def contact_policy_source(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict:
    """Bounded, read-only contact-policy source for an authenticated caller.

    Contacts and handles remain canonical in the contact store; outreach is a
    fresh evaluation of Colony's existing policy.  The exact-person posture is
    limited to the authenticated caller's server-attested grant projection.
    This endpoint never enumerates another principal and never mints standing,
    delivery, approval, goal, Charter, Operator, or private-context authority.
    """

    authority = request_authority(request)
    if authority.legacy or authority.anonymous or not authority.authenticated:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "scoped_principal_required",
                "message": "contact policy requires one scoped authenticated principal",
            },
        )
    if _contacts_store is None:
        return {
            "schema": "ColonyContactPolicySourceV1",
            "version": 1,
            "available": False,
            "complete": False,
            "reason": "contacts_store_unavailable",
            "observed_at": datetime.now(timezone.utc).timestamp(),
            "read_only": True,
            "execution_authority": False,
            "caller_principal": authority.principal_id,
            "caller_contact_grants": {
                "available": False,
                "reason": "contacts_store_unavailable",
                "count": 0,
                "updated_at": None,
            },
            "offset": offset,
            "next_offset": None,
            "truncated": False,
            "items": [],
        }

    registry = getattr(request.state, "colony_contact_grants", None)
    if authority.attested_contact_limit > 0 and registry is not None:
        try:
            caller_grants = registry.principal_projection(
                authority.principal_id,
                max_person_ids=authority.attested_contact_limit,
            )
        except Exception:
            logger.warning("contact-policy caller grant projection failed", exc_info=True)
            caller_grants = {
                "available": False,
                "reason": "caller_contact_grant_projection_invalid",
                "person_ids": [],
                "updated_at": None,
            }
    else:
        caller_grants = {
            "available": False,
            "reason": "caller_not_configured_for_attested_contact_grants",
            "person_ids": [],
            "updated_at": None,
        }
    granted_ids = frozenset(caller_grants.get("person_ids") or ())

    try:
        contacts = await _contacts_store.list(
            include_deleted=False,
            limit=limit + 1,
            offset=offset,
        )
    except Exception as exc:
        logger.warning("contact-policy contact list failed: %s", exc)
        return {
            "schema": "ColonyContactPolicySourceV1",
            "version": 1,
            "available": False,
            "complete": False,
            "reason": "contact_list_unavailable",
            "observed_at": datetime.now(timezone.utc).timestamp(),
            "read_only": True,
            "execution_authority": False,
            "caller_principal": authority.principal_id,
            "caller_contact_grants": {
                "available": bool(caller_grants.get("available")),
                "reason": caller_grants.get("reason"),
                "count": len(granted_ids),
                "updated_at": caller_grants.get("updated_at"),
            },
            "offset": offset,
            "next_offset": None,
            "truncated": False,
            "items": [],
        }

    truncated = len(contacts) > limit
    contacts = contacts[:limit]
    now = datetime.now(timezone.utc)
    try:
        owner_contact_id = await _contact_policy_owner_contact_id()
    except Exception:
        logger.error("contact-policy owner identity could not be resolved")
        return _contact_policy_page_failure(
            reason="owner_contact_unresolved",
            principal=authority.principal_id,
            caller_grants=caller_grants,
            offset=offset,
            observed_at=now.timestamp(),
        )

    items = []
    for contact in contacts:
        raw_contact_id = getattr(contact, "contact_id", "")
        contact_id = _contact_policy_exact_text(raw_contact_id, 128)
        if (
            not contact_id
            or _CONTACT_POLICY_ID_RE.fullmatch(contact_id) is None
        ):
            logger.error("contact-policy encountered a non-canonical contact ID")
            return _contact_policy_page_failure(
                reason="contact_identity_invalid",
                principal=authority.principal_id,
                caller_grants=caller_grants,
                offset=offset,
                observed_at=now.timestamp(),
            )
        handles = []
        try:
            contact_handles = await _contacts_store.get_handles(contact_id)
        except Exception:
            logger.warning("contact-policy handle read failed", exc_info=True)
            return _contact_policy_page_failure(
                reason="contact_handles_unavailable",
                principal=authority.principal_id,
                caller_grants=caller_grants,
                offset=offset,
                observed_at=now.timestamp(),
            )
        if len(contact_handles) > 32:
            return _contact_policy_page_failure(
                reason="contact_handle_limit_exceeded",
                principal=authority.principal_id,
                caller_grants=caller_grants,
                offset=offset,
                observed_at=now.timestamp(),
            )
        for handle in contact_handles[:32]:
            raw_gateway = getattr(handle, "gateway", "")
            raw_address = getattr(handle, "address", "")
            gateway = _contact_policy_exact_text(raw_gateway, 64).lower()
            address = _contact_policy_exact_text(raw_address, 512)
            if (
                not gateway
                or not address
            ):
                logger.error("contact-policy encountered a non-canonical handle")
                return _contact_policy_page_failure(
                    reason="contact_handle_invalid",
                    principal=authority.principal_id,
                    caller_grants=caller_grants,
                    offset=offset,
                    observed_at=now.timestamp(),
                )
            handles.append({
                "gateway": gateway,
                "address": address,
                "is_primary": bool(getattr(handle, "is_primary", False)),
                "verified": bool(getattr(handle, "verified", False)),
            })

        first = _contact_policy_time(getattr(contact, "first_seen_at", None))
        last = _contact_policy_time(getattr(contact, "last_interaction_at", None))
        interactions = int(getattr(contact, "interaction_count", 0) or 0)
        cadence_days = None
        overdue = False
        if first is not None and last is not None and interactions > 1:
            cadence_days = max(
                0.5,
                min(90.0, (last - first).total_seconds() / 86400.0 / (interactions - 1)),
            )
            overdue = (now - last).total_seconds() / 86400.0 > max(
                2.0, cadence_days * 1.5
            )

        followups = []
        outreach_dependencies_available = (
            _commitment_store is not None and _comms_log is not None
        )
        if _commitment_store is not None:
            try:
                listed = _commitment_store.list(
                    person_id=contact_id,
                    status=["pending", "overdue"],
                    limit=10,
                )
                candidates = (
                    listed.get("commitments", [])
                    if isinstance(listed, dict) else (listed or [])
                )
                followups = [
                    str(item.get("description"))
                    for item in candidates
                    if isinstance(item, Mapping) and item.get("description")
                ][:10]
            except Exception:
                outreach_dependencies_available = False
                followups = []
        last_outbound = None
        if _comms_log is not None:
            try:
                last_outbound = _comms_log.last_outbound(contact_id)
            except Exception:
                outreach_dependencies_available = False
                last_outbound = None
        primary_channel = next(
            (item["gateway"] for item in handles if item["is_primary"]),
            handles[0]["gateway"] if handles else "",
        )
        is_owner = bool(owner_contact_id and contact_id == owner_contact_id)
        if outreach_dependencies_available:
            from colony_sidecar.contacts.comms import evaluate_outreach
            outreach = evaluate_outreach(
                contact,
                is_owner=is_owner,
                last_outbound_ts=(last_outbound or {}).get("ts"),
                cadence_days=cadence_days,
                overdue=overdue,
                open_followups=followups,
                suggested_channel=primary_channel,
                now=now,
            )
        else:
            outreach = {
                "should_contact": False,
                "reason": "outreach dependencies unavailable; hold",
                "requires_owner_approval": not is_owner,
                "suggested_channel": primary_channel,
                "cooldown_active": False,
            }
        should_contact = outreach.get("should_contact") is True
        requires_owner = outreach.get("requires_owner_approval") is True
        if not bool(getattr(contact, "interaction_allowed", False)):
            decision = "deny"
            # Standing is the outer contact gate.  Never publish an internally
            # contradictory deny that still recommends outreach or asks for
            # an approval; downstream consumers correctly reject that shape.
            should_contact = False
            requires_owner = False
        elif should_contact and requires_owner:
            decision = "ask_owner"
        elif should_contact:
            decision = "allow"
        else:
            decision = "hold"
        items.append({
            "contact_id": contact_id,
            "display_name": _contact_policy_text(
                getattr(contact, "display_name", ""), 160
            ),
            "is_owner": is_owner,
            "authority": "none" if not is_owner else "owner_identity_only",
            "context_class": "owner_private" if is_owner else "scoped_or_empty",
            "trust_tier": _contact_policy_text(
                getattr(contact, "trust_tier", ""), 48
            ).lower(),
            "privacy_level": _contact_policy_text(
                getattr(contact, "privacy_level", ""), 48
            ).lower(),
            "interaction_allowed": bool(
                getattr(contact, "interaction_allowed", False)
            ),
            "handles": handles,
            "caller_exact_person_grant": (
                contact_id in granted_ids
                if caller_grants.get("available") is True else None
            ),
            "outreach": {
                "available": outreach_dependencies_available,
                "decision": decision,
                "should_contact": should_contact,
                "requires_owner_approval": requires_owner,
                "cooldown_active": outreach.get("cooldown_active") is True,
                "suggested_channel": _contact_policy_text(
                    outreach.get("suggested_channel"), 64
                ).lower(),
                "reason": _contact_policy_text(outreach.get("reason"), 480),
                "open_followup_count": len(followups),
            },
        })

    return {
        "schema": "ColonyContactPolicySourceV1",
        "version": 1,
        "available": True,
        "complete": (
            not truncated
            and all(item["outreach"]["available"] for item in items)
        ),
        "reason": (
            "outreach_dependencies_unavailable"
            if any(not item["outreach"]["available"] for item in items)
            else None
        ),
        "observed_at": now.timestamp(),
        "read_only": True,
        "execution_authority": False,
        "caller_principal": authority.principal_id,
        "caller_contact_grants": {
            "available": bool(caller_grants.get("available")),
            "reason": caller_grants.get("reason"),
            "count": len(granted_ids),
            "updated_at": caller_grants.get("updated_at"),
        },
        "offset": offset,
        "next_offset": offset + len(contacts) if truncated else None,
        "truncated": truncated,
        "items": items,
    }


class ContactPolicyStandingRequest(BaseModel):
    """Exact state toggle requested by a separately scoped operator BFF."""

    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(min_length=1, max_length=128)
    interaction_allowed: bool
    operation_id: str = Field(min_length=8, max_length=128)


_CONTACT_POLICY_OPERATION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,127}$"
)
_CONTACT_POLICY_E164_RE = re.compile(r"^\+([1-9][0-9]{7,14})$")
_CONTACT_POLICY_WHATSAPP_JID_RE = re.compile(
    r"^([1-9][0-9]{7,19})@(s\.whatsapp\.net|lid)$"
)


class ContactPolicyProvisionRequest(BaseModel):
    """One exact owner-operated contact create or handle verification."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(min_length=1, max_length=16)
    operation_id: str = Field(min_length=8, max_length=128)
    whatsapp_identity: str = Field(min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    contact_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


def _contact_policy_whatsapp_identity(value: object) -> str:
    """Return one exact WhatsApp DM JID, normalizing only canonical E.164."""

    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("identity must be exact")
    e164 = _CONTACT_POLICY_E164_RE.fullmatch(value)
    if e164 is not None:
        return e164.group(1) + "@s.whatsapp.net"
    if _CONTACT_POLICY_WHATSAPP_JID_RE.fullmatch(value) is not None:
        return value
    raise ValueError("identity is not a canonical WhatsApp DM")


def _contact_policy_display_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 120
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("display name must be canonical")
    return value


async def _contact_policy_owner_contact_id() -> str:
    """Resolve one exact canonical owner CID for reads and mutations."""

    if _contacts_store is None:
        raise RuntimeError("contacts store unavailable")
    from colony_sidecar.identity import (
        IdentityResolver,
        OwnerIdentityError,
        get_owner_contact_id,
    )

    configured = get_owner_contact_id()
    resolver = IdentityResolver(
        contact_store=_contacts_store,
        owner_id=configured,
    )
    try:
        forms = await resolver.owner_identities()
    except OwnerIdentityError as error:
        raise RuntimeError("owner identity unresolved") from error
    candidates = set()
    for form in forms:
        if (
            isinstance(form, str)
            and form.startswith("cid-")
            and _CONTACT_POLICY_ID_RE.fullmatch(form)
        ):
            contact = await _contacts_store.get(form)
            if contact is not None and getattr(contact, "contact_id", None) == form:
                candidates.add(form)
    if len(candidates) != 1:
        raise RuntimeError("owner identity is not one canonical contact")
    return next(iter(candidates))


@router.post("/contact-policy/standing")
async def set_contact_policy_standing(
    body: ContactPolicyStandingRequest,
    request: Request,
) -> dict:
    """Toggle only standing for one existing non-owner canonical contact.

    This is deliberately separate from the read projection and from contact
    editing.  The authenticated principal is recorded by the contact store's
    existing audit path; neither legacy auth nor anonymous dev mode can use it.
    """

    authority = request_authority(request)
    contact_id = body.contact_id
    if (
        authority.legacy
        or authority.anonymous
        or not authority.authenticated
        or not authority.has_scope("contacts:policy-write")
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "scoped_contact_policy_writer_required",
                "message": (
                    "contact standing requires one scoped authenticated principal"
                ),
            },
        )
    if (
        not contact_id
        or contact_id != contact_id.strip()
        or _CONTACT_POLICY_ID_RE.fullmatch(contact_id) is None
        or _CONTACT_POLICY_OPERATION_RE.fullmatch(body.operation_id) is None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "contact_standing_request_invalid",
                "message": "contact ID and operation ID must be canonical",
            },
        )
    if _contacts_store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contacts_store_unavailable",
                "message": "contact store is unavailable",
            },
        )

    try:
        owner_contact_id = await _contact_policy_owner_contact_id()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "owner_contact_unavailable",
                "message": "owner identity is unavailable; standing is immutable",
            },
        ) from exc
    if contact_id == owner_contact_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "owner_standing_immutable",
                "message": "the owner contact is outside guest standing controls",
            },
        )

    try:
        contact = await _contacts_store.get(contact_id)
    except Exception as exc:
        logger.warning("contact standing lookup failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contact_lookup_unavailable",
                "message": "contact lookup is unavailable",
            },
        ) from exc
    if contact is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "contact_not_found",
                "message": "contact does not exist",
            },
        )

    current = bool(getattr(contact, "interaction_allowed", False))
    requested = bool(body.interaction_allowed)
    changed = current != requested
    if changed:
        try:
            await _contacts_store.update_interaction_allowed(
                contact_id,
                requested,
                performed_by=authority.principal_id,
            )
        except Exception as exc:
            logger.warning("contact standing update failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "contact_standing_update_failed",
                    "message": "contact standing could not be updated",
                },
            ) from exc

    try:
        await _contacts_store.record_audit(
            contact_id,
            "contact_policy_standing_command",
            {
                "operation_id": body.operation_id,
                "interaction_allowed": requested,
                "changed": changed,
            },
            performed_by=authority.principal_id,
        )
    except Exception as exc:
        logger.warning("contact standing correlation audit failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contact_standing_audit_failed",
                "message": "contact standing audit could not be recorded",
            },
        ) from exc

    return {
        "schema": "ColonyContactStandingResultV1",
        "version": 1,
        "contact_id": contact_id,
        "interaction_allowed": requested,
        "changed": changed,
        "operation_id": body.operation_id,
        "principal": authority.principal_id,
    }


@router.post("/contact-policy/provision")
async def provision_contact_policy_identity(
    body: ContactPolicyProvisionRequest,
    request: Request,
) -> dict:
    """Create or map one exact owner-verified WhatsApp identity.

    This command deliberately does not import an allowlist, resolve a display
    name, change trust, or grant outreach standing.  Existing contacts are
    selected by their canonical ID from the owner-private projection; the
    authenticated principal, not the body, supplies audit authority.
    """

    authority = request_authority(request)
    if (
        authority.legacy
        or authority.anonymous
        or not authority.authenticated
        or not authority.has_scope("contacts:policy-write")
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "scoped_contact_policy_writer_required",
                "message": "contact provisioning requires the scoped owner operator",
            },
        )
    if (
        body.mode not in {"create", "verify"}
        or _CONTACT_POLICY_OPERATION_RE.fullmatch(body.operation_id) is None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "contact_provision_request_invalid",
                "message": "provisioning mode and operation ID must be canonical",
            },
        )
    try:
        address = _contact_policy_whatsapp_identity(body.whatsapp_identity)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "contact_whatsapp_identity_invalid",
                "message": "an exact E.164 number or WhatsApp DM JID is required",
            },
        ) from exc

    display_name: Optional[str] = None
    contact_id: Optional[str] = None
    if body.mode == "create":
        if body.contact_id is not None or body.display_name is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "contact_provision_request_invalid",
                    "message": "create requires only a display name and identity",
                },
            )
        try:
            display_name = _contact_policy_display_name(body.display_name)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "contact_display_name_invalid",
                    "message": "display name must be exact bounded text",
                },
            ) from exc
    else:
        contact_id = body.contact_id
        if (
            body.display_name is not None
            or not isinstance(contact_id, str)
            or contact_id != contact_id.strip()
            or _CONTACT_POLICY_ID_RE.fullmatch(contact_id) is None
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "contact_provision_request_invalid",
                    "message": "verify requires one canonical selected contact ID",
                },
            )

    if _contacts_store is None or not callable(
        getattr(_contacts_store, "provision_verified_handle", None)
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contacts_store_unavailable",
                "message": "contact provisioning is unavailable",
            },
        )
    try:
        owner_contact_id = await _contact_policy_owner_contact_id()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "owner_contact_unavailable",
                "message": "owner identity is unavailable; contacts are immutable",
            },
        ) from exc
    if contact_id == owner_contact_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "owner_contact_immutable",
                "message": "the owner contact cannot be provisioned here",
            },
        )
    if contact_id is not None:
        try:
            selected = await _contacts_store.get(contact_id)
        except Exception as exc:
            logger.warning("contact provisioning selection lookup failed")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "contact_lookup_unavailable",
                    "message": "contact lookup is unavailable",
                },
            ) from exc
        if selected is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "contact_not_found",
                    "message": "selected contact does not exist",
                },
            )

    try:
        result = await _contacts_store.provision_verified_handle(
            operation_id=body.operation_id,
            performed_by=authority.principal_id,
            gateway="whatsapp",
            address=address,
            display_name=display_name,
            contact_id=contact_id,
        )
    except ValueError as exc:
        message = str(exc)
        if "display_name is not unique" in message:
            code = "contact_display_name_ambiguous"
        elif "operation_id" in message:
            code = "contact_provision_operation_conflict"
        elif "handle" in message:
            code = "contact_handle_conflict"
        elif "does not exist" in message:
            raise HTTPException(
                status_code=404,
                detail={"code": "contact_not_found", "message": "contact does not exist"},
            ) from exc
        else:
            code = "contact_provision_request_invalid"
        raise HTTPException(
            status_code=409 if code != "contact_provision_request_invalid" else 400,
            detail={"code": code, "message": "contact provisioning was rejected"},
        ) from exc
    except Exception as exc:
        logger.warning("contact provisioning transaction failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contact_provision_failed",
                "message": "contact provisioning did not commit",
            },
        ) from exc

    if not isinstance(result, Mapping):
        logger.error("contact provisioning result is not a mapping")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contact_provision_result_invalid",
                "message": "contact provisioning result is invalid",
            },
        )
    expected_id = contact_id or str(result.get("contact_id") or "")
    if (
        _CONTACT_POLICY_ID_RE.fullmatch(expected_id) is None
        or result.get("contact_id") != expected_id
        or result.get("gateway") != "whatsapp"
        or result.get("address") != address
        or result.get("operation_id") != body.operation_id
        or result.get("verified") is not True
        or type(result.get("interaction_allowed")) is not bool
        or (body.mode == "create" and result.get("interaction_allowed") is not False)
    ):
        logger.error("contact provisioning result failed invariants")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "contact_provision_result_invalid",
                "message": "contact provisioning result is invalid",
            },
        )
    return {
        "schema": "ColonyContactProvisionResultV1",
        "version": 1,
        "mode": body.mode,
        "contact_id": expected_id,
        "display_name": result.get("display_name"),
        "gateway": "whatsapp",
        "address": address,
        "handle_id": result.get("handle_id"),
        "created": result.get("created") is True,
        "handle_created": result.get("handle_created") is True,
        "changed": result.get("changed") is True,
        "verified": True,
        "interaction_allowed": bool(result["interaction_allowed"]),
        "operation_id": body.operation_id,
        "principal": authority.principal_id,
    }


@router.post("/contacts", response_model=ContactResponse, status_code=201)
async def create_contact(body: ContactCreateRequest) -> ContactResponse:
    """Create a curated contact (with optional handles) via the API.

    Exists primarily so deployments can bootstrap the OWNER contact the
    IdentityResolver requires — before this, contacts could only appear
    as side effects of message ingestion.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        contact = await _contacts_store.create(
            display_name=body.display_name,
            given_name=body.given_name,
            family_name=body.family_name,
            organization=body.organization,
            trust_tier=body.trust_tier,
            tags=body.tags,
            notes=body.notes,
            import_source="manual",
        )
        for handle in body.handles:
            await _contacts_store.add_handle(
                contact.contact_id,
                gateway=handle.gateway,
                address=handle.address,
                is_primary=handle.is_primary,
                verified=handle.verified,
                source="manual",
            )
        created = await _contacts_store.get(contact.contact_id)
        return ContactResponse(**(created or contact).to_dict())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("create_contact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/contacts/proposals")
async def list_contact_proposals(limit: int = 50) -> dict:
    """Pending handle-link proposals from scoped-name attribution, for owner
    review (docs/RELATIONSHIPS.md: rung 4 never links silently)."""
    if _contacts_store is None:
        return {"available": False, "proposals": []}
    try:
        return {"available": True,
                "proposals": await _contacts_store.list_handle_proposals(limit)}
    except Exception as exc:
        return {"available": True, "error": str(exc), "proposals": []}


@router.post("/contacts/{contact_id}/handles", status_code=201)
async def add_contact_handle(contact_id: str, body: dict) -> dict:
    """Attach a channel handle to a contact ('that WhatsApp is Sam's').
    Owner curation surface behind colony_link_contact."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    gateway = str(body.get("gateway", "")).strip().lower()
    address = str(body.get("address", "")).strip()
    if not gateway or not address:
        raise HTTPException(status_code=400, detail="gateway and address required")
    try:
        h = await _contacts_store.add_handle(
            contact_id, gateway, address,
            is_primary=bool(body.get("is_primary", False)),
            verified=True, source="owner")
        return {"linked": True, "contact_id": contact_id,
                "gateway": gateway, "address": address,
                "handle_id": getattr(h, "handle_id", "")}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.warning("add_contact_handle failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/contacts/merge")
async def merge_contacts_endpoint(body: dict) -> dict:
    """Merge one contact into another (keep, merge). Audited + reversible."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    keep = str(body.get("keep", "")).strip()
    merge = str(body.get("merge", "")).strip()
    if not keep or not merge:
        raise HTTPException(status_code=400, detail="keep and merge contact ids required")
    try:
        kept = await _contacts_store.merge_contacts(keep, merge, performed_by="owner")
        return {"merged": True, "kept_contact_id": keep,
                "merged_contact_id": merge,
                "interaction_count": getattr(kept, "interaction_count", None)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("merge_contacts failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/contacts/intro", response_model=ContactIntroResponse, status_code=201)
async def capture_introduction(body: ContactIntroRequest) -> ContactIntroResponse:
    """Capture an organic introduction (social-graph autonomy, generic).

    The agent met or learned of a person; record them as a durable, queryable
    graph node WITH provenance (introduced_by + met_via) but WITHOUT any
    interaction standing. If the handle already resolves to a known contact, the
    provenance is recorded on that contact instead of duplicating it. A
    provisional contact is always inert: interaction_allowed is forced false so
    an intro can never become outreach — promotion/merge reconciles it later.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        # If a handle was given and already resolves to a known person, annotate
        # rather than duplicate (a number is one identity across phone gateways).
        existing = None
        if body.gateway and body.address:
            existing = await _contacts_store.resolve_messaging_handle(
                body.gateway, body.address)
        if existing is not None:
            updated = await _contacts_store.record_introduction(
                existing.contact_id,
                introduced_by=body.introduced_by,
                met_via=body.met_via,
            )
            return ContactIntroResponse(
                contact=ContactResponse(**(updated or existing).to_dict()),
                created=False,
            )

        contact = await _contacts_store.create(
            display_name=body.name,
            trust_tier=body.trust_tier,
            interaction_allowed=False,   # provisional: an intro never grants standing
            import_source="agent_intro",
            notes=body.note,
            introduced_by=body.introduced_by,
            met_via=body.met_via,
        )
        if body.gateway and body.address:
            try:
                # rcs is a transport over the phone identity; store the handle under
                # the canonical phone gateway (sms) so it resolves across channels.
                store_gw = "sms" if body.gateway == "rcs" else body.gateway
                await _contacts_store.add_handle(
                    contact.contact_id, gateway=store_gw,
                    address=body.address, source="agent_intro")
            except ValueError as exc:
                # Handle raced onto another contact between resolve and create.
                logger.info("intro add_handle conflict for %s: %s",
                            contact.contact_id, exc)
        created = await _contacts_store.get(contact.contact_id)
        return ContactIntroResponse(
            contact=ContactResponse(**(created or contact).to_dict()),
            created=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("capture_introduction failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/contacts/resolve", response_model=ContactResponse)
async def resolve_contact_by_handle(gateway: str, address: str, create: bool = False) -> ContactResponse:
    """Resolve a contact from a messaging handle (v0.21.2).

    Registered BEFORE /contacts/{contact_id} so it isn't shadowed by the
    parameterized route. Lets the host plugin map an inbound sender
    (platform + address) to the real Colony contact, so per-contact
    memory/affect/facts engage instead of pooling everything under 'default'.

    With ``create=true`` an unknown messaging sender is PROVISIONED as an inert
    contact (trust_tier=unknown, interaction_allowed=false -> no proactive
    outreach) so its memory attributes to a real person instead of being lost;
    contact merge / promotion reconcile it later.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact store not initialized")
    try:
        # Normalized, cross-gateway phone-identity resolution (a number is one contact regardless of
        # the transport it arrived on). find_by_handle stays exact-match for dedup callers.
        contact = await _contacts_store.resolve_messaging_handle(gateway, address)
        if contact is None:
            if create and gateway and address:
                contact = await _contacts_store.create(
                    display_name=address, trust_tier="unknown",
                    interaction_allowed=False, import_source="auto_provision",
                )
                try:
                    await _contacts_store.add_handle(
                        contact.contact_id, gateway=gateway, address=address, source="auto_provision")
                except Exception as exc:
                    logger.warning("auto-provision add_handle failed: %s", exc)
                logger.info("auto-provisioned contact %s for %s:%s", contact.contact_id, gateway, address)
                return ContactResponse(**contact.to_dict())
            raise HTTPException(status_code=404, detail="No contact for that handle")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("resolve_contact_by_handle failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: str) -> ContactResponse:
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact = await _contacts_store.get(contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("get_contact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/authz/scope", response_model=ScopeAuthzResponse)
async def authz_scope(platform: str, external_id: str, gateway: str, address: str) -> ScopeAuthzResponse:
    """Is this sender authorized WITHIN this group scope? Context-scoped only — it
    says nothing about 1:1 DM rights. Used by the messaging bridge to admit group
    members of an agent-created/joined group without granting them per-user access."""
    if _contacts_store is None:
        return ScopeAuthzResponse(authorized=False)
    try:
        scope = await _contacts_store.get_scope(platform=platform, external_id=external_id)
        if scope is None or not scope.active:
            return ScopeAuthzResponse(
                authorized=False,
                scope_id=(scope.scope_id if scope else None),
                active=bool(scope and scope.active),
            )
        contact = await _contacts_store.resolve_messaging_handle(gateway, address)
        if contact is None:
            return ScopeAuthzResponse(
                authorized=False, scope_id=scope.scope_id,
                granted_tier=scope.granted_tier, active=True,
            )
        ok = await _contacts_store.is_authorized_in_scope(contact.contact_id, scope.scope_id)
        return ScopeAuthzResponse(
            authorized=ok, scope_id=scope.scope_id, granted_tier=scope.granted_tier,
            contact_id=contact.contact_id, active=True,
        )
    except Exception as exc:
        logger.warning("authz_scope failed: %s", exc)
        return ScopeAuthzResponse(authorized=False)


@router.post("/authz/scope", response_model=ScopeResponse, status_code=201)
async def create_authz_scope(body: ScopeCreateRequest) -> ScopeResponse:
    """Create (idempotent by platform+external_id) a trust scope and add members.
    Unknown member handles are auto-created as shadow contacts on first sight
    (acquaintance tier, no 1:1 interaction) — see social-graph-autonomy spec."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        scope = await _contacts_store.create_scope(
            scope_type=body.scope_type, platform=body.platform, external_id=body.external_id,
            label=body.label, granted_tier=body.granted_tier, created_by=body.created_by,
        )
        member_ids: List[str] = []
        for m in body.members:
            cid = m.contact_id
            if cid is None and m.gateway and m.address:
                contact = await _contacts_store.resolve_messaging_handle(m.gateway, m.address)
                if contact is None:
                    contact = await _contacts_store.create(
                        display_name=(m.name or m.address),
                        trust_tier="acquaintance", import_source="agent_scope",
                    )
                    # rcs is a transport over the phone identity; store the handle under the
                    # canonical phone gateway (sms) so it resolves across phone-bearing channels.
                    store_gw = "sms" if m.gateway == "rcs" else m.gateway
                    await _contacts_store.add_handle(
                        contact.contact_id, gateway=store_gw, address=m.address,
                        source="agent_scope", confidence=0.6,
                    )
                cid = contact.contact_id
            if cid:
                await _contacts_store.add_scope_member(scope.scope_id, cid, role=m.role)
                member_ids.append(cid)
        return ScopeResponse(
            scope_id=scope.scope_id, scope_type=scope.scope_type, platform=scope.platform,
            external_id=scope.external_id, label=scope.label, granted_tier=scope.granted_tier,
            active=scope.active, members=member_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("create_authz_scope failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/authz/scope/deactivate")
async def deactivate_authz_scope(body: ScopeDeactivateRequest) -> Dict[str, Any]:
    """Revoke group-trust for a whole scope at once (e.g. owner mutes/leaves the group)."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        scope_id = body.scope_id
        if scope_id is None and body.platform and body.external_id:
            scope = await _contacts_store.get_scope(platform=body.platform, external_id=body.external_id)
            scope_id = scope.scope_id if scope else None
        if scope_id is None:
            raise HTTPException(status_code=404, detail="scope not found")
        await _contacts_store.deactivate_scope(scope_id)
        return {"ok": True, "scope_id": scope_id, "active": False}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("deactivate_authz_scope failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/scopes/promotion-candidates")
async def scope_promotion_candidates() -> Dict[str, Any]:
    """Group-scope members with sustained contact but no 1:1 rights yet — the people the owner
    can promote (group_guest -> regular). ``auto_promote`` reports the configured mode: when
    True the consumer auto-promotes; when False these are proposals for the owner to approve."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    cfg = getattr(_contacts_store, "_config", None)
    auto = bool(getattr(cfg, "auto_promote_group_to_1on1", False))
    min_int = int(getattr(cfg, "group_promote_min_interactions", 5))
    cands = await _contacts_store.group_promotion_candidates(min_interactions=min_int)
    return {
        "auto_promote": auto,
        "min_interactions": min_int,
        "candidates": [
            {"contact_id": c.contact_id, "display_name": c.display_name,
             "trust_tier": c.trust_tier, "interaction_count": c.interaction_count}
            for c in cands
        ],
    }


@router.post("/scopes/promote")
async def scope_promote(body: ScopePromoteRequest) -> Dict[str, Any]:
    """Promote one group-scope member to global 1:1 (tier >= to_tier + interaction allowed).
    Only ever raises standing. Called after owner approval, or by the auto-promote sweep."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        changed = await _contacts_store.promote_scope_member(body.contact_id, to_tier=body.to_tier)
    except Exception as exc:
        logger.warning("scope_promote failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "contact_id": body.contact_id, "changed": changed}


def guard_derive_context_enabled() -> bool:
    """COLONY_GUARD_DERIVE_CONTEXT (default on): complete a guard-check
    request server-side when the host omits context. Off restores the
    legacy null-key behavior (context-dependent checks silently pass)."""
    return os.environ.get("COLONY_GUARD_DERIVE_CONTEXT", "1").strip().lower() \
        not in ("0", "off", "false", "no")


async def _derive_guard_context(body: ResponseGuardCheckRequest) -> None:
    """L3.3 — server-side completion of the guard-check context.

    The chat hot path's host plugin sends only text + ids today, so the
    context-dependent checks (cross_context, tom2_epistemic) evaluated
    against a null conversation_key and returned [] — dead in exactly the
    place they matter. Derive what the host omitted, from what the sidecar
    already knows:

      * ``conversation_key``  — the same derivation turns/sync uses
        (_ensure_channel_id: session gateway, then primary handle gateway,
        then unknown:<contact>), so guard keys and provenance keys AGREE;
      * ``trust_tier``        — the contact store's tier for the target;
      * ``mentioned_entities``— rule-based NER over the INCOMING message
        (entities the counterpart just introduced belong to this
        conversation and must not read as leaks).

    Mutates ``body`` in place; never raises — any failure evaluates with
    whatever the host sent (today's behavior).
    """
    if not guard_derive_context_enabled():
        return
    try:
        from types import SimpleNamespace
        if not body.conversation_key and body.target_contact_id:
            body.conversation_key = await _ensure_channel_id(
                SimpleNamespace(channel_id=None,
                                contact_id=body.target_contact_id))
        if not body.trust_tier and body.target_contact_id \
                and _contacts_store is not None:
            contact = await _contacts_store.get(body.target_contact_id)
            tier = str(getattr(contact, "trust_tier", "") or "") \
                if contact is not None else ""
            if tier:
                body.trust_tier = tier
        if not body.mentioned_entities and body.incoming_message_text:
            extractor = _get_conversation_extractor()
            if extractor is not None:
                res = await extractor.extract(body.incoming_message_text,
                                              "guard-context")
                names: List[str] = []
                seen: set = set()
                for cand in getattr(res, "entities", []):
                    name = (getattr(cand, "text", None)
                            or getattr(cand, "name", "") or "").strip()
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        names.append(name)
                    if len(names) >= 10:
                        break
                if names:
                    body.mentioned_entities = names
    except Exception:
        logger.debug("guard context derivation failed (evaluating with "
                     "what the host sent)", exc_info=True)


@router.post("/response-guard/check")
async def response_guard_check(body: ResponseGuardCheckRequest) -> Dict[str, Any]:
    """Evaluate an outbound reply under the exact outbound-surface policy.

    Speech surfaces bypass without context derivation. A missing configured
    guard allows in shadow and blocks in enforce for guarded text/artifacts.
    Missing context (conversation_key / trust_tier / mentioned_entities) is
    derived server-side (COLONY_GUARD_DERIVE_CONTEXT, default on) so the
    context-dependent checks actually fire on the chat hot path."""
    from colony_sidecar.gate.response_guard import (
        GuardMode,
        unavailable_guard_result,
    )
    from colony_sidecar.gate.surface_policy import EXCLUDED_SPEECH_SURFACES

    if _response_guard is None:
        configured_mode = (
            GuardMode.ENFORCE
            if os.environ.get("COLONY_GUARD_MODE", "").strip().lower()
            == GuardMode.ENFORCE.value
            else GuardMode.SHADOW
        )
        return unavailable_guard_result(
            surface=body.surface,
            configured_mode=configured_mode,
            requested_mode=body.mode,
            response_text=body.response_text,
            communication_policy=body.communication_policy,
        ).to_dict()

    if body.surface not in EXCLUDED_SPEECH_SURFACES:
        await _derive_guard_context(body)
    mode = GuardMode(body.mode) if body.mode else None
    result = await _response_guard.evaluate(
        surface=body.surface,
        response_text=body.response_text,
        incoming_message_text=body.incoming_message_text or "",
        trust_tier=body.trust_tier or "regular",
        target_contact_id=body.target_contact_id or "",
        target_gateway=body.target_gateway or "",
        session_id=body.session_id or "",
        turn_id=body.turn_id or "",
        conversation_key=body.conversation_key,
        mentioned_entities=body.mentioned_entities,
        mode=mode,
        # A bearer that can request an evaluation is not thereby allowed to
        # exempt its own cross-context transfer.  Owner-directed exemptions are
        # available only to trusted in-process paths that derive identity from
        # server-owned state.
        authorized=False,
        communication_policy=body.communication_policy,
    )
    return result.to_dict()


@router.get("/response-guard/audit")
async def response_guard_audit(limit: int = 50, authorized: Optional[bool] = None,
                               check: Optional[str] = None) -> Dict[str, Any]:
    """Review guard audit events (any check, not just cross_context), split by authorized
    (owner-directed) vs not, optionally filtered to one check. The summary carries 24h/7d/14d
    windows with per-check counts and the would_block_rate — the numbers that decide whether
    a check is inside its false-positive budget before enforce is turned on. When present,
    digest-bound communication-policy evaluations are returned separately so clean policy
    checks do not alter those metrics."""
    audit = getattr(_response_guard, "_audit", None) if _response_guard is not None else None
    breaker = None
    if _response_guard is not None and hasattr(_response_guard, "breaker_status"):
        try:
            breaker = _response_guard.breaker_status()
        except Exception:
            breaker = None
    if audit is None:
        return {"summary": {"total": 0}, "events": [], "breaker": breaker}
    policy_reader = getattr(audit, "recent_communication_policy", None)
    policy_evaluations = (
        policy_reader(limit=limit) if callable(policy_reader) else []
    )
    result = {"summary": audit.summary(),
              "events": audit.recent(limit=limit, authorized=authorized, check=check),
              "breaker": breaker}
    if policy_evaluations:
        result["communication_policy_evaluations"] = policy_evaluations
    return result


@router.get("/env-risk")
async def env_risk(conversation_key: str, contact_id: str) -> Dict[str, Any]:
    """Owner observability for the environment-risk classifier (L1.2): grade
    one (conversation, reader) pair R0..R3 and show the census it was graded
    on. Identity/topology only — contact ids, methods, timestamps; never
    message content. Fail-closed: any missing store or error grades R3."""
    from colony_sidecar.gate.env_risk import classify, env_risk_window_hours
    risk = await classify(conversation_key, contact_id,
                          presence_store=_presence_store,
                          contacts_store=_contacts_store)
    census: List[Dict[str, Any]] = []
    if _presence_store is not None:
        try:
            census = [
                {"contact_id": r.get("contact_id"),
                 "method": r.get("method"),
                 "group_id": r.get("group_id"),
                 "last_seen_at": r.get("last_seen_at")}
                for r in _presence_store.census(
                    conversation_key, window_hours=env_risk_window_hours())
            ]
        except Exception:
            census = []
    return {"conversation_key": conversation_key, "contact_id": contact_id,
            "window_hours": env_risk_window_hours(),
            **risk.to_dict(), "census": census}



@router.post("/contacts/{contact_id}/timezone", response_model=ContactResponse)
async def set_contact_timezone(contact_id: str, body: ContactTimezoneRequest) -> ContactResponse:
    """Set (or clear, with null) a contact's IANA timezone (v0.21.0, editable)."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        await _contacts_store.set_timezone(contact_id, body.timezone)
        contact = await _contacts_store.get(contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("set_contact_timezone failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/temporal/config", response_model=TemporalConfigResponse)
async def get_temporal_config() -> TemporalConfigResponse:
    """Current temporal reference frame (agent home tz + defaults). v0.21.0."""
    from colony_sidecar.util import temporal as _temporal
    atz = _temporal.agent_timezone()
    return TemporalConfigResponse(
        agent_timezone=atz,
        default_contact_timezone=_temporal.default_contact_timezone(),
        now_utc=_temporal.now_utc().isoformat(),
        now_agent_local=_temporal.now_in(atz).isoformat(),
        agent_local_clock=_temporal.format_clock(_temporal.now_in(atz)),
    )


@router.post("/temporal/config", response_model=TemporalConfigResponse)
async def set_temporal_config(body: TemporalConfigRequest) -> TemporalConfigResponse:
    """Edit the agent home tz and/or the default contact tz. v0.21.0."""
    from colony_sidecar.util import temporal as _temporal
    try:
        if body.agent_timezone is not None:
            _temporal.set_agent_timezone(body.agent_timezone)
        if body.clear_default_contact_timezone:
            _temporal.set_default_contact_timezone(None)
        elif body.default_contact_timezone is not None:
            _temporal.set_default_contact_timezone(body.default_contact_timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    atz = _temporal.agent_timezone()
    return TemporalConfigResponse(
        agent_timezone=atz,
        default_contact_timezone=_temporal.default_contact_timezone(),
        now_utc=_temporal.now_utc().isoformat(),
        now_agent_local=_temporal.now_in(atz).isoformat(),
        agent_local_clock=_temporal.format_clock(_temporal.now_in(atz)),
    )


_TIMELINE_TYPE_LABELS = {
    "conversation.turn": "💬 talked",
    "outreach.sent": "📤 reached out",
    "initiative.generated": "💡 initiative",
    "initiative.completed": "✅ initiative done",
    "task.created": "📋 task created",
    "task.completed": "✅ task done",
    "commitment.made": "🤝 promised",
    "commitment.fulfilled": "✅ kept promise",
    "memory.written": "🧠 remembered",
}


@router.get("/timeline", response_model=TimelineResponse)
async def get_timeline(
    since: str = Query("24h", description="Relative ('24h','7d') or ISO, or today/yesterday"),
    types: Optional[str] = Query(None, description="Comma-separated event types to include"),
    contact_id: Optional[str] = Query(None, description="Only events involving this contact"),
    limit: int = Query(100, ge=1, le=500),
) -> TimelineResponse:
    """Chronological timeline across all Colony subsystems (v0.21.0).

    Backed by the event journal. Lets the agent answer 'what happened recently',
    'what's been going on with X', 'what changed since I last looked'.
    """
    from colony_sidecar.events.journal import replay_events
    from colony_sidecar.util import temporal as _t

    since_iso = _t.parse_relative_since(since)
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    # Walk the journal newest-first so the cap drops the OLD end of a large
    # window — an oldest-first walk here showed stale activity while the
    # endpoint claimed "newest first" whenever the window exceeded the cap.
    raw = replay_events(since_iso, limit=max(500, limit), types=type_list,
                        newest_first=True)

    all_events: list[TimelineEvent] = []
    for e in raw.get("events", []):
        data = e.get("data", {}) or {}
        cid = data.get("contact_id") or data.get("person_id")
        if contact_id and cid != contact_id:
            continue
        at = e.get("recordedAt", "")
        all_events.append(TimelineEvent(
            seq=e.get("seq", 0),
            type=e.get("type", "unknown"),
            at=at,
            when=_t.humanize_delta(at),
            bucket=_t.bucket(at),
            summary=(data.get("summary") or data.get("title") or data.get("text")
                     or data.get("reason")),
            contact_id=cid,
            data=data,
        ))

    all_events.sort(key=lambda x: x.seq, reverse=True)  # newest first
    events = all_events[:limit]
    has_more = len(all_events) > limit or raw.get("hasMore", False)

    # Resolve contact ids -> names for a readable digest (cached).
    _name_cache: dict = {}
    async def _contact_name(cid):
        if not cid or cid == "default":
            return None
        if cid in _name_cache:
            return _name_cache[cid]
        nm = cid
        if _contacts_store is not None:
            try:
                c = await _contacts_store.get(cid)
                if c is not None:
                    nm = c.display_name or c.given_name or cid
            except Exception:
                pass
        _name_cache[cid] = nm
        return nm

    digest_lines = []
    for ev in events[:40]:
        label = _TIMELINE_TYPE_LABELS.get(ev.type, ev.type)
        snippet = (ev.summary or "").replace("\n", " ").strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        nm = await _contact_name(ev.contact_id)
        who = f" with {nm}" if nm else ""
        digest_lines.append(f"• {ev.when} — {label}{who}{': ' + snippet if snippet else ''}")
    digest = ("\n".join(digest_lines)
              if digest_lines else f"No journaled events since {since_iso}.")

    return TimelineResponse(
        since=since_iso,
        count=len(events),
        digest=digest,
        events=events,
        has_more=has_more,
    )


@router.get("/temporal/contacts", response_model=TemporalContactsResponse)
async def temporal_contacts(
    overdue_only: bool = Query(True, description="Only contacts overdue vs their own cadence"),
    limit: int = Query(20, ge=1, le=100),
) -> TemporalContactsResponse:
    """Per-contact cadence + silence (v0.21.0).

    Estimates each contact's typical rhythm and flags those overdue relative to
    *their* cadence. Powers cadence-aware proactive outreach (the worker/agent
    queries this) and the per-turn 'heads-up' line.
    """
    from colony_sidecar.util import temporal as _t
    if _contacts_store is None:
        return TemporalContactsResponse(now=_t.now_utc().isoformat(), count=0, contacts=[])
    rows = await _contacts_store.compute_cadence_overdue(
        overdue_only=overdue_only, limit=limit,
    )
    return TemporalContactsResponse(
        now=_t.now_utc().isoformat(),
        count=len(rows),
        contacts=[TemporalContact(**r) for r in rows],
    )


@router.post("/contacts/{contact_id}/style", response_model=ContactStyleResponse)
async def get_contact_style(contact_id: str, body: ContactStyleRequest) -> ContactStyleResponse:
    if _contacts_store is None:
        return ContactStyleResponse(person_id=contact_id)
    try:
        style = await _contacts_store.get_style(contact_id)
        return ContactStyleResponse(person_id=contact_id, **style)
    except Exception as exc:
        logger.warning("get_contact_style failed: %s", exc)
        return ContactStyleResponse(person_id=contact_id)


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

_briefings_engine = None

def set_briefings_engine(engine) -> None:
    global _briefings_engine
    _briefings_engine = engine


def _briefing_to_response(b) -> BriefingResponse:
    """Map a stored briefing onto the API schema.

    The engine returns ``briefings.models.Briefing`` dataclasses, not dicts —
    unpacking them with ``**`` was the failure that silently emptied this
    endpoint. Plain dicts are still accepted for forward compatibility.
    """
    if isinstance(b, dict):
        return BriefingResponse(**b)
    sections = [
        s for s in (getattr(b, "sections", None) or [])
        if not getattr(s, "suppressed", False)
    ]
    body = "\n\n".join(
        n for n in (getattr(s, "narrative", "") for s in sections) if n
    )
    btype = getattr(b, "briefing_type", None)
    btype_str = getattr(btype, "value", btype) if btype is not None else None
    created = getattr(b, "created_at", None)
    return BriefingResponse(
        id=str(getattr(b, "briefing_id", "") or ""),
        title=f"{btype_str} briefing" if btype_str else None,
        body=body,
        briefing_type=btype_str,
        created_at=created.isoformat() if hasattr(created, "isoformat")
                   else (str(created) if created else None),
    )


@router.get("/briefings", response_model=BriefingListResponse)
async def list_briefings(limit: int = 10) -> BriefingListResponse:
    if _briefings_engine is None:
        return BriefingListResponse(briefings=[])
    try:
        briefings = _briefings_engine.get_recent(limit=limit)
        return BriefingListResponse(
            briefings=[_briefing_to_response(b) for b in briefings])
    except Exception as exc:
        # A store/mapping failure must surface, never masquerade as an empty
        # briefing list (200 [] is indistinguishable from "no briefings").
        logger.warning("list_briefings failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"list_briefings failed: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# World Model
# ---------------------------------------------------------------------------

_world_store = None

def set_world_store(store) -> None:
    global _world_store
    _world_store = store


@router.post("/world/entities/query", response_model=EntityListResponse)
@router.post("/world-model/entities", response_model=EntityListResponse, include_in_schema=False)
async def query_entities(body: EntityQueryRequest) -> EntityListResponse:
    if _world_store is None:
        return EntityListResponse(entities=[])
    entity_type = body.entity_type if body.entity_type and body.entity_type != "all" else None
    try:
        entities = await _world_store.find_entities(
            query=body.query, entity_type=entity_type, limit=body.limit or 10,
        )
        return EntityListResponse(entities=[EntityResponse(**_to_dict(e)) for e in entities])
    except Exception as exc:
        logger.warning("query_entities failed: %s", exc)
        return EntityListResponse(entities=[])


@router.get("/world/entities", response_model=EntityListResponse)
@router.get("/world-model/entities", response_model=EntityListResponse, include_in_schema=False)
async def list_entities(entity_type: Optional[str] = None, limit: int = 50) -> EntityListResponse:
    if _world_store is None:
        return EntityListResponse(entities=[])
    try:
        entities = await _world_store.find_entities(query="", entity_type=entity_type, limit=limit)
        return EntityListResponse(entities=[EntityResponse(**_to_dict(e)) for e in entities])
    except Exception as exc:
        logger.warning("find_entities failed: %s", exc)
        return EntityListResponse(entities=[])


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_extraction_pipeline = None


def set_extraction_pipeline(pipeline) -> None:
    global _extraction_pipeline
    _extraction_pipeline = pipeline


@router.post("/world/extract", response_model=ExtractionResponse)
async def extract_entities(body: ExtractionRequest) -> ExtractionResponse:
    if _extraction_pipeline is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    import base64
    import binascii
    # ``content`` is contractually base64 (see ExtractionRequest). Decode it
    # BEFORE the generic handler below so plain text yields a clear 400, not
    # an opaque 500 "Incorrect padding".
    try:
        content = base64.b64decode(body.content)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_content_encoding",
            "message": (
                "content must be base64-encoded document bytes "
                f"(decode failed: {exc}); base64-encode plain text "
                "before sending"
            ),
        })
    try:
        entities = await _extraction_pipeline.extract(
            content=content,
            filename=body.filename or "",
            mime_type=body.mime_type or "",
            metadata=body.metadata or {},
        )
        return ExtractionResponse(
            format_detected="detected",
            entities=[
                ExtractedEntityResponse(
                    name=e.name,
                    entity_type=e.entity_type,
                    attributes=e.attributes,
                    confidence=e.confidence,
                )
                for e in entities
            ],
            text_length=len(content),
        )
    except Exception as exc:
        logger.warning("extract_entities failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

_metalearner = None

def set_metalearner(learner) -> None:
    global _metalearner
    _metalearner = learner


@router.post("/cognition/cycle")
async def cognition_cycle(body: CognitionCycleRequest) -> dict:
    """Run the legacy detector cycle but publish the canonical benchmark.

    The detector still consumes its internal CPI object until it is retired;
    the public API must not manufacture the historical memory/reasoning/social
    dimensions when their evidence is unavailable.
    """
    from colony_sidecar.self_model.benchmark import legacy_cpi_payload

    canonical_cpi = legacy_cpi_payload(_benchmark)
    if _metalearner is None:
        return {"cpi": canonical_cpi, "gaps": [], "adjustments": []}
    try:
        result = await _metalearner.run_cycle()
        gaps = []
        if result and hasattr(result, "gaps"):
            for g in result.gaps:
                severity = getattr(g, "severity", 0.0)
                if hasattr(severity, "value"):
                    severity = severity.value
                gaps.append({
                    "gap_id": getattr(g, "id", str(uuid.uuid4())),
                    "domain": getattr(g, "domain", "general"),
                    "severity": severity,
                    "description": getattr(g, "description", None),
                })
        adjustments = []
        if result and hasattr(result, "adjustments"):
            for a in result.adjustments:
                adjustments.append({"domain": getattr(a, "domain", ""), "action": getattr(a, "action", "")})
        return {"cpi": canonical_cpi, "gaps": gaps,
                "adjustments": adjustments}
    except Exception as exc:
        logger.warning("cognition_cycle failed: %s", exc)
        return {"cpi": canonical_cpi, "gaps": [], "adjustments": [],
                "error": str(exc)}


@router.get("/cognition/cpi")
async def get_cpi() -> dict:
    """Deprecated compatibility surface backed by SelfhoodBenchmark."""
    from colony_sidecar.self_model.benchmark import legacy_cpi_payload

    try:
        return legacy_cpi_payload(_benchmark)
    except Exception as exc:
        logger.warning("get_cpi failed: %s", exc)
        return {"deprecated": True, "available": False,
                "canonical_endpoint": "/v1/host/self/benchmark",
                "error": str(exc)}


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

_research_pipeline = None

def set_research_pipeline(pipeline) -> None:
    global _research_pipeline
    _research_pipeline = pipeline


_search_orchestrator = None


def set_search_orchestrator(orchestrator) -> None:
    global _search_orchestrator
    _search_orchestrator = orchestrator


@router.get("/search/providers")
async def list_search_providers():
    if _search_orchestrator is None:
        return {"providers": [], "available": False}
    return {
        "providers": _search_orchestrator.list_providers(),
        "available": _search_orchestrator.has_providers,
    }


@router.post("/search")
async def search(body: dict):
    if _search_orchestrator is None or not _search_orchestrator.has_providers:
        raise HTTPException(status_code=501, detail="No search provider configured")
    query = body.get("query", "")
    max_results = body.get("max_results", 5)
    provider = body.get("provider", "")
    results = await _search_orchestrator.search(query, max_results, provider)
    return {
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "source": r.source}
            for r in results
        ],
        "count": len(results),
    }


@router.post("/research/start", response_model=ResearchRunResponse)
async def start_research(body: ResearchStartRequest) -> ResearchRunResponse:
    if _research_pipeline is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        depth_map = {"quick": 1, "standard": 3, "deep": 5}
        depth_map.get(body.depth or "standard", 3)
        run = await _research_pipeline.run(goal=body.topic, metadata={"depth": body.depth, "person_id": body.person_id})
        return ResearchRunResponse(
            run_id=run.id,
            topic=body.topic,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            stages_completed=[run.current_stage.value if hasattr(run.current_stage, "value") else str(run.current_stage)],
            artifact=run.artifact.__dict__ if run.artifact and hasattr(run.artifact, "__dict__") else (run.artifact if isinstance(run.artifact, dict) else None),
        )
    except Exception as exc:
        logger.warning("start_research failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/research", response_model=ResearchListResponse)
async def list_research(limit: int = 20, status_filter: Optional[str] = Query(None, alias="status")) -> ResearchListResponse:
    if _research_pipeline is None:
        return ResearchListResponse(runs=[])
    try:
        runs = _research_pipeline.list_runs(status=status_filter, limit=limit)
        return ResearchListResponse(runs=[
            ResearchRunResponse(
                run_id=r.id,
                topic=r.goal,
                status=r.status.value if hasattr(r.status, "value") else str(r.status),
                stages_completed=[
                    r.current_stage.value if hasattr(r.current_stage, "value") else str(r.current_stage)
                ],
                artifact=(
                    r.artifact.__dict__ if r.artifact and hasattr(r.artifact, "__dict__")
                    else (r.artifact if isinstance(r.artifact, dict) else None)
                ),
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in runs
        ])
    except Exception as exc:
        logger.warning("list_research failed: %s", exc)
        return ResearchListResponse(runs=[])


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

_delivery_bridge = None

def set_delivery_bridge(bridge) -> None:
    global _delivery_bridge
    _delivery_bridge = bridge


@router.get("/delivery/pending", response_model=DeliveryListResponse)
async def list_pending_deliveries(gateway_id: str = "", limit: int = 20) -> DeliveryListResponse:
    if _delivery_bridge is None:
        raise HTTPException(
            status_code=503,
            detail="delivery_bridge_not_initialized",
        )
    try:
        pending = _delivery_bridge.get_pending(gateway_id=gateway_id, limit=limit)
        return DeliveryListResponse(pending=pending)
    except Exception as exc:
        logger.warning("list_pending_deliveries failed: %s", exc)
        return DeliveryListResponse(pending=[])


@router.post("/delivery/mark-sent")
async def mark_delivery_sent(body: DeliveryMarkRequest) -> dict:
    if _delivery_bridge is None:
        raise HTTPException(
            status_code=503,
            detail="delivery_bridge_not_initialized",
        )
    try:
        ok = _delivery_bridge.mark_sent(body.delivery_id)
        return {"ok": ok}
    except Exception as exc:
        logger.warning("mark_delivery_sent failed: %s", exc)
        return {"ok": False}


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

_connection_discoverer = None
_insight_store = None

def set_connection_discoverer(discoverer) -> None:
    global _connection_discoverer
    _connection_discoverer = discoverer


def set_insight_store(store) -> None:
    global _insight_store
    _insight_store = store


@router.post("/synthesis/discover", response_model=SynthesisDiscoverResponse)
async def discover_connections(body: SynthesisDiscoverRequest) -> SynthesisDiscoverResponse:
    if _connection_discoverer is None:
        return SynthesisDiscoverResponse(connections=[])
    try:
        connections = await _connection_discoverer.discover_connections(
            person_id=body.person_id,
            min_novelty=body.min_novelty or 0.3,
        )
        results = []
        for c in connections:
            results.append(SynthesisConnection(
                id=getattr(c, "id", str(uuid.uuid4())),
                connection_type=getattr(c, "connection_type", "unknown"),
                entities=getattr(c, "entities", []),
                novelty=getattr(c, "novelty", 0.0),
                description=getattr(c, "description"),
            ))
        return SynthesisDiscoverResponse(connections=results)
    except Exception as exc:
        logger.warning("discover_connections failed: %s", exc)
        return SynthesisDiscoverResponse(connections=[])


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

_learner = None
_learning_feedback_store = None

def set_learner(learner) -> None:
    global _learner
    _learner = learner


def set_learning_feedback_store(store) -> None:
    """Wire the one durable correction ledger shared by learning and P4."""
    global _learning_feedback_store
    _learning_feedback_store = store


@router.post("/learning/correction")
async def submit_correction(
    body: LearningCorrectionRequest,
    request: Request,
) -> dict:
    if _learner is None and _learning_feedback_store is None:
        return {"accepted": False}
    try:
        from colony_sidecar.intelligence.learning.feedback_store import (
            UserCorrection,
        )

        person_id = resolve_request_person(
            request, context_person_id=body.context.contact_id) or ""
        correction = UserCorrection.create(
            original_response=body.original,
            correction_text=body.correction,
            correction_type=body.correction_type,
            context_hash=(body.external_ref or "").strip(),
            person_id=person_id,
        )
        if body.correction_id:
            correction_id = body.correction_id.strip()
            if not correction_id or len(correction_id) > 192:
                raise ValueError("correction_id is malformed")
            correction.correction_id = correction_id
        if _learning_feedback_store is not None:
            # Persistence precedes volatile adaptation.  A learner failure
            # cannot erase owner feedback or its future benchmark evidence.
            _learning_feedback_store.record_correction(correction)
    except Exception as exc:
        logger.warning("submit_correction persistence failed: %s", exc)
        return {"accepted": False}

    learned = False
    if _learner is not None:
        try:
            await _learner.ingest_correction(correction)
            learned = True
        except Exception as exc:
            # The durable owner correction remains accepted.  Continuous
            # adaptation can replay it from FeedbackStore after recovery.
            logger.warning("submit_correction learner failed: %s", exc)
    return {"accepted": True, "learned": learned,
            "correction_id": correction.correction_id}


@router.post("/learning/engagement")
async def submit_engagement(body: LearningEngagementRequest) -> dict:
    if _learner is None:
        return {"accepted": False}
    try:
        await _learner.ingest_engagement({
            "briefing_id": body.briefing_id,
            "action": body.action,
            "dwell_seconds": body.dwell_seconds,
        })
        return {"accepted": True}
    except Exception as exc:
        logger.warning("submit_engagement failed: %s", exc)
        return {"accepted": False}


@router.get("/learning/weights", response_model=LearningWeightsResponse)
async def get_learning_weights() -> LearningWeightsResponse:
    if _learner is None:
        return LearningWeightsResponse()
    try:
        weights = await _learner.get_component_weights()
        stats = _learner.stats()
        return LearningWeightsResponse(weights=weights, stats=stats)
    except Exception as exc:
        logger.warning("get_learning_weights failed: %s", exc)
        return LearningWeightsResponse()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_skills_registry = None
_commitment_store = None


def set_commitment_store(store):
    global _commitment_store
    _commitment_store = store


_affect_store = None


def set_affect_store(store):
    global _affect_store
    _affect_store = store


_facts_store = None


def set_facts_store(store):
    global _facts_store
    _facts_store = store


_p8_runtime = None


def set_p8_runtime(runtime) -> None:
    global _p8_runtime
    _p8_runtime = runtime


# --- Second-order theory of mind (tom2: refs-not-content, owner-only) ---
_tom2_store = None
_tom2_engine = None


def set_tom2_store(store) -> None:
    global _tom2_store
    _tom2_store = store


def set_tom2_engine(engine) -> None:
    global _tom2_engine
    _tom2_engine = engine


_tom2_exposure = None


def set_tom2_exposure_store(store) -> None:
    global _tom2_exposure
    _tom2_exposure = store


def _tom2_approvals():
    """Pair-approval registry over the live ProposalStore, or None."""
    if _proposal_store is None:
        return None
    from colony_sidecar.tom.approvals import Tom2ApprovalRegistry
    return Tom2ApprovalRegistry(_proposal_store)


class Tom2PairApprovalRequest(BaseModel):
    reader: str
    subject: str
    action: str = "request"      # request | approve | revoke


@router.get("/tom2/approvals")
async def tom2_approvals_list(limit: int = 100) -> dict:
    """Owner view of level-2 pair approvals (L2.4): who may receive
    epistemic lines about whom, with live TTL validity. Ids only."""
    reg = _tom2_approvals()
    if reg is None:
        return {"available": False, "pairs": []}
    return {"available": True, "pairs": reg.list_pairs(limit=limit)}


@router.post("/tom2/approvals")
async def tom2_approvals_act(body: Tom2PairApprovalRequest) -> dict:
    """Owner action on a (reader, subject) pair: request files a proposal,
    approve stamps it with a fresh TTL, revoke kills it. The eligibility
    pipeline consumes only is_approved — everything else here is inert."""
    reg = _tom2_approvals()
    if reg is None:
        raise HTTPException(status_code=501,
                            detail="Proposal store not initialized")
    action = (body.action or "request").strip().lower()
    try:
        if action == "approve":
            reg.approve_pair(body.reader, body.subject)
        elif action == "revoke":
            reg.revoke_pair(body.reader, body.subject)
        elif action == "request":
            reg.request_pair(body.reader, body.subject)
        else:
            raise HTTPException(status_code=400,
                                detail=f"unknown action {action!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "action": action, "reader": body.reader,
            "subject": body.subject,
            "approved": reg.is_approved(body.reader, body.subject)}


@router.get("/tom2/exposure")
async def tom2_exposure(reader: str = "", subject: str = "",
                        limit: int = 50) -> dict:
    """Owner read surface for the level-2 exposure ledger (L2.3): what was
    rendered to whom about whom, by REFS only (contact ids, fact refs,
    conversation keys — never fact text), plus the live budget posture.
    Empty and inert until level-2 rendering is wired and used."""
    from colony_sidecar.tom.exposure import (
        budget_global_day, budget_pair_day, budget_reader_day)
    budgets = {"pair_day": budget_pair_day(),
               "reader_day": budget_reader_day(),
               "global_day": budget_global_day()}
    if _tom2_exposure is None:
        return {"available": False, "budgets": budgets, "events": []}
    try:
        return {"available": True,
                "budgets": budgets,
                "summary": _tom2_exposure.counts(),
                "events": _tom2_exposure.recent(
                    reader_contact_id=reader or None,
                    subject_contact_id=subject or None, limit=limit)}
    except Exception as exc:
        return {"available": True, "budgets": budgets,
                "error": str(exc), "events": []}


@router.get("/tom2/status")
async def tom2_status() -> dict:
    """Owner observability for the asymmetry engine: mode, aggregate counts
    and the last run report, plus the leveled posture (L4.3) — configured/
    max level, live risk caps, and a SAMPLE decision resolved against a
    hostile placeholder environment so the owner can see every brake term
    (configured, max, risk cap, enforce evidence, cross-context) as the
    resolver sees it right now. Counts only — no inference contents here."""
    from colony_sidecar.tom.asymmetry import tom2_mode
    from colony_sidecar.tom.levels import (
        configured_level, configured_max_level, parse_risk_caps,
        resolve_effective_level, risk_caps_valid)
    counts = None
    if _tom2_store is not None:
        try:
            counts = _tom2_store.counts()
        except Exception:
            counts = None
    sample = None
    try:
        sample = (await resolve_effective_level(
            "status:probe", "status-probe-reader",
            presence_store=_presence_store,
            contacts_store=_contacts_store,
            use_cache=False)).to_dict()
    except Exception:
        sample = None
    return {"mode": tom2_mode(), "counts": counts,
            "last_run": getattr(_tom2_engine, "last_report", None),
            "configured": configured_level(),
            "max": configured_max_level(),
            "risk_caps": {"valid": risk_caps_valid(),
                          "caps": {str(k): v for k, v
                                   in parse_risk_caps().items()}},
            "sample_decision": sample}


@router.get("/tom2/report")
async def tom2_report(contact_id: str = "", kind: str = "",
                      limit: int = 100,
                      request: Request = None) -> dict:
    """Owner-facing tom2 report (H3.3): the full inference rows, owner
    reader scope, with fact refs resolved to their text where the facts
    store can. This is the OWNER'S API surface — rendering any of this for
    a non-owner contact is a separate, double-gated path that ships dark
    (see tom.render_for_contact)."""
    if _tom2_store is None:
        return {"available": False, "inferences": []}
    try:
        facts_view = _facts_store
        if _p8_runtime is not None:
            owner = (
                os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
                or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
                or "owner"
            )
            authority = request_authority(request)
            if str(authority.viewer_person_id or "") != owner:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "p8_owner_authority_required",
                        "message": "P8 Tom2 report is owner-scoped",
                    },
                )
            owner_viewer = _p8_viewer_for_request(request, owner)
            facts_view = _p8_runtime.projected_facts_view(
                owner_viewer, now=datetime.now(timezone.utc))
        rows = _tom2_store.list_inferences(
            contact_id=contact_id or None, kind=kind or None,
            limit=max(1, min(500, int(limit))))
        if facts_view is not None:
            projected_rows = []
            for r in rows:
                try:
                    refs = [r.get("fact_ref")]
                    if _p8_runtime is not None:
                        refs += list(r.get("evidence_refs") or [])
                    visible = [
                        facts_view.get_fact(str(ref or ""))
                        for ref in refs
                    ]
                    f = visible[0] if visible else None
                    if f and (
                        _p8_runtime is None or all(visible)
                    ):
                        r["fact"] = f.get("fact")
                        r["fact_contact_id"] = f.get("contact_id")
                        if _p8_runtime is not None:
                            projected_rows.append(r)
                except Exception:
                    pass
            if _p8_runtime is not None:
                rows = projected_rows
        return {"available": True, "count": len(rows), "inferences": rows}
    except HTTPException:
        raise
    except Exception as exc:
        return {"available": True, "error": str(exc), "inferences": []}


_DEFAULT_TOM_FACTS_STORE = object()


def _render_tom2_context(
    max_lines: int = 8,
    *,
    facts_store: Any = _DEFAULT_TOM_FACTS_STORE,
    strict_projection: bool = False,
) -> str:
    """Compact owner-context rendering of the freshest asymmetries.

    unaware_of rows are the informative ones ("X hasn't heard this yet");
    fact refs resolve to text here because this renders ONLY into the
    owner's context — the caller enforces that."""
    if _tom2_store is None:
        return ""
    resolved_facts = (
        _facts_store
        if facts_store is _DEFAULT_TOM_FACTS_STORE else facts_store
    )
    rows = _tom2_store.list_inferences(kind="unaware_of", limit=50)
    lines = []
    candidates = rows if strict_projection else rows[:max_lines]
    for r in candidates:
        subject = ""
        if resolved_facts is not None:
            try:
                refs = [r.get("fact_ref")]
                if strict_projection:
                    refs += list(r.get("evidence_refs") or [])
                visible = [
                    resolved_facts.get_fact(str(ref or ""))
                    for ref in refs
                ]
                f = visible[0] if visible else None
                if f and all(visible):
                    subject = str(f.get("fact") or "")[:120]
            except Exception:
                subject = ""
        if not subject:
            if strict_projection:
                continue
            subject = f"a shared fact ({r.get('fact_ref')})"
        lines.append(
            f"- {r.get('contact_id')} appears unaware of: {subject} "
            f"(confidence {float(r.get('confidence') or 0):.2f})")
        if len(lines) >= max_lines:
            break
    if not lines:
        return ""
    if not strict_projection and len(rows) > max_lines:
        lines.append(f"... and {len(rows) - max_lines} more "
                     "(GET /v1/host/tom2/report)")
    return "\n".join(lines)


_context_provenance = None


def set_context_provenance_store(store):
    global _context_provenance
    _context_provenance = store


_channel_store = None


def set_channel_store(store) -> None:
    """Wire the channel registration store so turn traffic keeps it alive."""
    global _channel_store
    _channel_store = store


_presence_store = None


def set_presence_store(store) -> None:
    """Wire the conversation presence registry (L1.1) so attributed turns
    feed the census the environment-risk classifier reads."""
    global _presence_store
    _presence_store = store


_taint_registry = None


def set_taint_registry(registry) -> None:
    """Wire the injection-taint registry (L3.1). The level-2 context wiring
    registers a taint per rendered epistemic line; the tom2_epistemic guard
    check reads it. No registry => level 2 never renders (fail closed)."""
    global _taint_registry
    _taint_registry = registry


def _observe_channel(channel_id: str) -> None:
    """Auto-register a channel on first sighting and keep last_seen_at fresh.

    Channels become first-class registered entities from real traffic alone: a
    surface that has never called /v1/channels/register still appears in the
    registry (as an observed, minimally-described channel) the moment a turn
    flows through it. Explicit registration with a fuller manifest can later
    upsert over this using the channel token.
    """
    if _channel_store is None or not channel_id:
        return
    try:
        if _channel_store.get(channel_id) is None:
            from colony_sidecar.channels.manifest import ChannelManifest

            gateway = channel_id.split(":", 1)[0] if ":" in channel_id else channel_id
            _channel_store.register(
                ChannelManifest(
                    channel_key=channel_id,
                    display_name=channel_id,
                    gateway_family=gateway,
                    platform_hint=gateway,
                )
            )
            logger.info("channel observed + auto-registered: %s", channel_id)
        else:
            _channel_store.touch(channel_id)
    except Exception:
        # registration must never affect turn processing
        logger.debug("channel observe failed for %s", channel_id, exc_info=True)


async def _ensure_channel_id(
    context,
    identity=None,
) -> str:
    """Derive a stable channel_id when the host does not provide one.

    Resolution order:
    1. Host-provided channel_id (pass-through)
    2. Active session's gateway + contact_id
    3. Contact's primary handle gateway + contact_id
    4. host_id + contact_id
    5. "unknown:" + contact_id
    """
    if context.channel_id:
        return context.channel_id

    contact = context.contact_id or "anonymous"
    gateway = None

    if _session_store is not None and context.contact_id:
        try:
            session = await _session_store.get_by_contact(context.contact_id)
            if session is not None:
                gateway = session.gateway
        except Exception:
            pass

    if not gateway and _contacts_store is not None and context.contact_id:
        try:
            handles = await _contacts_store.get_handles(context.contact_id)
            primary = next(
                (h for h in handles if getattr(h, "is_primary", False)),
                handles[0] if handles else None,
            )
            if primary is not None:
                gateway = getattr(primary, "gateway", None)
        except Exception:
            pass

    if not gateway and identity is not None:
        gateway = getattr(identity, "host_id", None)

    return f"{gateway or 'unknown'}:{contact}"


_conversation_extractor = None


def _get_conversation_extractor():
    """Lazily build a shared rule-based entity extractor (regex NER, no LLM)."""
    global _conversation_extractor
    if _conversation_extractor is None:
        try:
            from colony_sidecar.world_model.extraction.conversation_extractor import (
                ConversationExtractor,
            )
            _conversation_extractor = ConversationExtractor()
        except Exception:
            _conversation_extractor = False  # tried and failed; don't retry every turn
    return _conversation_extractor or None


async def _world_context_entities(query_text: str, limit: int = 5) -> list:
    """World-model entities relevant to a context query.

    COLONY_WORLD_CONTEXT_QUERY governs how the query hits the store:
      * ``message`` (default): the legacy whole-message FTS call, unchanged.
      * ``entities``: extract proper-noun candidates from the message with the
        shared rule-based extractor and OR their find_entities lookups (<=5
        candidates, limit 2 each, deduped, capped at ``limit``) — precise
        entity matching instead of FTS noise over a whole sentence.
    Empty extraction (or extractor failure) falls back to the whole-message
    call, so entities mode can never return LESS than a degraded message run.
    """
    mode = os.environ.get("COLONY_WORLD_CONTEXT_QUERY", "message").strip().lower()
    if mode == "entities":
        extractor = _get_conversation_extractor()
        if extractor is not None:
            try:
                res = await extractor.extract(query_text, "context-query")
                names: list = []
                seen = set()
                for c in getattr(res, "entities", []):
                    name = (getattr(c, "text", None) or getattr(c, "name", "") or "").strip()
                    key = name.lower()
                    if name and key not in seen:
                        seen.add(key)
                        names.append(name)
                    if len(names) >= 5:
                        break
                if names:
                    out: list = []
                    seen_ids = set()
                    for name in names:
                        try:
                            hits = await _world_store.find_entities(query=name, limit=2)
                        except Exception:
                            logger.debug("world context lookup failed for %r",
                                         name, exc_info=True)
                            continue
                        for e in hits or []:
                            eid = getattr(e, "id", None) or getattr(e, "name", str(e))
                            if eid in seen_ids:
                                continue
                            seen_ids.add(eid)
                            out.append(e)
                            if len(out) >= limit:
                                return out
                    return out
            except Exception:
                logger.debug("world context entity extraction failed; falling "
                             "back to whole-message query", exc_info=True)
    return await _world_store.find_entities(query=query_text, limit=limit)


_response_guard = None


def set_response_guard(guard):
    global _response_guard
    _response_guard = guard


_engagement_store = None


def set_engagement_store(store):
    global _engagement_store
    _engagement_store = store


_comms_log = None


def set_comms_log(store):
    global _comms_log
    _comms_log = store


_relationship_profiler = None


def set_relationship_profiler(profiler):
    global _relationship_profiler
    _relationship_profiler = profiler


@router.get("/comms/recent")
async def comms_recent(limit: int = 50, window_days: int = 30) -> dict:
    """Cross-channel communication ledger: the newest exchanges across every
    contact and channel, plus an inbound/outbound rollup per channel over the
    window. Read-only view over the same ledger that ``/turns/sync`` writes;
    contact ids are resolved to display names for the ops view. System turns
    are recorded in the ledger and returned tagged, not hidden."""
    if _comms_log is None:
        return {"available": False}
    try:
        entries = _comms_log.recent(limit=limit)
        names: dict = {}
        if _contacts_store is not None:
            for cid in {e.get("contact_id") for e in entries if e.get("contact_id")}:
                try:
                    c = await _contacts_store.get(cid)
                    if c is not None:
                        names[cid] = getattr(c, "display_name", None)
                except Exception:
                    pass
        for e in entries:
            e["display_name"] = names.get(e.get("contact_id"))
        return {"available": True, "window_days": int(window_days),
                "entries": entries,
                "by_channel": _comms_log.rollup(since_days=window_days)}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/relationships")
async def list_relationship_briefs() -> dict:
    """Profiled relationships: who Colony has real standing knowledge of."""
    if _relationship_profiler is None:
        return {"available": False}
    try:
        return {"available": True,
                "profiled": _relationship_profiler.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/relationships/{contact_id}")
async def get_relationship_brief(contact_id: str,
                                 refresh: bool = False,
                                 request: Request = None) -> dict:
    """One contact's RelationshipBrief (standing, psyche, approach guidance).
    ``refresh=true`` recomputes from the live stores."""
    if _relationship_profiler is None:
        return {"available": False}
    try:
        resolved = contact_id
        p8_viewer = None
        if _p8_runtime is not None:
            resolved = resolve_request_person(
                request, claimed_person_id=contact_id) or contact_id
            try:
                p8_viewer = _p8_viewer_for_request(request, resolved)
            except HTTPException:
                # Preserve the general relationship endpoint during scoped-auth
                # migration, but omit all P8-derived content without attestation.
                logger.debug(
                    "P8 relationship topics omitted: scoped viewer unavailable")
        if refresh:
            brief = None
        elif _p8_runtime is not None:
            brief = _relationship_profiler.cached(
                resolved, viewer=p8_viewer)
        else:
            brief = _relationship_profiler.cached(resolved)
        if brief is None:
            if _p8_runtime is not None:
                brief = await _relationship_profiler.profile(
                    resolved, viewer=p8_viewer)
            else:
                brief = await _relationship_profiler.profile(resolved)
        if brief is None:
            raise HTTPException(status_code=404,
                                detail=f"no profile for {resolved!r}")
        return {"available": True, "brief": brief.to_dict(),
                "rendered": brief.render()}
    except HTTPException:
        raise
    except Exception as exc:
        return {"available": True, "error": str(exc)}


_preference_learner = None


def set_preference_learner(learner):
    global _preference_learner
    _preference_learner = learner


# --- Directive / boundary memory (owner standing directives + enforcement) ---
_directive_manager = None


def set_directive_manager(manager) -> None:
    global _directive_manager
    _directive_manager = manager


def get_directive_manager():
    return _directive_manager


# --- World-model population from conversation (shadow-first) ---
_world_populator = None


def set_world_populator(populator) -> None:
    global _world_populator
    _world_populator = populator


def get_world_populator():
    return _world_populator


_proposal_store = None


def set_proposal_store(store) -> None:
    global _proposal_store
    _proposal_store = store


_feedback_store = None


def set_feedback_store(store) -> None:
    global _feedback_store
    _feedback_store = store


@router.get("/feedback")
async def get_type_feedback() -> dict:
    """Per-type outcome feedback + priority multipliers (observability)."""
    if _feedback_store is None:
        return {"available": False, "types": []}
    try:
        return {"available": True, "types": _feedback_store.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc), "types": []}


# --- Directed action (option A) + read-only repo mirrors ---
_directed_service = None
_repo_mirrors = None


def set_directed_service(svc) -> None:
    global _directed_service
    _directed_service = svc


def set_repo_mirrors(mgr) -> None:
    global _repo_mirrors
    _repo_mirrors = mgr


@router.post("/directed/tasks")
async def directed_intake(body: dict) -> dict:
    """Owner directive -> gated ScopedTask (boundary check first, then
    approval tiering). Optionally dispatches when already approved.

    body: {directive: str, dispatch?: bool}
    """
    if _directed_service is None:
        return {"ok": False, "reason": "directed_not_wired"}
    directive = (body or {}).get("directive", "").strip()
    if not directive:
        return {"ok": False, "reason": "directive_required"}
    task = await _directed_service.intake(directive)
    out = {"ok": True, "task": task.to_dict()}
    if (body or {}).get("dispatch") and task.status == "approved":
        out["dispatch"] = await _directed_service.dispatch(task.id)
        out["task"] = (_directed_service.store.get(task.id) or task).to_dict()
    return out


@router.get("/directed/tasks")
async def directed_list(status: str = "", limit: int = 30) -> dict:
    if _directed_service is None:
        return {"ok": False, "tasks": []}
    tasks = _directed_service.store.list(status=status or None, limit=limit)
    return {"ok": True, "count": len(tasks), "tasks": [t.to_dict() for t in tasks]}


@router.post("/directed/tasks/{task_id}/approve")
async def directed_approve(
    task_id: str,
    body: dict = Body(default={}),
    request: Request = None,
) -> dict:
    if _directed_service is None:
        return {"ok": False, "reason": "directed_not_wired"}
    from colony_sidecar.api.routers.task_queue import _decision_authority

    actor, _evidence, _mode = _decision_authority(request)
    try:
        grant_ttl = int(
            (body or {}).get("grant_expires_in_seconds", 7 * 24 * 60 * 60)
        )
        grant_uses = int((body or {}).get("grant_max_uses", 5))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid bounded grant limits") from exc
    if not 60 <= grant_ttl <= 30 * 24 * 60 * 60 or not 1 <= grant_uses <= 100:
        raise HTTPException(status_code=422, detail="bounded grant limits are out of range")
    task = _directed_service.approve(
        task_id,
        approved_by=actor,
        standing=bool((body or {}).get("standing")),
        grant_expires_in_seconds=grant_ttl,
        grant_max_uses=grant_uses,
    )
    return {"ok": task is not None, "task": task.to_dict() if task else None}


@router.post("/directed/tasks/{task_id}/dispatch")
async def directed_dispatch(task_id: str) -> dict:
    if _directed_service is None:
        return {"ok": False, "reason": "directed_not_wired"}
    return await _directed_service.dispatch(task_id)


@router.post("/directed/tasks/{task_id}/report")
async def directed_report(task_id: str, body: dict = Body(default={})) -> dict:
    """Delegate report-back: audited against the granted scope."""
    if _directed_service is None:
        return {"ok": False, "reason": "directed_not_wired"}
    return await _directed_service.complete(task_id, body or {})


@router.get("/repos")
async def repos_status() -> dict:
    if _repo_mirrors is None:
        return {"available": False, "repos": {}}
    cfg = _repo_mirrors.configured()
    return {
        "available": True,
        "repos": {name: {"url": info.get("url", ""),
                         "mirrored": _repo_mirrors.path_for(name) is not None}
                  for name, info in cfg.items()},
    }


@router.post("/repos/refresh")
async def repos_refresh() -> dict:
    if _repo_mirrors is None:
        return {"available": False}
    return {"available": True, "results": _repo_mirrors.refresh_all()}


# --- Cognition program (items 1/3/4/7 + Amendment 1) ---
_self_model = None
_skill_store = None
_project_engine = None
_belief_engine = None
_world_llm_extractor = None
_worker_governor = None
_sandbox = None
_connector_manager = None
_adaptive_params = None


def set_self_model(sm) -> None:
    global _self_model
    _self_model = sm


def set_adaptive_params(store) -> None:
    global _adaptive_params
    _adaptive_params = store


def set_skill_store(store) -> None:
    global _skill_store
    _skill_store = store


def set_project_engine(engine) -> None:
    global _project_engine
    _project_engine = engine


def set_belief_engine(engine) -> None:
    global _belief_engine
    _belief_engine = engine


def set_world_llm_extractor(x) -> None:
    global _world_llm_extractor
    _world_llm_extractor = x


def set_worker_governor(g) -> None:
    global _worker_governor
    _worker_governor = g
    # QueueManager is the authority chokepoint. Keep the singleton and the
    # router handle synchronized so HTTP, embedded, mesh, and direct claimers
    # cannot observe different governors.
    queues = []
    queue = getattr(_task_queue, "queue", None)
    if queue is not None:
        queues.append(queue)
    try:
        from colony_sidecar.task_queue.queue_manager import TaskQueueManager
        instance = TaskQueueManager._instance
        singleton_queue = getattr(instance, "queue", None)
        if singleton_queue is not None and singleton_queue not in queues:
            queues.append(singleton_queue)
    except Exception:
        pass
    for queue in queues:
        if hasattr(queue, "configure_governance"):
            queue.configure_governance(g)


def set_sandbox(s) -> None:
    global _sandbox
    _sandbox = s


def set_connector_manager(m) -> None:
    global _connector_manager
    _connector_manager = m


_benchmark = None


def set_benchmark(b) -> None:
    global _benchmark
    _benchmark = b


@router.get("/self/benchmark")
async def get_benchmark(weeks: int = 8) -> dict:
    """Selfhood benchmark: weekly rollups of derived self-improvement
    metrics plus latest-vs-previous trends. Metrics whose sources were
    unavailable in a week are absent for that week, never zero-filled."""
    if _benchmark is None:
        return {"available": False}
    try:
        out = {"available": True}
        out.update(_benchmark.snapshot(weeks=max(1, min(52, weeks))))
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


class BenchmarkSample(BaseModel):
    metric: str
    value: float
    ts: Optional[float] = None
    meta: Optional[Dict[str, Any]] = None
    definition_version: Optional[str] = None
    source_ref: Optional[str] = None
    receipt_ref: Optional[str] = None
    sample_id: Optional[str] = None
    exposure_id: Optional[str] = None
    effect_claim: bool = False


class BenchmarkSamplesRequest(BaseModel):
    samples: List[BenchmarkSample]
    source: str = "host"


@router.post("/self/benchmark/samples")
async def post_benchmark_samples(
    body: BenchmarkSamplesRequest,
    request: Request,
) -> dict:
    """Ingest measured samples from deployment surfaces (e.g. voice TTFB as
    latency.voice_ttfb_ms).

    P4 shadow/live samples are attributed to the authenticated request
    principal and bound to a versioned evidence definition.  The historical
    body ``source`` label survives only in P4-off compatibility mode.
    """
    if _benchmark is None:
        return {"available": False, "accepted": 0}
    from colony_sidecar.self_model.benchmark import cognition_p4_mode

    mode = cognition_p4_mode()
    principal = request_authority(request).principal_id
    accepted = 0
    rejection_reasons: Dict[str, int] = {}
    for s in body.samples[:500]:
        try:
            if mode == "off":
                saved = _benchmark.store.add_sample(
                    s.metric, s.value, source=body.source or "host",
                    ts=s.ts, meta=s.meta)
            else:
                if not (s.definition_version or "").strip():
                    raise ValueError("definition_version is required")
                if not (s.source_ref or "").strip():
                    raise ValueError("source_ref is required")
                if (s.effect_claim or s.exposure_id) and not (
                        s.receipt_ref or "").strip():
                    raise ValueError(
                        "receipt_ref is required for an effect claim")
                saved = _benchmark.store.add_evidence_sample(
                    s.metric,
                    s.value,
                    definition_version=s.definition_version or "",
                    sample_principal=principal,
                    source_ref=s.source_ref or "",
                    receipt_ref=s.receipt_ref,
                    sample_id=s.sample_id,
                    exposure_id=s.exposure_id,
                    ts=s.ts,
                    meta=s.meta,
                )
            if saved:
                accepted += 1
            else:
                rejection_reasons["invalid_sample"] = (
                    rejection_reasons.get("invalid_sample", 0) + 1)
        except ValueError as exc:
            reason = str(exc)[:160] or "invalid_sample"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    return {"available": True, "accepted": accepted,
            "rejected": len(body.samples[:500]) - accepted,
            "rejection_reasons": rejection_reasons}


class RecallProbeRequest(BaseModel):
    probes: int = 50
    seed: Optional[int] = None


@router.post("/self/benchmark/recall-probe")
async def post_benchmark_recall_probe(body: RecallProbeRequest) -> dict:
    """On-demand, deterministic recall probe (the graduation gate for recall
    changes): re-queries a seeded sample of high-confidence shared facts
    against graph recall and grades token coverage. Read-only against the
    graph; probe samples are recorded as source="manual-probe" and are never
    read back into weekly rollups, so probing cannot distort the scorecard."""
    if _benchmark is None:
        return {"available": False}
    try:
        result = await _benchmark.run_recall_probe(
            probes=body.probes, seed=body.seed)
        if result is None:
            return {"available": True, "ran": False,
                    "reason": "graph/facts unavailable or no "
                              "high-confidence facts to probe"}
        return {"available": True, "ran": True, **result}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


_experiments = None


def set_experiments(e) -> None:
    global _experiments
    _experiments = e


_toolsmith = None


def set_toolsmith(t) -> None:
    global _toolsmith
    _toolsmith = t


_workspace = None
_cognition_spine = None
_external_event_intake = None
_cognition_attachment_status = {
    "configured_mode": "off",
    "state": "off",
    "reason": "cognition_not_configured",
    "configured_handler_catalog": [],
    "effective_handler_catalog": [],
}
_situation_store = None
_situation_reducer = None
_cognition_evidence_store = None
_cognition_evidence_reducer = None
_project_event_projector = None
_cognition_evidence_attachment_status = {
    "configured_mode": "off",
    "state": "off",
    "reason": "cognition_evidence_not_configured",
}
_drive_governance = None
_drive_ranker = None
_drive_project_store = None


def set_workspace(w) -> None:
    global _workspace
    _workspace = w


def set_cognition_spine(spine, attachment_status=None) -> None:
    global _cognition_spine, _cognition_attachment_status
    _cognition_spine = spine
    if attachment_status is not None:
        _cognition_attachment_status = dict(attachment_status)
    elif spine is not None:
        # Test/local embedders that attach the already-built spine directly
        # still get truthful, non-stale attachment state.
        _cognition_attachment_status = {
            "configured_mode": "attached",
            "state": "attached",
            "reason": "cognition_spine_attached_directly",
            "configured_handler_catalog": [],
            "effective_handler_catalog": [],
        }


def set_external_event_intake(intake) -> None:
    """Publish or clear the strict Phase C external evidence intake."""

    global _external_event_intake
    _external_event_intake = intake


def set_cognition_attachment_status(status) -> None:
    """Publish truthful configured/effective P3 attachment state."""

    global _cognition_attachment_status
    _cognition_attachment_status = dict(status or {})


def set_situation_spine(store, reducer) -> None:
    """Publish or clear the complete P6 observer graph atomically."""

    global _situation_store, _situation_reducer
    _situation_store = store
    _situation_reducer = reducer


def set_cognition_evidence(
    store, reducer, project_event_projector, attachment_status=None,
) -> None:
    """Publish or clear the complete receipt-derived evidence graph."""

    global _cognition_evidence_store, _cognition_evidence_reducer
    global _project_event_projector, _cognition_evidence_attachment_status
    _cognition_evidence_store = store
    _cognition_evidence_reducer = reducer
    _project_event_projector = project_event_projector
    if attachment_status is not None:
        _cognition_evidence_attachment_status = dict(attachment_status)


def set_drive_governance(governance, ranker, project_store) -> None:
    """Publish or clear the complete P7 graph atomically."""

    global _drive_governance, _drive_ranker, _drive_project_store
    _drive_governance = governance
    _drive_ranker = ranker
    _drive_project_store = project_store


class CognitionConcernPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_material_digest: str = Field(min_length=1, max_length=128)


class ExternalCognitionEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=192)
    kind: str = Field(min_length=1, max_length=64)
    occurred_at: str = Field(min_length=20, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class CognitionGoalPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_thought_result_ref: str = Field(min_length=1, max_length=256)


def _external_owner_goal_objective(event, authority) -> str:
    """Return the exact owner RCS ``Goal:`` objective, otherwise no intent.

    External cognition events remain reported evidence.  This narrow adapter
    promotes only an identity that the server has already authenticated and
    bound to the configured owner, on the RCS text lane, with the explicit
    case-sensitive control prefix.  All other text continues through the
    ordinary observation path.
    """

    owner = _owner_person_id()
    if not (
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("cognition:events-ingest")
        and authority.viewer_person_id == owner
        and owner in authority.person_ids
        and "owner" in authority.audiences
        and event.kind == "text_turn_observation"
        and event.subject_person_id == owner
        and event.viewer_person_id == owner
        and event.viewer_scope == "owner"
        and event.shareability == "owner_private"
        and tuple(event.audience_scope) == ("owner",)
        and event.attributes.get("channel") == "rcs"
    ):
        return ""
    observation = str(event.attributes.get("observation") or "")
    if not observation.startswith("Goal:"):
        return ""
    return " ".join(observation[len("Goal:"):].split()).strip()


def _cognition_owner_authority(request: Request):
    authority = request_authority(request)
    owner = _owner_person_id()
    allowed = bool(
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("cognition:manage")
        and "owner" in authority.audiences
        and authority.viewer_person_id == owner
        and authority.principal_id
        and authority.credential_id
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "owner_authority_required",
                "message": "scoped authenticated owner authority is required",
            },
        )
    return authority


@router.get("/cognition/spine")
async def get_cognition_spine_health(request: Request, limit: int = 100) -> dict:
    """Viewer-filtered P3 mode, worker-independent health, and read trace."""

    if _cognition_spine is None:
        return {
            "available": False,
            "healthy": _cognition_attachment_status.get("state") == "off",
            "attachment": dict(_cognition_attachment_status),
        }
    authority = request_authority(request)
    viewer = authority.viewer_person_id or ""
    if authority.legacy:
        viewer = _owner_person_id()
    if not viewer:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "person_binding_required",
                "message": "the credential has no exact viewer binding",
            },
        )
    snapshot = _cognition_spine.health_snapshot(
        viewer_person_id=viewer,
        owner_person_id=_owner_person_id(),
        audiences=authority.audiences,
        limit=max(1, min(int(limit), 500)),
    )
    snapshot["attachment"] = dict(_cognition_attachment_status)
    snapshot["healthy"] = bool(
        snapshot.get("healthy")
        and _cognition_attachment_status.get("state") == "attached"
    )
    return snapshot


@router.post("/cognition/events")
async def ingest_external_cognition_event(
    body: ExternalCognitionEventRequest,
    request: Request,
) -> dict:
    """Accept bounded text/system evidence under server-derived authority."""

    from colony_sidecar.cognition.external_events import (
        ExternalCognitionEventV1,
        ExternalEventConflict,
        ExternalEventProjectionError,
        ExternalEventValidationError,
    )

    if _external_event_intake is None:
        raise HTTPException(
            status_code=503, detail="external cognition intake is unavailable",
        )
    authority = request_authority(request)
    if (
        not authority.authenticated
        or authority.legacy
        or authority.anonymous
        or not authority.has_scope("cognition:events-ingest")
        or not authority.viewer_person_id
        or not authority.principal_id
        or not authority.credential_id
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "external_event_authority_required",
                "message": (
                    "scoped principal with an exact viewer binding is required"
                ),
            },
        )
    try:
        event = ExternalCognitionEventV1.from_authority(
            body.model_dump(), authority=authority,
        )
        receipt = _external_event_intake.ingest(event)
        owner_goal = _external_owner_goal_objective(event, authority)
        if owner_goal:
            if _project_engine is None:
                raise ExternalEventProjectionError(
                    "owner goal Project engine is unavailable"
                )
            try:
                promotion = await _project_engine.create_owner_goal_work_order(
                    owner_goal,
                    external_event_id=event.event_id,
                    external_event_digest=event.event_digest,
                    intake_receipt=receipt,
                    subject_person_id=event.subject_person_id,
                    viewer_scope=event.viewer_scope,
                    shareability=event.shareability,
                    occurred_at=event.occurred_at,
                )
            except ExternalEventProjectionError:
                raise
            except Exception as exc:
                logger.error(
                    "owner Goal promotion failed (%s)", type(exc).__name__,
                )
                raise ExternalEventProjectionError(
                    "owner goal Project/WorkOrder promotion is retryable"
                ) from exc
            logger.info(
                "Owner Goal promoted: event=%s project=%s work_order=%s status=%s",
                event.event_id,
                promotion.get("project_id"),
                promotion.get("work_order_id"),
                promotion.get("status"),
            )
        return receipt
    except ExternalEventConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExternalEventValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExternalEventProjectionError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "external_event_projection_retryable",
                "message": str(exc),
            },
        ) from exc


@router.post("/cognition/concerns/{concern_id}/promote")
async def promote_cognition_concern(
    concern_id: str,
    body: CognitionConcernPromotionRequest,
    request: Request,
) -> dict:
    """Owner-attest one exact shadow/legacy concern material version.

    Promotion only makes that immutable concern version eligible for P3. It
    does not run cognition, create a project, approve an action, or execute an
    effect; all downstream canonical gates remain in force.
    """

    if _cognition_spine is None:
        return {"available": False, "effect_executed": False}
    authority = _cognition_owner_authority(request)
    concern = _cognition_spine.concern_store.get(concern_id)
    if concern is None:
        raise HTTPException(status_code=404, detail="concern not found")
    authority_payload = {
        "schema": "ConcernPromotionAuthorityV1",
        "version": 1,
        "concern_id": concern_id,
        "material_digest": body.expected_material_digest,
        "principal_id": authority.principal_id,
        "credential_id": authority.credential_id,
    }
    encoded = json.dumps(
        authority_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    promotion_ref = (
        "owner-promotion:" + hashlib.sha256(encoded).hexdigest()[:24]
    )
    try:
        promoted = _cognition_spine.concern_store.promote_concern(
            concern_id,
            expected_material_digest=body.expected_material_digest,
            promotion_ref=promotion_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "available": True,
        "status": "concern_material_promoted",
        "concern": promoted.public(),
        "effect_executed": False,
    }


@router.post("/cognition/goals/{proposal_id}/promote")
async def promote_cognition_goal(
    proposal_id: str,
    body: CognitionGoalPromotionRequest,
    request: Request,
) -> dict:
    """Owner-promote one exact shadow GoalProposal through current gates."""

    if _cognition_spine is None:
        return {"available": False, "effect_executed": False}
    authority = _cognition_owner_authority(request)
    authority_payload = {
        "schema": "GoalPromotionAuthorityV1",
        "version": 1,
        "proposal_id": proposal_id,
        "expected_thought_result_ref": body.expected_thought_result_ref,
        "principal_id": authority.principal_id,
        "credential_id": authority.credential_id,
    }
    encoded = json.dumps(
        authority_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    promotion_ref = (
        "owner-goal-promotion:" + hashlib.sha256(encoded).hexdigest()[:24]
    )
    try:
        result = await _cognition_spine.promote_goal_proposal(
            proposal_id,
            expected_thought_result_ref=body.expected_thought_result_ref,
            promotion_ref=promotion_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"available": True, **result}


_expectations = None


def set_expectations(e) -> None:
    global _expectations
    _expectations = e


def _owner_person_id() -> str:
    return (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )


def _authority_view(request: Request, *, person_id: str = "") -> tuple:
    """Derive one exact viewer/subject lane from middleware authority."""

    authority = request_authority(request)
    if authority.legacy:
        subject = person_id.strip() or _owner_person_id()
        return authority, subject, _owner_person_id(), "owner"
    subject = resolve_request_person(
        request, claimed_person_id=person_id or None,
    )
    if not subject:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "person_binding_required",
                "message": "the credential has no exact viewer binding",
            },
        )
    viewer = authority.viewer_person_id or subject
    viewer_scope = (
        "owner"
        if "owner" in authority.audiences and viewer == _owner_person_id()
        else f"person:{viewer}"
    )
    return authority, subject, viewer, viewer_scope


def _scope_from_authority(request: Request):
    """Build a P7 scope without accepting person/scope fields from JSON."""

    from colony_sidecar.cognition.drive_governance import ScopeV1

    authority, subject, viewer, viewer_scope = _authority_view(request)
    if viewer_scope == "owner":
        return authority, ScopeV1(subject, "owner", "owner_private")
    return authority, ScopeV1(
        subject, f"person:{subject}", "subject_private",
    )


def _governance_error(exc, *, not_found: bool = False) -> HTTPException:
    code = str(getattr(exc, "code", "drive_governance_error"))
    message = str(getattr(exc, "message", str(exc)))[:500]
    if not_found or code.endswith("_unknown") or code.endswith("_missing"):
        status_code = 404
    elif code in {
        "operation_replay_conflict", "immutable_drive_conflict",
        "approval_binding_mismatch", "authority_replay",
        "transition_not_live", "transition_not_authoritative",
        "bootstrap_operation_held", "bootstrap_transition_held",
        "approval_not_approved", "approval_expired",
        "approval_binding_stale", "stale_action_digest",
        "stale_request_digest",
    }:
        status_code = 409
    elif code in {
        "owner_authority_required",
        "owner_charter_approval_authority_required",
    }:
        status_code = 403
    else:
        status_code = 400
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


@router.get("/self/situation")
async def get_situation(request: Request, person_id: str = "") -> dict:
    """Return only the snapshot lane granted by the request credential."""

    if _situation_store is None or _situation_reducer is None:
        return {"available": False}
    _authority, subject, _viewer, viewer_scope = _authority_view(
        request, person_id=person_id,
    )
    try:
        snapshot = _situation_store.snapshot(
            subject_person_id=subject,
            viewer_scope=viewer_scope,
        )
        return {
            "available": True,
            "status": _situation_reducer.status(),
            "snapshot": snapshot.public(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cognition/evidence")
async def get_cognition_evidence(
    request: Request,
    person_id: str = "",
    project_id: str = "",
    limit: int = 100,
) -> dict:
    """Return a request-scoped receipt/learning trace for operator diagnosis."""

    _authority, subject, _viewer, viewer_scope = _authority_view(
        request, person_id=person_id,
    )
    status = dict(_cognition_evidence_attachment_status)
    if _cognition_evidence_reducer is not None:
        try:
            status = _cognition_evidence_reducer.status()
            status["attachment"] = dict(
                _cognition_evidence_attachment_status
            )
        except Exception as exc:
            status = {
                **status,
                "healthy": False,
                "last_error": f"status_failed:{type(exc).__name__}",
            }
    elif _project_event_projector is not None:
        status["project_outbox"] = _project_event_projector.status()
    trace = []
    trace_error = ""
    if _cognition_evidence_store is not None:
        try:
            trace = _cognition_evidence_store.trace(
                project_id=str(project_id or "").strip(),
                subject_person_id=(
                    "" if viewer_scope == "owner" and not person_id else subject
                ),
                viewer_scope=viewer_scope,
                limit=max(1, min(int(limit), 500)),
            )
        except ValueError as exc:
            # Never return an unverifiable row as operator truth.
            trace_error = str(exc)[:500]
            status["healthy"] = False
            status["last_error"] = "evidence_ledger_integrity_failed"
    return {
        "available": _project_event_projector is not None,
        "learning_available": (
            _cognition_evidence_reducer is not None
            and getattr(_cognition_evidence_reducer, "mode", "shadow") != "off"
        ),
        "status": status,
        "trace": trace,
        "trace_error": trace_error,
    }


class _StrictGovernanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DriveProposalRequest(_StrictGovernanceBody):
    operation_id: str
    key: str
    version: str
    title: str
    definition_summary: str
    max_abs_contribution: float
    max_signals_per_goal: int
    state: str = "enabled"
    evidence_refs: List[str] = Field(min_length=1, max_length=20)
    ttl_seconds: Optional[int] = Field(
        default=None, ge=60, le=366 * 24 * 60 * 60,
    )


class DriveSignalRequest(_StrictGovernanceBody):
    operation_id: str
    drive_id: str
    project_id: str
    normalized_value: float
    confidence: float
    state: str = "active"
    rationale_summary: str
    evidence_refs: List[str] = Field(min_length=1, max_length=20)
    ttl_seconds: int = Field(default=21600, ge=60, le=90 * 24 * 60 * 60)


class RankingBudgetRequest(_StrictGovernanceBody):
    max_goals: int = 50
    max_signals_per_drive: int = 5
    max_total_signals: int = 250
    max_evidence_refs_per_goal: int = 20


class CharterAdmissionConstraintsRequest(_StrictGovernanceBody):
    objective_allow_terms: List[str] = Field(default_factory=list, max_length=30)
    objective_deny_terms: List[str] = Field(
        default_factory=lambda: [
            "destroy", "drop", "format", "overwrite", "wipe",
        ],
        max_length=30,
    )
    capability_ceiling: List[str] = Field(
        default_factory=lambda: [
            "concerns:read", "directives:read", "memory:read",
            "projects:read", "reasoning", "situation:read", "web:read",
            "world_model:read",
        ],
        max_length=30,
    )
    capability_deny: List[str] = Field(
        default_factory=lambda: ["messaging:send", "root:shell"],
        max_length=30,
    )
    required_boundary_refs: List[str] = Field(default_factory=list, max_length=30)
    allowed_shareability: List[str] = Field(
        default_factory=lambda: ["owner_private"], min_length=1, max_length=4,
    )
    allowed_recipient_ids: List[str] = Field(default_factory=list, max_length=30)
    allow_destructive: bool = False
    allow_root_shell: bool = False
    allow_messaging: bool = False


class CharterProposalRequest(_StrictGovernanceBody):
    operation_id: str
    charter_key: str = "default"
    revision_label: str
    parent_revision_id: Optional[str] = None
    title: str
    purpose_summary: str
    principles: List[str] = Field(min_length=1, max_length=20)
    drive_weights: Dict[str, float] = Field(min_length=1, max_length=20)
    ranking_budget: RankingBudgetRequest = Field(
        default_factory=RankingBudgetRequest
    )
    evidence_refs: List[str] = Field(min_length=1, max_length=30)
    ttl_seconds: int = Field(
        default=90 * 24 * 60 * 60,
        ge=3600,
        le=366 * 24 * 60 * 60,
    )
    admission_constraints: CharterAdmissionConstraintsRequest = Field(
        default_factory=CharterAdmissionConstraintsRequest,
    )


class CharterTransitionRequest(_StrictGovernanceBody):
    ttl_seconds: int = Field(default=3600, ge=60, le=24 * 60 * 60)


class CharterRatifyRequest(_StrictGovernanceBody):
    transition: str
    approval_request_id: str
    operation_id: str


class CharterApprovalDecisionRequest(_StrictGovernanceBody):
    decision: str
    decision_id: str
    expected_action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _observer_identity(request: Request) -> tuple:
    authority, _subject, viewer, _viewer_scope = _authority_view(request)
    return authority, viewer, _owner_person_id()


def _owner_charter_approval_authority(request: Request, scope: str):
    """Require one exact scoped owner principal for the typed surface."""

    authority = request_authority(request)
    allowed = bool(
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("api:access")
        and authority.has_scope(scope)
        and "owner" in authority.audiences
        and authority.viewer_person_id == _owner_person_id()
        and _owner_person_id() in authority.person_ids
    )
    if not allowed:
        raise HTTPException(status_code=403, detail={
            "code": "owner_charter_approval_authority_required",
            "message": (
                "an exact scoped owner charter approval principal is required"
            ),
            "required_scopes": ["api:access", scope],
        })
    return authority


def _scope_visible_to_request(scope, request: Request) -> bool:
    authority, viewer, owner = _observer_identity(request)
    return bool(scope.visible_to(
        viewer_person_id=viewer,
        owner_person_id=owner,
        audiences=set(authority.audiences),
    ))


def _durable_p3_rank_input(project):
    """Build one rank input from a complete persisted P3 project or None."""
    from colony_sidecar.cognition.drive_governance import (
        GoalRankInputV1,
        ScopeV1,
    )

    if (
        project is None
        or project.source != "cognition_spine"
        or project.status not in {"planning", "active"}
        or not all((
            project.id,
            project.goal_proposal_id,
            project.goal_fingerprint,
            project.title,
            project.objective,
            project.evidence_refs,
            project.policy_decision_refs,
            project.subject_person_id,
            project.viewer_scope,
            project.shareability,
        ))
    ):
        return None
    try:
        return GoalRankInputV1(
            goal_id=project.id,
            proposal_id=project.goal_proposal_id,
            goal_fingerprint=project.goal_fingerprint,
            title=project.title[:160],
            objective_summary=project.objective[:600],
            rationale_summary=(
                "Durable P3 project admitted through persisted policy gates."
            ),
            evidence_refs=tuple(project.evidence_refs[:30]),
            policy_decision_refs=tuple(project.policy_decision_refs[:20]),
            scope=ScopeV1(
                project.subject_person_id,
                project.viewer_scope,
                project.shareability,
            ),
        )
    except (TypeError, ValueError):
        logger.warning(
            "Skipping malformed durable P3 project %s during P7 ranking",
            getattr(project, "id", "<unknown>"),
        )
        return None


def _durable_p3_rank_inputs(limit: int):
    """Project immutable rank inputs solely from persisted P3 projects."""

    if _drive_project_store is None:
        return ()
    bounded = max(1, min(200, int(limit)))
    result = []
    # Pull extra rows because legacy/malformed entries are intentionally
    # filtered and must not crowd durable P3 candidates out of the bound.
    for project in _drive_project_store.list_projects(limit=min(1000, bounded * 5)):
        if len(result) >= bounded:
            break
        goal = _durable_p3_rank_input(project)
        if goal is not None:
            result.append(goal)
    return tuple(result)


@router.get("/cognition/drives")
async def get_cognition_drives(request: Request, limit: int = 100) -> dict:
    if _drive_governance is None:
        return {"available": False, "drives": [], "signals": []}
    authority, viewer, owner = _observer_identity(request)
    projection = _drive_governance.store.observer_projection(
        viewer_person_id=viewer,
        owner_person_id=owner,
        audiences=set(authority.audiences),
        signal_limit=max(1, min(500, limit)),
    )
    return {
        "available": True,
        "mode": _drive_governance.mode,
        "drives": projection["drives"],
        "signals": projection["signals"],
        "generated_at": projection["generated_at"],
    }


@router.get("/cognition/charters")
async def get_cognition_charters(request: Request) -> dict:
    if _drive_governance is None:
        return {"available": False, "charters": []}
    authority, viewer, owner = _observer_identity(request)
    projection = _drive_governance.store.observer_projection(
        viewer_person_id=viewer,
        owner_person_id=owner,
        audiences=set(authority.audiences),
    )
    return {
        "available": True,
        "mode": _drive_governance.mode,
        "active_charter_revision_id": projection[
            "active_charter_revision_id"
        ],
        "charters": projection["charter_revisions"],
        "generated_at": projection["generated_at"],
    }


@router.get("/cognition/rankings")
async def get_cognition_rankings(request: Request, limit: int = 50) -> dict:
    if _drive_governance is None or _drive_ranker is None:
        return {"available": False, "ranking": None}
    authority, viewer, owner = _observer_identity(request)
    goals = _durable_p3_rank_inputs(limit)
    batch = _drive_ranker.rank(goals, mode=_drive_governance.mode)
    return {
        "available": True,
        "mode": _drive_governance.mode,
        "project_count": len(goals),
        "ranking": batch.observer_projection(
            viewer_person_id=viewer,
            owner_person_id=owner,
            audiences=set(authority.audiences),
        ),
    }


@router.post("/cognition/drives")
async def propose_cognition_drive(
    body: DriveProposalRequest,
    request: Request,
) -> dict:
    if _drive_governance is None:
        return {"available": False}
    from colony_sidecar.cognition.drive_governance import DriveV1

    _authority, scope = _scope_from_authority(request)
    now = datetime.now(timezone.utc)
    expires = (
        now + timedelta(seconds=body.ttl_seconds)
        if body.ttl_seconds is not None else None
    )
    try:
        drive = DriveV1.create(
            key=body.key,
            version=body.version,
            title=body.title,
            definition_summary=body.definition_summary,
            max_abs_contribution=body.max_abs_contribution,
            max_signals_per_goal=body.max_signals_per_goal,
            state=body.state,
            scope=scope,
            evidence_refs=body.evidence_refs,
            created_at=now,
            expires_at=expires,
        )
        result = _drive_governance.register_drive(
            drive, operation_id=body.operation_id,
        )
        return {"available": True, **result, "drive": drive.payload()}
    except ValueError as exc:
        raise _governance_error(exc) from exc


@router.post("/cognition/drive-signals")
async def propose_cognition_drive_signal(
    body: DriveSignalRequest,
    request: Request,
) -> dict:
    if _drive_governance is None or _drive_project_store is None:
        return {"available": False}
    from colony_sidecar.cognition.drive_governance import DriveSignalV1

    project = _drive_project_store.get_project(body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={
            "code": "project_unavailable",
            "message": "a visible complete durable P3 project is required",
        })
    # Reuse the exact durable projection check; signals cannot target a body
    # fingerprint or a legacy/partially-provenanced project.
    goal = _durable_p3_rank_input(project)
    if goal is None or not _scope_visible_to_request(goal.scope, request):
        # Do not distinguish a hidden project from an unknown one.
        raise HTTPException(status_code=404, detail={
            "code": "project_unavailable",
            "message": "a visible complete durable P3 project is required",
        })
    drive = _drive_governance.store.get_drive(body.drive_id)
    if drive is None or not _scope_visible_to_request(drive.scope, request):
        raise HTTPException(status_code=404, detail={
            "code": "drive_unavailable",
            "message": "a visible drive was not found",
        })
    _authority, scope = _scope_from_authority(request)
    now = datetime.now(timezone.utc)
    try:
        signal = DriveSignalV1.derive(
            drive=drive,
            goal_fingerprint=goal.goal_fingerprint,
            normalized_value=body.normalized_value,
            confidence=body.confidence,
            state=body.state,
            rationale_summary=body.rationale_summary,
            evidence_refs=body.evidence_refs,
            observed_at=now,
            expires_at=now + timedelta(seconds=body.ttl_seconds),
            scope=scope,
        )
        result = _drive_governance.record_signal(
            signal, operation_id=body.operation_id,
        )
        return {"available": True, **result, "signal": signal.payload()}
    except ValueError as exc:
        raise _governance_error(exc) from exc


@router.post("/cognition/charters")
async def propose_cognition_charter(
    body: CharterProposalRequest,
    request: Request,
) -> dict:
    if _drive_governance is None:
        return {"available": False}
    from colony_sidecar.cognition.drive_governance import (
        CharterAdmissionConstraintsV1,
        CharterRevisionV1,
        RankingBudgetV1,
    )

    authority, scope = _scope_from_authority(request)
    now = datetime.now(timezone.utc)
    try:
        for drive_id in body.drive_weights:
            drive = _drive_governance.store.get_drive(drive_id)
            if drive is None or not _scope_visible_to_request(
                drive.scope, request,
            ):
                raise HTTPException(status_code=404, detail={
                    "code": "drive_unavailable",
                    "message": "every proposed drive must be visible",
                })
        revision = CharterRevisionV1.create(
            charter_key=body.charter_key,
            revision_label=body.revision_label,
            parent_revision_id=body.parent_revision_id,
            title=body.title,
            purpose_summary=body.purpose_summary,
            principles=body.principles,
            drive_weights=body.drive_weights,
            ranking_budget=RankingBudgetV1(
                **body.ranking_budget.model_dump()
            ),
            scope=scope,
            evidence_refs=body.evidence_refs,
            proposed_by=authority.principal_id,
            proposed_at=now,
            expires_at=now + timedelta(seconds=body.ttl_seconds),
            admission_constraints=CharterAdmissionConstraintsV1(
                objective_allow_terms=tuple(sorted({
                    str(item).strip().casefold()
                    for item in body.admission_constraints.objective_allow_terms
                    if str(item).strip()
                })),
                objective_deny_terms=tuple(sorted({
                    str(item).strip().casefold()
                    for item in body.admission_constraints.objective_deny_terms
                    if str(item).strip()
                })),
                capability_ceiling=tuple(sorted(set(
                    body.admission_constraints.capability_ceiling
                ))),
                capability_deny=tuple(sorted(set(
                    body.admission_constraints.capability_deny
                ))),
                required_boundary_refs=tuple(dict.fromkeys(
                    body.admission_constraints.required_boundary_refs
                )),
                allowed_shareability=tuple(sorted(set(
                    body.admission_constraints.allowed_shareability
                ))),
                allowed_recipient_ids=tuple(sorted(set(
                    body.admission_constraints.allowed_recipient_ids
                ))),
                allow_destructive=body.admission_constraints.allow_destructive,
                allow_root_shell=body.admission_constraints.allow_root_shell,
                allow_messaging=body.admission_constraints.allow_messaging,
            ),
        )
        result = _drive_governance.propose_charter(
            revision, operation_id=body.operation_id,
        )
        return {"available": True, **result, "charter": revision.payload()}
    except ValueError as exc:
        raise _governance_error(exc) from exc


async def _request_charter_transition(
    revision_id: str,
    transition: str,
    body: CharterTransitionRequest,
    request: Request,
) -> dict:
    if _drive_governance is None:
        return {"available": False}
    authority, viewer, owner = _observer_identity(request)
    projection = _drive_governance.store.observer_projection(
        viewer_person_id=viewer,
        owner_person_id=owner,
        audiences=set(authority.audiences),
    )
    visible_ids = {
        item.get("revision_id")
        for item in projection.get("charter_revisions", ())
    }
    if revision_id not in visible_ids:
        raise HTTPException(status_code=404, detail={
            "code": "charter_unavailable",
            "message": "a visible charter revision was not found",
        })
    try:
        result = _drive_governance.ensure_transition_request(
            revision_id,
            transition=transition,
            ttl_seconds=body.ttl_seconds,
        )
        return {"available": True, **result}
    except ValueError as exc:
        raise _governance_error(exc) from exc


@router.post("/cognition/charters/{revision_id}/request-activation")
async def request_cognition_charter_activation(
    revision_id: str,
    body: CharterTransitionRequest,
    request: Request,
) -> dict:
    return await _request_charter_transition(
        revision_id, "activate", body, request,
    )


@router.post("/cognition/charters/{revision_id}/request-revocation")
async def request_cognition_charter_revocation(
    revision_id: str,
    body: CharterTransitionRequest,
    request: Request,
) -> dict:
    return await _request_charter_transition(
        revision_id, "revoke", body, request,
    )


@router.get("/cognition/charter-transition-approvals/readiness")
async def charter_transition_approval_readiness(request: Request) -> dict:
    _owner_charter_approval_authority(request, "charter:approval-read")
    if _drive_governance is None:
        return {
            "schema": "ColonyCharterApprovalReadinessV1",
            "version": 1,
            "available": False,
            "ready": False,
            "route_ready": False,
            "status": "unavailable",
            "mode": "off",
            "authority_mode": "unavailable",
            "pending_count": 0,
            "approved_unapplied_count": 0,
            "approved_stale_count": 0,
            "stale_count": 0,
            "invalid_hidden_count": 0,
            "blockers": ["drive_governance_unavailable"],
        }
    from colony_sidecar.initiatives.approval_authority import authority_mode

    try:
        inventory = _drive_governance.transition_approval_inventory()
    except ValueError as exc:
        raise _governance_error(exc) from exc
    projections = inventory["requests"]
    selected_authority_mode = authority_mode()
    blockers = []
    if _drive_governance.mode not in {"bootstrap", "live"}:
        blockers.append("drive_governance_not_authoritative")
    if selected_authority_mode == "invalid":
        blockers.append("approval_authority_mode_invalid")
    if _drive_governance.approval_store is None:
        blockers.append("approval_authority_store_unavailable")
    approved_unapplied = sum(
        item["status"] == "approved_unapplied" for item in projections
    )
    approved_stale = sum(
        item["status"] == "approved_stale" for item in projections
    )
    stale = sum(item["status"] == "stale_pending" for item in projections)
    if approved_unapplied:
        blockers.append("approved_transition_recovery_required")
    if stale:
        blockers.append("stale_transition_decision_required")
    invalid_hidden = int(inventory["invalid_hidden_count"])
    if invalid_hidden:
        blockers.append("invalid_hidden_transition_approval")
    route_ready = bool(
        _drive_governance.mode in {"bootstrap", "live"}
        and selected_authority_mode in {"shadow", "enforce"}
        and _drive_governance.approval_store is not None
    )
    return {
        "schema": "ColonyCharterApprovalReadinessV1",
        "version": 1,
        "available": True,
        "ready": not blockers,
        "route_ready": route_ready,
        "status": "ready" if not blockers else "blocked",
        "mode": _drive_governance.mode,
        "authority_mode": selected_authority_mode,
        "pending_count": sum(
            item["status"] == "pending" for item in projections
        ),
        "approved_unapplied_count": approved_unapplied,
        "approved_stale_count": approved_stale,
        "stale_count": stale,
        "invalid_hidden_count": invalid_hidden,
        "request_count": len(projections),
        "blockers": blockers,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/cognition/charter-transition-approvals")
async def list_charter_transition_approvals(
    request: Request,
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    _owner_charter_approval_authority(request, "charter:approval-read")
    if _drive_governance is None:
        return {"available": False, "requests": []}
    try:
        inventory = _drive_governance.transition_approval_inventory(
            status=status, limit=limit,
        )
    except ValueError as exc:
        raise _governance_error(exc) from exc
    projections = inventory["requests"]
    return {
        "available": True,
        "mode": _drive_governance.mode,
        "requests": projections,
        "count": len(projections),
        "complete": inventory["complete"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/cognition/charter-transition-approvals/{request_id}")
async def get_charter_transition_approval(
    request_id: str,
    request: Request,
) -> dict:
    _owner_charter_approval_authority(request, "charter:approval-read")
    if _drive_governance is None:
        raise HTTPException(status_code=404, detail={
            "code": "charter_transition_approval_unavailable",
            "message": "charter transition approval was not found",
        })
    try:
        projection = _drive_governance.transition_approval_projection(
            request_id,
        )
    except ValueError as exc:
        raise _governance_error(exc, not_found=True) from exc
    return {"available": True, **projection}


@router.post(
    "/cognition/charter-transition-approvals/{request_id}/decision"
)
async def decide_charter_transition_approval(
    request_id: str,
    body: CharterApprovalDecisionRequest,
    request: Request,
) -> dict:
    authority = _owner_charter_approval_authority(
        request, "charter:approval-decide",
    )
    if _drive_governance is None:
        raise HTTPException(status_code=404, detail={
            "code": "charter_transition_approval_unavailable",
            "message": "charter transition approval was not found",
        })
    try:
        projection = _drive_governance.decide_transition_request(
            request_id,
            decision=body.decision,
            decision_id=body.decision_id,
            expected_action_digest=body.expected_action_digest,
            expected_request_digest=body.expected_request_digest,
            authority=authority,
        )
    except ValueError as exc:
        raise _governance_error(exc) from exc
    return {"available": True, **projection}


@router.post("/cognition/charters/{revision_id}/ratify")
async def ratify_cognition_charter(
    revision_id: str,
    body: CharterRatifyRequest,
    request: Request,
) -> dict:
    if _drive_governance is None:
        return {"available": False}
    authority = request_authority(request)
    if not (
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("approvals:decide")
        and "owner" in authority.audiences
    ):
        raise HTTPException(status_code=403, detail={
            "code": "owner_authority_required",
            "message": (
                "ratification requires scoped owner approvals:decide authority"
            ),
        })
    try:
        result = _drive_governance.ratify_transition(
            revision_id,
            transition=body.transition,
            approval_request_id=body.approval_request_id,
            operation_id=body.operation_id,
            authority=authority,
        )
        return {"available": True, **result}
    except ValueError as exc:
        raise _governance_error(exc) from exc


@router.get("/self/expectations")
async def get_expectations(limit: int = 50) -> dict:
    """Expectation engine: pending predictions with their horizons and
    per-domain calibration (Mind M3a). A prediction that missed became a
    surprise on her mind."""
    if _expectations is None:
        return {"available": False}
    try:
        out = {"available": True}
        out.update(_expectations.snapshot(limit=max(1, min(200, limit))))
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/self/workspace")
async def get_workspace(request: Request, limit: int = 24) -> dict:
    """Cognitive workspace: the concerns currently on her mind, most salient
    first, with each concern's last thought and how much thinking it has had.
    Real concerns only (Mind M2)."""
    if _workspace is None:
        return {"available": False}
    try:
        authority = request_authority(request)
        owner_person_id = (
            os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
            or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
            or "owner"
        )
        out = {"available": True}
        out.update(_workspace.snapshot(
            limit=max(1, min(200, limit)),
            unrestricted=authority.legacy,
            viewer_person_id=authority.viewer_person_id or "",
            owner_person_id=owner_person_id,
            audiences=authority.audiences,
        ))
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


def _owner_workspace_concern(concern_id: str, request: Request):
    """Load one concern without disclosing it outside the owner lane."""

    concern = _workspace.store.get(concern_id)
    if concern is None:
        raise HTTPException(status_code=404, detail="no concern with that id")
    authority = request_authority(request)
    if authority.legacy:
        return concern
    owner_person_id = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    if authority.viewer_person_id != owner_person_id or not concern.visible_to(
        viewer_person_id=authority.viewer_person_id or "",
        owner_person_id=owner_person_id,
        audiences=authority.audiences,
    ):
        raise HTTPException(status_code=404, detail="no concern with that id")
    return concern


@router.get("/self/workspace/{concern_id}/resolution")
async def get_concern_resolution(concern_id: str, request: Request) -> dict:
    """Return the immutable owner-visible receipt for one terminal concern."""

    if _workspace is None:
        return {"available": False}
    _owner_workspace_concern(concern_id, request)
    receipt = _workspace.store.get_resolution(concern_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail={
            "code": "concern_resolution_not_found",
            "message": "no immutable resolution receipt exists for that concern",
        })
    return {
        "available": True,
        "resolved": concern_id,
        "outcome": receipt["outcome"],
        "cascade_status": receipt["cascade_evidence"]["status"],
        "resolution": receipt,
    }


@router.post("/self/workspace/{concern_id}/resolve")
async def resolve_concern(
    concern_id: str,
    request: Request,
    body: Optional[ConcernResolveRequest] = None,
    note: str = "resolved by owner",
) -> dict:
    """Settle a concern so it leaves her mind — and, with cascade (default),
    settle the sources it was raised from. Without the cascade, an ingest
    loop re-raises the concern from the still-open source on the next tick
    and the resolve is silently undone. Accepts a JSON body (note, outcome,
    cascade, resolved_by); the bare `note` query param remains for old
    callers. A terminal exact replay performs no callback. An exact replay of
    a durable pending cascade may recover only through source settlers that
    prove the same operation-bound transition; unsafe recovery is a 409. A
    different terminal claim conflicts with the first immutable receipt."""
    if _workspace is None:
        return {"available": False}
    req = body or ConcernResolveRequest(note=note)
    c = _owner_workspace_concern(concern_id, request)
    if c.status not in ("active", "resolved"):
        raise HTTPException(status_code=404, detail="no active concern with that id")
    from colony_sidecar.self_model.workspace import ConcernResolutionConflict
    try:
        receipt, created = _workspace.store.resolve_with_owner_record(
            concern_id,
            outcome=req.outcome,
            note=req.note,
            cascade=req.cascade,
            resolved_by=req.resolved_by,
        )
    except ConcernResolutionConflict as exc:
        existing = _workspace.store.get_resolution(concern_id)
        code = "concern_resolution_replay_conflict"
        if existing and existing.get("provenance") == "legacy_unrecorded":
            code = "legacy_concern_resolution_conflict"
        raise HTTPException(status_code=409, detail={
            "code": code,
            "message": str(exc),
            "resolution": existing,
        }) from exc
    already_resolved = not created
    settled: list = []
    recovery_attempted = False
    cascade_state = receipt["cascade_evidence"]
    if cascade_state["status"] == "pending":
        cascade_error = None
        if created:
            try:
                from colony_sidecar.self_model.settlement import settle_sources
                settled = settle_sources(
                    cascade_state["source_refs"],
                    outcome=receipt["outcome"],
                    note=receipt["note"],
                    resolved_by=receipt["resolved_by"],
                    operation_root=cascade_state["intent_id"],
                )
            except Exception as exc:
                logger.warning(
                    "concern source settlement failed (%s)", type(exc).__name__,
                )
                cascade_error = exc
        else:
            recovery_attempted = True
            from colony_sidecar.self_model.settlement import (
                SettlementRetryUnsafe,
                retry_safe_settle_sources,
            )
            try:
                settled = retry_safe_settle_sources(
                    cascade_state["source_refs"],
                    operation_root=cascade_state["intent_id"],
                    outcome=receipt["outcome"],
                    note=receipt["note"],
                    resolved_by=receipt["resolved_by"],
                )
            except SettlementRetryUnsafe as exc:
                raise HTTPException(status_code=409, detail={
                    "code": "concern_cascade_reconciliation_required",
                    "message": "one or more sources do not support safe recovery",
                    "unsafe_sources": exc.sources,
                    "resolution": receipt,
                }) from exc
            except Exception as exc:
                logger.warning(
                    "concern cascade recovery failed (%s)", type(exc).__name__,
                )
                cascade_error = exc
            recovery_proved = (
                cascade_error is None
                and len(settled) == len(cascade_state["source_refs"])
                and [entry.get("source") for entry in settled]
                == cascade_state["source_refs"]
                and all(
                    entry.get("settled") is True and not entry.get("error")
                    for entry in settled
                )
            )
            if not recovery_proved:
                raise HTTPException(status_code=409, detail={
                    "code": "concern_cascade_reconciliation_required",
                    "message": "cascade recovery did not prove every source operation",
                    "settled_sources": settled,
                    "resolution": receipt,
                })
        try:
            receipt = _workspace.store.finalize_owner_cascade(
                concern_id, results=settled, error=cascade_error,
            )
            cascade_state = receipt["cascade_evidence"]
        except Exception as exc:
            logger.error(
                "concern cascade outcome persistence failed",
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail={
                "code": "concern_cascade_evidence_unavailable",
                "message": "cascade outcome could not be recorded",
            }) from exc
        if recovery_attempted and cascade_state["status"] != "succeeded":
            raise HTTPException(status_code=409, detail={
                "code": "concern_cascade_reconciliation_required",
                "message": "cascade recovery did not prove every source operation",
                "settled_sources": settled,
                "resolution": receipt,
            })
    journal = getattr(_workspace, "_journal", None)
    if journal is not None and created:
        try:
            journal.record(
                "workspace",
                f"concern resolved by {req.resolved_by} ({req.outcome}): "
                f"{c.summary[:80]}",
                reasoning=req.note[:300], decision="resolved",
                outcome=req.outcome)
        except Exception:
            logger.debug("concern resolve journal write failed", exc_info=True)
    return {"available": True, "resolved": concern_id,
            "outcome": receipt["outcome"],
            "already_resolved": already_resolved,
            "cascade_status": cascade_state["status"],
            "recovery_attempted": recovery_attempted,
            "settled_sources": settled, "resolution": receipt}


@router.get("/self/tools")
async def list_tools(status: str = "") -> dict:
    """Self-built tools: the toolsmith registry (draft/verified/shadow/live/
    retired/rejected), each with usage and verification detail."""
    if _toolsmith is None:
        return {"available": False}
    try:
        tools = _toolsmith.registry.list(status=status or None)
        projected = []
        for tool in tools:
            item = tool.public()
            audit = _toolsmith.registry.audit_projection(tool.tool_id)
            item["clean_comparison_receipts"] = sum(
                1 for row in audit["shadow_comparisons"] if row.get("success"))
            item["graduation_receipts"] = len(audit["graduations"])
            projected.append(item)
        return {"available": True, "mode": os.environ.get("COLONY_TOOLSMITH", "off"),
                "trust_stage": _toolsmith.trust_stage(),
                "tools": projected}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/self/tools/{tool_id}")
async def get_tool(tool_id: str) -> dict:
    if _toolsmith is None:
        return {"available": False}
    tool = _toolsmith.registry.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    d = tool.public()
    d["source_code"] = tool.source_code
    d["test_source"] = tool.test_source
    audit = _toolsmith.registry.audit_projection(tool_id)
    clean_comparisons = _toolsmith.registry.clean_comparison_count(tool_id)
    try:
        from colony_sidecar.toolsmith.engine import _shadow_min
        shadow_min = _shadow_min()
    except Exception:
        shadow_min = 5
    return {
        "available": True,
        "tool": d,
        "graduation_binding": {
            "tool_id": tool.tool_id,
            "candidate_digest": tool.candidate_digest,
            "artifact_digest": tool.artifact_digest,
            "clean_comparisons": clean_comparisons,
            "required_clean_comparisons": shadow_min,
            "eligible": (
                tool.status == "shadow"
                and clean_comparisons >= shadow_min
                and tool.failures == 0
            ),
        },
        "audit": audit,
    }


class ToolShadowComparisonRequest(BaseModel):
    capture_id: str
    captured_input: Dict[str, Any]
    incumbent_output: Any = None
    capture_source: str = "captured"


class ToolGraduationAuthorityRequest(BaseModel):
    authority_id: str
    decision_id: str
    expected_candidate_digest: str
    expected_artifact_digest: str
    issued_at: str
    expires_at: str
    max_uses: int = 1


def _toolsmith_scoped_authority(
    request: Request,
    *,
    scope: str,
    owner_required: bool = False,
) -> tuple[Any, str]:
    authority = request_authority(request)
    if (
        not authority.authenticated
        or authority.legacy
        or authority.anonymous
        or not authority.has_scope(scope)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "toolsmith_scope_required",
                "message": f"a scoped authenticated {scope} principal is required",
            },
        )
    owner_person_id = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    if owner_required and (
        "owner" not in authority.audiences
        or owner_person_id not in authority.person_ids
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "toolsmith_owner_authority_required",
                "message": "graduation requires an owner-bound scoped principal",
            },
        )
    return authority, owner_person_id


@router.post("/self/tools/{tool_id}/shadow-compare")
async def compare_shadow_tool(
    tool_id: str,
    body: ToolShadowComparisonRequest,
    request: Request,
) -> dict:
    """Record a digest-only same-input incumbent/candidate comparison."""

    if _toolsmith is None:
        return {"available": False}
    authority, _ = _toolsmith_scoped_authority(
        request, scope="toolsmith:evaluate")
    tool = _toolsmith.registry.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    try:
        passed, evidence = await _toolsmith.verify_shadow_run(
            tool,
            captured_input=body.captured_input,
            incumbent_output=body.incumbent_output,
            capture_id=body.capture_id,
            capture_source=body.capture_source,
            principal_id=authority.principal_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "shadow_comparison_conflict", "message": str(exc)},
        ) from exc
    return {"available": True, "passed": passed, "evidence": evidence}


@router.post("/self/tools/{tool_id}/graduate")
async def graduate_tool(
    tool_id: str,
    body: ToolGraduationAuthorityRequest,
    request: Request,
) -> dict:
    """Publish one exact artifact with one owner-scoped bounded authority."""

    if _toolsmith is None:
        return {"available": False}
    authority, owner_person_id = _toolsmith_scoped_authority(
        request, scope="toolsmith:graduate", owner_required=True)
    try:
        from colony_sidecar.toolsmith.authority import (
            GraduationAuthorityError,
            GraduationAuthorityV1,
        )
        payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        grant = GraduationAuthorityV1.from_request(
            payload,
            tool_id=tool_id,
            principal_id=authority.principal_id,
            owner_person_id=owner_person_id,
        )
        result = _toolsmith.graduate(tool_id, authority=grant)
    except GraduationAuthorityError as exc:
        status_code = 404 if exc.code == "tool_not_found" else 409
        if exc.code in {"owner_authority_required"}:
            status_code = 403
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "graduation_conflict", "message": str(exc)},
        ) from exc
    return {"available": True, **result}


@router.post("/self/tools/{tool_id}/retire")
async def retire_tool(tool_id: str, reason: str = "owner retired") -> dict:
    if _toolsmith is None:
        return {"available": False}
    ok = _toolsmith.retire(tool_id, reason=reason)
    if not ok:
        raise HTTPException(status_code=404, detail="tool not found")
    return {"available": True, "retired": tool_id}


@router.get("/self/experiments")
async def list_experiments(limit: int = 30) -> dict:
    """Self-experiments: running and recently decided controlled changes."""
    if _experiments is None:
        return {"available": False}
    try:
        return {"available": True,
                **_experiments.snapshot(limit=max(1, min(200, limit)))}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


class ExperimentRequest(BaseModel):
    hypothesis: str
    ref: str
    variant: float
    metric: str
    metric_version: str = ""
    assignment_mode: str = ""
    control_ratio: float = 0.5
    min_control_samples: int = 20
    min_variant_samples: int = 20
    min_total_samples: int = 40
    min_power: float = 0.8
    min_effect: float = 0.0
    owner_negative_limit: int = 1
    max_regression: float = 0.05
    window_days: int = 7
    # Deprecated compatibility field.  The handler always derives source from
    # the authenticated credential and never treats this body label as
    # authority.
    source: str = "api"


def _experiment_approval_response(
    exc,
    response: Response,
) -> dict:
    """Project a durable approval request without calling it pending falsely."""

    exp = exc.experiment
    request_id = exp.get("approval_request_id")
    approval_status = "unknown"
    authority_store = getattr(_experiments, "_approval_authority", None)
    if authority_store is not None and request_id:
        request_row = authority_store.get_request(request_id)
        if request_row is not None:
            approval_status = request_row.get("status") or "unknown"
    if approval_status == "pending":
        response.status_code = status.HTTP_202_ACCEPTED
        projected_status = "approval_required"
    else:
        # Rejected/expired/superseded authority must never be advertised as a
        # pending approval that could still authorize this immutable action.
        response.status_code = status.HTTP_409_CONFLICT
        projected_status = f"approval_{approval_status}"
    return {
        "available": True,
        "status": projected_status,
        "approval_status": approval_status,
        "experiment": exp,
        "approval_request_id": request_id,
    }


@router.post("/self/experiments")
async def post_experiment(
    body: ExperimentRequest,
    request: Request,
    response: Response,
) -> dict:
    """Propose and start a bounded self-experiment (adaptive-param variant,
    judged against a benchmark metric with auto-revert on regression)."""
    if _experiments is None:
        return {"available": False}
    from colony_sidecar.self_model.experiments import (
        ExperimentApprovalRequired,
    )

    try:
        exp = _experiments.propose_and_start(
            hypothesis=body.hypothesis, ref=body.ref, variant=body.variant,
            metric=body.metric, max_regression=body.max_regression,
            window_days=body.window_days,
            metric_version=body.metric_version,
            assignment_mode=body.assignment_mode,
            control_ratio=body.control_ratio,
            min_control_samples=body.min_control_samples,
            min_variant_samples=body.min_variant_samples,
            min_total_samples=body.min_total_samples,
            min_power=body.min_power,
            min_effect=body.min_effect,
            owner_negative_limit=body.owner_negative_limit,
            source=request_authority(request).principal_id)
        return {"available": True, "experiment": exp}
    except ExperimentApprovalRequired as exc:
        return _experiment_approval_response(exc, response)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/self/experiments/{exp_id}/start")
async def start_experiment(exp_id: str, response: Response) -> dict:
    """Start an already-approved or pregranted durable proposal."""
    if _experiments is None:
        return {"available": False}
    existing = _experiments.store.get(exp_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="experiment was not found")
    if existing.get("status") == "running":
        return {"available": True, "experiment": existing,
                "idempotent_replay": True}
    if existing.get("status") != "proposed":
        raise HTTPException(
            status_code=409,
            detail=f"experiment cannot start from {existing.get('status')}",
        )
    from colony_sidecar.self_model.experiments import (
        ExperimentApprovalRequired,
    )

    try:
        exp = _experiments.start(exp_id)
        return {"available": True, "experiment": exp}
    except ExperimentApprovalRequired as exc:
        return _experiment_approval_response(exc, response)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ExperimentExposureRequest(BaseModel):
    unit_id: str
    source_ref: str
    receipt_ref: str = ""
    exposed_at: Optional[float] = None


@router.post("/self/experiments/{exp_id}/exposures")
async def assign_experiment_exposure(
    exp_id: str,
    body: ExperimentExposureRequest,
    request: Request,
) -> dict:
    if _experiments is None:
        return {"available": False}
    try:
        exposure = _experiments.assign_exposure(
            exp_id,
            unit_id=body.unit_id,
            sample_principal=request_authority(request).principal_id,
            source_ref=body.source_ref,
            receipt_ref=body.receipt_ref,
            exposed_at=body.exposed_at,
        )
        return {"available": True, "exposure": exposure}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class ExperimentOutcomeRequest(BaseModel):
    exposure_id: str
    value: float
    source_ref: str
    receipt_ref: str
    owner_reaction: str = ""
    outcome_id: str = ""
    recorded_at: Optional[float] = None


@router.post("/self/experiments/{exp_id}/outcomes")
async def record_experiment_outcome(
    exp_id: str,
    body: ExperimentOutcomeRequest,
    request: Request,
) -> dict:
    if _experiments is None:
        return {"available": False}
    try:
        outcome = _experiments.record_outcome(
            exp_id,
            exposure_id=body.exposure_id,
            value=body.value,
            sample_principal=request_authority(request).principal_id,
            source_ref=body.source_ref,
            receipt_ref=body.receipt_ref,
            owner_reaction=body.owner_reaction,
            outcome_id=body.outcome_id,
            recorded_at=body.recorded_at,
        )
        return {"available": True, "outcome": outcome}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/self/experiments/{exp_id}/evidence")
async def get_experiment_evidence(exp_id: str) -> dict:
    if _experiments is None:
        return {"available": False}
    evidence = _experiments.evidence(exp_id)
    if evidence.get("experiment") is None:
        raise HTTPException(status_code=404, detail="experiment was not found")
    return {"available": True, **evidence}


@router.post("/self/experiments/{exp_id}/abort")
async def abort_experiment(exp_id: str, reason: str = "manual abort") -> dict:
    if _experiments is None:
        return {"available": False}
    ok = _experiments.abort(exp_id, reason=reason)
    if not ok:
        raise HTTPException(status_code=404,
                            detail="no running experiment with that id")
    return {"available": True, "aborted": exp_id}


@router.post("/self/benchmark/compute")
async def compute_benchmark(week: str = "") -> dict:
    """Compute (or recompute) a week's rollups on demand. Default: the
    previous completed ISO week."""
    if _benchmark is None:
        return {"available": False}
    try:
        return {"available": True,
                **await _benchmark.compute_week(week or None)}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/self")
async def get_self_model(request: Request = None) -> dict:
    """Self-model: per-domain competence, live load, trust stages."""
    if _self_model is None:
        return {"available": False}
    _require_perspective_owner(request)
    try:
        out = {"available": True}
        out.update(_self_model.status())
        out["brief"] = _self_model.brief()
        if getattr(_self_model, 'perspective', None) is not None:
            out['perspective'] = _self_model.perspective.status()
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/self/params")
async def get_adaptive_params() -> dict:
    """Adaptive parameters: the meta-learning knobs consumers read back,
    with their bounds, current values, and last adjustment attribution."""
    if _adaptive_params is None:
        return {"available": False}
    try:
        return {"available": True, "params": _adaptive_params.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/autonomy/posture")
async def get_autonomy_posture(request: Request) -> dict:
    """Effective autonomy posture: the active COLONY_AUTONOMY_PRESET (if any)
    and the resolved value of every preset-managed mode flag, as the RUNNING
    process sees them. This is what `colony doctor` reads so plist/unit-pinned
    env is never invisible to diagnostics."""
    try:
        from colony_sidecar.util.autonomy_preset import snapshot
        posture = snapshot()
        from colony_sidecar.initiatives.approval_authority import (
            ApprovalAuthorityStore,
        )
        posture["grant_envelope"] = ApprovalAuthorityStore(
            grant_envelope=getattr(request.app.state, "grant_envelope", None),
        ).grant_posture()
        # The loop mode decides whether preset-enabled subsystems ever get a
        # tick at all; report it so the doctor can flag an incoherent posture
        # (e.g. calibration preset with a reactive loop = nothing calibrates).
        # Prefer the RUNNING loop's resolved mode over the raw env default.
        try:
            if _autonomy_loop is not None:
                posture["COLONY_AUTONOMY_MODE"] = _autonomy_loop.config.mode.value
                posture["COLONY_AUTONOMY_MODE_SOURCE"] = getattr(
                    _autonomy_loop.config, "mode_source", "") or "default"
            else:
                # No running loop: mirror AutonomyConfig.from_env's mode
                # resolution (env > coupled preset > legacy tick > default)
                # so the reported source is honest even pre-loop.
                raw = (os.environ.get("COLONY_AUTONOMY_MODE") or "").strip().lower()
                if raw:
                    posture["COLONY_AUTONOMY_MODE"] = (
                        raw if raw in ("reactive", "proactive") else "reactive")
                    posture["COLONY_AUTONOMY_MODE_SOURCE"] = "env"
                else:
                    from colony_sidecar.util.autonomy_preset import coupled_loop_mode
                    coupled = coupled_loop_mode()
                    if coupled:
                        posture["COLONY_AUTONOMY_MODE"] = coupled
                        posture["COLONY_AUTONOMY_MODE_SOURCE"] = "preset"
                    elif os.environ.get("COLONY_AUTONOMY_TICK_INTERVAL_SECS"):
                        posture["COLONY_AUTONOMY_MODE"] = "proactive"
                        posture["COLONY_AUTONOMY_MODE_SOURCE"] = "legacy_tick"
                    else:
                        posture["COLONY_AUTONOMY_MODE"] = "reactive"
                        posture["COLONY_AUTONOMY_MODE_SOURCE"] = "default"
        except Exception:
            pass
        return {"available": True, "posture": posture}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


@router.get("/self/journal")
async def get_action_journal(limit: int = 50, domain: str = "",
                             today: bool = False) -> dict:
    """Unified action journal (what was done, why, with what confidence)."""
    journal = getattr(_self_model, "journal", None) if _self_model else None
    if journal is None:
        return {"available": False, "entries": []}
    try:
        entries = (journal.today(domain=domain or None) if today
                   else journal.recent(limit=limit, domain=domain or None))
        return {"available": True, "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"available": True, "error": str(exc), "entries": []}


@router.get("/skills-memory")
async def get_skills_memory() -> dict:
    """Procedure-memory skills (item 3) observability."""
    if _skill_store is None:
        return {"available": False}
    try:
        return {"available": True, **_skill_store.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


def _project_visible_to_request(project, request: Request | None) -> bool:
    """Apply the persisted Project visibility envelope before serialization."""

    authority = request_authority(request)
    owner = _owner_person_id()
    viewer = owner if authority.legacy else str(
        authority.viewer_person_id or ""
    ).strip()
    if not viewer:
        return False
    if viewer == owner:
        return True
    try:
        from colony_sidecar.cognition.drive_governance import ScopeV1
        scope = ScopeV1(
            str(project.subject_person_id or ""),
            str(project.viewer_scope or ""),
            str(project.shareability or ""),
        )
        return scope.visible_to(
            viewer_person_id=viewer,
            owner_person_id=owner,
            audiences=authority.audiences,
        )
    except Exception:
        # Legacy rows without a valid visibility envelope remain owner-only.
        return False


def _project_owner_request(request: Request | None) -> bool:
    """Recognize the migration bearer or one exact scoped owner identity.

    Project creation and lifecycle changes are owner-directed operations.  A
    generic ``api:access`` credential may observe its granted project lane,
    but that read grant must not become mutation authority.
    """

    authority = request_authority(request)
    if authority.legacy:
        return True
    owner = _owner_person_id()
    return bool(
        authority.authenticated
        and not authority.anonymous
        and authority.viewer_person_id == owner
        and owner in authority.person_ids
        and "owner" in authority.audiences
    )


def _project_mutation_not_found() -> dict:
    """One existence-hiding result for missing or unauthorized projects."""

    return {"ok": False, "reason": "not_found", "project": None}


@router.get("/projects")
async def list_projects(
    request: Request, status: str = "", limit: int = 30,
) -> dict:
    if _project_engine is None:
        return {
            "available": False,
            "reason": "projects_not_wired",
            "projects": [],
        }
    try:
        from colony_sidecar.projects.models import projects_mode
        items = _project_engine.store.list_projects(status=status or None,
                                                    limit=limit)
        items = [
            project for project in items
            if _project_visible_to_request(project, request)
        ]
        return {"available": True, "count": len(items),
                "mode": projects_mode(),
                "projects": [p.to_row() for p in items]}
    except Exception as exc:
        logger.warning("list_projects failed (%s)", type(exc).__name__)
        return {
            "available": False,
            "reason": "projects_unavailable",
            "projects": [],
        }


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request) -> dict:
    if _project_engine is None:
        return {"available": False}
    project = _project_engine.store.get_project(project_id)
    if project is None or not _project_visible_to_request(project, request):
        return {"available": True, "error": "not_found"}
    out = _project_engine.project_status(project_id)
    return {"available": True, **(out or {"error": "not_found"})}


@router.post("/projects")
async def create_project(
    request: Request, body: dict = Body(default={}),
) -> dict:
    """Owner-directed project creation (boundary-gated; planning happens on
    the next autonomy tick; step dispatch carries its own gates)."""
    if _project_engine is None:
        return {"ok": False, "reason": "projects_not_wired"}
    if not _project_owner_request(request):
        return {
            "ok": False,
            "reason": "owner_authority_required",
            "project": None,
        }
    objective = (body or {}).get("objective", "").strip()
    project, reason = _project_engine.create_project(
        objective, title=(body or {}).get("title", ""),
        # The transport body is not a provenance authority.  Governed and
        # cognition-spine projects are minted only by their typed pipelines.
        source="owner")
    return {"ok": project is not None, "reason": reason,
            "project": project.to_row() if project else None}


@router.post("/projects/{project_id}/abandon")
async def abandon_project(
    project_id: str, request: Request, body: dict = Body(default={}),
) -> dict:
    if _project_engine is None:
        return {"ok": False, "reason": "projects_not_wired"}
    project = _project_engine.store.get_project(project_id)
    if (
        project is None
        or not _project_owner_request(request)
        or not _project_visible_to_request(project, request)
    ):
        return _project_mutation_not_found()
    project = _project_engine.abandon(
        project_id, reason=(body or {}).get("reason", "owner_request"))
    return {"ok": project is not None,
            "reason": "ok" if project is not None else "not_found",
            "project": project.to_row() if project else None}


@router.get("/beliefs")
async def get_beliefs_status() -> dict:
    if _belief_engine is None:
        return {"available": False}
    try:
        return {"available": True, **_belief_engine.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/beliefs/run")
async def run_belief_maintenance() -> dict:
    """Manually trigger one belief-maintenance pass (ops/verification
    surface; the daily autonomy phase is the normal cadence)."""
    if _belief_engine is None:
        return {"available": False}
    try:
        report = await _belief_engine.run()
        return {"available": True, "report": report}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/beliefs/conflicts")
async def get_belief_conflicts(status: str = "", limit: int = 50) -> dict:
    if _belief_engine is None:
        return {"available": False, "conflicts": []}
    try:
        items = _belief_engine.conflicts(status=status or None, limit=limit)
        return {"available": True, "count": len(items), "conflicts": items}
    except Exception as exc:
        return {"available": True, "error": str(exc), "conflicts": []}


@router.get("/sandbox/status")
async def get_sandbox_status() -> dict:
    """Exploration sandbox (item 6): mode, backend, containment limits."""
    if _sandbox is None:
        return {"available": False}
    try:
        return {"available": True, **_sandbox.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/sandbox/run")
async def run_sandbox(
    request: Request,
    body: dict = Body(default={}),
) -> dict:
    """Owner surface: run a script in the sandbox. Owner-directed runs auto-run
    within default limits; still boundary-checked and journaled. The caller
    cannot widen containment (limits are server-side)."""
    if _sandbox is None:
        return {"ran": False, "reason": "sandbox_not_wired"}
    b = body or {}
    authority = request_authority(request)
    owner_person_id = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    # Owner direction is derived from authenticated transport authority.  The
    # legacy bearer remains a migration-compatible owner surface, but request
    # JSON can no longer assert either owner direction or approval.
    owner_directed = bool(
        authority.legacy
        or (
            authority.authenticated
            and not authority.anonymous
            and authority.has_scope("sandbox:execute")
            and "owner" in authority.audiences
            and owner_person_id in authority.person_ids
        )
    )
    return _sandbox.run(
        b.get("script", ""),
        lang=b.get("lang", "python"),
        purpose=b.get("purpose", ""),
        owner_directed=owner_directed,
        approved=owner_directed)


@router.get("/connectors/status")
async def get_connectors_status() -> dict:
    """Connector framework (item 2): mode + per-connector cadence/last-poll."""
    if _connector_manager is None:
        return {"available": False}
    try:
        return {"available": True, **_connector_manager.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/connectors/poll")
async def poll_connectors() -> dict:
    """Manually run one connector ingest pass (ops/verification surface; the
    autonomy phase is the normal cadence)."""
    if _connector_manager is None:
        return {"available": False}
    try:
        report = await _connector_manager.poll_due()
        return {"available": True, "report": report}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/world/llm-extract/status")
async def get_world_llm_extract_status() -> dict:
    if _world_llm_extractor is None:
        return {"available": False}
    from colony_sidecar.world_model.llm_extract import llm_extract_mode
    return {"available": True, "mode": llm_extract_mode(),
            "last_report": getattr(_world_llm_extractor, "last_report", {})}


@router.post("/world/llm-extract/run")
async def run_world_llm_extract() -> dict:
    """Manually trigger one extraction batch (ops/verification surface;
    the daily autonomy phase is the normal cadence)."""
    if _world_llm_extractor is None:
        return {"available": False}
    try:
        report = await _world_llm_extractor.run()
        return {"available": True, "report": report}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/proposals")
async def list_proposals(
    request: Request, status: str = "", limit: int = 30,
) -> dict:
    """List proposals Colony has generated (observability)."""
    if _proposal_store is None:
        return {"available": False, "proposals": []}
    try:
        items = _proposal_store.list(status=status or None, limit=limit)
        authority = request_authority(request)
        viewer = (
            _owner_person_id() if authority.legacy
            else authority.viewer_person_id or ""
        )
        items = [
            item for item in items
            if item.visible_to(
                viewer_person_id=viewer,
                owner_person_id=_owner_person_id(),
                audiences=authority.audiences,
            )
        ]
        return {
            "available": True,
            "count": len(items),
            "proposals": [
                {
                    "id": p.id, "title": p.title, "finding": p.finding,
                    "why_it_helps": p.why_it_helps, "suggested_action": p.suggested_action,
                    "citations": p.citations, "source": p.source,
                    "type": p.initiative_type, "confidence": p.confidence,
                    "status": p.status, "rendered": p.render(),
                    "route_ref": p.route_ref,
                    "result_ref": p.result_ref,
                    "subject_person_id": p.subject_person_id,
                    "viewer_scope": p.viewer_scope,
                    "shareability": p.shareability,
                    "scope_digest": p.scope_digest,
                }
                for p in items
            ],
        }
    except Exception as exc:
        return {"available": True, "error": str(exc), "proposals": []}


@router.get("/world/populate/status")
async def world_populate_status() -> dict:
    if _world_populator is None:
        return {"available": False}
    out = {"available": True, "mode": _world_populator.mode}
    if _world_store is not None:
        try:
            stats = await _world_store.get_stats()
            out["entities"] = stats.total_entities
            out["entities_by_type"] = stats.entities_by_type
            out["relationships"] = stats.total_relationships
        except Exception as exc:
            out["stats_error"] = str(exc)
    return out


@router.get("/directives")
async def list_directives(status: str = "active") -> dict:
    """List the owner's standing directives / boundaries (observability)."""
    if _directive_manager is None:
        return {"available": False, "directives": []}
    try:
        items = (_directive_manager.store.active() if status == "active"
                 else _directive_manager.store.list(status=status))
        return {
            "available": True,
            "count": len(items),
            "directives": [
                {
                    "id": d.id, "polarity": d.polarity.value, "subject": d.subject,
                    "raw_text": d.raw_text, "source": d.source,
                    "status": d.status.value, "match_terms": d.match_terms,
                    "entity_ids": d.entity_ids,
                }
                for d in items
            ],
        }
    except Exception as exc:
        return {"available": True, "error": str(exc), "directives": []}


@router.post("/directives")
async def add_directive(body: dict) -> dict:
    """Explicitly record an owner directive/boundary.

    body: {subject, polarity(prohibit|require|prefer), raw_text?, entity_ids?}
    """
    if _directive_manager is None:
        return {"stored": False, "reason": "directives_not_wired"}
    subject = (body or {}).get("subject", "").strip()
    if not subject:
        return {"stored": False, "reason": "subject_required"}
    d = _directive_manager.add_explicit(
        subject=subject,
        polarity=(body or {}).get("polarity", "prohibit"),
        raw_text=(body or {}).get("raw_text", ""),
        entity_ids=(body or {}).get("entity_ids") or [],
    )
    return {"stored": True, "id": d.id, "polarity": d.polarity.value, "subject": d.subject}


@router.post("/directives/{directive_id}/revoke")
async def revoke_directive(directive_id: str) -> dict:
    if _directive_manager is None:
        return {"revoked": False, "reason": "directives_not_wired"}
    ok = _directive_manager.store.revoke(directive_id)
    return {"revoked": ok, "id": directive_id}


@router.get("/preferences")
async def get_owner_preferences(request: Request = None) -> dict:
    """Return the owner's learned communication preferences and rendered brief."""
    if _preference_learner is None:
        return {"available": False, "brief": "", "preferences": []}
    _require_perspective_owner(request)
    prefs = await _preference_learner.get_all_preferences()
    return {
        "available": True,
        "brief": _preference_learner.build_brief(),
        "sourced": _preference_learner.perspective.status() if getattr(_preference_learner, 'perspective', None) is not None else None,
        "preferences": [
            {
                "category": p.category, "key": p.key, "value": p.value,
                "confidence": p.confidence, "learned_from": p.learned_from,
                "last_updated": p.last_updated.isoformat(),
            }
            for p in prefs
        ],
    }


@router.post("/preferences/learn")
async def learn_owner_preference(body: dict, request: Request = None) -> dict:
    """Teach the owner-preference learner from text.

    Body: ``{"text": "be concise", "explicit": true, "force": false}``
      - explicit (default true): parse as a directive ("be concise"); learned at
        high confidence only if it reads like a communication directive.
      - force (with explicit): if the text isn't a recognized directive, still
        store it as explicit feedback.
      - explicit false: record as an observed behavior signal.
    """
    if _preference_learner is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    _require_perspective_owner(request)
    if getattr(_preference_learner, 'perspective', None) is not None:
        source_id = body.get('source_id')
        if not isinstance(source_id, str) or not source_id:
            raise HTTPException(status_code=422, detail='Use an attributed owner turn and supply its source_id; free text cannot mint a source-backed correction.')
        changes = _preference_learner.learn_source(source_id)
        return {'learned': changes, 'brief': _preference_learner.build_brief()}
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text required")
    learned: Any = None
    if body.get("explicit", True):
        hit = await _preference_learner.learn_directive(text)
        if hit is not None:
            learned = {"category": hit[0], "key": hit[1], "value": hit[2]}
        elif body.get("force"):
            await _preference_learner.learn_from_feedback("communication_style", text)
            learned = {"category": "communication_style", "key": "parsed"}
    else:
        await _preference_learner.learn_from_behavior(text)
        learned = {"category": "behavior"}
    return {"learned": learned, "brief": _preference_learner.build_brief()}


def _require_perspective_owner(request):
    if request is not None:
        from colony_sidecar.identity import get_owner_contact_id
        owner = get_owner_contact_id()
        if not owner:
            raise HTTPException(status_code=503, detail='Owner identity is not configured')
        resolve_request_person(request, claimed_person_id=owner)


_tom_extractor = None


def set_tom_extractor(extractor) -> None:
    global _tom_extractor
    _tom_extractor = extractor


_pattern_store = None


def set_pattern_store(store):
    global _pattern_store
    _pattern_store = store


_surprise_store = None


def set_surprise_store(store):
    global _surprise_store
    _surprise_store = store
_skill_executor = None

def set_skills_registry(registry) -> None:
    global _skills_registry
    _skills_registry = registry


def set_skill_executor(executor) -> None:
    global _skill_executor
    _skill_executor = executor


@router.get("/skills/registry", response_model=SkillsListResponse)
async def list_skills() -> SkillsListResponse:
    if _skills_registry is None:
        return SkillsListResponse(skills=[])
    try:
        skills = await _skills_registry.list_all()
        result = []
        for s in skills:
            d = _to_dict(s)
            d.setdefault("id", d.pop("skill_id", ""))
            for skip in ("created_at", "updated_at", "author_colony_id", "status", "input_schema", "tags", "trigger_patterns"):
                d.pop(skip, None)
            result.append(SkillSummary(**{k: v for k, v in d.items() if k in SkillSummary.model_fields}))
        return SkillsListResponse(skills=result)
    except Exception as exc:
        logger.warning("list_all failed: %s", exc)
        return SkillsListResponse(skills=[])


@router.get("/skills/drafts")
async def list_skill_drafts() -> dict:
    """List skills in DRAFT status awaiting approval."""
    if _skills_registry is None:
        return {"drafts": []}
    try:
        from colony_sidecar.skills.models import SkillStatus
        drafts = await _skills_registry.list_all(status=SkillStatus.DRAFT)
        return {
            "drafts": [
                {
                    "id": getattr(d, "skill_id", ""),
                    "name": getattr(d, "name", ""),
                    "description": getattr(d, "description", ""),
                    "created_at": (
                        getattr(d, "created_at").isoformat()
                        if getattr(d, "created_at", None) else None
                    ),
                }
                for d in drafts
            ]
        }
    except Exception as exc:
        logger.warning("list_skill_drafts failed: %s", exc)
        return {"drafts": []}


@router.post("/skills/{skill_id}/approve")
async def approve_skill(skill_id: str) -> dict:
    """Move a DRAFT skill to ACTIVE."""
    _validate_skill_id(skill_id)
    if _skills_registry is None:
        raise HTTPException(status_code=503, detail="skills_registry_not_initialized")
    try:
        existing = await _skills_registry.get(skill_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        await _skills_registry.activate(skill_id)
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("skill_draft_approved", {
                "skill_id": skill_id,
                "name": getattr(existing, "name", ""),
            })
        except Exception:
            pass
        # v0.18.0 Hermes bridge: best-effort render of the approved skill
        # as an instructional Hermes SKILL.md. Gated inside the exporter
        # by COLONY_EMIT_HERMES_SKILLS (off by default) and a procedural
        # heuristic; a failure here must never block activation.
        try:
            from colony_sidecar.skills.hermes_export import export_approved_skill
            exported = export_approved_skill(existing)
            if exported is not None:
                logger.info("Hermes SKILL.md exported for %s → %s", skill_id, exported)
        except Exception as exc:
            logger.warning("Hermes export failed for %s (non-fatal): %s", skill_id, exc)
        return {"ok": True, "skill_id": skill_id, "status": "active"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("approve_skill failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/skills/{skill_id}/execute", response_model=SkillExecuteResponse)
async def execute_skill(
    skill_id: str, body: SkillExecuteRequest,
) -> SkillExecuteResponse:
    """Invoke an ACTIVE skill in the sandboxed SkillExecutor."""
    _validate_skill_id(skill_id)
    if _skill_executor is None:
        raise HTTPException(
            status_code=503, detail="skill_executor_not_initialized",
        )
    try:
        result = await _skill_executor.invoke(skill_id, body.arguments)
        return SkillExecuteResponse(
            status=result.status,
            output=result.output,
            error=result.error,
            execution_id=result.execution_id,
            duration_ms=result.duration_ms,
        )
    except Exception as exc:
        logger.warning("execute_skill('%s') failed: %s", skill_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/skills/{skill_id}/reject")
async def reject_skill(skill_id: str) -> dict:
    """Reject a DRAFT skill by archiving it."""
    _validate_skill_id(skill_id)
    if _skills_registry is None:
        raise HTTPException(status_code=503, detail="skills_registry_not_initialized")
    try:
        existing = await _skills_registry.get(skill_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        await _skills_registry.archive(skill_id)
        return {"ok": True, "skill_id": skill_id, "status": "archived"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("reject_skill failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/skills/registry/{skill_id}", response_model=SkillDetailResponse)
async def get_skill(skill_id: str) -> SkillDetailResponse:
    _validate_skill_id(skill_id)
    if _skills_registry is None:
        raise HTTPException(status_code=404, detail="Skills not available")
    try:
        skill = await _skills_registry.get(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return SkillDetailResponse(
            id=_to_dict(skill).get("skill_id", _to_dict(skill).get("id", skill_id)),
            name=_to_dict(skill).get("name", ""),
            description=skill.get("description"),
            version=skill.get("version"),
            triggers=skill.get("triggers", []),
            input_schema=skill.get("input_schema"),
            permissions=skill.get("permissions"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("get_skill failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@router.get("/insights", response_model=InsightsListResponse)
async def list_insights(limit: int = 10, dismissed: bool = False) -> InsightsListResponse:
    # Insights come from the synthesis module's connection discoveries
    if _connection_discoverer is None:
        return InsightsListResponse(insights=[])
    try:
        connections = await _connection_discoverer.discover_connections(min_novelty=0.3)
        dismissed_ids = _insight_store.list_dismissed() if _insight_store is not None else set()
        insights = []
        for c in connections:
            cid = getattr(c, "id", None) or str(uuid.uuid4())
            is_dismissed = cid in dismissed_ids
            if not dismissed and is_dismissed:
                continue
            if dismissed and not is_dismissed:
                continue
            insights.append(InsightResponse(
                id=cid,
                title=getattr(c, "connection_type", "Connection"),
                body=getattr(c, "description", "") or f"Connection between {', '.join(getattr(c, 'entities', []))}",
                insight_type=getattr(c, "connection_type", "unknown"),
                novelty=getattr(c, "novelty", 0.0),
                entities=getattr(c, "entities", []),
                dismissed=is_dismissed,
            ))
            if len(insights) >= limit:
                break
        return InsightsListResponse(insights=insights)
    except Exception as exc:
        logger.warning("list_insights failed: %s", exc)
        return InsightsListResponse(insights=[])


@router.post("/insights/{insight_id}/dismiss")
async def dismiss_insight(insight_id: str) -> dict:
    if _insight_store is None:
        raise HTTPException(status_code=503, detail="insight_store_not_initialized")
    _insight_store.dismiss(insight_id)
    return {"ok": True, "insight_id": insight_id}


# ---------------------------------------------------------------------------
# Enriched Context (all-systems assembly)
# ---------------------------------------------------------------------------

@router.post("/context/enriched", response_model=EnrichedContextResponse)
async def enriched_context(
    body: EnrichedContextRequest,
    request: Request = None,
) -> EnrichedContextResponse:
    """Pull from all intelligence systems to build enriched context.

    This is the one-stop endpoint for context assembly — it queries
    memory, relationships, goals, world model, insights, and style
    in parallel and returns assembled sections.
    """
    import asyncio

    sections: list[ContextSection] = []
    contact_id = resolve_request_person(
        request,
        context_person_id=(body.context.contact_id if body.context else None),
        audience=body.audience,
    )
    if body.context is not None and contact_id is not None:
        body.context.contact_id = contact_id
    _enriched_p8_viewer = None
    if _p8_runtime is not None and contact_id:
        try:
            _enriched_p8_viewer = _p8_viewer_for_request(
                request, contact_id)
        except HTTPException:
            logger.debug("P8 enriched context omitted: scoped viewer unavailable")
    _enriched_legacy_global_allowed = _p8_legacy_global_context_allowed(
        _enriched_p8_viewer)
    _enriched_exact_person_allowed = _p8_exact_person_context_allowed(
        _enriched_p8_viewer)
    features = body.features or {}
    msg = body.message

    # Collect context from all available systems in parallel
    tasks: dict[str, Any] = {}

    # 1. Memory search
    if _enriched_exact_person_allowed and _graph is not None:
        async def _mem():
            try:
                recall_kwargs = {
                    "query": msg,
                    "limit": 5,
                    "person_id": contact_id,
                }
                if _p8_runtime is not None:
                    recall_kwargs["exclude_source_uris"] = [
                        "tom:shared_fact"]
                results = await _graph.recall(**recall_kwargs)
                results = _p8_filter_graph_recall(results)
                return ("memory", results)
            except Exception:
                return ("memory", [])
        tasks["memory"] = _mem()

    # 2. Contact / relationship
    if _enriched_exact_person_allowed and _contacts_store is not None \
            and contact_id and features.get("relationships", True):
        async def _contact():
            try:
                c = await _contacts_store.get(contact_id)
                return ("contact", c)
            except Exception:
                return ("contact", None)
        tasks["contact"] = _contact()

    # 3. Contact style
    if _enriched_exact_person_allowed and _contacts_store is not None \
            and contact_id and features.get("style", True):
        async def _style():
            try:
                s = await _contacts_store.get_style(contact_id)
                return ("style", s)
            except Exception:
                return ("style", None)
        tasks["style"] = _style()

    # 4. Active goals
    if _enriched_legacy_global_allowed and _goals_store is not None \
            and features.get("goals", True):
        async def _goals():
            try:
                # GoalEngine.list_goals takes (status, limit, offset) — no person_id —
                # and returns Goal objects; the section renderer expects dicts.
                items = _goals_store.list_goals(status="active", limit=10)
                g = [{
                    "title": getattr(x, "title", "?"),
                    "status": getattr(getattr(x, "status", None), "value",
                                      str(getattr(x, "status", "?"))),
                    "progress": float(getattr(x, "progress_pct", 0.0) or 0.0),
                } for x in (items or [])]
                return ("goals", g)
            except Exception:
                return ("goals", [])
        tasks["goals"] = _goals()

    # 5. World model entities
    if _enriched_legacy_global_allowed and _world_store is not None \
            and features.get("worldModel", True):
        async def _world():
            try:
                e = await _world_context_entities(msg, limit=5)
                return ("world", e)
            except Exception:
                return ("world", [])
        tasks["world"] = _world()

    # 6. Recent insights
    if _enriched_legacy_global_allowed and _connection_discoverer is not None \
            and features.get("insights", True):
        async def _insights():
            try:
                c = await _connection_discoverer.discover_connections(person_id=contact_id, min_novelty=0.3)
                return ("insights", c[:3])
            except Exception:
                return ("insights", [])
        tasks["insights"] = _insights()

    # 7. Identity snapshot (colony_id, node_id, trust tier)
    if features.get("identity", True) and _chain_manager is not None:
        async def _identity():
            try:
                status = await identity_status()
                return ("identity", status)
            except Exception:
                return ("identity", None)
        tasks["identity"] = _identity()

    # 8. Recent briefings
    if _enriched_legacy_global_allowed and _briefings_engine is not None \
            and features.get("briefings", False):
        async def _briefings():
            try:
                briefings = _briefings_engine.get_recent(limit=3)
                return ("briefings", briefings or [])
            except Exception:
                return ("briefings", [])
        tasks["briefings"] = _briefings()

    # 9. Known contacts (top N — useful when the agent references someone
    # not tied to the current contact_id).
    if _enriched_legacy_global_allowed and _contacts_store is not None \
            and features.get("contactsList", False):
        async def _contacts_list():
            try:
                contacts = await _contacts_store.list()
                return ("contactsList", contacts[:8] if contacts else [])
            except Exception:
                return ("contactsList", [])
        tasks["contactsList"] = _contacts_list()

    # 10. Cognition snapshot (CPI — self-awareness metric)
    if _enriched_legacy_global_allowed and _metalearner is not None \
            and features.get("cognition", False):
        async def _cognition():
            try:
                cpi = await _metalearner.evaluate()
                return ("cognition", cpi)
            except Exception:
                return ("cognition", None)
        tasks["cognition"] = _cognition()

    # 11. P8 shared facts: authorization and freshness happen before content
    # reaches the parallel result set or any renderer.
    if _p8_runtime is not None and _enriched_p8_viewer is not None \
            and features.get("shared_facts", True):
        async def _p8_facts():
            try:
                batch = _p8_runtime.project_shared_facts(
                    _enriched_p8_viewer,
                    now=datetime.now(timezone.utc),
                    subject_person_id=contact_id,
                    max_facts=5,
                )
                return ("p8_shared_facts", batch.facts)
            except Exception:
                return ("p8_shared_facts", ())
        tasks["p8_shared_facts"] = _p8_facts()

    # Run all tasks in parallel
    results = {}
    if tasks:
        task_items = list(tasks.items())
        gathered = await asyncio.gather(*[t[1] for t in task_items], return_exceptions=True)
        for (name, _), result in zip(task_items, gathered):
            if isinstance(result, Exception):
                logger.debug("enriched_context %s failed: %s", name, result)
            elif isinstance(result, tuple):
                results[result[0]] = result[1]

    # Build sections from results
    if results.get("memory"):
        body_text = "\n".join(
            f"- [{r.get('score', 0):.2f}] {r.get('content', '')}"
            for r in results["memory"]
        )
        sections.append(ContextSection(id="colony-memory", title="Relevant Memories", body=body_text, priority=90))

    if results.get("p8_shared_facts"):
        facts = results["p8_shared_facts"]
        body_text = "\n".join(
            f"- [{fact.confidence:.0%}] {fact.content}" for fact in facts)
        sections.append(ContextSection(
            id="colony-shared-facts",
            title="Known Facts About Contact",
            body=body_text,
            priority=70,
        ))

    if results.get("contact"):
        c = results["contact"]
        sections.append(ContextSection(
            id="colony-relationship",
            title="Relationship",
            body=f"Trust tier: {c.get('trust_tier', 'unknown')}\n{c.get('style_notes', '')}",
            priority=85,
        ))

    if results.get("style"):
        s = results["style"]
        lines = [f"{k}: {v}" for k, v in s.items() if v]
        if lines:
            sections.append(ContextSection(id="colony-style", title="Communication Style", body="\n".join(lines), priority=80))

    if results.get("goals"):
        goals = results["goals"]
        if goals:
            body_text = "\n".join(f"- {g.get('title', '?')} [{g.get('status', '?')}] {g.get('progress', 0):.0%}" for g in goals)
            sections.append(ContextSection(id="colony-goals", title="Active Goals", body=body_text, priority=75))

    if results.get("world"):
        entities = results["world"]
        if entities:
            body_text = "\n".join(f"- {e.get('name', '?')} ({e.get('entity_type', '?')})" for e in entities)
            sections.append(ContextSection(id="colony-world", title="Known Entities", body=body_text, priority=70))

    if results.get("insights"):
        connections = results["insights"]
        if connections:
            body_text = "\n".join(
                f"- [{getattr(c, 'novelty', 0):.2f}] {getattr(c, 'description', '') or getattr(c, 'connection_type', '')}"
                for c in connections
            )
            sections.append(ContextSection(id="colony-insights", title="Recent Insights", body=body_text, priority=65))

    identity = results.get("identity")
    if identity is not None:
        lines = []
        if getattr(identity, "colony_id", None):
            lines.append(f"colony_id: {identity.colony_id}")
        if getattr(identity, "node_id", None):
            lines.append(f"node_id: {identity.node_id}")
        if getattr(identity, "trust_tier", None):
            anchor = "verified" if identity.trust_anchor_verified else "unverified"
            lines.append(f"trust_tier: {identity.trust_tier} (anchor {anchor})")
        if getattr(identity, "is_genesis", False):
            lines.append("role: GENESIS colony")
        if lines:
            sections.append(ContextSection(
                id="colony-identity",
                title="Colony Identity",
                body="\n".join(lines),
                priority=95,
            ))

    briefings = results.get("briefings")
    if briefings:
        parts = []
        for b in briefings[:3]:
            # Careful not to shadow the request model `body` — it is
            # still read below (compression, citations).
            b_title = b.get("title") if isinstance(b, dict) else getattr(b, "title", "")
            b_body = b.get("body") if isinstance(b, dict) else getattr(b, "body", "")
            if b_title or b_body:
                parts.append(f"- {b_title}: {b_body[:200]}" if b_title else f"- {b_body[:200]}")
        if parts:
            sections.append(ContextSection(
                id="colony-briefings",
                title="Recent Briefings",
                body="\n".join(parts),
                priority=60,
            ))

    contacts_list = results.get("contactsList")
    if contacts_list:
        parts = []
        for c in contacts_list[:8]:
            cd = c if isinstance(c, dict) else _to_dict(c)
            name = cd.get("display_name") or cd.get("name") or cd.get("contact_id") or ""
            tier = cd.get("trust_tier") or ""
            if name:
                parts.append(f"- {name}" + (f" ({tier})" if tier else ""))
        if parts:
            sections.append(ContextSection(
                id="colony-contacts",
                title="Known Contacts",
                body="\n".join(parts),
                priority=55,
            ))

    cognition = results.get("cognition")
    if cognition is not None:
        lines = []
        for attr in ("overall", "memory", "reasoning", "social", "autonomy"):
            val = getattr(cognition, attr, None)
            if val is not None:
                lines.append(f"{attr}: {val:.2f}")
        if lines:
            sections.append(ContextSection(
                id="colony-cognition",
                title="Cognitive Performance",
                body="\n".join(lines),
                priority=50,
            ))

    # Pending commitments
    if _enriched_exact_person_allowed and _commitment_store is not None \
            and contact_id and features.get("commitments", True):
        try:
            pending = _commitment_store.get_pending_for_person(contact_id)
            if pending:
                body_text = "\n".join(
                    f"- {c['description']}"
                    + (f" (due {c['due_at'][:10]})" if c.get('due_at') else "")
                    + f" [priority {c['priority']}]"
                    for c in pending[:5]
                )
                sections.append(ContextSection(
                    id="colony-commitments",
                    title="Pending Commitments",
                    body=body_text,
                    priority=72,
                ))
        except Exception:
            logger.debug("commitment section failed", exc_info=True)

    # Affect (emotional context)
    if _enriched_exact_person_allowed and _affect_store is not None \
            and contact_id and features.get("affect", True):
        try:
            state = _affect_store.get_state(contact_id)
            if state["event_count"] > 0:
                valence = state["current_valence"]
                trend = state["trend"]
                trend_label = {"improving": "trending up", "declining": "trending down", "stable": "stable"}.get(trend, trend)
                body_text = f"Mood: {valence:+.1f} ({trend_label}). Event count: {state['event_count']}."
                if valence > 0.3:
                    body_text += " Positive disposition."
                elif valence < -0.3:
                    body_text += " Negative disposition — consider tone."
                sections.append(ContextSection(
                    id="colony-affect",
                    title="Emotional Context",
                    body=body_text,
                    priority=80,
                ))
        except Exception:
            logger.debug("affect section failed", exc_info=True)

    # Shared facts
    if _p8_runtime is None and _facts_store is not None and contact_id \
            and features.get("shared_facts", True):
        try:
            result = _facts_store.list_facts(contact_id=contact_id, limit=10)
            if result["total"] > 0:
                lines = []
                for f in result["facts"]:
                    source_label = {"told_by_contact": "They told us", "told_to_contact": "We told them", "shared_context": "Shared", "inferred": "Inferred"}.get(f["source"], f["source"])
                    lines.append(f"- [{source_label}] {f['fact']}")
                sections.append(ContextSection(
                    id="colony-shared-facts",
                    title=f"Shared Knowledge with {contact_id}",
                    body="\n".join(lines),
                    priority=70,
                ))
        except Exception:
            logger.debug("shared facts section failed", exc_info=True)

    # Surprises (noteworthy observations)
    if _enriched_legacy_global_allowed and _surprise_store is not None \
            and contact_id and features.get("surprises", True):
        try:
            unresolved = _surprise_store.get_unresolved(min_score=0.5, limit=5)
            if unresolved:
                lines = []
                for s in unresolved:
                    lines.append(f"- [{s['surprise_score']:.1f}] {s['observation']}")
                sections.append(ContextSection(
                    id="colony-surprises",
                    title="Noteworthy Observations",
                    body="Unexpected observations:\n" + "\n".join(lines),
                    priority=75,
                ))
        except Exception:
            logger.debug("surprises section failed", exc_info=True)

    # Adaptive compression
    compression_mode_str = None
    if body.compression:
        compression_mode_str = body.compression
    try:
        from colony_sidecar.compression import (
            CompressionMode,
            compress_sections,
            compress_sections_with_llm,
        )
        override = CompressionMode(compression_mode_str) if compression_mode_str else None
        # Aggressive mode can use the LLM router (when wired) to actually
        # summarize truncated sections instead of just tight-truncating.
        if (
            override == CompressionMode.AGGRESSIVE
            or (override is None and os.environ.get("COLONY_COMPRESSION_MODE", "").lower() == "aggressive")
        ) and _llm_router is not None:
            result = await compress_sections_with_llm(
                sections=[s.model_dump() for s in sections],
                llm_router=_llm_router,
                query=msg,
                override_mode=override,
            )
        else:
            result = compress_sections(
                sections=[s.model_dump() for s in sections],
                query=msg,
                override_mode=override,
            )
        compressed = [ContextSection(**s) for s in result["sections"]]
        return EnrichedContextResponse(
            sections=compressed,
            contact_id=contact_id,
            metadata=result.get("metadata"),
        )
    except Exception:
        logger.debug("compression failed, returning uncompressed", exc_info=True)

    return EnrichedContextResponse(sections=sections, contact_id=contact_id)


# ---------------------------------------------------------------------------
# Chain / Identity
# ---------------------------------------------------------------------------

_chain_manager = None

def set_chain_manager(manager) -> None:
    global _chain_manager
    _chain_manager = manager


@router.get("/identity/status", response_model=IdentityStatusResponse)
@router.get("/identity/info", response_model=IdentityStatusResponse, include_in_schema=False)
async def identity_status() -> IdentityStatusResponse:
    if _chain_manager is None:
        return IdentityStatusResponse(initialized=False)
    try:
        import hashlib
        import os

        colony_id = _chain_manager.colony_id
        pubkey = None
        keys_configured = False
        is_genesis_flag = False
        node_id = None
        node_pubkey = None
        node_cert_fingerprint = None
        trust_anchor_verified = False

        # Try to get public key from key manager
        key_mgr = getattr(_chain_manager, "_key_manager", None)
        if key_mgr is not None:
            try:
                pubkey = key_mgr.public_key_hex()
                keys_configured = True
                from colony_sidecar.chain.identity import is_genesis as check_genesis
                is_genesis_flag = check_genesis(colony_id, pubkey)
            except Exception:
                pass

        # Get node info + cert fingerprint
        state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())
        try:
            from colony_sidecar.chain.node import get_node_info, load_node_certificate
            info = get_node_info(state_dir)
            node_id = info.get("node_id")
            node_pubkey = info.get("node_public_key")
            cert = load_node_certificate(state_dir)
            if cert:
                sig = cert.get("signature", "")
                pub = cert.get("node_public_key") or cert.get("public_key") or ""
                if sig or pub:
                    fp_source = f"{pub}|{sig}".encode("utf-8")
                    node_cert_fingerprint = hashlib.sha256(fp_source).hexdigest()[:32]
        except Exception:
            pass

        # Derive trust tier + anchor verification.
        from colony_sidecar.chain.identity import get_genesis_manifest
        manifest = get_genesis_manifest()
        trust_anchor_verified = manifest is not None
        if is_genesis_flag:
            trust_tier = "GENESIS"
        elif keys_configured and trust_anchor_verified:
            # A properly-keyed colony sitting under a verified Genesis anchor
            # starts at REGULAR. Higher tiers (TRUSTED / PRIVILEGED) are
            # reserved for future attestation flows.
            trust_tier = "REGULAR"
        else:
            trust_tier = None

        return IdentityStatusResponse(
            colony_id=colony_id,
            public_key=pubkey,
            node_id=node_id,
            node_public_key=node_pubkey,
            node_cert_fingerprint=node_cert_fingerprint,
            initialized=colony_id is not None,
            keys_configured=keys_configured,
            is_genesis=is_genesis_flag,
            trust_tier=trust_tier,
            trust_anchor_verified=trust_anchor_verified,
        )
    except Exception as exc:
        logger.warning("identity_status failed: %s", exc)
        return IdentityStatusResponse(initialized=False)


@router.post("/identity/init", response_model=IdentityStatusResponse)
async def identity_init(body: IdentityInitRequest) -> IdentityStatusResponse:
    if _chain_manager is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        # ChainManager initializes at construction time — just return status
        status = _chain_manager.get_status()
        colony_id = _chain_manager.colony_id
        pubkey = status.get("public_key") or getattr(_chain_manager, "public_key_pem", None)
        return IdentityStatusResponse(
            colony_id=colony_id,
            public_key=pubkey,
            initialized=True,
        )
    except Exception as exc:
        logger.warning("identity_init failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/chain/verify", response_model=ChainVerifyResponse)
async def chain_verify(body: ChainVerifyRequest) -> ChainVerifyResponse:
    """Verify the chain is initialized and (when possible) return a
    signed attestation proving the sidecar's authority over the
    ``data`` payload.

    The attestation is ``sign(colony_id || ':' || data || ':' || now)``
    using the colony's Ed25519 private key. Callers verify it with
    ``signer_public_key``. When the key manager isn't loaded the
    attestation fields are ``None`` but the ``valid`` bit is still
    computed from chain state.
    """
    if _chain_manager is None:
        return ChainVerifyResponse(valid=False)
    try:
        state = await _chain_manager.get_state()
        is_valid = state is not None and state.height >= 0
        colony_id = _chain_manager.colony_id

        signed_attestation = None
        signer_pub = None
        attested_at = None
        if is_valid:
            key_mgr = getattr(_chain_manager, "_key_manager", None)
            if key_mgr is not None:
                try:
                    from datetime import datetime, timezone
                    attested_at = datetime.now(timezone.utc).isoformat()
                    payload = (
                        f"{colony_id}:{body.data}:{attested_at}".encode("utf-8")
                    )
                    signed_attestation = key_mgr.sign(payload)
                    signer_pub = key_mgr.public_key_hex()
                except Exception as sig_exc:
                    logger.debug("attestation signing failed: %s", sig_exc)

        return ChainVerifyResponse(
            valid=is_valid,
            colony_id=colony_id,
            signed_attestation=signed_attestation,
            attested_at=attested_at,
            signer_public_key=signer_pub,
        )
    except Exception as exc:
        logger.warning("chain_verify failed: %s", exc)
        return ChainVerifyResponse(valid=False)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_secrets_manager = None

def set_secrets_manager(manager) -> None:
    global _secrets_manager
    _secrets_manager = manager


def _memory_context_selector():
    """Keep one selector per active reranker, also usable without graph storage."""
    global _context_recall_selector
    if _context_recall_selector is None or _context_recall_selector[0] is not _reranker:
        from colony_sidecar.intelligence.graph.selection import RecallSelector
        from colony_sidecar.intelligence.graph.recall import provider_calibration_metadata
        provider = _reranker
        selector = RecallSelector(
            provider.rerank if provider is not None else None,
            calibration_metadata=(lambda: provider_calibration_metadata(provider))
                if provider is not None else None,
            logger=logger)
        _context_recall_selector = (provider, selector)
    return _context_recall_selector[1]


def set_reranker(reranker) -> None:
    global _reranker
    _reranker = reranker


def set_session_store(store) -> None:
    global _session_store
    _session_store = store


def set_task_queue(queue) -> None:
    global _task_queue
    _task_queue = queue
    manager = getattr(queue, "queue", None)
    if manager is not None and hasattr(manager, "configure_governance"):
        manager.configure_governance(globals().get("_worker_governor"))


def set_session_report_store(store) -> None:
    global _session_report_store
    _session_report_store = store


def _map_initiative_to_schema(i) -> AgentSnapshotInitiative:
    """Map an Initiative model to the AgentSnapshotInitiative schema."""
    return AgentSnapshotInitiative(
        id=i.id,
        type=i.type,
        description=i.description,
        priority=i.priority,
        status=i.status,
        rationale=i.rationale,
        action_hint=i.action_hint,
        entity_id=i.entity_id,
        dedup_key=i.dedup_key,
        created_at=i.created_at.isoformat() if i.created_at else "",
        expires_at=i.expires_at.isoformat() if i.expires_at else None,
        assigned_agent_id=i.assigned_agent_id,
        acknowledged_at=i.acknowledged_at.isoformat() if i.acknowledged_at else None,
        completed_at=i.completed_at.isoformat() if i.completed_at else None,
        failed_at=i.failed_at.isoformat() if i.failed_at else None,
        failed_reason=i.failed_reason,
    )


@router.post("/secrets/list", response_model=SecretListResponse)
async def secrets_list(body: SecretListRequest) -> SecretListResponse:
    if _secrets_manager is None:
        return SecretListResponse(keys=[])
    try:
        all_keys = _secrets_manager.list()
        if body.prefix:
            keys = [k for k in all_keys if k.startswith(body.prefix)]
        else:
            keys = all_keys
        return SecretListResponse(keys=keys)
    except Exception as exc:
        logger.warning("secrets_list failed: %s", exc)
        return SecretListResponse(keys=[])


@router.post("/secrets/get", response_model=SecretGetResponse)
async def secrets_get(body: SecretGetRequest) -> SecretGetResponse:
    if _secrets_manager is None:
        return SecretGetResponse(key=body.key, exists=False)
    try:
        value = _secrets_manager.get(body.key)
        if value is None:
            return SecretGetResponse(key=body.key, exists=False)
        return SecretGetResponse(key=body.key, value=value, exists=True)
    except Exception as exc:
        logger.warning("secrets_get failed: %s", exc)
        return SecretGetResponse(key=body.key, exists=False)


@router.post("/secrets/set", response_model=SecretSetResponse)
async def secrets_set(body: SecretSetRequest) -> SecretSetResponse:
    if _secrets_manager is None:
        return SecretSetResponse(key=body.key, stored=False)
    try:
        _secrets_manager.set(body.key, body.value, secret_type=body.secret_type)
        return SecretSetResponse(key=body.key, stored=True)
    except Exception as exc:
        logger.warning("secrets_set failed: %s", exc)
        return SecretSetResponse(key=body.key, stored=False)


@router.post("/secrets/delete", response_model=SecretDeleteResponse)
async def secrets_delete(body: SecretDeleteRequest) -> SecretDeleteResponse:
    if _secrets_manager is None:
        return SecretDeleteResponse(key=body.key, deleted=False)
    try:
        _secrets_manager.delete(body.key)
        return SecretDeleteResponse(key=body.key, deleted=True)
    except Exception as exc:
        logger.warning("secrets_delete failed: %s", exc)
        return SecretDeleteResponse(key=body.key, deleted=False)


# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------

_autonomy_loop = None
_autonomy_task = None
_reranker = None
_context_recall_selector = None
_session_store = None
_task_queue = None
_session_report_store = None
_agent_bridge = None
_initiative_executor = None

def set_autonomy_loop(loop) -> None:
    global _autonomy_loop
    _autonomy_loop = loop


def set_agent_bridge(bridge) -> None:
    global _agent_bridge
    _agent_bridge = bridge


def set_initiative_executor(executor) -> None:
    global _initiative_executor
    _initiative_executor = executor


_scheduler = None


def set_scheduler(scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


@router.get("/autonomy/schedule")
async def list_schedules():
    if _scheduler is None:
        return {"schedules": []}
    return {"schedules": [s.to_dict() for s in _scheduler.list_schedules()]}


@router.post("/autonomy/schedule/{schedule_id}/enable")
async def enable_schedule(schedule_id: str):
    if _scheduler is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    if _scheduler.enable(schedule_id):
        return {"status": "enabled"}
    raise HTTPException(status_code=404, detail="Schedule not found")


@router.post("/autonomy/schedule/{schedule_id}/disable")
async def disable_schedule(schedule_id: str):
    if _scheduler is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    if _scheduler.disable(schedule_id):
        return {"status": "disabled"}
    raise HTTPException(status_code=404, detail="Schedule not found")


@router.get("/autonomy/status", response_model=AutonomyStatusResponse)
async def autonomy_status() -> AutonomyStatusResponse:
    if _autonomy_loop is None:
        return AutonomyStatusResponse()
    try:
        s = _autonomy_loop.status()
        return AutonomyStatusResponse(
            running=s.get("running", False),
            mode=s.get("mode", "reactive"),
            timezone=s.get("timezone", "UTC"),
            in_quiet_hours=s.get("in_quiet_hours", False),
            ticks=s.get("stats", {}).get("ticks", 0),
            events_processed=s.get("stats", {}).get("events_processed", 0),
            goals_checked=s.get("stats", {}).get("goals_checked", 0),
            initiatives_generated=s.get("stats", {}).get("initiatives_generated", 0),
            actions_executed=s.get("stats", {}).get("actions_executed", 0),
            errors=s.get("stats", {}).get("errors", 0),
            phases_cancelled=s.get("stats", {}).get("phases_cancelled", 0),
            last_cancelled_phase=s.get("stats", {}).get("last_cancelled_phase"),
            phases=s.get("phases"),
            config=s.get("config"),
        )
    except Exception as exc:
        logger.warning("autonomy_status failed: %s", exc)
        return AutonomyStatusResponse()


@router.post("/autonomy/start", response_model=AutonomyStatusResponse)
async def autonomy_start() -> AutonomyStatusResponse:
    global _autonomy_task
    if _autonomy_loop is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    if _autonomy_loop.is_running:
        return await autonomy_status()
    try:
        _autonomy_task = asyncio.create_task(_autonomy_loop.start())
        # Give it a moment to start
        await asyncio.sleep(0.1)
        return await autonomy_status()
    except Exception as exc:
        logger.warning("autonomy_start failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/autonomy/stop", response_model=AutonomyStatusResponse)
async def autonomy_stop() -> AutonomyStatusResponse:
    global _autonomy_task
    if _autonomy_loop is None:
        return AutonomyStatusResponse()
    try:
        await _autonomy_loop.stop()
        if _autonomy_task is not None:
            try:
                await asyncio.wait_for(_autonomy_task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Autonomy loop did not stop within timeout")
            _autonomy_task = None
        return await autonomy_status()
    except Exception as exc:
        logger.warning("autonomy_stop failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/autonomy/cycle", response_model=dict)
async def autonomy_cycle() -> dict:
    """Trigger a single autonomy tick for testing.

    In reactive mode this runs _tick() directly.
    In proactive mode it just wakes the loop early.
    """
    if _autonomy_loop is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        mode = (_autonomy_loop.config.mode.value
                if getattr(_autonomy_loop, "config", None) else "unknown")
        # In reactive mode the loop isn't actively ticking — run one directly and
        # the DB reflects the tick on return. In proactive mode we only WAKE the
        # loop: the tick (incl. job-writeback, phase 6c near the end) runs async,
        # so the DB is NOT yet updated when this returns. Callers/e2e tests must
        # poll rather than read immediately — `ran_synchronously` says which.
        ran_synchronously = mode == "reactive"
        if ran_synchronously:
            await _autonomy_loop._tick()
        else:
            _autonomy_loop.wake()
        status = _autonomy_loop.status()
        return {
            "completed": True,
            "mode": mode,
            "ran_synchronously": ran_synchronously,
            "note": (None if ran_synchronously else
                     "proactive mode: loop woken; tick runs asynchronously, "
                     "DB not yet updated — poll for results"),
            "result": status,
        }
    except Exception as exc:
        logger.warning("autonomy_cycle failed: %s", exc)
        return {"completed": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Agent Bridge status
# ---------------------------------------------------------------------------

@router.get("/bridge/status")
async def bridge_status() -> dict:
    if _agent_bridge is None:
        return {"running": False, "wired": False}
    return {
        "running": getattr(_agent_bridge, "is_running", False),
        "wired": True,
        "stats": getattr(_agent_bridge, "stats", {}),
    }


# ---------------------------------------------------------------------------
# Initiative Executor status
# ---------------------------------------------------------------------------

@router.get("/executor/status")
async def executor_status() -> dict:
    if _initiative_executor is None:
        return {"running": False, "wired": False}
    return {
        "running": getattr(_initiative_executor, "is_running", False),
        "wired": True,
        "stats": getattr(_initiative_executor, "stats", {}),
    }


# ---------------------------------------------------------------------------
# Self-Knowledge Seeding
# ---------------------------------------------------------------------------


class SeedResponse(BaseModel):
    memories: int = 0
    entities: int = 0
    skills: int = 0
    insights: int = 0
    errors: list[str] = []
    skipped: list[str] = []  # Reasons for skipping (e.g., "already_seeded")


# ---------------------------------------------------------------------------
# Commitment Tracking
# ---------------------------------------------------------------------------

@router.post("/commitments", status_code=status.HTTP_201_CREATED)
async def create_commitment(body: CommitmentCreateRequest) -> CommitmentResponse:
    """Create a new commitment. With `dedupe` set, an open commitment for the
    same person that already says the same thing is returned instead of
    creating a twin (response carries deduped=true)."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    try:
        result = _commitment_store.create(
            person_id=body.person_id,
            description=body.description,
            due_at=body.due_at,
            priority=body.priority,
            source_type=body.source_type,
            source_context=body.source_context,
            metadata=body.metadata,
            dedupe=body.dedupe,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if result.get("deduped"):
        return CommitmentResponse(**result)

    try:
        from colony_sidecar.events.broadcaster import emit as _emit
        _emit("commitment.created", {
            "commitment_id": result["id"],
            "person_id": result["person_id"],
            "description": result["description"],
        })
    except Exception:
        pass
    return CommitmentResponse(**result)


@router.get("/commitments", response_model=CommitmentListResponse)
async def list_commitments(
    person_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    overdue_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CommitmentListResponse:
    """List commitments with optional filters."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    statuses = [s.strip() for s in status_filter.split(",")] if status_filter else None

    # When "overdue" is requested, get commitments that are actually overdue
    # (past due_date + still pending), not just ones already transitioned
    if statuses and "overdue" in statuses:
        try:
            overdue = _commitment_store.get_overdue()
            other_statuses = [s for s in statuses if s != "overdue"]
            if other_statuses:
                result = _commitment_store.list(
                    person_id=person_id,
                    status=other_statuses,
                    overdue_only=False,
                    limit=limit,
                    offset=offset,
                )
                # Merge
                other_items = result if isinstance(result, list) else result.get("commitments", [])
                all_items = overdue + other_items
            else:
                all_items = overdue
            return CommitmentListResponse(
                commitments=all_items, total=len(all_items),
                limit=limit, offset=offset,
            )
        except Exception as exc:
            logger.warning("get_overdue failed: %s", exc)

    result = _commitment_store.list(
        person_id=person_id,
        status=statuses,
        overdue_only=overdue_only,
        limit=limit,
        offset=offset,
    )
    return CommitmentListResponse(**result)


@router.get("/commitments/{commitment_id}", response_model=CommitmentResponse)
async def get_commitment(commitment_id: str) -> CommitmentResponse:
    """Get a single commitment by ID."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    result = _commitment_store.get(commitment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return CommitmentResponse(**result)


def _resolve_linked_concerns(commitment_id: str, note: str) -> None:
    """Reverse cascade: a commitment settled directly (agent tool, MCP, API)
    must also leave the workspace, or the deck keeps showing a concern for an
    item that no longer exists as open work."""
    if _workspace is None:
        return
    try:
        n = _workspace.store.resolve_by_dedup(
            f"commitment:{commitment_id}", note)
        if n:
            logger.info("resolved %d workspace concern(s) linked to commitment %s",
                        n, commitment_id)
    except Exception:
        logger.debug("linked-concern resolve failed", exc_info=True)


@router.patch("/commitments/{commitment_id}", response_model=CommitmentResponse)
async def update_commitment(commitment_id: str, body: CommitmentUpdateRequest) -> CommitmentResponse:
    """Update a commitment. With `outcome` set this is a resolution: status
    is derived, the reason is recorded in metadata for the learning loop, and
    any workspace concern raised from this commitment is resolved too."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    if body.outcome is not None:
        # Resolution path: store.resolve() maps outcome -> status, records
        # {outcome, note, by, at} in metadata and emits the event itself.
        try:
            result = _commitment_store.resolve(
                commitment_id,
                outcome=body.outcome,
                note=body.reason,
                resolved_by=body.resolved_by or "owner",
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if result is None:
            raise HTTPException(status_code=404, detail="Commitment not found")
        _resolve_linked_concerns(
            commitment_id, f"commitment settled ({body.outcome})")
        return CommitmentResponse(**result)

    try:
        result = _commitment_store.update(
            commitment_id=commitment_id,
            status=body.status,
            fulfilled_at=body.fulfilled_at,
            description=body.description,
            due_at=body.due_at,
            priority=body.priority,
            metadata=body.metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail="Commitment not found")

    # Emit events for status changes
    if body.status == "fulfilled":
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("commitment.fulfilled", {
                "commitment_id": result["id"],
                "person_id": result["person_id"],
            })
        except Exception:
            pass
    elif body.status == "cancelled":
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("commitment.cancelled", {
                "commitment_id": result["id"],
                "person_id": result["person_id"],
            })
        except Exception:
            pass

    if body.status in ("fulfilled", "cancelled"):
        _resolve_linked_concerns(commitment_id, f"commitment {body.status}")

    return CommitmentResponse(**result)


@router.get("/commitments/stats/resolution")
async def commitment_resolution_stats(days: int = Query(30, ge=1, le=365)) -> dict:
    """How commitments created in the window got resolved, per source_type —
    the calibration signal for whatever generates items (introspection,
    cognition, agents): a source whose items keep getting cancelled as
    invalid should get more conservative."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")
    try:
        stats = _commitment_store.resolution_stats(days=days)
        stats["recent_rejections"] = _commitment_store.recent_rejections(limit=10)
        return stats
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/commitments/{commitment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_commitment(commitment_id: str):
    """Delete a commitment. Only allowed for terminal states (fulfilled/cancelled)."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    deleted = _commitment_store.delete(commitment_id)
    if not deleted:
        existing = _commitment_store.get(commitment_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Commitment not found")
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete commitment in '{existing['status']}' state. Cancel it first.",
            )


# ---------------------------------------------------------------------------
# Cognition Substrate
# ---------------------------------------------------------------------------

@router.post("/cognition/trigger", response_model=CognitionTriggerResponse)
async def cognition_trigger(body: CognitionTriggerRequest) -> CognitionTriggerResponse:
    """Trigger a cognition cycle via OpenClaw subagent spawn.

    The sidecar emits a cognition.requested event with the built prompt.
    The Colony plugin picks this up and calls sessions_spawn with the
    configured model and restricted tool allowlist.
    """
    from colony_sidecar.cognition.trigger import trigger_cognition

    result = await trigger_cognition(
        trigger_type=body.trigger_type,
        context=body.context,
        priority=body.priority,
    )
    return CognitionTriggerResponse(**result)


# ---------------------------------------------------------------------------
# Theory of Mind — Affect
# ---------------------------------------------------------------------------

async def _require_person_contact(contact_id: str) -> None:
    """ToM stores accept only REAL contacts (docs/RELATIONSHIPS.md #5).

    Free-text names, test strings, and the machine sentinel are refused so
    psyche/affect/fact state can never be minted for a non-person. When the
    contact store is unavailable the check degrades open (single-store
    test deployments keep working)."""
    cid = (contact_id or "").strip()
    if not cid or cid in ("system", "default"):
        raise HTTPException(
            status_code=422,
            detail=f"contact_id {cid!r} is not a person contact")
    if _contacts_store is None:
        return
    try:
        exists = await _contacts_store.get(cid) is not None
    except Exception:
        return
    if not exists:
        raise HTTPException(
            status_code=422,
            detail=f"unknown contact_id {cid!r} — create the contact first "
                   "(POST /v1/host/contacts) or resolve the sender handle")


@router.post("/affect/events", response_model=AffectEventResponse, status_code=status.HTTP_201_CREATED)
async def create_affect_event(body: AffectEventCreateRequest) -> AffectEventResponse:
    """Record an affect event for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    await _require_person_contact(body.contact_id)
    try:
        result = _affect_store.create_event(
            contact_id=body.contact_id,
            valence=body.valence,
            arousal=body.arousal,
            source=body.source,
            trigger=body.trigger,
            session_id=body.session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        from colony_sidecar.events.broadcaster import emit as _emit
        _emit("affect.event_created", {
            "event_id": result["id"],
            "contact_id": result["contact_id"],
            "valence": result["valence"],
        })
    except Exception:
        pass

    # Check for negative spike
    if _affect_store.detect_negative_spike(body.contact_id):
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("affect.negative_spike", {
                "contact_id": body.contact_id,
                "valence": result["valence"],
            })
        except Exception:
            pass

    return AffectEventResponse(**result)


@router.get("/affect/state/{contact_id}", response_model=AffectStateResponse)
async def get_affect_state(contact_id: str) -> AffectStateResponse:
    """Get the current affect state for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    state = _affect_store.get_state(contact_id)
    return AffectStateResponse(**state)


@router.get("/affect/history/{contact_id}", response_model=AffectEventListResponse)
async def list_affect_history(
    contact_id: str,
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AffectEventListResponse:
    """Get affect event history for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    events = _affect_store.list_events(contact_id=contact_id, source=source, limit=limit, offset=offset)
    try:
        total = _affect_store.count_events(contact_id=contact_id, source=source)
    except Exception:
        total = offset + len(events)
    return AffectEventListResponse(events=[AffectEventResponse(**e) for e in events], total=total, limit=limit, offset=offset)


@router.delete("/affect/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_affect_event(event_id: str):
    """Delete an affect event."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    if not _affect_store.delete_event(event_id):
        raise HTTPException(status_code=404, detail="Affect event not found")


# ---------------------------------------------------------------------------
# Theory of Mind — Shared Facts
# ---------------------------------------------------------------------------

async def _mirror_fact_to_graph(fact: str, contact_id: Optional[str],
                                source: str, confidence: float, *, record=None) -> bool:
    """Mirror a shared fact into the memory graph as a `fact` memory.

    Shared facts live in their own store; semantic recall searches the memory
    graph. Without this mirror a stored fact is structurally unrecallable
    (recall.fact_coverage measured exactly that). A linked fact's hash includes
    its source and contact: replay can reinforce that support, but cannot
    take ownership of identical wording from another turn or a legacy row.

    Returns True when the fact reached the graph (created or reinforced),
    False otherwise — callers on the hot path ignore this; the backfill
    endpoint uses it to count per-fact outcomes."""
    if _graph is None or not (fact or "").strip():
        return False
    try:
        import hashlib
        lineage = record.get('source_lineage') if record else None
        if lineage and (_facts_store is None or not _facts_store.source_visible(record)):
            return False
        source_uri = 'turn:' + lineage['turn_id'] if lineage else 'tom:shared_fact'
        metadata = {"shared_fact": True, "fact_source": source or ""}
        if lineage:
            metadata.update(source_turn_id=lineage['turn_id'], fact_record_id=record['id'],
                source_message_hashes=lineage['message_hashes'],
                model_provenance=(record.get('metadata') or {}).get('model_provenance', {}))
        # A linked support must not reinforce or take ownership of an old
        # independent fact, or of the same wording learned in another turn.
        basis = json.dumps([source_uri, contact_id, fact], ensure_ascii=False) if lineage else fact
        memory_id = await _graph.store_memory(
            content=fact,
            memory_type="fact",
            entities=[],
            metadata=metadata,
            importance=max(0.1, min(1.0, confidence if confidence is not None else 0.7)),
            person_id=contact_id,
            source_type="inference",
            source_uri=source_uri,
            content_hash=hashlib.sha256(basis.encode("utf-8")).hexdigest(),
        )
        return bool(memory_id) if lineage else True
    except Exception:
        logger.debug("shared fact -> graph mirror failed", exc_info=True)
        return False


def _append_p8_fact_record(
    record: Mapping[str, Any],
    *,
    producer,
    origin: str,
) -> None:
    """Best-effort shadow append; never changes SharedFactsStore semantics."""

    if _p8_runtime is None or producer is None:
        return
    try:
        _p8_runtime.append_shared_fact(
            record, producer=producer, origin=origin)
    except Exception:
        logger.warning(
            "P8 visibility envelope append failed for shared fact %s",
            record.get("id"),
            exc_info=True,
        )


@router.post("/mind/facts", response_model=SharedFactResponse, status_code=status.HTTP_201_CREATED)
async def create_shared_fact(
    body: SharedFactCreateRequest,
    request: Request = None,
) -> SharedFactResponse:
    """Add a shared fact about what a contact knows."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    body.contact_id = resolve_request_person(
        request, claimed_person_id=body.contact_id) or body.contact_id
    await _require_person_contact(body.contact_id)
    try:
        result = _facts_store.create_fact(
            contact_id=body.contact_id,
            fact=body.fact,
            source=body.source,
            confidence=body.confidence,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _mirror_fact_to_graph(body.fact, body.contact_id, body.source, body.confidence)
    producer = None
    if _p8_runtime is not None:
        try:
            producer = _p8_viewer_for_request(request, body.contact_id)
        except HTTPException:
            logger.debug("P8 fact envelope omitted: scoped producer unavailable")
    _append_p8_fact_record(result, producer=producer, origin="body")

    try:
        from colony_sidecar.events.broadcaster import emit as _emit
        _emit("mind.fact_created", {
            "fact_id": result["id"],
            "contact_id": result["contact_id"],
            "source": result["source"],
        })
    except Exception:
        pass

    return SharedFactResponse(**result)


# Single-flight state for the shared-facts -> graph backfill. Facts created
# before the create-time mirror existed never reached the memory graph, so
# semantic recall can't see them. The backfill replays them through the same
# mirror; content-hash dedup makes re-runs mostly idempotent (a re-run
# reinforces the existing node: +0.05 strength / +1 corroboration — documented
# behavior, not a bug).
_facts_backfill_state: Dict[str, Any] = {"running": False}


class FactsBackfillRequest(BaseModel):
    dry_run: bool = True
    limit: int = 0              # 0 = no cap
    min_confidence: float = 0.0
    sleep_ms: int = 150         # serial pacing between embeds (shared embedder)


@router.post("/mind/facts/backfill")
async def backfill_shared_facts(body: FactsBackfillRequest) -> dict:
    """Mirror existing shared facts into the memory graph (explicit admin op).

    Runs as a background task, mirroring facts serially with ``sleep_ms``
    spacing so a large backlog cannot saturate a shared embedder. Per-fact
    failures (e.g. embedder outage — store_memory fails closed) are counted
    and skipped, never fatal to the run. Single-flight: a second invocation
    while one is running returns 409. Progress is journaled every 100 facts.
    """
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    if _graph is None:
        raise HTTPException(status_code=501, detail="Memory graph not initialized")
    if _facts_backfill_state.get("running"):
        raise HTTPException(status_code=409, detail="facts backfill already running")

    # Collect candidates up-front (paged reads from SQLite; no awaits, so the
    # single-flight check above cannot interleave with another request).
    facts: list = []
    offset = 0
    cap = body.limit if body.limit and body.limit > 0 else None
    while True:
        page = _facts_store.list_facts(
            min_confidence=body.min_confidence, limit=200, offset=offset)
        rows = page.get("facts") or []
        if not rows:
            break
        facts.extend(rows)
        offset += len(rows)
        if cap is not None and len(facts) >= cap:
            facts = facts[:cap]
            break

    if body.dry_run:
        return {"dry_run": True, "started": False, "total": len(facts)}

    sleep_secs = max(0, body.sleep_ms) / 1000.0
    _facts_backfill_state.update({
        "running": True, "dry_run": False, "total": len(facts),
        "processed": 0, "mirrored": 0, "failed": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    })

    def _journal_progress(final: bool = False) -> None:
        try:
            from colony_sidecar.events.journal import append_event
            append_event("memory.facts_backfill", {
                "processed": _facts_backfill_state["processed"],
                "mirrored": _facts_backfill_state["mirrored"],
                "failed": _facts_backfill_state["failed"],
                "total": _facts_backfill_state["total"],
                "final": final,
            })
        except Exception:
            logger.debug("facts backfill journal failed", exc_info=True)

    async def _run() -> None:
        try:
            for fact_row in facts:
                ok = await _mirror_fact_to_graph(
                    fact_row.get("fact") or "",
                    fact_row.get("contact_id"),
                    fact_row.get("source") or "",
                    fact_row.get("confidence"),
                    record=fact_row,
                )
                _facts_backfill_state["processed"] += 1
                if ok:
                    _facts_backfill_state["mirrored"] += 1
                else:
                    _facts_backfill_state["failed"] += 1
                if _facts_backfill_state["processed"] % 100 == 0:
                    _journal_progress()
                if sleep_secs:
                    await asyncio.sleep(sleep_secs)
        finally:
            _facts_backfill_state["running"] = False
            _facts_backfill_state["finished_at"] = (
                datetime.now(timezone.utc).isoformat())
            _journal_progress(final=True)

    _spawn_task(_run())
    return {"dry_run": False, "started": True, "total": len(facts)}


@router.get("/mind/facts/backfill")
async def backfill_shared_facts_status() -> dict:
    """Progress/status of the current (or last) shared-facts backfill."""
    return dict(_facts_backfill_state)


@router.get("/mind/facts", response_model=SharedFactListResponse)
async def list_shared_facts(
    contact_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SharedFactListResponse:
    """List shared facts with optional filters.

    Also searches the memory graph (Neo4j) for fact/preference/semantic
    memories that haven't been synced to the SQLite facts store.
    """
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    result = _facts_store.list_facts(
        contact_id=contact_id,
        source=source,
        min_confidence=min_confidence,
        limit=limit,
        offset=offset,
    )
    facts = result["facts"]

    # Fallback: search memory graph for fact-type memories
    if _graph is not None and contact_id and len(facts) < limit:
        try:
            memories = await _graph.recall(
                query=f"facts about {contact_id}",
                limit=limit - len(facts),
                person_id=contact_id,
            )
            for mem in memories:
                mem_type = mem.get("type", "")
                if mem_type in ("fact", "preference", "semantic"):
                    # Check if already in facts (avoid duplicates)
                    mem_content = mem.get("content", "")
                    if not any(f["fact"] == mem_content for f in facts):
                        facts.append({
                            "id": mem.get("id", ""),
                            "contact_id": contact_id,
                            "fact": mem_content,
                            "source": "memory_graph",
                            "confidence": mem.get("strength", 0.8),
                            "created_at": mem.get("created_at", ""),
                            "expires_at": None,
                            "metadata": None,
                        })
        except Exception as exc:
            logger.debug("Memory graph fallback search failed: %s", exc)

    return SharedFactListResponse(
        facts=[SharedFactResponse(**f) for f in facts],
        total=len(facts),
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/mind/facts/{fact_id}", response_model=SharedFactResponse)
async def get_shared_fact(fact_id: str) -> SharedFactResponse:
    """Get a specific shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    result = _facts_store.get_fact(fact_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    return SharedFactResponse(**result)


@router.patch("/mind/facts/{fact_id}", response_model=SharedFactResponse)
async def update_shared_fact(
    fact_id: str,
    body: SharedFactUpdateRequest,
    request: Request = None,
) -> SharedFactResponse:
    """Update a shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    existing = _facts_store.get_fact(fact_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    contact_id = resolve_request_person(
        request, claimed_person_id=existing.get("contact_id"))
    result = _facts_store.update_fact(
        fact_id,
        confidence=body.confidence,
        expires_at=body.expires_at,
        fact=body.fact,
        metadata=body.metadata,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    producer = None
    if _p8_runtime is not None:
        try:
            producer = _p8_viewer_for_request(
                request, str(contact_id or existing.get("contact_id") or ""))
        except HTTPException:
            logger.debug("P8 updated fact envelope omitted: producer unavailable")
    _append_p8_fact_record(result, producer=producer, origin="body")
    return SharedFactResponse(**result)


@router.delete("/mind/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shared_fact(fact_id: str):
    """Delete a shared fact. Cascades to second-order inferences that
    reference it (reversibility, docs/TOM2-LEVELS.md): a dangling ref could
    never render anyway — H3.5 fails closed — this keeps the store honest."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    if not _facts_store.delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Shared fact not found")
    if _tom2_store is not None:
        try:
            _tom2_store.delete_for_fact(fact_id)
        except Exception:
            logger.debug("tom2 delete_for_fact cascade failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pattern Extraction
# ---------------------------------------------------------------------------

@router.post("/patterns", response_model=PatternResponse, status_code=status.HTTP_201_CREATED)
async def create_pattern(body: PatternCreateRequest) -> PatternResponse:
    """Register a pattern (manual or extraction)."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    try:
        result = _pattern_store.create_pattern(
            pattern_type=body.pattern_type,
            description=body.description,
            pattern_key=body.pattern_key,
            frequency=body.frequency,
            confidence=body.confidence,
            metadata=body.metadata,
            source=body.source,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        from colony_sidecar.events.broadcaster import emit as _emit
        _emit("pattern.created", {"pattern_id": result["id"], "pattern_type": result["pattern_type"]})
    except Exception:
        pass
    return PatternResponse(**result)


@router.get("/patterns", response_model=PatternListResponse)
async def list_patterns(
    pattern_type: Optional[str] = Query(None),
    min_frequency: int = Query(1, ge=1),
    source: Optional[str] = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PatternListResponse:
    """List patterns with optional filters."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.list_patterns(
        pattern_type=pattern_type,
        min_frequency=min_frequency,
        source=source,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return PatternListResponse(
        patterns=[PatternResponse(**p) for p in result["patterns"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/patterns/{pattern_id}", response_model=PatternResponse)
async def get_pattern(pattern_id: str) -> PatternResponse:
    """Get a specific pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.get_pattern(pattern_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return PatternResponse(**result)


@router.patch("/patterns/{pattern_id}", response_model=PatternResponse)
async def update_pattern(pattern_id: str, body: PatternUpdateRequest) -> PatternResponse:
    """Update a pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.update_pattern(
        pattern_id,
        description=body.description,
        confidence=body.confidence,
        metadata=body.metadata,
        active=body.active,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return PatternResponse(**result)


@router.delete("/patterns/{pattern_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pattern(pattern_id: str):
    """Delete a pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    if not _pattern_store.delete_pattern(pattern_id):
        raise HTTPException(status_code=404, detail="Pattern not found")


@router.post("/patterns/extract", response_model=PatternExtractResponse)
async def extract_patterns_endpoint() -> PatternExtractResponse:
    """Trigger a pattern extraction run against the world model."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    from colony_sidecar.patterns.extract import extract_patterns
    result = extract_patterns(world_store=_world_store, pattern_store=_pattern_store)
    try:
        from colony_sidecar.events.broadcaster import emit as _emit
        _emit("pattern.extracted", {"new": result["new"], "updated": result["updated"], "total": result["total"]})
    except Exception:
        pass
    return PatternExtractResponse(**result)


# ---------------------------------------------------------------------------
# Surprise Engine
# ---------------------------------------------------------------------------

@router.post("/surprises", response_model=SurpriseResponse, status_code=status.HTTP_201_CREATED)
async def create_surprise(body: SurpriseCreateRequest) -> SurpriseResponse:
    """Record a surprise observation."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")

    score = body.surprise_score
    expected = body.expected
    # Auto-score if requested.
    if body.auto_score and _pattern_store is not None:
        from colony_sidecar.surprise.scorer import compute_surprise
        scored = compute_surprise(body.observation, pattern_store=_pattern_store)
        if score is None:
            score = scored["surprise_score"]
        if expected is None:
            expected = scored.get("expected")
    elif score is None:
        score = 0.5

    try:
        result = _surprise_store.create_surprise(
            observation=body.observation,
            expected=expected,
            surprise_score=score,
            pattern_id=body.pattern_id,
            context=body.context,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Emit high surprise event.
    if result["surprise_score"] >= 0.8:
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("surprise.high", {
                "surprise_id": result["id"],
                "observation": result["observation"],
                "score": result["surprise_score"],
            })
        except Exception:
            pass

    return SurpriseResponse(**result)


@router.get("/surprises", response_model=SurpriseListResponse)
async def list_surprises(
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    resolved: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SurpriseListResponse:
    """List surprises with optional filters."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")
    result = _surprise_store.list_surprises(
        min_score=min_score,
        resolved=resolved,
        limit=limit,
        offset=offset,
    )
    return SurpriseListResponse(
        surprises=[SurpriseResponse(**s) for s in result["surprises"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/surprises/unresolved", response_model=List[SurpriseResponse])
async def list_unresolved_surprises(
    min_score: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(10, ge=1, le=50),
) -> List[SurpriseResponse]:
    """Get unresolved high-score surprises."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")
    results = _surprise_store.get_unresolved(min_score=min_score, limit=limit)
    return [SurpriseResponse(**s) for s in results]


@router.get("/surprises/{surprise_id}", response_model=SurpriseResponse)
async def get_surprise(surprise_id: str) -> SurpriseResponse:
    """Get a specific surprise."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")
    result = _surprise_store.get_surprise(surprise_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Surprise not found")
    return SurpriseResponse(**result)


@router.patch("/surprises/{surprise_id}", response_model=SurpriseResponse)
async def resolve_surprise(surprise_id: str, body: SurpriseResolveRequest) -> SurpriseResponse:
    """Resolve/acknowledge a surprise."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")
    result = _surprise_store.resolve_surprise(surprise_id, resolution=body.resolution)
    if result is None:
        raise HTTPException(status_code=404, detail="Surprise not found")
    return SurpriseResponse(**result)


@router.delete("/surprises/{surprise_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_surprise(surprise_id: str):
    """Delete a surprise."""
    if _surprise_store is None:
        raise HTTPException(status_code=501, detail="Surprise engine not initialized")
    if not _surprise_store.delete_surprise(surprise_id):
        raise HTTPException(status_code=404, detail="Surprise not found")


# ---------------------------------------------------------------------------
# ToM LLM Extraction
# ---------------------------------------------------------------------------

async def _run_tom_extraction(
    conversation_text: str,
    contact_id: str,
    session_id: Optional[str] = None,
    p8_producer=None,
    source_id: Optional[str] = None,
) -> None:
    """Background task: extract affect + facts from a conversation turn."""
    if _tom_extractor is None:
        return
    lineage = None
    if source_id is not None:
        try:
            lineage, conversation_text = _facts_store.source_input(source_id, contact_id)
        except Exception:
            logger.debug('ToM source unavailable; background projection skipped', exc_info=True)
            return
    def source_current():
        return lineage is None or _facts_store._source_visible(contact_id, lineage)
    # Affect
    try:
        affect = await _tom_extractor.extract_affect(
            conversation_text, contact_id, session_id=session_id,
        )
        if not source_current():
            return
        if affect and _affect_store is not None:
            _affect_store.create_event(
                contact_id=affect["contact_id"],
                valence=affect["valence"],
                arousal=affect["arousal"],
                source="inferred",
                trigger=affect.get("trigger"),
                session_id=session_id,
                source_lineage=lineage,
            )
            try:
                from colony_sidecar.events.broadcaster import emit as _emit
                _emit("affect.event_created", {"contact_id": contact_id, "source": "inferred"})
            except Exception:
                pass
    except Exception:
        logger.debug("ToM affect extraction failed", exc_info=True)
    # Facts
    try:
        facts = await _tom_extractor.extract_facts(
            conversation_text, contact_id, session_id=session_id,
        )
        if not source_current():
            return
        if facts and _facts_store is not None:
            for f in facts:
                record = _facts_store.create_fact(
                    contact_id=f["contact_id"],
                    fact=f["fact"],
                    source=f["source"],
                    confidence=f["confidence"],
                    source_lineage=lineage,
                    metadata={'model_provenance': f.get('model_provenance', {})} if lineage else None,
                )
                _append_p8_fact_record(
                    record, producer=p8_producer, origin="model")
                await _mirror_fact_to_graph(f["fact"], f["contact_id"],
                                            f["source"], f["confidence"], record=record)
            try:
                from colony_sidecar.events.broadcaster import emit as _emit
                _emit("mind.fact_created", {"contact_id": contact_id, "source": f["source"]})
            except Exception:
                pass
    except Exception:
        logger.debug("ToM fact extraction failed", exc_info=True)
    # Engagement profile (OCEAN + communication style)
    try:
        eng = await _tom_extractor.extract_engagement(
            conversation_text, contact_id, session_id=session_id,
        )
        if not source_current():
            return
        if eng and _engagement_store is not None:
            _engagement_store.update_from_observation(
                contact_id,
                ocean=eng.get("ocean"),
                style=eng.get("style"),
                motivators=eng.get("motivators"),
                topics=eng.get("topics"),
                avoid=eng.get("avoid"),
                source_lineage=lineage,
            )
    except Exception:
        logger.debug("ToM engagement extraction failed", exc_info=True)


@router.post("/tom/extract", response_model=TomExtractResponse)
async def extract_tom(
    body: TomExtractRequest,
    request: Request = None,
) -> TomExtractResponse:
    """Manually trigger ToM extraction for a conversation snippet."""
    if _tom_extractor is None:
        raise HTTPException(status_code=501, detail="ToM extraction not available (no LLM router)")
    # A manual ToM write must target a real person, same as the affect/facts
    # POST paths: a stale group contact_id here would pollute the wrong
    # person's psyche (docs/RELATIONSHIPS.md #5).
    body.contact_id = resolve_request_person(
        request, claimed_person_id=body.contact_id) or body.contact_id
    await _require_person_contact(body.contact_id)
    _manual_p8_producer = None
    if _p8_runtime is not None:
        try:
            _manual_p8_producer = _p8_viewer_for_request(
                request, body.contact_id)
        except HTTPException:
            logger.debug("P8 manual extraction envelope omitted: producer unavailable")

    affect_result = None
    facts_result = []

    if body.extract_affect:
        affect_result = await _tom_extractor.extract_affect(
            body.conversation_text,
            body.contact_id,
            session_id=body.session_id,
        )
        if affect_result and _affect_store is not None:
            _affect_store.create_event(
                contact_id=affect_result["contact_id"],
                valence=affect_result["valence"],
                arousal=affect_result["arousal"],
                source="inferred",
                trigger=affect_result.get("trigger"),
            )

    if body.extract_facts:
        facts_result = await _tom_extractor.extract_facts(
            body.conversation_text,
            body.contact_id,
            session_id=body.session_id,
        )
        if facts_result and _facts_store is not None:
            for f in facts_result:
                record = _facts_store.create_fact(
                    contact_id=f["contact_id"],
                    fact=f["fact"],
                    source=f["source"],
                    confidence=f["confidence"],
                )
                _append_p8_fact_record(
                    record, producer=_manual_p8_producer, origin="model")
                await _mirror_fact_to_graph(f["fact"], f["contact_id"],
                                            f["source"], f["confidence"])

    throttled = not _tom_extractor._can_extract(body.contact_id)
    return TomExtractResponse(
        affect=affect_result,
        facts=facts_result,
        throttled=throttled,
    )


@router.post("/seed", response_model=SeedResponse)
async def seed_self_knowledge_endpoint(force: bool = Query(False, description="Force re-seeding even if already seeded")) -> SeedResponse:
    """Seed Colony with self-knowledge via API.

    This endpoint triggers the self-knowledge seeding process that populates
    Colony's memory, world model, and skills registry with deep understanding
    of its own architecture and capabilities.

    Args:
        force: If True, re-seed even if already seeded (updates existing)
    """
    from colony_sidecar.seed import seed_self_knowledge

    # Ensure world store is connected
    ws = _world_store
    if ws is not None and hasattr(ws, "connect") and getattr(ws, "_backend", None) is None:
        try:
            await ws.connect()
        except Exception:
            pass

    # Ensure skills registry is opened
    sr = _skills_registry
    if sr is not None and hasattr(sr, "open"):
        try:
            sr.open()
        except Exception:
            pass

    results = await seed_self_knowledge(
        graph=_graph,
        world_store=ws,
        skills_registry=sr,
        force=force,
    )

    return SeedResponse(
        memories=results.get("memories", 0),
        entities=results.get("entities", 0),
        skills=results.get("skills", 0),
        insights=results.get("insights", 0),
        errors=results.get("errors", []),
        skipped=results.get("skipped", []),
    )


# ============================================================================
# World Model — Entity CRUD
# ============================================================================

@router.post("/world/entities", response_model=WorldEntityDetailResponse)
async def create_world_entity(body: WorldEntityCreateRequest) -> WorldEntityDetailResponse:
    """Create a new entity in the world model."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        from colony_sidecar.world_model.entities import BaseEntity, ENTITY_CLASS_MAP
        from colony_sidecar.world_model.neo4j.backend import _generate_id
        cls = ENTITY_CLASS_MAP.get(body.entity_type, BaseEntity)
        import dataclasses
        valid = {f.name for f in dataclasses.fields(cls)}
        now = datetime.now(timezone.utc)
        kwargs = {k: v for k, v in {
            "id": _generate_id("we"),
            "name": body.name,
            "entity_type": body.entity_type,
            "aliases": body.aliases or [],
            "external_ids": body.external_ids or {},
            "confidence": body.confidence,
            "properties": body.properties or {},
            "first_seen": now,
            "last_seen": now,
            "created_at": now,
            "updated_at": now,
        }.items() if k in valid}
        entity = cls(**kwargs)
        result = await _world_store.upsert_entity(entity)
        return _wm_entity_to_response(result)
    except Exception as exc:
        logger.warning("create_world_entity failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/world/entities/{entity_id}", response_model=WorldEntityDetailResponse)
async def get_world_entity(entity_id: str) -> WorldEntityDetailResponse:
    """Get a single entity by ID."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    entity = await _world_store.get_entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return _wm_entity_to_response(entity)


@router.patch("/world/entities/{entity_id}", response_model=WorldEntityDetailResponse)
async def update_world_entity(entity_id: str, body: WorldEntityUpdateRequest) -> WorldEntityDetailResponse:
    """Update an existing entity's properties."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        entity = await _world_store.get_entity(entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        if body.name is not None:
            entity.name = body.name
        if body.confidence is not None:
            entity.confidence = body.confidence
        if body.properties:
            for k, v in body.properties.items():
                await _world_store.update_entity_property(entity_id, k, v, entity.confidence)
        if body.aliases:
            for alias in body.aliases:
                await _world_store.add_entity_alias(entity_id, alias)
        # Re-fetch to get updated state
        entity = await _world_store.get_entity(entity_id)
        return _wm_entity_to_response(entity)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("update_world_entity failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/world/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_world_entity(entity_id: str):
    """Delete an entity from the world model."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        if _world_store._backend is None:
            raise HTTPException(status_code=501, detail="World model backend not connected")
        await _world_store._backend.delete_entity(entity_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("delete_world_entity failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# World Model — Relationship CRUD
# ============================================================================

@router.post("/world/relationships", response_model=WorldRelationshipResponse)
async def create_world_relationship(body: WorldRelationshipCreateRequest) -> WorldRelationshipResponse:
    """Create a new relationship between two entities."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        from colony_sidecar.world_model.relationships import WorldRelationship
        from colony_sidecar.world_model.neo4j.backend import _generate_id
        now = datetime.now(timezone.utc).isoformat()
        rel = WorldRelationship(
            id=_generate_id("wr"),
            source_id=body.source_id,
            target_id=body.target_id,
            relationship_type=body.relationship_type,
            confidence=body.confidence,
            valid_from=body.valid_from or now,
            properties=body.properties or {},
            created_at=now,
            updated_at=now,
        )
        result = await _world_store.upsert_relationship(rel)
        return _wm_rel_to_response(result)
    except Exception as exc:
        logger.warning("create_world_relationship failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/world/relationships", response_model=WorldRelationshipListResponse)
async def list_world_relationships(
    source_id: Optional[str] = None,
    target_id: Optional[str] = None,
    relationship_type: Optional[str] = None,
    active_only: bool = False,
    limit: int = 100,
) -> WorldRelationshipListResponse:
    """Query relationships with flexible filtering."""
    if _world_store is None:
        return WorldRelationshipListResponse()
    try:
        rels = await _world_store.query_relationships(
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type,
            active_only=active_only,
            limit=limit,
        )
        return WorldRelationshipListResponse(
            relationships=[_wm_rel_to_response(r) for r in rels],
            total=len(rels),
        )
    except Exception as exc:
        logger.warning("list_world_relationships failed: %s", exc)
        return WorldRelationshipListResponse()


@router.get("/world/relationships/{rel_id}", response_model=WorldRelationshipResponse)
async def get_world_relationship(rel_id: str) -> WorldRelationshipResponse:
    """Get a single relationship by ID."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    if _world_store._backend is None:
        raise HTTPException(status_code=501, detail="World model backend not connected")
    rel = await _world_store._backend.get_relationship(rel_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="Relationship not found")
    return _wm_rel_to_response(rel)


@router.patch("/world/relationships/{rel_id}", response_model=WorldRelationshipResponse)
async def update_world_relationship(rel_id: str, body: WorldRelationshipUpdateRequest) -> WorldRelationshipResponse:
    """Update a relationship (close it or update properties)."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        if body.valid_to is not None:
            await _world_store.close_relationship(rel_id, body.valid_to)
        if body.properties and _world_store._backend:
            # Update properties on the relationship
            rel = await _world_store._backend.get_relationship(rel_id)
            if rel is None:
                raise HTTPException(status_code=404, detail="Relationship not found")
            rel.properties.update(body.properties)
            if body.confidence is not None:
                rel.confidence = body.confidence
            await _world_store.upsert_relationship(rel)
        elif body.confidence is not None:
            if _world_store._backend:
                rel = await _world_store._backend.get_relationship(rel_id)
                if rel is None:
                    raise HTTPException(status_code=404, detail="Relationship not found")
                rel.confidence = body.confidence
                await _world_store.upsert_relationship(rel)
        # Re-fetch
        if _world_store._backend:
            rel = await _world_store._backend.get_relationship(rel_id)
            if rel:
                return _wm_rel_to_response(rel)
        raise HTTPException(status_code=404, detail="Relationship not found after update")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("update_world_relationship failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/world/relationships/{rel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_world_relationship(rel_id: str):
    """Delete a relationship from the world model."""
    # Neo4j doesn't have a dedicated delete in store, use close
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        now = datetime.now(timezone.utc).isoformat()
        await _world_store.close_relationship(rel_id, now)
    except Exception as exc:
        logger.warning("delete_world_relationship failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# World Model — Graph Traversal
# ============================================================================

@router.get("/world/entities/{entity_id}/neighborhood", response_model=WorldNeighborhoodResponse)
async def get_entity_neighborhood(
    entity_id: str,
    max_hops: int = 2,
    relationship_types: Optional[str] = None,  # comma-separated
    max_nodes: int = 200,
) -> WorldNeighborhoodResponse:
    """Get the graph neighborhood around an entity."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        types_list = relationship_types.split(",") if relationship_types else None
        result = await _world_store.get_neighborhood(
            entity_id=entity_id,
            max_hops=max_hops,
            relationship_types=types_list,
            max_nodes=max_nodes,
        )
        return WorldNeighborhoodResponse(
            center=_wm_entity_to_response(result.center) if result.center else None,
            reachable=[_wm_entity_to_response(e) for e in result.reachable],
            edges=[_wm_rel_to_response(r) for r in result.edges],
            hop_counts=result.hop_counts,
            truncated=result.truncated,
        )
    except Exception as exc:
        logger.warning("get_entity_neighborhood failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/world/entities/{source_id}/path/{target_id}", response_model=WorldPathResponse)
async def find_entity_path(
    source_id: str,
    target_id: str,
    max_hops: int = 5,
) -> WorldPathResponse:
    """Find the shortest path between two entities."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        path = await _world_store.find_path(
            source_id=source_id,
            target_id=target_id,
            max_hops=max_hops,
        )
        if path is None:
            return WorldPathResponse(source_id=source_id, target_id=target_id, found=False)
        return WorldPathResponse(
            source_id=source_id,
            target_id=target_id,
            path=[_wm_rel_to_response(r) for r in path],
            found=True,
        )
    except Exception as exc:
        logger.warning("find_entity_path failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# World Model — Causal chain (read-only; the sanctioned causal-edge surface)
# ============================================================================

@router.get("/world/causal/chain")
async def world_causal_chain(
    entity_id: str,
    direction: str = "downstream",
    max_hops: int = 3,
    min_confidence: float = 0.0,
) -> dict:
    """Walk causal edges only (WM_CAUSES/ENABLES/BLOCKS/INHIBITS) to answer
    "why" / "what happens if" questions. Causal edges are query-only by
    policy and excluded from generic graph reads; this endpoint (and
    explicitly-typed relationship queries) are the only surfaces returning
    them."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        from colony_sidecar.world_model.causal_query import causal_chain
        return await causal_chain(
            _world_store, entity_id, direction=direction,
            max_hops=max_hops, min_confidence=min_confidence)
    except Exception as exc:
        logger.warning("world_causal_chain failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/world/causal/edges")
async def world_causal_edges(min_confidence: float = 0.0,
                             limit: int = 100) -> dict:
    """Flat list of stored causal edges (read-only observability surface)."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        from colony_sidecar.world_model.causal_query import causal_edges
        edges = await causal_edges(_world_store, min_confidence=min_confidence,
                                   limit=limit)
        return {"edges": edges, "total": len(edges)}
    except Exception as exc:
        logger.warning("world_causal_edges failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/world/stats", response_model=WorldStatsResponse)
async def get_world_stats() -> WorldStatsResponse:
    """Get world model statistics."""
    if _world_store is None:
        raise HTTPException(status_code=501, detail="World model not initialized")
    try:
        stats = await _world_store.get_stats()
        return WorldStatsResponse(**stats.__dict__ if hasattr(stats, "__dict__") else stats)
    except Exception as exc:
        logger.warning("get_world_stats failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# World Model — Helpers
# ============================================================================

def _wm_entity_to_response(entity) -> WorldEntityDetailResponse:
    """Convert a BaseEntity subclass to WorldEntityDetailResponse."""
    return WorldEntityDetailResponse(
        id=entity.id,
        name=entity.name,
        entity_type=entity.entity_type,
        aliases=entity.aliases or [],
        external_ids=entity.external_ids or {},
        confidence=entity.confidence,
        properties=entity.properties or {},
        first_seen=entity.first_seen.isoformat() if entity.first_seen else None,
        last_seen=entity.last_seen.isoformat() if entity.last_seen else None,
        created_at=entity.created_at.isoformat() if entity.created_at else None,
        updated_at=entity.updated_at.isoformat() if entity.updated_at else None,
    )


def _wm_rel_to_response(rel) -> WorldRelationshipResponse:
    """Convert a WorldRelationship to WorldRelationshipResponse."""
    return WorldRelationshipResponse(
        id=rel.id,
        source_id=rel.source_id,
        target_id=rel.target_id,
        relationship_type=rel.relationship_type,
        confidence=rel.confidence,
        valid_from=rel.valid_from,
        valid_to=rel.valid_to,
        properties=rel.properties or {},
        is_active=rel.is_active if hasattr(rel, "is_active") else rel.valid_to is None,
        created_at=rel.created_at,
    )


# ============================================================================
# Multi-Agent — Agent Management (v0.7.0)
# ============================================================================

_agent_store = None
_invite_store = None
_initiative_store = None
_assignment_engine = None
_websocket_manager = None


def set_agent_store(store) -> None:
    global _agent_store
    _agent_store = store


def set_invite_store(store) -> None:
    global _invite_store
    _invite_store = store


def set_initiative_store(store) -> None:
    global _initiative_store
    _initiative_store = store


def set_assignment_engine(engine) -> None:
    global _assignment_engine
    _assignment_engine = engine


def set_websocket_manager(manager) -> None:
    global _websocket_manager
    _websocket_manager = manager


# --- Agent Onboarding ---

@router.post("/agents/invite", response_model=AgentInviteResponse)
async def create_agent_invite(body: AgentInviteRequest) -> AgentInviteResponse:
    """Generate a setup code for remote agent onboarding."""
    if _invite_store is None:
        raise HTTPException(status_code=501, detail="Invite store not initialized")

    colony_id = os.environ.get("COLONY_ID", str(uuid.uuid4()))

    invite = _invite_store.create(
        colony_id=colony_id,
        capabilities=body.granted_capabilities,
        is_primary=body.granted_is_primary,
        max_concurrent=body.granted_max_concurrent,
        expires_seconds=body.expires_in_seconds,
        label=body.label,
    )

    # Build setup command
    colony_url = os.environ.get("COLONY_URL", "http://localhost:7777")
    setup_command = f"colony agent connect --setup-code {invite['setup_code']} --colony-url {colony_url}"

    return AgentInviteResponse(
        code=invite["setup_code"],
        expires_at=invite["expires_at"],
        max_uses=1,  # Single use by default
        setup_command=setup_command,
    )


@router.post("/agents/connect", response_model=AgentConnectResponse)
async def connect_remote_agent(body: AgentConnectRequest) -> AgentConnectResponse:
    """Connect a remote agent using setup code."""
    if _invite_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent system not initialized")

    # Generate agent ID and node ID
    agent_id = str(uuid.uuid4())
    node_id = body.node_id or str(uuid.uuid4())
    colony_id = os.environ.get("COLONY_ID", str(uuid.uuid4()))

    # Validate and use setup code
    try:
        invite = _invite_store.use(body.setup_code, node_id, agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Create node certificate. When the chain key manager is available the
    # cert is signed by the Colony's Ed25519 key over the canonical payload
    # (the node public key the verifier checks lives in the metadata field
    # `node_public_key_ed25519`); without a key manager the cert is
    # explicitly UNSIGNED and the remote-agent handshake will not verify.
    # NOTE: remote multi-agent connect + chain consensus are EXPERIMENTAL
    # (no consensus loop runs; see docs/MULTI_AGENT.md).
    issued_at = datetime.now(timezone.utc)
    _sig = ""
    _km = getattr(_chain_manager, "_key_manager", None) if _chain_manager is not None else None
    _cert_body = {
        "colony_id": colony_id,
        "node_id": node_id,
        "node_public_key_ed25519": body.node_public_key,
        "issued_at": issued_at.isoformat(),
    }
    if _km is not None:
        try:
            import json as _json
            _payload = _json.dumps(_cert_body, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
            _sig = _km.sign(_payload)
        except Exception:
            logger.warning("chain cert signing failed; issuing unsigned cert",
                           exc_info=True)
    else:
        logger.warning("No chain key manager — remote-agent cert is UNSIGNED "
                       "and will not verify (chain surface is experimental)")
    node_cert = AgentNodeCert(
        colony_id=colony_id,
        node_id=node_id,
        public_key=body.node_public_key,
        signature=_sig,
        issued_at=issued_at.isoformat(),
    )

    # Register agent
    agent = _agent_store.create({
        "agent_id": agent_id,
        "node_id": node_id,
        "colony_id": colony_id,
        "name": body.name,
        "connection_mode": "remote",
        "capabilities": invite.get("capabilities", []),
        "is_primary": invite.get("is_primary", False),
        "max_concurrent": invite.get("max_concurrent", 5),
        "metadata": body.metadata,
    })

    # Build websocket URL
    colony_url = os.environ.get("COLONY_URL", "ws://localhost:7777")
    ws_url = f"{colony_url.replace('http', 'ws')}/v1/host/agents/{agent_id}/stream"

    return AgentConnectResponse(
        agent_id=agent_id,
        node_id=node_id,
        colony_id=colony_id,
        node_cert=node_cert,
        websocket_url=ws_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        max_concurrent=agent.max_concurrent,
    )


@router.post("/agents/register", response_model=AgentRegisterResponse)
async def register_local_agent(body: AgentRegisterRequest) -> AgentRegisterResponse:
    """Register a local agent (same network, no setup code)."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent_id = body.agent_id or str(uuid.uuid4())
    node_id = body.node_id or str(uuid.uuid4())
    colony_id = os.environ.get("COLONY_ID", str(uuid.uuid4()))

    _agent_store.create({
        "agent_id": agent_id,
        "node_id": node_id,
        "colony_id": colony_id,
        "name": body.name,
        "connection_mode": body.connection_mode,
        "gateway_url": body.gateway_url,
        "capabilities": body.capabilities,
        "is_primary": body.is_primary,
        "priority": body.priority,
        "max_concurrent": body.max_concurrent,
        "excluded_types": body.excluded_types,
        "metadata": body.metadata,
    })

    ws_url = None
    if body.connection_mode == "remote":
        colony_url = os.environ.get("COLONY_URL", "ws://localhost:7777")
        ws_url = f"{colony_url.replace('http', 'ws')}/v1/host/agents/{agent_id}/stream"

    return AgentRegisterResponse(
        agent_id=agent_id,
        node_id=node_id,
        colony_id=colony_id,
        websocket_url=ws_url,
    )


# --- Agent Management ---

@router.post("/agents/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str, body: AgentHeartbeatRequest) -> Dict[str, Any]:
    """Update agent status with heartbeat."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update status and metadata
    updates = {
        "status": body.status,
        "current_assignments": body.current_assignments,
        "last_seen_at": datetime.now(timezone.utc),
    }
    if body.metadata:
        updates["metadata"] = body.metadata

    _agent_store.update(agent_id, **updates)

    return {"status": "ok", "agent_id": agent_id}


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    status: Optional[str] = Query(None),
    capability: Optional[str] = Query(None),
) -> AgentListResponse:
    """List all registered agents."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agents = _agent_store.list(status=status, capability=capability)

    return AgentListResponse(
        agents=[
            AgentResponse(
                agent_id=a.agent_id,
                node_id=a.node_id,
                name=a.name,
                colony_id=a.colony_id,
                connection_mode=a.connection_mode,
                gateway_url=a.gateway_url,
                capabilities=a.capabilities,
                is_primary=a.is_primary,
                priority=a.priority,
                max_concurrent=a.max_concurrent,
                excluded_types=a.excluded_types,
                status=a.status,
                current_assignments=a.current_assignments,
                metadata=AgentMetadataSchema(**a.metadata.to_dict()) if hasattr(a.metadata, 'to_dict') else AgentMetadataSchema(),
                registered_at=a.registered_at.isoformat() if a.registered_at else "",
                last_seen_at=a.last_seen_at.isoformat() if a.last_seen_at else None,
            )
            for a in agents
        ],
        total=len(agents),
    )


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str) -> AgentResponse:
    """Get agent details."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentResponse(
        agent_id=agent.agent_id,
        node_id=agent.node_id,
        name=agent.name,
        colony_id=agent.colony_id,
        connection_mode=agent.connection_mode,
        gateway_url=agent.gateway_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        priority=agent.priority,
        max_concurrent=agent.max_concurrent,
        excluded_types=agent.excluded_types,
        status=agent.status,
        current_assignments=agent.current_assignments,
        metadata=AgentMetadataSchema(**agent.metadata.to_dict()) if hasattr(agent.metadata, 'to_dict') else AgentMetadataSchema(),
        registered_at=agent.registered_at.isoformat() if agent.registered_at else "",
        last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
    )


@router.delete("/agents/{agent_id}")
async def revoke_agent(agent_id: str) -> Dict[str, Any]:
    """Revoke an agent's access."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _agent_store.revoke(agent_id)

    return {"status": "revoked", "agent_id": agent_id}


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, body: AgentUpdateRequest) -> AgentResponse:
    """Update agent configuration."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    updates = body.dict(exclude_unset=True)
    if updates:
        agent = _agent_store.update(agent_id, **updates)

    return AgentResponse(
        agent_id=agent.agent_id,
        node_id=agent.node_id,
        name=agent.name,
        colony_id=agent.colony_id,
        connection_mode=agent.connection_mode,
        gateway_url=agent.gateway_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        priority=agent.priority,
        max_concurrent=agent.max_concurrent,
        excluded_types=agent.excluded_types,
        status=agent.status,
        current_assignments=agent.current_assignments,
        metadata=AgentMetadataSchema(**agent.metadata.to_dict()) if hasattr(agent.metadata, 'to_dict') else AgentMetadataSchema(),
        registered_at=agent.registered_at.isoformat() if agent.registered_at else "",
        last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
    )


@router.get("/agents/health", response_model=AgentHealthResponse)
async def get_agents_health() -> AgentHealthResponse:
    """Get health status of all agents."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agents = _agent_store.list()

    return AgentHealthResponse(
        agents=[
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "status": a.status,
                "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                "current_initiatives": a.current_assignments,
            }
            for a in agents
        ],
        websocket_endpoint="/v1/host/agents/{agent_id}/stream",
    )


# --- Initiative Management ---

@router.post("/initiatives", response_model=InitiativeResponse)
async def create_initiative(body: InitiativeCreateRequest) -> InitiativeResponse:
    """Create a new initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.create(
        type=body.initiative_type,
        description=body.description,
        # Request priority is 0-100; the store holds 0.0-1.0.
        priority=body.priority / 100.0,
        timeout_seconds=body.timeout_seconds,
        dedup_key=body.dedup_key,
        entity_id=body.entity_id,
        preferred_agent_id=body.target_agent_id,
        context=body.context or None,
    )

    if _telemetry is not None:
        try:
            await _telemetry.touch("last_initiative_at")
        except Exception:
            pass

    try:  # timeline (v0.21.0)
        from colony_sidecar.events.journal import append_event
        append_event("initiative.generated", {
            "initiative_id": getattr(initiative, "id", None),
            "contact_id": body.entity_id,
            "summary": body.description,
            "initiative_type": body.initiative_type,
        })
    except Exception:
        logger.debug("journal initiative.generated failed", exc_info=True)

    return _initiative_to_response(initiative)


@router.get("/initiatives", response_model=InitiativeListResponse)
async def list_initiatives(
    status: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
) -> InitiativeListResponse:
    """List initiatives with optional filters."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiatives = _initiative_store.list(
        status=status,
        assigned_agent_id=agent_id,
        limit=limit,
    )

    return InitiativeListResponse(
        initiatives=[_initiative_to_response(i) for i in initiatives],
        total=len(initiatives),
    )


@router.get("/initiatives/{initiative_id}", response_model=InitiativeResponse)
async def get_initiative(initiative_id: str) -> InitiativeResponse:
    """Get initiative details."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    return _initiative_to_response(initiative)


@router.post("/initiatives/{initiative_id}/claim")
async def claim_initiative(
    initiative_id: str,
    body: InitiativeClaimRequest,
) -> Dict[str, Any]:
    """Claim an initiative for an agent."""
    if _initiative_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="System not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.status != "pending":
        raise HTTPException(status_code=400, detail=f"Initiative already {initiative.status}")

    agent = _agent_store.get(body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _initiative_store.assign(initiative_id, body.agent_id)

    return {"status": "claimed", "initiative_id": initiative_id, "agent_id": body.agent_id}

    return {"status": "claimed", "initiative_id": initiative_id, "agent_id": body.agent_id}


@router.post("/initiatives/{initiative_id}/complete")
async def complete_initiative(
    initiative_id: str,
    body: InitiativeCompleteRequest,
) -> Dict[str, Any]:
    """Mark initiative as completed."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.assigned_agent_id != body.agent_id:
        raise HTTPException(status_code=403, detail="Not assigned to this agent")

    _initiative_store.complete(initiative_id, body.agent_id, body.result.get("result"), body.result)

    return {"status": "completed", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/fail")
async def fail_initiative(
    initiative_id: str,
    body: InitiativeFailRequest,
) -> Dict[str, Any]:
    """Mark initiative as failed."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.assigned_agent_id != body.agent_id:
        raise HTTPException(status_code=403, detail="Not assigned to this agent")

    _initiative_store.fail(initiative_id, body.agent_id, body.error_message)

    return {"status": "failed", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/delegate")
async def delegate_initiative(
    initiative_id: str,
    body: InitiativeDelegateRequest,
) -> Dict[str, Any]:
    """Delegate initiative to another agent."""
    if _initiative_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="System not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    agent = _agent_store.get(body.target_agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Target agent not found")

    _initiative_store.update(
        initiative_id,
        assigned_agent_id=body.target_agent_id,
    )
    _initiative_store.log_history(
        initiative_id,
        action="delegated",
        agent_id=initiative.assigned_agent_id,
        details={"target_agent_id": body.target_agent_id, "reason": body.reason},
    )

    return {"status": "delegated", "initiative_id": initiative_id, "target_agent_id": body.target_agent_id}


@router.patch("/initiatives/{initiative_id}/priority")
async def update_initiative_priority(
    initiative_id: str,
    body: InitiativePriorityRequest,
) -> Dict[str, Any]:
    """Update initiative priority."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    _initiative_store.update(initiative_id, priority=body.priority)

    return {"status": "updated", "initiative_id": initiative_id, "priority": body.priority}


@router.post("/initiatives/{initiative_id}/retry")
async def retry_initiative(initiative_id: str) -> Dict[str, Any]:
    """Retry a failed initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.status != "failed":
        raise HTTPException(status_code=400, detail="Can only retry failed initiatives")

    _initiative_store.update(
        initiative_id,
        status="pending",
        assigned_agent_id=None,
        failed_reason=None,
        failed_at=None,
    )
    _initiative_store.log_history(initiative_id, action="retry", agent_id=None)

    return {"status": "pending", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/context/refresh", response_model=InitiativeResponse)
async def refresh_initiative_context(initiative_id: str) -> InitiativeResponse:
    """Rebuild the context snapshot for one initiative's subject (v0.16.0).

    Volatile initiative types (calendar, coding, system, agent_action)
    carry context that can go stale while the initiative sits in the
    queue. The agent calls this before acting when the snapshot's
    ``context_captured_at`` is older than the type's freshness TTL.
    Durable types return their stored snapshot unchanged.
    """
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    from colony_sidecar.initiatives.context_freshness import DURABLE, durability_for

    engine = None
    if _autonomy_loop is not None:
        registry = getattr(_autonomy_loop, "_registry", None)
        if registry is not None:
            engine = getattr(registry, "initiative_engine", None)

    fresh = None
    if engine is not None and hasattr(engine, "rebuild_context"):
        fresh = await engine.rebuild_context(initiative.type, initiative.entity_id)

    if fresh is None:
        if durability_for(initiative.type) == DURABLE:
            # Durable context: the creation-time snapshot is still valid.
            return _initiative_to_response(initiative)
        raise HTTPException(
            status_code=501,
            detail=(
                f"No per-entity context loader registered for volatile "
                f"type '{initiative.type}' — context cannot be refreshed"
            ),
        )

    # Volatile auto-close: if the refreshed snapshot shows the condition
    # has cleared (CI green again, service recovered, meeting over), the
    # initiative retires itself instead of surfacing stale work.
    condition_cleared = bool(fresh.pop("condition_cleared", False))
    if condition_cleared and initiative.is_active:
        _initiative_store.update(
            initiative_id,
            context=fresh,
            status="cancelled",
            cancelled_at=datetime.now(timezone.utc).isoformat(),
            cancelled_by="context_refresh",
            cancelled_reason="condition_cleared",
            stale_reason="condition_cleared",
        )
        updated = _initiative_store.get(initiative_id)
        return _initiative_to_response(updated or initiative)

    updated = _initiative_store.update(initiative_id, context=fresh)
    return _initiative_to_response(updated or initiative)


@router.delete("/initiatives/{initiative_id}")
async def cancel_initiative(initiative_id: str) -> Dict[str, Any]:
    """Cancel an initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    _initiative_store.cancel(initiative_id, cancelled_by="api")

    return {"status": "cancelled", "initiative_id": initiative_id}


# --- Initiative Helpers ---

def _initiative_to_response(initiative) -> InitiativeResponse:
    """Convert StoredInitiative to InitiativeResponse."""
    # Handle result as dict if it's a string or None
    result_dict = None
    if initiative.result:
        if isinstance(initiative.result, dict):
            result_dict = initiative.result
        elif isinstance(initiative.result, str):
            result_dict = {"result": initiative.result}

    from colony_sidecar.initiatives.context_freshness import durability_for

    return InitiativeResponse(
        id=initiative.id,
        initiative_type=initiative.type,
        # Title is the ACTION ("Check in with Jordan"), not the reason.
        # The rationale lives in the context dict.
        title=initiative.description[:100],
        description=initiative.description,
        priority=int(initiative.priority * 100) if initiative.priority else 0,
        status=initiative.status,
        timeout_seconds=initiative.timeout_seconds,
        # NULL for rows created before the v0.16.0 context migration.
        context=initiative.context or {},
        context_durability=durability_for(initiative.type),
        entity_id=initiative.entity_id,
        target_agent_id=initiative.assigned_agent_id or initiative.preferred_agent_id,
        assigned_agent_id=initiative.assigned_agent_id,
        dedup_key=initiative.dedup_key,
        result=result_dict,
        error_message=initiative.failed_reason,
        created_at=initiative.created_at.isoformat() if initiative.created_at else "",
        acknowledged_at=initiative.acknowledged_at.isoformat() if initiative.acknowledged_at else None,
        completed_at=initiative.completed_at.isoformat() if initiative.completed_at else None,
        failed_at=initiative.failed_at.isoformat() if initiative.failed_at else None,
        expires_at=initiative.expires_at.isoformat() if initiative.expires_at else None,
    )


# --- Task Management Endpoints (v0.7.10) ---

@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str) -> Dict[str, Any]:
    """Mark a task/goal as completed."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.complete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "task_id": task_id}


@router.post("/tasks/{task_id}/snooze")
async def snooze_task(
    task_id: str,
    hours: int = Body(24, ge=1, le=168),
    reason: str = Body(""),
) -> Dict[str, Any]:
    """Snooze a task for N hours (1-168)."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.snooze_task(task_id, hours, reason)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    snoozed_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"success": True, "task_id": task_id, "snoozed_until": snoozed_until}


@router.post("/tasks/{task_id}/dismiss")
async def dismiss_task(
    task_id: str,
    reason: str = Body("stale"),
) -> Dict[str, Any]:
    """Dismiss a task as no longer relevant."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.dismiss_task(task_id, reason)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "task_id": task_id, "reason": reason}


@router.post("/initiatives/{initiative_id}/respond")
async def respond_to_initiative(
    initiative_id: str,
    action: str = Body(...),
    details: Optional[dict] = Body(None),
    request: Request = None,
) -> Dict[str, Any]:
    """Record a response; approval authority still comes from transport.

    Initiative text/action is feedback, not proof that a human approved an
    external effect. A linked queue decision is therefore routed through the
    same immutable ApprovalRequest gate as the dedicated queue endpoints.
    """
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")
    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    # Validate and durably apply a linked approval decision before changing
    # initiative status, feedback, delivery state, or history. A denied
    # transport principal must leave no false evidence that the owner acted.
    job_id = getattr(initiative, "job_id", None)
    if job_id and action in {
        "approve", "approved", "dismiss", "dismissed", "reject", "rejected",
    }:
        try:
            from colony_sidecar.api.routers import task_queue as approval_router

            decision_details = details if isinstance(details, dict) else {}
            if action in {"approve", "approved"}:
                grant_body = decision_details.get("grant")
                grant = (
                    approval_router.BoundedGrantRequest(**grant_body)
                    if isinstance(grant_body, dict) else None
                )
                await approval_router.approve_job(
                    job_id,
                    approval_router.JobApproveRequest(
                        approval_request_id=decision_details.get("approval_request_id"),
                        expected_action_digest=decision_details.get("expected_action_digest"),
                        decision_id=decision_details.get("decision_id"),
                        grant=grant,
                    ),
                    request,
                )
            else:
                await approval_router.reject_job(
                    job_id,
                    approval_router.JobRejectRequest(
                        reason=str(
                            decision_details.get("reason")
                            or "owner_rejected_via_initiative"
                        ),
                        approval_request_id=decision_details.get("approval_request_id"),
                        expected_action_digest=decision_details.get("expected_action_digest"),
                        decision_id=decision_details.get("decision_id"),
                    ),
                    request,
                )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "approval_queue_unavailable",
                    "message": "linked approval could not be validated",
                },
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to sync initiative %s response to job %s: %s",
                initiative_id, job_id, exc,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "approval_decision_unavailable",
                    "message": "linked approval could not be validated",
                },
            ) from exc

    # Update status based on action
    status_map = {
        "acknowledged": "acknowledged",
        "dismissed": "cancelled",
        "snoozed": "pending",
        "approved": "acknowledged",
        "actioned": "completed",
    }
    new_status = status_map.get(action)
    if new_status:
        _initiative_store.update(initiative_id, status=new_status)

    # Close the loop into TypeFeedbackStore: the owner's response to an
    # initiative is exactly the outcome signal the per-type priority
    # multiplier learns from. Best-effort — feedback recording must never
    # fail the respond itself.
    _FEEDBACK_OUTCOME_MAP = {
        "approved": "actioned",
        "actioned": "actioned",
        "dismissed": "dismissed",
        "snoozed": "snoozed",
        "acknowledged": "acknowledged",
    }
    try:
        outcome = _FEEDBACK_OUTCOME_MAP.get(action)
        itype = getattr(initiative, "type", None)
        if _feedback_store is not None and outcome is not None and itype:
            _feedback_store.record(itype, outcome)
    except Exception as exc:
        logger.warning(
            "Failed to record type feedback for initiative %s (action=%s): %s",
            initiative_id, action, exc,
        )

    # If acknowledged, also clear from delivery bridge
    if action == "acknowledged" and _delivery_bridge is not None:
        if hasattr(_delivery_bridge, "acknowledge_delivery"):
            _delivery_bridge.acknowledge_delivery(initiative_id)

    _initiative_store.log_history(
        initiative_id,
        action=f"llm_{action}",
        agent_id="openclaw",
        details=details or {},
    )
    return {
        "success": True,
        "initiative_id": initiative_id,
        "status": new_status or initiative.status,
    }


# --- Agent Snapshot Endpoints ---

@router.get("/agent-snapshot", response_model=AgentSnapshotResponse)
async def agent_snapshot() -> AgentSnapshotResponse:
    """Return a comprehensive snapshot of Colony state for agent evaluation."""
    now = datetime.now(timezone.utc)

    # Telemetry
    thresholds = {"sync": 1.0, "tick": 1.0, "initiative": 4.0, "prefetch": 24.0}
    telemetry_dict = await _telemetry.to_dict(thresholds) if _telemetry else {}

    # Pending initiatives (top 20 by priority)
    pending = []
    if _initiative_store is not None:
        pending = _initiative_store.list(status=["pending"], limit=20)

    # Recently completed (top 10 by priority — store orders by priority DESC)
    recent = []
    if _initiative_store is not None:
        recent = _initiative_store.list(status=["completed"], limit=10)

    # Failed initiatives
    failed = []
    if _initiative_store is not None:
        failed = _initiative_store.list(status=["failed"], limit=10)

    # Compute last tick age
    tick_age = None
    if _telemetry is not None and _telemetry.last_tick_at is not None:
        tick_age = (now - _telemetry.last_tick_at).total_seconds() / 60

    # Flags: high-signal items the agent should know about
    flags = []
    if (telemetry_dict.get("silence_hours", {}).get("initiative") or 0) > 4:
        flags.append("long_initiative_silence")
    if failed:
        flags.append("failed_initiatives")
    if pending and any(i.priority > 0.8 for i in pending):
        flags.append("high_priority_pending")
    if tick_age and tick_age > 30:
        flags.append("stale_autonomy_loop")

    return AgentSnapshotResponse(
        timestamp=now.isoformat(),
        telemetry=telemetry_dict,
        pending_initiatives=[_map_initiative_to_schema(i) for i in pending],
        pending_count=len(pending),
        assigned_count=(
            _initiative_store.count(status=["assigned"]) if _initiative_store else 0
        ),
        failed_count=len(failed),
        recently_completed=[_map_initiative_to_schema(i) for i in recent],
        autonomy_mode=_autonomy_loop.config.mode.value if _autonomy_loop else "unknown",
        autonomy_running=_autonomy_loop.is_running if _autonomy_loop else False,
        last_tick_age_minutes=tick_age,
        flags=flags,
    )


@router.post("/agent-snapshot/record-outreach", response_model=RecordOutreachResponse)
async def record_outreach(body: RecordOutreachRequest) -> RecordOutreachResponse:
    """Record that the agent proactively messaged the owner."""
    now = datetime.now(timezone.utc)
    outreach_at = now.isoformat()
    if _telemetry is not None:
        await _telemetry.touch("last_agent_outreach_at")
        if _telemetry.last_agent_outreach_at is not None:
            outreach_at = _telemetry.last_agent_outreach_at.isoformat()
    logger.info(
        "Agent outreach recorded: agent=%s channel=%s reason=%s",
        body.agent_id, body.channel, body.reason,
    )
    try:  # timeline (v0.21.0)
        from colony_sidecar.events.journal import append_event
        append_event("outreach.sent", {
            "contact_id": getattr(body, "contact_id", None),
            "channel": body.channel,
            "reason": body.reason,
            "summary": body.reason,
        })
    except Exception:
        logger.debug("journal outreach.sent failed", exc_info=True)
    try:
        # Proactive outreach carries an already-resolved target contact; log
        # it outbound so reciprocity accounting stays whole. Skip entirely
        # without a contact (never attribute an outbound to a placeholder).
        _oc = getattr(body, "contact_id", None)
        if _comms_log is not None and _oc and _oc not in ("system", "default"):
            _comms_log.log(_oc, channel=body.channel or "direct",
                           direction="out", summary=(body.reason or "")[:300])
    except Exception:
        logger.debug("comms ledger outbound log failed", exc_info=True)
    return RecordOutreachResponse(
        recorded_at=now.isoformat(),
        last_agent_outreach_at=outreach_at,
    )


@router.get("/contacts/{contact_id}/landscape")
async def contact_landscape(contact_id: str) -> dict:
    """Full cross-channel communication landscape + outreach recommendation for a
    contact: channels used, when we last talked (each way), open follow-ups,
    cadence, and whether/how/when to (re)initiate under the owner-approval policy."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="contacts store not wired")
    contact = await _contacts_store.get(contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)

    def _p(ts):
        try:
            d = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
        except Exception:
            return None

    cadence_days = None
    overdue = False
    days_since = None
    first = _p(getattr(contact, "first_seen_at", None))
    last = _p(getattr(contact, "last_interaction_at", None))
    ic = int(getattr(contact, "interaction_count", 0) or 0)
    if last is not None:
        days_since = (now - last).total_seconds() / 86400.0
        if first is not None and ic > 1:
            cadence_days = max(0.5, min(90.0, (last - first).total_seconds() / 86400.0 / (ic - 1)))
            overdue = days_since > max(2.0, cadence_days * 1.5)

    channels = []
    try:
        for h in await _contacts_store.get_handles(contact_id):
            channels.append({"gateway": getattr(h, "gateway", ""), "address": getattr(h, "address", ""),
                             "is_primary": getattr(h, "is_primary", False)})
    except Exception:
        pass

    followups = []
    if _commitment_store is not None:
        try:
            _cl = _commitment_store.list(person_id=contact_id,
                                         status=["pending", "overdue"], limit=10)
            for c in _cl.get("commitments", []) if isinstance(_cl, dict) else (_cl or []):
                if c.get("description"):
                    followups.append(c["description"])
        except Exception:
            pass

    per_channel = _comms_log.last_per_channel(contact_id) if _comms_log else {}
    last_out = _comms_log.last_outbound(contact_id) if _comms_log else None
    history = _comms_log.history(contact_id, limit=10) if _comms_log else []

    from colony_sidecar.identity import get_owner_contact_id
    is_owner = (get_owner_contact_id() == contact_id)
    primary_ch = next((c["gateway"] for c in channels if c["is_primary"]),
                      channels[0]["gateway"] if channels else "")
    from colony_sidecar.contacts.comms import evaluate_outreach
    decision = evaluate_outreach(contact, is_owner=is_owner,
                                 last_outbound_ts=(last_out or {}).get("ts"),
                                 cadence_days=cadence_days, overdue=overdue,
                                 open_followups=followups, suggested_channel=primary_ch, now=now)
    return {
        "contact_id": contact_id, "display_name": getattr(contact, "display_name", None),
        "is_owner": is_owner, "trust_tier": getattr(contact, "trust_tier", None),
        "relationship_score": getattr(contact, "relationship_score", None),
        "channels": channels, "cadence_days": cadence_days, "days_since_last": days_since,
        "overdue": overdue, "last_per_channel": per_channel, "last_outbound": last_out,
        "open_followups": followups, "recent_history": history, "outreach": decision,
    }


@router.post("/session-report", response_model=SessionReportResponse)
async def session_report(body: SessionReportRequest) -> SessionReportResponse:
    """Store a session summary from the agent for future context retrieval."""
    if _session_report_store is None:
        raise HTTPException(
            status_code=501, detail="Session report store not initialized"
        )

    from colony_sidecar.sessions.reports import SessionReport

    # Parse ISO datetimes, ensuring timezone awareness
    def _parse_iso(iso_str: str) -> datetime:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    report = SessionReport(
        report_id=str(uuid.uuid4()),
        session_id=body.session_id,
        contact_id=body.contact_id,
        started_at=_parse_iso(body.started_at),
        ended_at=_parse_iso(body.ended_at) if body.ended_at else None,
        summary=body.summary,
        topics=body.topics,
        resolutions=body.resolutions,
        pending=body.pending,
        notified_user=body.notified_user,
        metadata=body.metadata,
    )
    await _session_report_store.add_report(report)
    return SessionReportResponse(stored=True, report_id=report.report_id)


@router.get("/context-digest", response_model=ContextDigestResponse)
async def context_digest(
    contact_id: Optional[str] = None,
    hours: int = Query(24, ge=1, le=168),
    initiative_limit: int = Query(10, ge=1, le=100),
) -> ContextDigestResponse:
    """Return a comprehensive context digest for agent session boot.

    Combines recent session reports, pending initiatives, system state,
    and outreach history into a single response.
    """
    now = datetime.now(timezone.utc)

    # Session reports
    session_reports = []
    if _session_report_store is not None and contact_id:
        reports = await _session_report_store.get_recent(
            contact_id, hours=hours, limit=10
        )
        session_reports = [
            ContextDigestSessionReport(
                report_id=r.report_id,
                started_at=r.started_at.isoformat() if r.started_at else "",
                ended_at=r.ended_at.isoformat() if r.ended_at else None,
                summary=r.summary,
                topics=r.topics,
                resolutions=r.resolutions,
                pending=r.pending,
                notified_user=r.notified_user,
            )
            for r in reports
        ]

    # Pending initiatives (reuse agent-snapshot logic)
    pending = []
    if _initiative_store is not None:
        pending = _initiative_store.list(status=["pending"], limit=initiative_limit)

    # System state (reuse agent-snapshot logic)
    thresholds = {"sync": 1.0, "tick": 1.0, "initiative": 4.0, "prefetch": 24.0}
    telemetry_dict = await _telemetry.to_dict(thresholds) if _telemetry else {}

    tick_age = None
    if _telemetry is not None and _telemetry.last_tick_at is not None:
        tick_age = (now - _telemetry.last_tick_at).total_seconds() / 60

    silence_flags = telemetry_dict.get("silence_hours", {})
    stale_flags = telemetry_dict.get("stale_flags", [])

    # Last outreach
    last_outreach = {"at": None, "reason": None}
    if _telemetry is not None and _telemetry.last_agent_outreach_at is not None:
        last_outreach = {
            "at": _telemetry.last_agent_outreach_at.isoformat(),
            "reason": None,
        }

    # Map initiatives (module-level helper extracted from agent-snapshot)
    system_state = AgentSnapshotSystemState(
        autonomy_running=_autonomy_loop.is_running if _autonomy_loop else False,
        mode=_autonomy_loop.config.mode.value if _autonomy_loop else "unknown",
        last_tick_age_minutes=tick_age,
        silence_hours=silence_flags,
        stale_flags=stale_flags,
    )

    return ContextDigestResponse(
        generated_at=now.isoformat(),
        contact_id=contact_id,
        session_reports=session_reports,
        pending_initiatives=[_map_initiative_to_schema(i) for i in pending],
        system_state=system_state,
        last_outreach=last_outreach,
    )


# --- WebSocket Endpoint ---

@router.websocket("/agents/{agent_id}/stream")
async def agent_websocket_stream(ws: WebSocket, agent_id: str) -> None:
    """WebSocket endpoint for real-time initiative delivery."""
    if _websocket_manager is None:
        await ws.close(code=1011, reason="WebSocket manager not initialized")
        return

    await _websocket_manager.handle_connection(ws, agent_id)

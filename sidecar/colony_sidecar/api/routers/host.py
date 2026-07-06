"""Colony sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Colony's intelligence as a plugin.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from colony_sidecar.goals.store import GoalNotFoundError
from colony_sidecar import get_state_dir

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
    CognitionCycleResponse,
    CognitionGap,
    CognitivePerformanceIndex,
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

# ---------------------------------------------------------------------------
# Module-level wiring — subsystems are injected by the server lifespan
# ---------------------------------------------------------------------------

_graph = None
_response_gate = None
_signal_collector = None
_embedder = None
_reasoning_loop = None
_consolidator = None
_event_subscribers: list[asyncio.Queue] = []


def broadcast_event(event: dict) -> None:
    """Push an event dict to all connected WebSocket subscribers.

    Called by the autonomy loop, signal collector, and other subsystems
    when state changes that the host should know about (proactive
    messages, briefings, anomalies, etc.).
    """
    for q in _event_subscribers:
        try:
            q.put_nowait(event)
        except Exception:
            pass  # Queue full or closed — drop and continue


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
    if _affect_store is not None:
        caps.append("affect")
    if _facts_store is not None:
        caps.append("shared_facts")
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

    This rebuilds the LLMRouter with the new tiers and updates the
    ReasoningLoop to use the reconfigured router.
    """
    global _reasoning_loop

    if body.llm is None:
        return HostConfigureResponse(configured=False)

    from colony_sidecar.router.tiers import build_tiers_from_host
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor

    try:
        tiers = build_tiers_from_host(body.llm)

        new_router = LLMRouter(tiers=tiers)
        set_llm_router(new_router)

        # Re-wire the reasoning loop with the new router
        if _reasoning_loop is not None:
            _reasoning_loop = ReasoningLoop(
                model=new_router,
                tools=ToolExecutor(graph_client=_graph),
            )

            set_reasoning_loop(_reasoning_loop)
            logger.info(
                "ReasoningLoop re-wired with host LLM config (provider=%s)",
                body.llm.get("provider", "unknown"),
            )

        # Persist config for restarts
        try:
            import json
            config_path = get_state_dir() / ".colony-llm-config.json"
            config_path.write_text(json.dumps(body.llm, indent=2))
            logger.info("LLM config persisted to %s", config_path)
        except Exception as exc:
            logger.warning("Failed to persist LLM config: %s", exc)

        models_info = body.llm.get("models", {})
        return HostConfigureResponse(
            configured=True,
            provider=body.llm.get("provider"),
            models=models_info,
        )
    except Exception as exc:
        logger.error("configure_host failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


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

    if not provider:
        return ModelListResponse(
            provider="",
            error="No LLM provider configured. Call POST /v1/host/configure first.",
        )

    if provider not in ("ollama", "local", "custom", "lmstudio", "vllm"):
        return ModelListResponse(
            provider=provider,
            error="Model listing is only supported for local providers (ollama, local, custom, lmstudio, vllm).",
        )

    discovered = discover_local_models(provider, base_url, api_key)
    if discovered:
        return ModelListResponse(
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
        provider=provider,
        base_url=base_url or None,
        error="Could not discover models from the local server. Is it running?",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HostHealthResponse)
async def health() -> HostHealthResponse:
    caps = supported_capabilities()
    notes: dict[str, str] = {}
    embed_model = ""
    stored_models: list[str] = []
    model_mismatch = False

    if _graph is not None:
        notes["memory"] = "ColonyGraph wired"
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
    if _embedder is not None:
        # Get embed model info
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            embed_model = _embedder._provider._config.model_id
        embed_note = f"EmbeddingPipeline wired (model={embed_model})"

        # Check for model mismatch
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
        except Exception:
            pass

        # Check embedder health
        try:
            hc = await _embedder.health_check()
            if hc.get("status") != "ok":
                embed_note += f" [health: {hc.get('status', 'unknown')}"
                if hc.get("error"):
                    embed_note += f": {hc['error']}"
                embed_note += "]"
        except Exception:
            pass

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
        notes["commitments"] = "CommitmentStore wired"
    if _affect_store is not None:
        notes["affect"] = "AffectStore wired"
    if _facts_store is not None:
        notes["shared_facts"] = "SharedFactsStore wired"
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
    if model_mismatch:
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
            if temporal_data.get("stale_flags"):
                health_status = "degraded"
            from colony_sidecar.api.schemas.host import TemporalMetrics
            temporal = TemporalMetrics(**temporal_data)
    except Exception:
        pass

    return HostHealthResponse(
        status=health_status,
        capabilities=caps,
        notes=notes,
        temporal=temporal,
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


@router.post("/memory/read", response_model=MemoryReadResponse)
async def memory_read(body: MemoryReadRequest) -> MemoryReadResponse:
    if _graph is None:
        return MemoryReadResponse(entries=[])
    try:
        entries_raw = await _graph.read_memories(
            person_id=body.person_id,
            memory_id=body.memory_id,
            limit=body.limit or 20,
        )
        entries = [
            MemoryEntry(
                id=e.get("id", str(uuid.uuid4())),
                content=e.get("content", ""),
                type=e.get("type"),
                strength=e.get("strength"),
                person_id=e.get("person_id"),
                entities=e.get("entities"),
                tags=e.get("tags"),
                created_at=e.get("created_at"),
                score=e.get("score"),
            )
            for e in entries_raw
        ]
        return MemoryReadResponse(entries=entries)
    except Exception as exc:
        logger.warning("memory_read failed: %s", exc)
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
        "neo4j_connected": neo4j_connected,
        "embeddings_ready": embeddings_ready,
        "vector_store_ready": vector_store_ready,
    }


@router.post("/memory/write", response_model=MemoryWriteResponse)
async def memory_write(body: MemoryWriteRequest) -> MemoryWriteResponse:
    if _graph is None:
        # Degrade gracefully to match the pattern used by the rest of
        # the router (list_insights, list_briefings, etc.): when the
        # underlying store isn't wired, accept the call and mark the
        # write as not persisted rather than raising 501.
        return MemoryWriteResponse(id="", accepted=False)
    try:
        # Fallback to context.contact_id if person_id not provided
        person_id = body.person_id or (body.context.contact_id if body.context else None)
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
        return MemoryWriteResponse(id="error", accepted=False)


@router.post("/memory/search", response_model=MemorySearchResponse)
async def memory_search(body: MemorySearchRequest) -> MemorySearchResponse:
    if _graph is None:
        return MemorySearchResponse(entries=[])
    try:
        results = await _graph.recall(
            query=body.query,
            limit=body.limit or 10,
            min_confidence=body.min_confidence if body.min_confidence is not None else 0.1,
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

        results = await store.search_cross_modal(
            col, query_vector,
            limit=body.limit,
            filter_modality=body.filter_modality,
            min_score=body.min_score,
        )

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

    task_id = str(uuid.uuid4())

    async def _run():
        from colony_sidecar.vector.migrate import migrate_tier
        try:
            result = await migrate_tier(store, _embedder, old_model_id=body.old_model_id, batch_size=body.batch_size)
            _migrate_results[task_id] = result
        except Exception as exc:
            logger.error("Migration failed: %s", exc)

    _migrate_results: dict = getattr(router, "_migrate_results", {})
    router._migrate_results = _migrate_results

    _spawn_task(_run())
    return MigrateResponse(task_id=task_id, status="started")


@router.get("/memory/migrate/{task_id}", response_model=MigrateResponse)
async def migrate_status(task_id: str) -> MigrateResponse:
    """Check the status of a running migration task."""
    _migrate_results: dict = getattr(router, "_migrate_results", {})
    result = _migrate_results.get(task_id)
    if result is None:
        return MigrateResponse(task_id=task_id, status="running")
    return MigrateResponse(
        task_id=task_id,
        status="completed",
        collections_migrated=result.collections_migrated,
        vectors_migrated=result.vectors_migrated,
        vectors_failed=result.vectors_failed,
        duration_s=round(result.duration_s, 2),
        errors=result.errors,
    )


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
    contact_id: Optional[str], override_tz: Optional[str] = None
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
    try:
        from colony_sidecar.util.session_safety import load_last_user_message_at
        last_owner = load_last_user_message_at()
        if last_owner:
            t_lines.append(f"Owner last messaged {_temporal.humanize_delta(last_owner)}.")
    except Exception:
        pass
    # Heads-up: time-sensitive items (overdue commitments + cadence-overdue contacts)
    heads = []
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
                           tz: Optional[str] = None) -> dict:
    """Always-fresh temporal brief for per-turn injection (no caching layer).

    The memory provider calls this every turn so the agent's Current Time
    block can never go stale inside a long-running session (the full
    /context/assemble result is session-cached by design; time must not be).
    """
    section = await _build_temporal_section(contact_id, tz)
    return {"id": section.id, "title": section.title, "body": section.body}


@router.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(body: ContextAssembleRequest) -> ContextAssembleResponse:
    # Context assembly pulls from identity + memory + goals + contacts + world model + skills
    sections: list[ContextSection] = []
    query_text = body.incoming_message.content if body.incoming_message else ""

    # --- Temporal Context: see _build_temporal_section ---
    try:
        cid = body.context.contact_id if body.context else None
        override_tz = getattr(body.context, "timezone", None) if body.context else None
        sections.append(await _build_temporal_section(cid, override_tz))
    except Exception as exc:
        logger.debug("context_assemble temporal section failed: %s", exc)

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

    # --- Memory ---
    if _graph is not None and query_text:
        try:
            results = await _graph.recall(
                query=query_text,
                limit=5,
            )
            if results:
                body_text = "\n".join(
                    f"- [{r.get('relevance', r.get('score', 0)):.2f}] {r.get('content', '')}"
                    for r in results
                )
                sections.append(ContextSection(
                    id="colony-memory",
                    title="Relevant Memories",
                    body=body_text,
                    priority=90,
                ))
        except Exception as exc:
            logger.warning("context_assemble memory search failed: %s", exc)

    # --- Active Goals ---
    if _goals_store is not None:
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
    if body.include_initiatives and _initiative_store is not None:
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
    if _briefings_engine is not None and body.context and body.context.contact_id:
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
    if _world_store is not None and query_text:
        try:
            entities = await _world_store.find_entities(query=query_text, limit=5)
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
    if _skills_registry is not None:
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

    # --- Pending Commitments ---
    contact_id = body.context.contact_id if body.context else None
    if _commitment_store is not None:
        try:
            commitments = _commitment_store.list(
                person_id=contact_id, status=["pending"], limit=5,
            )
            overdue = _commitment_store.get_overdue()
            if contact_id:
                overdue = [c for c in overdue if c.get("person_id") == contact_id]
            all_comms = (commitments if isinstance(commitments, list) else commitments.get("commitments", [])) + overdue[:5]
            if all_comms:
                lines = []
                for c in all_comms:
                    status_tag = "[OVERDUE]" if c.get("status") == "overdue" or (c.get("due_at") and c.get("status") == "pending") else "[pending]"
                    due = f" (due: {c.get('due_at', '')})" if c.get('due_at') else ""
                    lines.append(f"- {status_tag} {c.get('description', '')}{due}")
                sections.append(ContextSection(
                    id="colony-commitments",
                    title="Pending Commitments",
                    body="\n".join(lines),
                    priority=72,
                ))
        except Exception as exc:
            logger.warning("context_assemble commitments failed: %s", exc)

    # --- Affect State ---
    if _affect_store is not None and contact_id:
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
    if _contacts_store is not None and contact_id:
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
    if _relationship_profiler is not None and contact_id:
        try:
            from colony_sidecar.identity import get_owner_contact_id
            if contact_id != (get_owner_contact_id() or ""):
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
    if _preference_learner is not None and contact_id:
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
        except Exception as exc:
            logger.debug("context_assemble owner preferences failed: %s", exc)

    # --- Standing boundaries (owner directives: MUST NOT / MUST) ---
    # Injected for every context so the reasoner is always aware of the owner's
    # binding boundaries. This is the SOFT layer; the DirectiveGuard hard-gate
    # at each action chokepoint is the enforced floor.
    if _directive_manager is not None:
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
    if _engagement_store is not None and contact_id:
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
    if _comms_log is not None and _contacts_store is not None and contact_id:
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
                        _cm = _commitment_store.list(person_id=contact_id, status=["pending"], limit=5)
                        if _cm:
                            _descs = [c.get("description", "") for c in _cm if c.get("description")]
                            if _descs:
                                _bits.append("Open follow-ups: " + "; ".join(_descs[:3]) + ".")
                    except Exception:
                        pass
                if _bits:
                    from colony_sidecar.identity import get_owner_contact_id, get_owner_name
                    _is_owner = (get_owner_contact_id() == contact_id)
                    if not _is_owner:
                        _bits.append("Proactively reaching out to them needs %s's approval first." % get_owner_name())
                    sections.append(ContextSection(
                        id="colony-comms-landscape",
                        title="Communication landscape",
                        body=" ".join(_bits),
                        priority=83,
                    ))
        except Exception as exc:
            logger.debug("context_assemble comms landscape failed: %s", exc)

    # --- Shared Facts ---
    if _facts_store is not None and contact_id:
        try:
            facts_result = _facts_store.list_facts(contact_id=contact_id, limit=5)
            facts = facts_result if isinstance(facts_result, list) else facts_result.get("facts", [])
            if facts:
                lines = [f"- [{f.get('confidence', 0):.0%}] {f['fact']}" for f in facts]
                sections.append(ContextSection(
                    id="colony-shared-facts",
                    title="Known Facts About Contact",
                    body="\n".join(lines),
                    priority=70,
                ))
        except Exception as exc:
            logger.warning("context_assemble shared facts failed: %s", exc)

    # --- Unresolved Surprises ---
    if _surprise_store is not None:
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

    return ContextAssembleResponse(sections=sections)


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

@router.post("/reasoning/turn", response_model=ReasoningTurnResponse)
async def reasoning_turn(body: ReasoningTurnRequest) -> ReasoningTurnResponse:
    if _reasoning_loop is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    session_id = body.context.session_id if body.context and body.context.session_id else str(uuid.uuid4())

    result = await _reasoning_loop.run_turn(
        session_id=session_id,
        messages=messages,
        available_tools=body.available_tools or None,
        model_override=body.model_override or None,
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
async def tools_invoke(body: ToolInvokeRequest) -> ToolInvokeResponse:
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


@router.post("/signals/ingest", response_model=SignalIngestResponse)
async def signals_ingest(body: SignalIngestRequest) -> SignalIngestResponse:
    if _signal_collector is None:
        return SignalIngestResponse(accepted=True, signals_recorded=0)

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

    # Raw signals from external sources
    if body.signals:
        try:
            for sig in body.signals:
                await _signal_collector.ingest_raw(sig)
            recorded += len(body.signals)
        except Exception as exc:
            logger.warning("signals_ingest raw signals failed: %s", exc)

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

@router.post("/turns/sync", response_model=TurnSyncResponse)
async def turns_sync(body: TurnSyncRequest) -> TurnSyncResponse:
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
                _resolved_human_sender = True
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

    # Best-effort: store turn metadata in the graph if available
    graph_ok = False
    if _graph is not None:
        try:
            await _graph.record_turn(
                session_id=body.context.session_id,
                contact_id=body.context.contact_id,
                topics=body.topics,
                entities=body.entities,
                tools_used=body.tools_used,
                summary=body.summary,
            )
            graph_ok = True
        except Exception as exc:
            logger.warning("turns_sync failed: %s", exc)

    # Context provenance: record this turn's entities under its conversation context, so a
    # later reply in a DIFFERENT context that surfaces an entity known only from here can be
    # flagged as a cross-context leak. Entities come from the host plus rule-based NER on the
    # incoming message (what the other party brought up = what belongs to this conversation).
    if _context_provenance is not None:
        ents = list(body.entities or [])
        _user_text = getattr(body.user_message, "content", "") if body.user_message else ""
        if _user_text:
            _extractor = _get_conversation_extractor()
            if _extractor is not None:
                try:   # extraction is additive; a failure here must not drop host entities
                    _src = body.context.turn_id or body.context.session_id or "turn"
                    _res = await _extractor.extract(_user_text, _src)
                    ents += [getattr(c, "text", None) or getattr(c, "name", "")
                             for c in getattr(_res, "entities", [])]
                except Exception:
                    logger.debug("provenance entity extraction failed", exc_info=True)
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
            try:
                _existing = _commitment_store.get_pending_for_person(body.context.contact_id) or []
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
                and _facts_store is not None and not _is_system_turn):
            _spawn_task(_run_tom_extraction(
                conversation_text=body.summary or "",
                contact_id=body.context.contact_id,
                session_id=body.context.session_id,
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
            append_event("conversation.turn", {
                "contact_id": body.context.contact_id,
                "session_id": body.context.session_id,
                "channel_id": body.context.channel_id,   # cross-channel provenance for the timeline / handoff
                "summary": (body.summary or "")[:300],
                "topics": (body.topics or [])[:10],
                "tools_used": (body.tools_used or [])[:20],
            })
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

    return TurnSyncResponse(accepted=True, continuity_updated=graph_ok, skipped_reason=None if graph_ok else "no_graph_store")


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

@router.post("/safety/check", response_model=SafetyCheckResponse)
@router.post("/response-gate/check", response_model=SafetyCheckResponse, include_in_schema=False)
async def safety_check(body: SafetyCheckRequest) -> SafetyCheckResponse:
    if _response_gate is None:
        return SafetyCheckResponse(decision="pass", blocked=False)

    try:
        from colony_sidecar.gate.models import GatePayload
        from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier
        payload = GatePayload(
            response_text=body.response_text,
            incoming_message_text=getattr(body, "incoming_message_text", ""),
            target_gateway=getattr(body, "target_gateway", ""),
            target_contact_id=getattr(body, "contact_id", ""),
            session_id=getattr(body, "session_id", ""),
            turn_id=getattr(body, "turn_id", ""),
            trust_tier=getattr(body, "trust_tier", TrustTier.REGULAR),
            mentioned_entities=frozenset(getattr(body, "mentioned_entities", []) or []),
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
        logger.warning("safety_check failed — passing through: %s", exc)
        return SafetyCheckResponse(decision="pass", blocked=False)


# ---------------------------------------------------------------------------
# Events (WebSocket)
# ---------------------------------------------------------------------------

@router.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()

    # Read auth message (may include lastEventId for reconnect replay)
    last_event_id: Optional[str] = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        import json as _json
        msg = _json.loads(raw)
        if msg.get("type") != "auth":
            await ws.close(code=4001, reason="Expected auth message")
            return
        token = msg.get("token", "")
        expected = os.environ.get("COLONY_API_KEY", "")
        if not expected:
            # Fail closed: without a configured key we cannot authenticate
            # event-stream subscribers, and this socket carries live state
            # changes. Operators must set COLONY_API_KEY to enable it.
            await ws.close(
                code=4003,
                reason="COLONY_API_KEY not set on server",
            )
            return
        if not hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        ):
            await ws.close(code=4003, reason="Invalid API key")
            return
        # Client sends lastEventId (ISO timestamp) to replay missed events
        last_event_id = msg.get("lastEventId")
    except asyncio.TimeoutError:
        await ws.close(code=4001, reason="Auth timeout")
        return
    except Exception:
        await ws.close(code=4001, reason="Invalid auth")
        return

    # Replay missed events if client provided lastEventId
    if last_event_id:
        try:
            from colony_sidecar.events.journal import replay_events
            result = replay_events(since=last_event_id, limit=500)
            for event in result["events"]:
                await ws.send_json({
                    "type": event["type"],
                    "occurred_at": event["recordedAt"],
                    "payload": event.get("data", {}),
                    "seq": event["seq"],
                })
            if result["events"]:
                await ws.send_json({
                    "type": "replay_complete",
                    "replayedCount": len(result["events"]),
                    "lastSeq": result["lastSeq"],
                })
        except Exception:
            logging.getLogger(__name__).debug(
                "Event replay failed for lastEventId=%s", last_event_id, exc_info=True
            )

    q: asyncio.Queue = asyncio.Queue()
    _event_subscribers.append(q)
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            _event_subscribers.remove(q)
        except ValueError:
            pass


@router.get("/events/replay")
async def events_replay(
    since: str = Query(..., description="ISO 8601 timestamp — replay events after this time"),
    limit: int = Query(500, ge=1, le=1000, description="Max events to return"),
    types: Optional[str] = Query(None, description="Comma-separated event type filter"),
) -> dict:
    """Replay journal events for disconnected clients.

    Returns events recorded after ``since`` in sequential order.
    Use ``Last-Event-Id`` from a previous WebSocket frame or the
    ``recordedAt`` timestamp of the last event you processed.
    """
    from colony_sidecar.events.journal import replay_events

    type_list = [t.strip() for t in types.split(",")] if types else None
    return replay_events(since=since, limit=limit, types=type_list)


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
        return GoalListResponse(goals=[])
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
        logger.warning("list_goals failed: %s", exc)
        return GoalListResponse(goals=[])


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
                goal = _goals_store.block_goal(goal_id, reason=body.notes or "Blocked via API")
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


@router.post("/response-guard/check")
async def response_guard_check(body: ResponseGuardCheckRequest) -> Dict[str, Any]:
    """Evaluate an outbound reply before sending. Returns {decision, mode, findings}.
    When no guard is configured, allows everything (the gate is opt-in)."""
    if _response_guard is None:
        return {"decision": "allow", "mode": "disabled", "findings": []}
    from colony_sidecar.gate.response_guard import GuardMode
    mode = None
    if body.mode:
        try:
            mode = GuardMode(body.mode)
        except ValueError:
            mode = None
    result = await _response_guard.evaluate(
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
        authorized=bool(body.authorized),
    )
    return result.to_dict()


@router.get("/response-guard/audit")
async def response_guard_audit(limit: int = 50, authorized: Optional[bool] = None) -> Dict[str, Any]:
    """Review cross-context guard events, split by authorized (owner-directed) vs not — used to
    judge, in shadow, whether the classifier separates valid transfers from leaks before enforce."""
    audit = getattr(_response_guard, "_audit", None) if _response_guard is not None else None
    if audit is None:
        return {"summary": {"total": 0}, "events": []}
    return {"summary": audit.summary(), "events": audit.recent(limit=limit, authorized=authorized)}



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
    # Read the whole (retention-bounded) window, then keep the most RECENT N.
    # replay_events caps at `limit` chronologically (oldest first), so a small
    # limit there would return the START of the window, not recent activity.
    raw = replay_events(since_iso, limit=500, types=type_list)

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


@router.get("/briefings", response_model=BriefingListResponse)
async def list_briefings(limit: int = 10) -> BriefingListResponse:
    if _briefings_engine is None:
        return BriefingListResponse(briefings=[])
    try:
        briefings = _briefings_engine.get_recent(limit=limit)
        return BriefingListResponse(briefings=[BriefingResponse(**b) for b in briefings])
    except Exception as exc:
        logger.warning("list_briefings failed: %s", exc)
        return BriefingListResponse(briefings=[])


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
    try:
        entities = await _world_store.find_entities(query=body.query, limit=body.limit or 10)
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
    try:
        import base64
        content = base64.b64decode(body.content)
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


@router.post("/cognition/cycle", response_model=CognitionCycleResponse)
async def cognition_cycle(body: CognitionCycleRequest) -> CognitionCycleResponse:
    if _metalearner is None:
        return CognitionCycleResponse()
    try:
        result = await _metalearner.run_cycle()
        cpi = None
        if result and hasattr(result, "cpi") and result.cpi:
            c = result.cpi
            cpi = CognitivePerformanceIndex(
                overall=getattr(c, "overall", 0.0),
                memory=getattr(c, "memory", 0.0),
                reasoning=getattr(c, "reasoning", 0.0),
                social=getattr(c, "social", 0.0),
                autonomy=getattr(c, "autonomy", 0.0),
            )
        gaps = []
        if result and hasattr(result, "gaps"):
            for g in result.gaps:
                gaps.append(CognitionGap(
                    gap_id=getattr(g, "id", str(uuid.uuid4())),
                    domain=getattr(g, "domain", "general"),
                    severity=getattr(g, "severity", 0.0),
                    description=getattr(g, "description"),
                ))
        adjustments = []
        if result and hasattr(result, "adjustments"):
            for a in result.adjustments:
                adjustments.append({"domain": getattr(a, "domain", ""), "action": getattr(a, "action", "")})
        return CognitionCycleResponse(cpi=cpi, gaps=gaps, adjustments=adjustments)
    except Exception as exc:
        logger.warning("cognition_cycle failed: %s", exc)
        return CognitionCycleResponse()


@router.get("/cognition/cpi", response_model=CognitivePerformanceIndex)
async def get_cpi() -> CognitivePerformanceIndex:
    if _metalearner is None:
        return CognitivePerformanceIndex()
    try:
        cpi = await _metalearner.evaluate()
        return CognitivePerformanceIndex(
            overall=getattr(cpi, "overall", 0.0),
            memory=getattr(cpi, "memory", 0.0),
            reasoning=getattr(cpi, "reasoning", 0.0),
            social=getattr(cpi, "social", 0.0),
            autonomy=getattr(cpi, "autonomy", 0.0),
        )
    except Exception as exc:
        logger.warning("get_cpi failed: %s", exc)
        return CognitivePerformanceIndex()


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

def set_learner(learner) -> None:
    global _learner
    _learner = learner


@router.post("/learning/correction")
async def submit_correction(body: LearningCorrectionRequest) -> dict:
    if _learner is None:
        return {"accepted": False}
    try:
        await _learner.ingest_correction({
            "original": body.original,
            "correction": body.correction,
            "component": body.component,
            "sender_id": body.context.contact_id if body.context else "unknown",
        })
        return {"accepted": True}
    except Exception as exc:
        logger.warning("submit_correction failed: %s", exc)
        return {"accepted": False}


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


_context_provenance = None


def set_context_provenance_store(store):
    global _context_provenance
    _context_provenance = store


_channel_store = None


def set_channel_store(store) -> None:
    """Wire the channel registration store so turn traffic keeps it alive."""
    global _channel_store
    _channel_store = store


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
                                 refresh: bool = False) -> dict:
    """One contact's RelationshipBrief (standing, psyche, approach guidance).
    ``refresh=true`` recomputes from the live stores."""
    if _relationship_profiler is None:
        return {"available": False}
    try:
        brief = None if refresh else _relationship_profiler.cached(contact_id)
        if brief is None:
            brief = await _relationship_profiler.profile(contact_id)
        if brief is None:
            raise HTTPException(status_code=404,
                                detail=f"no profile for {contact_id!r}")
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
async def directed_approve(task_id: str, body: dict = Body(default={})) -> dict:
    if _directed_service is None:
        return {"ok": False, "reason": "directed_not_wired"}
    task = _directed_service.approve(
        task_id, approved_by=(body or {}).get("approved_by", "owner"),
        standing=bool((body or {}).get("standing")))
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


def set_sandbox(s) -> None:
    global _sandbox
    _sandbox = s


def set_connector_manager(m) -> None:
    global _connector_manager
    _connector_manager = m


@router.get("/self")
async def get_self_model() -> dict:
    """Self-model: per-domain competence, live load, trust stages."""
    if _self_model is None:
        return {"available": False}
    try:
        out = {"available": True}
        out.update(_self_model.status())
        out["brief"] = _self_model.brief()
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
async def get_autonomy_posture() -> dict:
    """Effective autonomy posture: the active COLONY_AUTONOMY_PRESET (if any)
    and the resolved value of every preset-managed mode flag, as the RUNNING
    process sees them. This is what `colony doctor` reads so plist/unit-pinned
    env is never invisible to diagnostics."""
    try:
        from colony_sidecar.util.autonomy_preset import snapshot
        return {"available": True, "posture": snapshot()}
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


@router.get("/projects")
async def list_projects(status: str = "", limit: int = 30) -> dict:
    if _project_engine is None:
        return {"available": False, "projects": []}
    try:
        from colony_sidecar.projects.models import projects_mode
        items = _project_engine.store.list_projects(status=status or None,
                                                    limit=limit)
        return {"available": True, "count": len(items),
                "mode": projects_mode(),
                "projects": [p.to_row() for p in items]}
    except Exception as exc:
        return {"available": True, "error": str(exc), "projects": []}


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    if _project_engine is None:
        return {"available": False}
    out = _project_engine.project_status(project_id)
    return {"available": True, **(out or {"error": "not_found"})}


@router.post("/projects")
async def create_project(body: dict = Body(default={})) -> dict:
    """Owner-directed project creation (boundary-gated; planning happens on
    the next autonomy tick; step dispatch carries its own gates)."""
    if _project_engine is None:
        return {"ok": False, "reason": "projects_not_wired"}
    objective = (body or {}).get("objective", "").strip()
    project, reason = _project_engine.create_project(
        objective, title=(body or {}).get("title", ""),
        source=(body or {}).get("source", "owner"))
    return {"ok": project is not None, "reason": reason,
            "project": project.to_row() if project else None}


@router.post("/projects/{project_id}/abandon")
async def abandon_project(project_id: str, body: dict = Body(default={})) -> dict:
    if _project_engine is None:
        return {"ok": False, "reason": "projects_not_wired"}
    project = _project_engine.abandon(
        project_id, reason=(body or {}).get("reason", "owner_request"))
    return {"ok": project is not None,
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
async def run_sandbox(body: dict = Body(default={})) -> dict:
    """Owner surface: run a script in the sandbox. Owner-directed runs auto-run
    within default limits; still boundary-checked and journaled. The caller
    cannot widen containment (limits are server-side)."""
    if _sandbox is None:
        return {"ran": False, "reason": "sandbox_not_wired"}
    b = body or {}
    return _sandbox.run(
        b.get("script", ""),
        lang=b.get("lang", "python"),
        purpose=b.get("purpose", ""),
        owner_directed=bool(b.get("owner_directed", True)),
        approved=bool(b.get("approved", False)))


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
async def list_proposals(status: str = "", limit: int = 30) -> dict:
    """List proposals Colony has generated (observability)."""
    if _proposal_store is None:
        return {"available": False, "proposals": []}
    try:
        items = _proposal_store.list(status=status or None, limit=limit)
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
async def get_owner_preferences() -> dict:
    """Return the owner's learned communication preferences and rendered brief."""
    if _preference_learner is None:
        return {"available": False, "brief": "", "preferences": []}
    prefs = await _preference_learner.get_all_preferences()
    return {
        "available": True,
        "brief": _preference_learner.build_brief(),
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
async def learn_owner_preference(body: dict) -> dict:
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
async def enriched_context(body: EnrichedContextRequest) -> EnrichedContextResponse:
    """Pull from all intelligence systems to build enriched context.

    This is the one-stop endpoint for context assembly — it queries
    memory, relationships, goals, world model, insights, and style
    in parallel and returns assembled sections.
    """
    import asyncio

    sections: list[ContextSection] = []
    contact_id = body.context.contact_id if body.context else None
    features = body.features or {}
    msg = body.message

    # Collect context from all available systems in parallel
    tasks: dict[str, Any] = {}

    # 1. Memory search
    if _graph is not None:
        async def _mem():
            try:
                results = await _graph.recall(query=msg, limit=5)
                return ("memory", results)
            except Exception:
                return ("memory", [])
        tasks["memory"] = _mem()

    # 2. Contact / relationship
    if _contacts_store is not None and contact_id and features.get("relationships", True):
        async def _contact():
            try:
                c = await _contacts_store.get(contact_id)
                return ("contact", c)
            except Exception:
                return ("contact", None)
        tasks["contact"] = _contact()

    # 3. Contact style
    if _contacts_store is not None and contact_id and features.get("style", True):
        async def _style():
            try:
                s = await _contacts_store.get_style(contact_id)
                return ("style", s)
            except Exception:
                return ("style", None)
        tasks["style"] = _style()

    # 4. Active goals
    if _goals_store is not None and features.get("goals", True):
        async def _goals():
            try:
                g = _goals_store.list_goals(person_id=contact_id, status="active")
                return ("goals", g)
            except Exception:
                return ("goals", [])
        tasks["goals"] = _goals()

    # 5. World model entities
    if _world_store is not None and features.get("worldModel", True):
        async def _world():
            try:
                e = await _world_store.find_entities(query=msg, limit=5)
                return ("world", e)
            except Exception:
                return ("world", [])
        tasks["world"] = _world()

    # 6. Recent insights
    if _connection_discoverer is not None and features.get("insights", True):
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
    if _briefings_engine is not None and features.get("briefings", False):
        async def _briefings():
            try:
                briefings = _briefings_engine.get_recent(limit=3)
                return ("briefings", briefings or [])
            except Exception:
                return ("briefings", [])
        tasks["briefings"] = _briefings()

    # 9. Known contacts (top N — useful when the agent references someone
    # not tied to the current contact_id).
    if _contacts_store is not None and features.get("contactsList", False):
        async def _contacts_list():
            try:
                contacts = await _contacts_store.list()
                return ("contactsList", contacts[:8] if contacts else [])
            except Exception:
                return ("contactsList", [])
        tasks["contactsList"] = _contacts_list()

    # 10. Cognition snapshot (CPI — self-awareness metric)
    if _metalearner is not None and features.get("cognition", False):
        async def _cognition():
            try:
                cpi = await _metalearner.evaluate()
                return ("cognition", cpi)
            except Exception:
                return ("cognition", None)
        tasks["cognition"] = _cognition()

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
    if _commitment_store is not None and contact_id and features.get("commitments", True):
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
    if _affect_store is not None and contact_id and features.get("affect", True):
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
    if _facts_store is not None and contact_id and features.get("shared_facts", True):
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
    if _surprise_store is not None and contact_id and features.get("surprises", True):
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


def set_reranker(reranker) -> None:
    global _reranker
    _reranker = reranker


def set_session_store(store) -> None:
    global _session_store
    _session_store = store


def set_task_queue(queue) -> None:
    global _task_queue
    _task_queue = queue


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
    """Create a new commitment."""
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
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

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


@router.patch("/commitments/{commitment_id}", response_model=CommitmentResponse)
async def update_commitment(commitment_id: str, body: CommitmentUpdateRequest) -> CommitmentResponse:
    """Update a commitment."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

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

    return CommitmentResponse(**result)


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
    total = len(events)  # approximate for paginated view
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

@router.post("/mind/facts", response_model=SharedFactResponse, status_code=status.HTTP_201_CREATED)
async def create_shared_fact(body: SharedFactCreateRequest) -> SharedFactResponse:
    """Add a shared fact about what a contact knows."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
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
async def update_shared_fact(fact_id: str, body: SharedFactUpdateRequest) -> SharedFactResponse:
    """Update a shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    result = _facts_store.update_fact(
        fact_id,
        confidence=body.confidence,
        expires_at=body.expires_at,
        fact=body.fact,
        metadata=body.metadata,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    return SharedFactResponse(**result)


@router.delete("/mind/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shared_fact(fact_id: str):
    """Delete a shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    if not _facts_store.delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Shared fact not found")


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
) -> None:
    """Background task: extract affect + facts from a conversation turn."""
    if _tom_extractor is None:
        return
    # Affect
    try:
        affect = await _tom_extractor.extract_affect(
            conversation_text, contact_id, session_id=session_id,
        )
        if affect and _affect_store is not None:
            _affect_store.create_event(
                contact_id=affect["contact_id"],
                valence=affect["valence"],
                arousal=affect["arousal"],
                source="inferred",
                trigger=affect.get("trigger"),
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
        if facts and _facts_store is not None:
            for f in facts:
                _facts_store.create_fact(
                    contact_id=f["contact_id"],
                    fact=f["fact"],
                    source=f["source"],
                    confidence=f["confidence"],
                )
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
        if eng and _engagement_store is not None:
            _engagement_store.update_from_observation(
                contact_id,
                ocean=eng.get("ocean"),
                style=eng.get("style"),
                motivators=eng.get("motivators"),
                topics=eng.get("topics"),
                avoid=eng.get("avoid"),
            )
    except Exception:
        logger.debug("ToM engagement extraction failed", exc_info=True)


@router.post("/tom/extract", response_model=TomExtractResponse)
async def extract_tom(body: TomExtractRequest) -> TomExtractResponse:
    """Manually trigger ToM extraction for a conversation snippet."""
    if _tom_extractor is None:
        raise HTTPException(status_code=501, detail="ToM extraction not available (no LLM router)")
    # A manual ToM write must target a real person, same as the affect/facts
    # POST paths: a stale group contact_id here would pollute the wrong
    # person's psyche (docs/RELATIONSHIPS.md #5).
    await _require_person_contact(body.contact_id)

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
                _facts_store.create_fact(
                    contact_id=f["contact_id"],
                    fact=f["fact"],
                    source=f["source"],
                    confidence=f["confidence"],
                )

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
) -> Dict[str, Any]:
    """Record LLM response to an initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")
    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

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

    # If acknowledged, also clear from delivery bridge
    if action == "acknowledged" and _delivery_bridge is not None:
        if hasattr(_delivery_bridge, "acknowledge_delivery"):
            _delivery_bridge.acknowledge_delivery(initiative_id)

    # v0.17.0: sync the owner's response to the approval-gated job, if any.
    # The autonomy loop records job_id on the initiative after submission;
    # approving/dismissing the initiative resolves the BLOCKED job too.
    job_id = getattr(initiative, "job_id", None)
    if job_id and action in {"approve", "approved", "dismiss", "dismissed", "reject", "rejected"}:
        try:
            from colony_sidecar.task_queue.models import JobStatus
            from colony_sidecar.task_queue.queue_manager import TaskQueueManager

            queue = TaskQueueManager.get_instance().queue
            job = await queue.get_job(job_id)
            if job is not None and job.status == JobStatus.BLOCKED:
                now_iso = datetime.now(timezone.utc).isoformat()
                if action in {"approve", "approved"}:
                    await queue.update_job_status(
                        job_id,
                        JobStatus.QUEUED,
                        reason="owner_approved_via_initiative",
                        tags={"approved_by": "owner", "approved_at": now_iso},
                    )
                else:
                    await queue.update_job_status(
                        job_id,
                        JobStatus.CANCELLED,
                        reason="owner_rejected_via_initiative",
                        tags={"rejected_by": "owner", "rejected_at": now_iso},
                    )
        except RuntimeError:
            pass  # task queue not initialized — nothing to sync
        except Exception as exc:
            logger.warning(
                "Failed to sync initiative %s response to job %s: %s",
                initiative_id, job_id, exc,
            )

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
            for c in _commitment_store.list(person_id=contact_id, status=["pending"], limit=10):
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

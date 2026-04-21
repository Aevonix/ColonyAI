"""Colony sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Colony's intelligence as a plugin.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from colony_sidecar.goals.store import GoalNotFoundError

from colony_sidecar.api.schemas.host import (
    HostConfigureRequest,
    HostConfigureResponse,
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
    ContactListResponse,
    ContactResponse,
    ContactStyleRequest,
    ContactStyleResponse,
    ContextAssembleRequest,
    ContextAssembleResponse,
    ContextSection,
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
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryWriteRequest,
    MemoryWriteResponse,
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
)

logger = logging.getLogger(__name__)


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
            _reasoning_loop = ReasoningLoop(model=new_router, tools=ToolExecutor())

            set_reasoning_loop(_reasoning_loop)
            logger.info(
                "ReasoningLoop re-wired with host LLM config (provider=%s)",
                body.llm.get("provider", "unknown"),
            )

        # Persist config for restarts
        try:
            import json
            from pathlib import Path
            config_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-llm-config.json"
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
        notes["autonomy"] = "AutonomyLoop wired (not started)"
    if _session_store is not None:
        notes["sessions"] = "InMemorySessionStore wired"
    if _task_queue is not None:
        notes["task_queue"] = "TaskQueueManager wired"

    health_status = "ok"
    if model_mismatch:
        health_status = "degraded"

    return HostHealthResponse(
        status=health_status,
        capabilities=caps,
        notes=notes,
    )


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
        memory_id = await _graph.store_memory(
            content=body.content,
            person_id=body.person_id,
            memory_type=body.type or "episodic",
            entities=body.entities or [],
            importance=body.strength if body.strength is not None else 1.0,
            metadata={"tags": body.tags} if body.tags else None,
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
                score=float(e["relevance"]) if "relevance" in e and e["relevance"] is not None else e.get("score"),
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

    asyncio.create_task(_run())
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

    asyncio.create_task(_run())
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

    asyncio.create_task(_run())
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


@router.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(body: ContextAssembleRequest) -> ContextAssembleResponse:
    # Context assembly pulls from identity + memory + goals + contacts + world model + skills
    sections: list[ContextSection] = []
    query_text = body.incoming_message.content if body.incoming_message else ""

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
            title="Colony Identity",
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
                import asyncio as _aio
                content = ""
                if incoming and incoming.content:
                    content = incoming.content[:500]
                _aio.create_task(trigger_cognition(
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

    # Fire cognition trigger (best-effort, non-blocking)
    try:
        from colony_sidecar.cognition.trigger import trigger_cognition, _cognition_enabled
        if _cognition_enabled():
            import asyncio
            asyncio.create_task(trigger_cognition(
                trigger_type="turn_sync",
                context={
                    "conversation_text": body.summary or "",
                    "person_id": body.context.contact_id,
                    "session_id": body.context.session_id,
                },
                priority="normal",
            ))
    except Exception:
        logger.debug("cognition trigger from turn_sync failed", exc_info=True)

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
        if expected and not hmac.compare_digest(
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
async def list_contacts() -> ContactListResponse:
    if _contacts_store is None:
        return ContactListResponse(contacts=[])
    try:
        contacts = await _contacts_store.list()
        return ContactListResponse(contacts=[ContactResponse(**c) for c in contacts])
    except Exception as exc:
        logger.warning("list_contacts failed: %s", exc)
        return ContactListResponse(contacts=[])


@router.get("/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: str) -> ContactResponse:
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact = await _contacts_store.get(contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**contact)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("get_contact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


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
        max_stages = depth_map.get(body.depth or "standard", 3)
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
            title = b.get("title") if isinstance(b, dict) else getattr(b, "title", "")
            body = b.get("body") if isinstance(b, dict) else getattr(b, "body", "")
            if title or body:
                parts.append(f"- {title}: {body[:200]}" if title else f"- {body[:200]}")
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

def set_autonomy_loop(loop) -> None:
    global _autonomy_loop
    _autonomy_loop = loop


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
    """Trigger a single autonomy cycle for testing."""
    if _autonomy_loop is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        _autonomy_loop.wake()
        status = _autonomy_loop.status()
        return {"completed": True, "result": status}
    except Exception as exc:
        logger.warning("autonomy_cycle failed: %s", exc)
        return {"completed": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Self-Knowledge Seeding
# ---------------------------------------------------------------------------


class SeedResponse(BaseModel):
    memories: int = 0
    entities: int = 0
    skills: int = 0
    insights: int = 0
    errors: list[str] = []


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

    emit("commitment.created", {
        "commitment_id": result["id"],
        "person_id": result["person_id"],
        "description": result["description"],
    })
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
        emit("commitment.fulfilled", {
            "commitment_id": result["id"],
            "person_id": result["person_id"],
        })
    elif body.status == "cancelled":
        emit("commitment.cancelled", {
            "commitment_id": result["id"],
            "person_id": result["person_id"],
        })

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


@router.post("/seed", response_model=SeedResponse)
async def seed_self_knowledge_endpoint() -> SeedResponse:
    """Seed Colony with self-knowledge via API.
    
    This endpoint triggers the self-knowledge seeding process that populates
    Colony's memory, world model, and skills registry with deep understanding
    of its own architecture and capabilities.
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
    )
    
    return SeedResponse(
        memories=results.get("memories", 0),
        entities=results.get("entities", 0),
        skills=results.get("skills", 0),
        insights=results.get("insights", 0),
        errors=results.get("errors", []),
    )

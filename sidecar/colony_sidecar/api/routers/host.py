"""Colony sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Colony's intelligence as a plugin.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from colony_sidecar.api.schemas.host import (
    HostConfigureRequest,
    HostConfigureResponse,
    AutonomyStatusResponse,
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
    EnrichedContextRequest,
    EnrichedContextResponse,
    EntityListResponse,
    EntityQueryRequest,
    EntityResponse,
    GoalCreateRequest,
    GoalListResponse,
    GoalResponse,
    GoalUpdateRequest,
    HostHealthResponse,
    HostMessage,
    IdentityInitRequest,
    IdentityStatusResponse,
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
    ReasoningToolCall,
    ReasoningTurnRequest,
    ReasoningTurnResponse,
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
        caps.append("safety")
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
    if _skills_registry is not None:
        caps.append("skills")
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
    if _graph is not None:
        notes["memory"] = "ColonyGraph wired"
    else:
        notes["memory"] = "ColonyGraph not wired — memory endpoints return stubs"
    if _response_gate is not None:
        notes["safety"] = "ResponseGate wired"
    else:
        notes["safety"] = "ResponseGate not wired — safety/check passes everything"
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
        notes["embed"] = "EmbeddingPipeline wired"
    if _skills_registry is not None:
        notes["skills"] = "SkillRegistry wired"
    if _chain_manager is not None:
        notes["identity"] = "ChainManager wired"
    if _secrets_manager is not None:
        notes["secrets"] = "SecretsManager wired"
    return HostHealthResponse(status="ok", capabilities=caps, notes=notes)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

_NOT_WIRED = {"error": {"code": "not_wired", "message": "Backend not configured"}}


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


@router.post("/memory/write", response_model=MemoryWriteResponse)
async def memory_write(body: MemoryWriteRequest) -> MemoryWriteResponse:
    if _graph is None:
        return MemoryWriteResponse(id="stub", accepted=False)
    try:
        result = await _graph.store_memory(
            content=body.content,
            person_id=body.person_id,
            memory_type=body.type,
            entities=body.entities,
            tags=body.tags,
            strength=body.strength,
        )
        return MemoryWriteResponse(
            id=result.get("id", str(uuid.uuid4())),
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
        results = await _graph.search_memories(
            query=body.query,
            person_id=body.person_id,
            limit=body.limit or 10,
            min_score=body.min_score,
            types=body.types,
            tags=body.tags,
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
        model_id, vectors = await _embedder.embed(body.inputs, model=body.model)
        return MemoryEmbedResponse(model=model_id, vectors=vectors)
    except Exception as exc:
        logger.warning("memory_embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@router.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(body: ContextAssembleRequest) -> ContextAssembleResponse:
    # Context assembly pulls from memory search + available subsystems
    sections: list[ContextSection] = []

    if _graph is not None and body.incoming_message.content:
        try:
            results = await _graph.search_memories(
                query=body.incoming_message.content,
                person_id=body.context.contact_id if body.context else None,
                limit=5,
            )
            if results:
                body_text = "\n".join(
                    f"- [{r.get('score', 0):.2f}] {r.get('content', '')}"
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

    return SignalIngestResponse(accepted=True, signals_recorded=recorded)


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

@router.post("/turns/sync", response_model=TurnSyncResponse)
async def turns_sync(body: TurnSyncRequest) -> TurnSyncResponse:
    # Best-effort: store turn metadata in the graph if available
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
            return TurnSyncResponse(accepted=True, continuity_updated=True)
        except Exception as exc:
            logger.warning("turns_sync failed: %s", exc)

    return TurnSyncResponse(accepted=True, continuity_updated=False, skipped_reason="no_graph_store")


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

@router.post("/safety/check", response_model=SafetyCheckResponse)
async def safety_check(body: SafetyCheckRequest) -> SafetyCheckResponse:
    if _response_gate is None:
        return SafetyCheckResponse(decision="pass", blocked=False)

    try:
        from colony_sidecar.gate.models import GatePayload
        payload = GatePayload(
            response_text=body.response_text,
            incoming_message_text=body.incoming_message_text,
            target_gateway=body.target_gateway,
            trust_tier=body.trust_tier,
            mentioned_entities=body.mentioned_entities,
        )
        result = await _response_gate.evaluate(payload)
        return SafetyCheckResponse(
            decision="block" if result.blocked else "pass",
            blocked=result.blocked,
            blocking_layer=result.blocking_layer if hasattr(result, "blocking_layer") else None,
            reason=result.reason if hasattr(result, "reason") else None,
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

    # Read auth message
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        import json as _json
        msg = _json.loads(raw)
        if msg.get("type") != "auth":
            await ws.close(code=4001, reason="Expected auth message")
            return
        token = msg.get("token", "")
        expected = os.environ.get("COLONY_API_KEY", "")
        if expected and token != expected:
            await ws.close(code=4003, reason="Invalid API key")
            return
    except asyncio.TimeoutError:
        await ws.close(code=4001, reason="Auth timeout")
        return
    except Exception:
        await ws.close(code=4001, reason="Invalid auth")
        return

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
        goal = await _goals_store.create_goal(
            title=body.title,
            description=body.description,
            priority=body.priority,
            parent_goal_id=body.parent_goal_id,
            person_id=body.person_id,
        )
        return GoalResponse(**goal)
    except Exception as exc:
        logger.warning("create_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/goals", response_model=GoalListResponse)
async def list_goals(person_id: Optional[str] = None, status_filter: Optional[str] = None) -> GoalListResponse:
    if _goals_store is None:
        return GoalListResponse(goals=[])
    try:
        goals = await _goals_store.list_goals(person_id=person_id, status=status_filter)
        return GoalListResponse(goals=[GoalResponse(**g) for g in goals])
    except Exception as exc:
        logger.warning("list_goals failed: %s", exc)
        return GoalListResponse(goals=[])


@router.get("/goals/{goal_id}", response_model=GoalResponse)
async def get_goal(goal_id: str) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        goal = await _goals_store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return GoalResponse(**goal)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("get_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/goals/{goal_id}", response_model=GoalResponse)
async def update_goal(goal_id: str, body: GoalUpdateRequest) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        goal = await _goals_store.update_goal(goal_id, status=body.status, progress=body.progress, notes=body.notes)
        return GoalResponse(**goal)
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
        contacts = await _contacts_store.list_contacts()
        return ContactListResponse(contacts=[ContactResponse(**c) for c in contacts])
    except Exception as exc:
        logger.warning("list_contacts failed: %s", exc)
        return ContactListResponse(contacts=[])


@router.get("/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: str) -> ContactResponse:
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact = await _contacts_store.get_contact(contact_id)
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
        briefings = await _briefings_engine.list_briefings(limit=limit)
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
async def query_entities(body: EntityQueryRequest) -> EntityListResponse:
    if _world_store is None:
        return EntityListResponse(entities=[])
    try:
        entities = await _world_store.query(body.query, limit=body.limit or 10)
        return EntityListResponse(entities=[EntityResponse(**_to_dict(e)) for e in entities])
    except Exception as exc:
        logger.warning("query_entities failed: %s", exc)
        return EntityListResponse(entities=[])


@router.get("/world/entities", response_model=EntityListResponse)
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


@router.post("/research/start", response_model=ResearchRunResponse)
async def start_research(body: ResearchStartRequest) -> ResearchRunResponse:
    if _research_pipeline is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        run = await _research_pipeline.run(topic=body.topic, depth=body.depth)
        return ResearchRunResponse(
            run_id=run.run_id,
            topic=run.topic,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            stages_completed=[s.value if hasattr(s, "value") else str(s) for s in run.stages_completed],
            artifact=run.artifact if hasattr(run, "artifact") else None,
        )
    except Exception as exc:
        logger.warning("start_research failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/research", response_model=ResearchListResponse)
async def list_research(limit: int = 20) -> ResearchListResponse:
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
        return DeliveryListResponse(pending=[])
    try:
        pending = _delivery_bridge.get_pending(gateway_id=gateway_id, limit=limit)
        return DeliveryListResponse(pending=pending)
    except Exception as exc:
        logger.warning("list_pending_deliveries failed: %s", exc)
        return DeliveryListResponse(pending=[])


@router.post("/delivery/mark-sent")
async def mark_delivery_sent(body: DeliveryMarkRequest) -> dict:
    if _delivery_bridge is None:
        return {"ok": False}
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

def set_connection_discoverer(discoverer) -> None:
    global _connection_discoverer
    _connection_discoverer = discoverer


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

def set_skills_registry(registry) -> None:
    global _skills_registry
    _skills_registry = registry


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


@router.get("/skills/registry/{skill_id}", response_model=SkillDetailResponse)
async def get_skill(skill_id: str) -> SkillDetailResponse:
    if _skills_registry is None:
        raise HTTPException(status_code=404, detail="Skills not available")
    try:
        skill = await _skills_registry.get_skill(skill_id)
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
        insights = []
        for c in connections[:limit]:
            insights.append(InsightResponse(
                id=getattr(c, "id", str(uuid.uuid4())),
                title=getattr(c, "connection_type", "Connection"),
                body=getattr(c, "description", "") or f"Connection between {', '.join(getattr(c, 'entities', []))}",
                insight_type=getattr(c, "connection_type", "unknown"),
                novelty=getattr(c, "novelty", 0.0),
                entities=getattr(c, "entities", []),
                dismissed=False,
            ))
        return InsightsListResponse(insights=insights)
    except Exception as exc:
        logger.warning("list_insights failed: %s", exc)
        return InsightsListResponse(insights=[])


@router.post("/insights/{insight_id}/dismiss")
async def dismiss_insight(insight_id: str) -> dict:
    return {"ok": True}


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
                results = await _graph.search_memories(query=msg, person_id=contact_id, limit=5)
                return ("memory", results)
            except Exception:
                return ("memory", [])
        tasks["memory"] = _mem()

    # 2. Contact / relationship
    if _contacts_store is not None and contact_id and features.get("relationships", True):
        async def _contact():
            try:
                c = await _contacts_store.get_contact(contact_id)
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
                g = await _goals_store.list_goals(person_id=contact_id, status="active")
                return ("goals", g)
            except Exception:
                return ("goals", [])
        tasks["goals"] = _goals()

    # 5. World model entities
    if _world_store is not None and features.get("worldModel", True):
        async def _world():
            try:
                e = await _world_store.query(msg, limit=5)
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

    # Run all tasks in parallel
    results = {}
    if tasks:
        task_items = list(tasks.items())
        gathered = await asyncio.gather(*[t[1]() for t in task_items], return_exceptions=True)
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

    return EnrichedContextResponse(sections=sections, contact_id=contact_id)


# ---------------------------------------------------------------------------
# Chain / Identity
# ---------------------------------------------------------------------------

_chain_manager = None

def set_chain_manager(manager) -> None:
    global _chain_manager
    _chain_manager = manager


@router.get("/identity/status", response_model=IdentityStatusResponse)
async def identity_status() -> IdentityStatusResponse:
    if _chain_manager is None:
        return IdentityStatusResponse(initialized=False)
    try:
        colony_id = getattr(_chain_manager, "colony_id", None)
        pubkey = getattr(_chain_manager, "public_key_pem", None)
        return IdentityStatusResponse(
            colony_id=colony_id,
            public_key=pubkey,
            initialized=colony_id is not None,
        )
    except Exception as exc:
        logger.warning("identity_status failed: %s", exc)
        return IdentityStatusResponse(initialized=False)


@router.post("/identity/init", response_model=IdentityStatusResponse)
async def identity_init(body: IdentityInitRequest) -> IdentityStatusResponse:
    if _chain_manager is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        await _chain_manager.initialize(force=body.force)
        colony_id = getattr(_chain_manager, "colony_id", None)
        pubkey = getattr(_chain_manager, "public_key_pem", None)
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
    if _chain_manager is None:
        return ChainVerifyResponse(valid=False)
    try:
        result = await _chain_manager.verify(data=body.data, signature=body.signature)
        return ChainVerifyResponse(valid=result, colony_id=getattr(_chain_manager, "colony_id", None))
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


@router.post("/secrets/list", response_model=SecretListResponse)
async def secrets_list(body: SecretListRequest) -> SecretListResponse:
    if _secrets_manager is None:
        return SecretListResponse(keys=[])
    try:
        keys = await _secrets_manager.list_keys(prefix=body.prefix)
        return SecretListResponse(keys=keys)
    except Exception as exc:
        logger.warning("secrets_list failed: %s", exc)
        return SecretListResponse(keys=[])


@router.post("/secrets/get", response_model=SecretGetResponse)
async def secrets_get(body: SecretGetRequest) -> SecretGetResponse:
    if _secrets_manager is None:
        return SecretGetResponse(key=body.key, exists=False)
    try:
        value = await _secrets_manager.get(body.key)
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
        await _secrets_manager.set(body.key, body.value, secret_type=body.secret_type)
        return SecretSetResponse(key=body.key, stored=True)
    except Exception as exc:
        logger.warning("secrets_set failed: %s", exc)
        return SecretSetResponse(key=body.key, stored=False)


@router.post("/secrets/delete", response_model=SecretDeleteResponse)
async def secrets_delete(body: SecretDeleteRequest) -> SecretDeleteResponse:
    if _secrets_manager is None:
        return SecretDeleteResponse(key=body.key, deleted=False)
    try:
        await _secrets_manager.delete(body.key)
        return SecretDeleteResponse(key=body.key, deleted=True)
    except Exception as exc:
        logger.warning("secrets_delete failed: %s", exc)
        return SecretDeleteResponse(key=body.key, deleted=False)


# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------

_autonomy_loop = None
_autonomy_task = None

def set_autonomy_loop(loop) -> None:
    global _autonomy_loop
    _autonomy_loop = loop


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


# ---------------------------------------------------------------------------
# Self-Knowledge Seeding
# ---------------------------------------------------------------------------


class SeedResponse(BaseModel):
    memories: int = 0
    entities: int = 0
    skills: int = 0
    insights: int = 0
    errors: list[str] = []


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

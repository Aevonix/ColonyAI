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

from colony_sidecar.api.schemas.host import (
    BriefingListResponse,
    BriefingResponse,
    ContactListResponse,
    ContactResponse,
    ContactStyleRequest,
    ContactStyleResponse,
    ContextAssembleRequest,
    ContextAssembleResponse,
    ContextSection,
    EntityListResponse,
    EntityQueryRequest,
    EntityResponse,
    GoalCreateRequest,
    GoalListResponse,
    GoalResponse,
    GoalUpdateRequest,
    HostHealthResponse,
    HostMessage,
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
    SafetyCheckRequest,
    SafetyCheckResponse,
    SignalIngestRequest,
    SignalIngestResponse,
    TurnSyncRequest,
    TurnSyncResponse,
)

logger = logging.getLogger(__name__)

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
    caps.append("events")
    return caps


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
        return EntityListResponse(entities=[EntityResponse(**e) for e in entities])
    except Exception as exc:
        logger.warning("query_entities failed: %s", exc)
        return EntityListResponse(entities=[])


@router.get("/world/entities", response_model=EntityListResponse)
async def list_entities(entity_type: Optional[str] = None, limit: int = 50) -> EntityListResponse:
    if _world_store is None:
        return EntityListResponse(entities=[])
    try:
        entities = await _world_store.list_entities(entity_type=entity_type, limit=limit)
        return EntityListResponse(entities=[EntityResponse(**e) for e in entities])
    except Exception as exc:
        logger.warning("list_entities failed: %s", exc)
        return EntityListResponse(entities=[])

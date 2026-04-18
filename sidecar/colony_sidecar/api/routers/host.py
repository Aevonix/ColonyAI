"""Colony sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Colony's intelligence as a plugin.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status

from colony_sidecar.api.schemas.host import (
    HostHealthResponse,
    HostIdentity,
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

_reasoning_loop = None


def set_reasoning_loop(loop) -> None:
    """Wire a :class:`~colony_sidecar.reasoning.ReasoningLoop` instance."""
    global _reasoning_loop
    _reasoning_loop = loop


def supported_capabilities() -> List[str]:
    """Return the list of capabilities this sidecar advertises."""
    caps: list[str] = []
    if _reasoning_loop is not None:
        caps.append("reasoning")
    return caps


_PHASE1_REASONING_DETAIL = {
    "error": {
        "code": "phase1_wiring_required",
        "message": (
            "Reasoning loop not wired. Ensure the sidecar is configured "
            "with an LLMRouter and ReasoningLoop is initialized."
        ),
    }
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HostHealthResponse)
async def health() -> HostHealthResponse:
    caps = supported_capabilities()
    notes: dict[str, str] = {}
    if _reasoning_loop is not None:
        notes["reasoning"] = "ReasoningLoop wired (max_iterations=%d)" % _reasoning_loop._config.max_iterations
    else:
        notes["reasoning"] = "ReasoningLoop not wired — /reasoning/turn returns 501"
    return HostHealthResponse(
        status="ok",
        capabilities=caps,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

@router.post(
    "/reasoning/turn",
    response_model=ReasoningTurnResponse,
)
async def reasoning_turn(body: ReasoningTurnRequest) -> ReasoningTurnResponse:
    if _reasoning_loop is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_PHASE1_REASONING_DETAIL)

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    session_id = body.context.session_id if body.context and body.context.session_id else str(uuid.uuid4())

    result = await _reasoning_loop.run_turn(
        session_id=session_id,
        messages=messages,
        available_tools=body.available_tools or None,
        model_override=body.model_override or None,
    )

    from colony_sidecar.api.schemas.host import HostMessage as HM
    response_msg = None
    if result.message:
        response_msg = HM(
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
# Signals (stub — returns accepted for now)
# ---------------------------------------------------------------------------

@router.post("/signals/ingest", response_model=SignalIngestResponse)
async def signals_ingest(body: SignalIngestRequest) -> SignalIngestResponse:
    return SignalIngestResponse(accepted=True, signals_recorded=0)


# ---------------------------------------------------------------------------
# Turns (stub — returns accepted for now)
# ---------------------------------------------------------------------------

@router.post("/turns/sync", response_model=TurnSyncResponse)
async def turns_sync(body: TurnSyncRequest) -> TurnSyncResponse:
    return TurnSyncResponse(accepted=True, continuity_updated=False, skipped_reason="no_continuity_store")


# ---------------------------------------------------------------------------
# Safety (stub — passes everything for now)
# ---------------------------------------------------------------------------

@router.post("/safety/check", response_model=SafetyCheckResponse)
async def safety_check(body: SafetyCheckRequest) -> SafetyCheckResponse:
    return SafetyCheckResponse(decision="pass", blocked=False)

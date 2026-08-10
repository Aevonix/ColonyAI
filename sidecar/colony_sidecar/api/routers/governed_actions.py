"""Dedicated HTTP surface for durable governed Colony actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from colony_sidecar.api.authority import request_authority
from colony_sidecar.governed_actions import (
    GOVERNED_ACTION_REQUEST_MAX_BYTES,
    GovernedActionConflict,
    GovernedActionNotFound,
    GovernedActionService,
    GovernedActionValidationError,
)


router = APIRouter(prefix="/v1/host", tags=["governed-actions"])
_service: GovernedActionService | None = None


def _body_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


async def _bounded_execution_body(request: Request) -> bytes:
    """Read at most the parser's bound, including unframed ASGI streams."""

    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        raise _body_error(
            400,
            "governed_action_content_length_invalid",
            "execution Content-Length is invalid",
        )
    if content_lengths:
        content_length = content_lengths[0]
        if not content_length.isascii() or not content_length.isdigit():
            raise _body_error(
                400,
                "governed_action_content_length_invalid",
                "execution Content-Length is invalid",
            )
        significant_length = content_length.lstrip("0") or "0"
        maximum_text = str(GOVERNED_ACTION_REQUEST_MAX_BYTES)
        if (
            len(significant_length) > len(maximum_text)
            or (
                len(significant_length) == len(maximum_text)
                and significant_length > maximum_text
            )
        ):
            raise _body_error(
                413,
                "governed_action_too_large",
                "execution document exceeds its size bound",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > GOVERNED_ACTION_REQUEST_MAX_BYTES:
            raise _body_error(
                413,
                "governed_action_too_large",
                "execution document exceeds its size bound",
            )
        body.extend(chunk)
    return bytes(body)


def set_governed_action_service(service: GovernedActionService | None) -> None:
    global _service
    if service is not None and not isinstance(service, GovernedActionService):
        raise TypeError("governed action service is invalid")
    _service = service


def _configured() -> GovernedActionService:
    if _service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "governed_actions_unavailable",
                "message": "governed action ledger is not initialized",
            },
        )
    return _service


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=403,
            detail={
                "code": "governed_action_authority_denied",
                "message": "exact owner-bound governed-action principal required",
            },
        )
    if isinstance(exc, GovernedActionNotFound):
        return HTTPException(
            status_code=404,
            detail={
                "code": "governed_action_not_found",
                "message": "governed action was not found",
            },
        )
    if isinstance(exc, GovernedActionConflict):
        return HTTPException(
            status_code=409,
            detail={
                "code": "governed_action_conflict",
                "message": "action identifier is already bound",
            },
        )
    if isinstance(exc, GovernedActionValidationError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "governed_action_invalid",
                "message": "execution document is invalid",
            },
        )
    return HTTPException(
        status_code=500,
        detail={
            "code": "governed_action_internal_error",
            "message": "governed action boundary failed safely",
        },
    )


@router.put("/actions/{action_id}")
async def execute_governed_action(action_id: str, request: Request) -> dict:
    try:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise GovernedActionValidationError(
                "execution content type must be application/json"
            )
        raw = await _bounded_execution_body(request)
        return await _configured().execute(
            action_id, raw, request_authority(request)
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/actions/{action_id}")
async def observe_governed_action(action_id: str, request: Request) -> dict:
    try:
        return await _configured().observe(action_id, request_authority(request))
    except HTTPException:
        raise
    except Exception as exc:
        raise _translate(exc) from exc


__all__ = ("router", "set_governed_action_service")

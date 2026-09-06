"""Execution observations reuse turn-writer and context-reader authority."""
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from colony_sidecar.api.authority import request_authority, resolve_request_person
from colony_sidecar.turns.executions import registry

router = APIRouter(prefix="/v1/host/executions", tags=["executions"])


class ExecutionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    turn_id: str = Field(min_length=1, max_length=256)
    parent_execution_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    platform: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    state: Literal["observed", "completed", "failed", "interrupted", "ended"] = "observed"
    phase: Literal["turn", "model", "tool", "between_calls", "ended"] = "turn"
    tool_name: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9_.:-]*$")
    sequence: int = Field(ge=1, le=2147483647)


def authorized_viewer(request: Request, contact_id: str, *, scope: str) -> tuple[str, bool]:
    authority = request_authority(request)
    # Legacy body-selected identity is deliberately not sufficient for this new
    # shared surface. Use the existing exact person grants, never an owner flag.
    if not authority.authenticated or authority.anonymous or authority.legacy or not authority.has_scope(scope):
        raise HTTPException(403, detail={"code": "scoped_execution_authority_required"})
    person = resolve_request_person(request, claimed_person_id=contact_id)
    owner = os.environ.get("COLONY_OWNER_PERSON_ID", "").strip() or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
    return person, bool(owner and person == owner)


@router.post("/observe")
def observe(body: ExecutionObservation, request: Request):
    person, _ = authorized_viewer(request, body.contact_id, scope="turns:write")
    try:
        return registry().observe(body.model_dump(), principal_id=request_authority(request).principal_id, contact_id=person)
    except ValueError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc


@router.get("")
def active(request: Request, contact_id: str, session_id: str = "", limit: int = Query(20, ge=1, le=100)):
    person, owner = authorized_viewer(request, contact_id, scope="context:read")
    return registry().view(contact_id=person, owner=owner, session_id=session_id, limit=limit)

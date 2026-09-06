"""Explicit work coordination, with existing participant/writer authority."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from colony_sidecar.api.authority import request_authority
from colony_sidecar.api.routers.executions import authorized_viewer
from colony_sidecar.commitments.work import CommitmentWork

router = APIRouter(prefix='/v1/host/commitments', tags=['commitments'])


class WorkOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['claim', 'status', 'renew', 'release']
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    claim_id: str = Field(default='', pattern=r'^(?:[a-f0-9]{32})?$')


@router.post('/{commitment_id}/work')
def operate(commitment_id: str, body: WorkOperation, request: Request):
    person, _ = authorized_viewer(request, body.contact_id, scope='turns:write')
    from colony_sidecar.api.routers import host
    if host._commitment_store is None:
        raise HTTPException(503, detail='commitment store unavailable')
    try:
        return CommitmentWork(host._commitment_store).operate(commitment_id,
            **body.model_dump(exclude={'contact_id'}), contact_id=person,
            principal_id=request_authority(request).principal_id)
    except KeyError:
        raise HTTPException(404, detail='unknown commitment') from None

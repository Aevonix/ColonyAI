"""Owner/system handoff of registered internal reviews to native Hermes work."""
import hashlib
import sqlite3
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from colony_sidecar.api.routers.executions import authorized_viewer
from colony_sidecar.initiatives.native_work import NativeInitiativeWork
from colony_sidecar.turns.hermes_kanban import task_snapshot, observed_boards

router = APIRouter(prefix='/v1/host/initiative-work', tags=['initiatives'])


class ReviewBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    native_board: str = Field(pattern='^default$')
    native_task_id: str = Field(min_length=1, max_length=128)
    contract_sha256: str = Field(pattern='^[a-f0-9]{64}$')


class ModelObservation(ReviewBinding):
    native_run_id: int = Field(gt=0)
    native_claim_lock: str = Field(min_length=1, max_length=256)
    api_request_id: str = Field(min_length=1, max_length=256)
    phase: Literal['start', 'response', 'error']
    requested_model: str | None = Field(default=None, min_length=1, max_length=256)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    response_model: str | None = Field(default=None, min_length=1, max_length=256)


@router.post('/{initiative_id}/model-observation')
def model_observation(initiative_id: str, body: ModelObservation, request: Request):
    work, person = store(request, body.contact_id, write=True)
    def retain():
        native, state = task_snapshot(initiative_id, person, body.model_dump(), review=True)
        value = work.get(initiative_id)
        bound = value.get('native_work') or {}
        if (state['contract_sha256'] != body.contract_sha256
                or any(bound.get(k) != native[k] for k in ('native_task_id', 'native_board', 'source_home_id'))):
            raise ValueError('native_review_contract_mismatch')
        from colony_sidecar.self_model import runtime_models
        from colony_sidecar import get_state_dir
        from colony_sidecar.turns import get_turn_idempotency_ledger
        return runtime_models.retain(get_turn_idempotency_ledger(get_state_dir()), person, native, state, body.model_dump())
    return guarded(retain)


def store(request, contact_id, *, write=False):
    person, owner = authorized_viewer(request, contact_id, scope='turns:write' if write else 'context:read')
    if not owner:
        raise HTTPException(403, detail='owner_native_review_required')
    from colony_sidecar.api.routers import host
    if host._initiative_store is None:
        raise HTTPException(503, detail='initiative_store_unavailable')
    return NativeInitiativeWork(host._initiative_store), person


def guarded(operation):
    try:
        return operation()
    except KeyError:
        raise HTTPException(404, detail='unknown_initiative') from None
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, detail='native_review_unavailable') from None


@router.get('')
def pending(contact_id: str, request: Request):
    ledger, person = store(request, contact_id)
    return guarded(lambda: {'items': ledger.pending(person)})


@router.get('/{initiative_id}')
def review(initiative_id: str, contact_id: str, request: Request):
    ledger, _ = store(request, contact_id)
    def selected():
        home, boards, _ = observed_boards()
        if home is None or 'default' not in boards:
            raise ValueError('selected_native_review_board_required')
        return ledger.get(initiative_id) | {'execution': {'native_board': 'default',
            'worker_profile': 'default', 'source_home_id': hashlib.sha256(str(home).encode()).hexdigest()}}
    return guarded(selected)


@router.post('/{initiative_id}/native-task')
def attach(initiative_id: str, body: ReviewBinding, request: Request):
    ledger, person = store(request, body.contact_id, write=True)
    def bind():
        native, state = task_snapshot(initiative_id, person, body.model_dump(), review=True)
        if state['contract_sha256'] != body.contract_sha256:
            raise ValueError('native_review_contract_mismatch')
        value = ledger.attach(initiative_id, person, native, body.contract_sha256,
                              prospective=state['attempt_count'] == 0)
        from colony_sidecar.self_model import runtime_forecasts
        return {**value, 'forecast': runtime_forecasts.safe(runtime_forecasts.attach, value, native, state, person)}
    return guarded(bind)


@router.post('/{initiative_id}/observe')
def observe(initiative_id: str, body: ReviewBinding, request: Request):
    ledger, person = store(request, body.contact_id, write=True)
    def reconcile():
        native, state = task_snapshot(initiative_id, person, body.model_dump(), review=True)
        if state['contract_sha256'] != body.contract_sha256:
            raise ValueError('native_review_contract_mismatch')
        value = ledger.reconcile(initiative_id, native, state)
        from colony_sidecar.self_model.native_outcomes import retain_outcome
        retain_outcome(value, native, state, person)
        from colony_sidecar.self_model import runtime_forecasts
        return {**value, 'forecast': runtime_forecasts.safe(runtime_forecasts.observe, value, native, state, person)}
    return guarded(reconcile)

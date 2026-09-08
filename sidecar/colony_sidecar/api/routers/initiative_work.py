"""Owner/system handoff of registered internal reviews to native Hermes work."""
import hashlib
import sqlite3
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

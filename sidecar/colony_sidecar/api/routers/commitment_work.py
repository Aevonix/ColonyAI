"""Explicit work coordination, with existing participant/writer authority."""
from typing import Literal
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator

from colony_sidecar.api.authority import request_authority
from colony_sidecar.api.routers.executions import authorized_viewer
from colony_sidecar.commitments.work import CommitmentWork
from colony_sidecar.commitments.local_work import LocalWork

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


class LocalDraftAcceptance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(min_length=1, max_length=8)
    origin: 'NativeDraftOrigin | None' = None

    @field_validator('sources')
    @classmethod
    def explicit_paths(cls, value):
        if (len(set(value)) != len(value)
                or any(not Path(path).is_absolute() or len(path) > 4096 or '\x00' in path for path in value)):
            raise ValueError('Distinct absolute local source paths are required')
        return value


class NativeDraftOrigin(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9_.-]+$')
    chat_id: str = Field(min_length=1, max_length=512)
    thread_id: str | None = Field(default=None, max_length=512)
    user_id: str | None = Field(default=None, max_length=512)
    user_id_alt: str | None = Field(default=None, max_length=512)
    chat_type: str | None = Field(default=None, max_length=64)
    notifier_profile: str | None = Field(default=None, max_length=256)


LocalDraftAcceptance.model_rebuild()


class NativeWorkRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    native_job_id: str = Field(min_length=1, max_length=128)
    native_execution_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class LocalDraftResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['draft_created', 'unavailable']
    summary: str = Field(default='', max_length=1600)
    report_path: str = Field(default='', max_length=4096)
    report_sha256: str = Field(default='', pattern=r'^(?:[a-f0-9]{64})?$')
    sources: dict[str, str] = Field(default_factory=dict, max_length=8)
    model: str = Field(default='', max_length=256)
    error_type: str = Field(default='', max_length=128, pattern=r'^[A-Za-z0-9_]*$')


class NativeWorkFinish(NativeWorkRun):
    result: LocalDraftResult


class NativeTaskBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    native_board: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    native_task_id: str = Field(min_length=1, max_length=128)


class NativeKanbanRun(NativeTaskBinding):
    native_run_id: int = Field(ge=1)
    native_claim_lock: str = Field(min_length=1, max_length=256)


class NativeKanbanFinish(NativeKanbanRun):
    result: LocalDraftResult


def accepted_native(body, initiative_id, person):
    from colony_sidecar.turns.hermes_kanban import task_snapshot
    try:
        return task_snapshot(initiative_id, person, body.model_dump(exclude={'contact_id', 'result'}))
    except (OSError, sqlite3.Error):
        raise HTTPException(503, detail='native_board_unavailable') from None
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail=str(error)) from None


def local_store(request, contact_id, *, enabled=False):
    person, owner = authorized_viewer(request, contact_id, scope='turns:write')
    if not owner:
        raise HTTPException(403, detail='owner_local_work_required')
    if enabled and os.environ.get('COLONY_LOCAL_WORK_ENABLED', '').lower() != 'true':
        raise HTTPException(503, detail='local_work_not_enabled')
    from colony_sidecar.api.routers import host
    if host._initiative_store is None or host._commitment_store is None:
        raise HTTPException(503, detail='local_work_store_unavailable')
    return LocalWork(host._initiative_store, host._commitment_store), person


def native_run(body, *, finishing=False):
    from colony_sidecar.turns.hermes_work import selected_home
    home = selected_home()
    if not home or body.native_job_id != os.environ.get('COLONY_LOCAL_WORK_JOB_ID'):
        raise HTTPException(409, detail='selected_local_work_job_required')
    home_id = hashlib.sha256(str(home).encode()).hexdigest()
    def terminal(context):
        if context.get('source_home_id') != home_id or context.get('native_job_id') != body.native_job_id:
            return None
        with closing(sqlite3.connect((home/'cron/executions.db').as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
            row = db.execute('SELECT status FROM executions WHERE id=? AND job_id=?',
                             (context.get('native_execution_id'), body.native_job_id)).fetchone()
        return row[0] if row else None
    native = {'source_home_id': home_id, 'native_job_id': body.native_job_id,
              'native_execution_id': body.native_execution_id}
    try:
        status = terminal(native)
    except (OSError, sqlite3.Error):
        raise HTTPException(503, detail='native_execution_ledger_unavailable') from None
    if status not in ({'running', 'completed', 'failed'} if finishing else {'running'}):
        raise HTTPException(409, detail='running_native_execution_required')
    return native, terminal


@router.post('/{commitment_id}/local-draft')
def accept_local_draft(commitment_id: str, body: LocalDraftAcceptance, request: Request):
    return _accept_local_draft(commitment_id, body, request)


@router.post('/local-draft')
def accept_standalone_local_draft(body: LocalDraftAcceptance, request: Request):
    return _accept_local_draft(None, body, request)


def _accept_local_draft(commitment_id, body, request):
    store, person = local_store(request, body.contact_id, enabled=True)
    backend = os.environ.get('COLONY_LOCAL_WORK_EXECUTOR', 'cron')
    if backend not in {'cron', 'kanban'}:
        raise HTTPException(503, detail='local_work_executor_unavailable')
    try:
        return store.accept(commitment_id, **body.model_dump(exclude={'contact_id'}),
                            contact_id=person, principal_id=request_authority(request).principal_id,
                            execution_backend=backend)
    except KeyError:
        raise HTTPException(404, detail='unknown_commitment') from None
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.post('/local-work/next')
def next_local_draft(body: NativeWorkRun, request: Request):
    store, person = local_store(request, body.contact_id, enabled=True)
    native, terminal = native_run(body)
    return {'assignment': store.select(person, native, terminal)}


@router.get('/local-work/pending')
def pending_native_drafts(contact_id: str, request: Request):
    store, person = local_store(request, contact_id, enabled=True)
    if os.environ.get('COLONY_LOCAL_WORK_EXECUTOR', 'cron') != 'kanban':
        raise HTTPException(409, detail='native_execution_backend_required')
    return store.native_pending(person)


@router.post('/local-work/{initiative_id}/native-task')
def attach_native_draft(initiative_id: str, body: NativeTaskBinding, request: Request):
    store, person = local_store(request, body.contact_id, enabled=True)
    native, _ = accepted_native(body, initiative_id, person)
    try:
        return store.attach_native_task(initiative_id, person, native)
    except KeyError:
        raise HTTPException(404, detail='unknown_local_work') from None
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.post('/local-work/{initiative_id}/native-run')
def bind_native_draft(initiative_id: str, body: NativeKanbanRun, request: Request):
    store, person = local_store(request, body.contact_id, enabled=True)
    native, state = accepted_native(body, initiative_id, person)
    try:
        return store.bind_native_run(initiative_id, person, native, attempt_count=state['attempt_count'])
    except KeyError:
        raise HTTPException(404, detail='unknown_local_work') from None
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.get('/local-work/{initiative_id}')
def local_draft_status(initiative_id: str, contact_id: str, request: Request):
    store, person = local_store(request, contact_id)
    try:
        return store.status(initiative_id, person)
    except KeyError:
        raise HTTPException(404, detail='unknown_local_work') from None


@router.post('/local-work/{initiative_id}/finish')
def finish_local_draft(initiative_id: str, body: NativeWorkFinish | NativeKanbanFinish, request: Request):
    store, person = local_store(request, body.contact_id)
    native, _ = (accepted_native(body, initiative_id, person) if isinstance(body, NativeKanbanFinish)
                 else native_run(body, finishing=True))
    result = body.result.model_dump()
    if result['status'] == 'draft_created':
        if not result['report_sha256'] or not Path(result['report_path']).is_absolute() or not result['sources']:
            raise HTTPException(422, detail='local_report_receipt_required')
    elif not result['error_type']:
        raise HTTPException(422, detail='failure_reason_required')
    try:
        return store.finish(initiative_id, person, native, result)
    except KeyError:
        raise HTTPException(404, detail='unknown_local_work') from None
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from None

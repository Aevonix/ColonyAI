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
async def active(request: Request, contact_id: str, session_id: str = "", limit: int = Query(20, ge=1, le=100),
                 projection: Literal['full', 'request'] = 'full'):
    person, owner = authorized_viewer(request, contact_id, scope="context:read")
    if projection == 'request' and not owner:
        raise HTTPException(403, detail='owner_work_context_required')
    if projection == 'request':
        limit = min(limit, 8)
    view = registry().view(contact_id=person, owner=owner, session_id=session_id, limit=limit)
    view = await with_queue_work(view, owner=owner, limit=limit)
    if projection == 'request':
        from colony_sidecar.turns.executions import request_work_context
        return request_work_context(view, limit=limit)
    return view


async def with_queue_work(view, *, owner, limit=8):
    if not owner:
        return view
    import asyncio
    from colony_sidecar.turns.hermes_work import cron_view
    from colony_sidecar.turns.local_work import local_work_view
    from colony_sidecar.turns.hermes_kanban import kanban_view
    from colony_sidecar.turns.reported_workers import reported_worker_view
    from colony_sidecar.api.routers import host
    from colony_sidecar.turns.executions import work_source_coverage
    import time

    queue = getattr(host._task_queue, 'queue', host._task_queue)

    async def read(reader, *, asynchronous=False):
        # These are independent read-only snapshots. One unavailable ledger
        # must not hide work observed by all the other readers. Cancellation of
        # a thread await does not stop its read; native readers bound SQLite work.
        try:
            operation = reader(limit=limit) if asynchronous else asyncio.to_thread(reader, limit=limit)
            value = await asyncio.wait_for(operation, timeout=.2)
            if value is None:
                return None
            return {**value, 'observed_at': time.time()}
        except Exception:
            return {'items': [], 'available': False, 'unavailable': True,
                    'reason': 'work_source_unavailable', 'observed_at': time.time()}

    async def queue_view():
        if queue is None or not callable(getattr(queue, 'current_work', None)):
            return {'items': [], 'available': False, 'unavailable': True,
                    'reason': 'queue_not_attached', 'source': 'canonical_task_queue'}
        return await read(queue.current_work, asynchronous=True)

    # All sources share the existing request budget, rather than each taking
    # another sequential budget after the native ledgers finish.
    view['native_cron'], view['local_work'], view['native_kanban'], reported, view['worker_work'] = await asyncio.gather(
        read(cron_view), read(local_work_view), read(kanban_view),
        read(reported_worker_view), queue_view())
    if reported is not None:
        view['reported_worker'] = reported
    view['work_sources'] = work_source_coverage(view)
    view['coverage'] = 'registered Hermes turns and selected work ledgers; see work_sources for read coverage'
    return view

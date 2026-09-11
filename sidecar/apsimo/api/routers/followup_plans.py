"""Exact prepared task configuration; permission remains at the private outbox."""
import hashlib
import json
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from apsimo.api.authority import request_authority
from apsimo.api.routers import temporal_followups as temporal
from apsimo.commitments.work import CommitmentWork
from apsimo.initiatives.temporal_followup import TemporalFollowups

router = APIRouter(prefix='/v1/host/temporal-followups', tags=['commitments'])


class FollowupPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    wait_id: str = Field(min_length=1, max_length=128)
    commitment_id: str = Field(min_length=1, max_length=128)
    work_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(pattern='^[a-f0-9]{64}$')
    recipient_id: str = Field(min_length=1, max_length=128)
    channel: Literal['whatsapp', 'sms', 'rcs']
    purpose: str = Field(min_length=1, max_length=512)
    expires_at: float = Field(gt=0)
    max_followups: Literal[1]
    message: str = Field(min_length=1, max_length=8000)
    message_sha256: str = Field(pattern='^[a-f0-9]{64}$')


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode()).hexdigest()


class BindPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str
    session_id: str
    turn_id: str
    claim_id: str
    plan: FollowupPlan


class CheckPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan: FollowupPlan
    outbound_ref: str = Field(min_length=1, max_length=256)
    target: dict


def _consistent(row, plan):
    if any(row[key] != plan[key] for key in ('wait_id', 'commitment_id', 'work_id')):
        raise ValueError('prepared_followup_task_mismatch')
    if (row['contact_id'] != plan['recipient_id'] or row['expires_at'] != plan['expires_at']
            or row['source_versions'].get(plan['source_id']) != plan['source_version']
            or hashlib.sha256(plan['message'].encode()).hexdigest() != plan['message_sha256']):
        raise ValueError('prepared_followup_evidence_mismatch')


@router.post('/{wait_id}/bind-plan')
def bind(wait_id: str, body: BindPlan, request: Request):
    store, owner = temporal.ledger(request, body.contact_id, write=True)
    def register():
        row = temporal.owned(store, wait_id, owner)
        plan = body.plan.model_dump()
        _consistent(row, plan)
        if not plan['source_id'].startswith('task-instruction:'):
            raise ValueError('direct_owner_task_instruction_required')
        if row['source_session_id'] != body.session_id:
            raise ValueError('owner_task_session_mismatch')
        existing = row['authority_scope']
        if existing:
            if existing.get('plan') != plan:
                raise ValueError('task authority configuration is immutable')
            return {'bound': True, 'plan_digest': digest(plan), 'effect_authorized': False}
        current = CommitmentWork(store.store).operate(plan['commitment_id'], operation='renew',
            principal_id=request_authority(request).principal_id, contact_id=owner,
            session_id=body.session_id, task_id=plan['work_id'], turn_id=body.turn_id,
            claim_id=body.claim_id)
        if not current['accepted']:
            raise ValueError('current_parent_work_claim_required')
        store.bind_authority_scope(wait_id, scope={'plan': plan, 'plan_digest': digest(plan),
            'owner_contact_id': owner, 'owner_evidence_ref': plan['source_id'],
            'registered_at': time.time()}, evidence_ref=plan['source_id'])
        return {'bound': True, 'plan_digest': digest(plan), 'effect_authorized': False}
    return temporal.guarded(register)


def _check(wait_id, body, request, *, require_coverage=False):
    authority = request_authority(request)
    if (not authority.authenticated or authority.anonymous or authority.legacy
            or not authority.has_scope('transport:write')):
        raise HTTPException(403, detail='trusted_transport_producer_required')
    from apsimo.api.routers import host
    if host._commitment_store is None:
        raise HTTPException(503, detail='commitment_store_unavailable')
    store = TemporalFollowups(host._commitment_store)
    row = store.get(wait_id)
    with closing(store.store._connect()) as db:
        parent = db.execute('SELECT person_id FROM commitments WHERE id=?', (row['commitment_id'],)).fetchone()
    if not parent:
        raise ValueError('unknown_owner_task')
    row = temporal.refresh_source_bindings(store, row, parent['person_id'])
    if host._comms_log is not None:
        from apsimo.api.routers.transport import reconcile_receipts
        row = reconcile_receipts(store, host._comms_log, row)
    plan = body.plan.model_dump()
    _consistent(row, plan)
    scope = row['authority_scope']
    if (scope.get('plan') != plan or scope.get('plan_digest') != digest(plan)
            or row['outbound_ref'] != body.outbound_ref
            or body.target.get('channel') != plan['channel']):
        raise ValueError('prepared_followup_binding_mismatch')
    # Actual provider recipient/account/thread resolution and permission belong
    # to the private policy/outbox. This proves the canonical task configuration.
    check = store.preflight(wait_id)
    current_sources = not temporal.source_bindings(row['source_refs'], row['source_versions'],
        person=parent['person_id'], session_id=row['source_session_id'])
    coverage = None
    if require_coverage:
        from .transport_ingress_api import followup_coverage
        coverage = followup_coverage(principal=authority.principal_id, contact_id=row['contact_id'],
                                     since=row['created_at'], channel=plan['channel'])
        if not coverage['observed']:
            check = {**check, 'review_allowed': False, 'reason': 'intake_coverage_unknown'}
    return {'verified': current_sources and row['state'] not in {'cancelled', 'expired'},
            'review_allowed': check['review_allowed'],
            'reason': 'due' if check['review_allowed'] else check['reason'],
            'plan_digest': digest(plan), 'owner_contact_id': scope['owner_contact_id'],
            'owner_evidence_ref': scope['owner_evidence_ref'],
            'registered_at': scope['registered_at'], 'resolved_contact_id': row['contact_id'],
            **({'transport_coverage': coverage} if coverage is not None else {})}


@router.post('/{wait_id}/verify-plan')
async def verify(wait_id: str, body: CheckPlan, request: Request):
    return temporal.guarded(lambda: _check(wait_id, body, request))


@router.post('/{wait_id}/check-plan')
async def check(wait_id: str, body: CheckPlan, request: Request):
    return temporal.guarded(lambda: _check(wait_id, body, request, require_coverage=True))

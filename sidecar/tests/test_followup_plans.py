"""Prepared owner-task configuration survives restart but never grants a send."""
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host, followup_plans, temporal_followups
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.commitments.work import CommitmentWork
from colony_sidecar.initiatives.temporal_followup import TemporalFollowups
from colony_sidecar.turns import get_turn_idempotency_ledger, canonical_turn_digest
from test_scoped_api_authority import _principal, _write_keyring


@pytest.mark.asyncio
async def test_bound_plan_current_source_and_changed_task(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID','cid-owner')
    source = get_turn_idempotency_ledger(tmp_path)
    messages = [{'role':'user','content':'Obtain this response and follow up once if needed.'}]
    sid='task-instruction:fixture'; version=canonical_turn_digest(messages)
    source.record_source(sid, contact_id='cid-owner', session_id='session', messages=messages, derive_claims=False)
    store=CommitmentStore(tmp_path/'commitments.db')
    monkeypatch.setattr(host,'_commitment_store',store)
    monkeypatch.setattr(host,'_comms_log',None)
    monkeypatch.setattr(host,'_contacts_store',SimpleNamespace(get=AsyncMock(return_value=object())))
    parent=store.create(person_id='cid-owner',description='Obtain response')
    holder=dict(principal_id='native',contact_id='cid-owner',session_id='session',task_id='work',turn_id='turn')
    claim=CommitmentWork(store).operate(parent['id'],operation='claim',**holder)
    expiry=float(time.time()+3600)
    waits=TemporalFollowups(store)
    waits.expect_reply(wait_id='wait',commitment_id=parent['id'],work_id='work',contact_id='cid-person',
        outbound_ref='native-delivery',source_refs=[sid],source_versions={sid:version},source_session_id='session',
        expected_after_seconds=30,expires_at=expiry)
    message='Please send the café report when convenient.'
    plan=dict(wait_id='wait',commitment_id=parent['id'],work_id='work',source_id=sid,source_version=version,
        recipient_id='cid-person',channel='whatsapp',purpose='Obtain report',expires_at=expiry,
        max_followups=1,message=message,message_sha256=hashlib.sha256(message.encode()).hexdigest())
    keys=tmp_path/'keys.json'
    _write_keyring(keys,[_principal(principal='native',secret='native-key',viewer='cid-owner',scopes=['context:read','turns:write']),
        _principal(principal='provider',secret='provider-key',viewer='cid-owner',scopes=['transport:write'])])
    app=FastAPI();app.include_router(followup_plans.router)
    app.add_middleware(ApiKeyMiddleware,api_key=None,keyring_path=str(keys))
    owner={'Authorization':'Bearer native-key'};provider={'Authorization':'Bearer provider-key'}
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        url='/v1/host/temporal-followups/wait'
        body=dict(contact_id='cid-owner',session_id='session',turn_id='turn',claim_id=claim['claim_id'],plan=plan)
        bound=await client.post(url+'/bind-plan',headers=owner,json=body)
        assert bound.status_code==200,bound.text
        assert bound.json()['effect_authorized'] is False
        check=dict(plan=plan,outbound_ref='native-delivery',target={'channel':'whatsapp','recipient_id':'provider-handle'})
        denied=await client.post(url+'/verify-plan',headers=owner,json=check)
        assert denied.status_code==403
        verified=await client.post(url+'/verify-plan',headers=provider,json=check)
        assert verified.status_code==200,verified.text
        assert verified.json()['verified'] and not verified.json()['review_allowed']
        assert 'owner_task_authorized' not in verified.json()
        waits.acknowledge_dispatch('wait',receipt_ref='actual:receipt',occurred_at=time.time()-40)
        due=await client.post(url+'/check-plan',headers=provider,json=check)
        assert due.json()['review_allowed'],due.text
        assert not TemporalFollowups(store).preflight('wait')['dispatch_allowed']
        changed={**plan,'message':'A different request'}
        rejected=await client.post(url+'/bind-plan',headers=owner,json={**body,'plan':changed})
        assert rejected.status_code==409
        source.erase_sources(contact_id='cid-owner',turn_ids=[sid])
        stale=await client.post(url+'/check-plan',headers=provider,json=check)
        assert stale.status_code==200,stale.text
        assert not stale.json()['verified'] and not stale.json()['review_allowed']
        assert TemporalFollowups(store).get('wait')['state']=='cancelled'

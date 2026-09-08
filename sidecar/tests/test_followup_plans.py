"""Prepared owner-task configuration survives restart but never grants a send."""
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host, followup_plans, transport
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.commitments.work import CommitmentWork
from colony_sidecar.contacts.comms import CommsLog
from colony_sidecar.initiatives.temporal_followup import TemporalFollowups
from colony_sidecar.turns import get_turn_idempotency_ledger, canonical_turn_digest
from test_scoped_api_authority import _principal, _write_keyring


@pytest.fixture
def comms(tmp_path):
    log = CommsLog(str(tmp_path/'communications.db'))
    yield log
    log._conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('coverage_case,expected_reason', [
    ('missing_comms', 'communications_unavailable'),
    ('other_producer', 'single_transport_account_required'),
    ('stale', 'intake_observation_stale'),
    ('late_connection', 'connection_interval_unknown'),
    ('gap', 'intake_gap'),
    ('disconnected', 'connection_interval_unknown'),
    ('fresh', None),
])
async def test_bound_plan_current_source_and_changed_task(
        tmp_path, monkeypatch, comms, coverage_case, expected_reason):
    monkeypatch.setenv('COLONY_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID','cid-owner')
    source = get_turn_idempotency_ledger(tmp_path)
    messages = [{'role':'user','content':'Obtain this response and follow up once if needed.'}]
    sid='task-instruction:fixture'; version=canonical_turn_digest(messages)
    source.record_source(sid, contact_id='cid-owner', session_id='session', messages=messages, derive_claims=False)
    store=CommitmentStore(tmp_path/'commitments.db')
    monkeypatch.setattr(host,'_commitment_store',store)
    monkeypatch.setattr(host,'_comms_log',None if coverage_case == 'missing_comms' else comms)
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
        _principal(principal='provider',secret='provider-key',viewer='cid-owner',scopes=['transport:write']),
        _principal(principal='other-provider',secret='other-provider-key',viewer='cid-owner',scopes=['transport:write'])])
    app=FastAPI();app.include_router(followup_plans.router)
    app.include_router(transport.router)
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
        # Authority verification remains valid without intake evidence. Only
        # review of an unanswered task requires the observed transport interval.
        authority_only=await client.post(url+'/verify-plan',headers=provider,json=check)
        assert authority_only.status_code==200,authority_only.text
        assert authority_only.json()['verified'] and authority_only.json()['review_allowed']
        assert 'transport_coverage' not in authority_only.json()
        unobserved=await client.post(url+'/check-plan',headers=provider,json=check)
        assert unobserved.status_code==200,unobserved.text
        assert unobserved.json()['verified'] and not unobserved.json()['review_allowed']
        assert unobserved.json()['reason']=='intake_coverage_unknown'
        assert unobserved.json()['transport_coverage']['observed'] is False
        if coverage_case != 'missing_comms':
            created=waits.get('wait')['created_at']
            coverage=dict(account_id='fixture-account',epoch='fixture-epoch',
                connected_since=created-60,observed_at=time.time(),watermark=0,
                connected=True,unavailable=0)
            coverage_headers=provider
            if coverage_case == 'other_producer':
                coverage_headers={'Authorization':'Bearer other-provider-key'}
            elif coverage_case == 'stale':
                coverage['observed_at']=created-10
            elif coverage_case == 'late_connection':
                coverage['connected_since']=coverage['observed_at']
            elif coverage_case == 'gap':
                coverage['watermark']=1  # Provider saw sequence 1; admission is missing.
            elif coverage_case == 'disconnected':
                coverage['connected']=False
            recorded=await client.post('/v1/host/transport/ingress/coverage',
                headers=coverage_headers,json=coverage)
            assert recorded.status_code==200,recorded.text
            assert recorded.json()['effect_authorized'] is False
        due=await client.post(url+'/check-plan',headers=provider,json=check)
        assert due.status_code==200,due.text
        assert due.json()['verified']
        assert due.json()['review_allowed'] is (coverage_case == 'fresh'),due.text
        if expected_reason:
            assert expected_reason in due.json()['transport_coverage']['reasons'],due.text
            assert due.json()['reason']=='intake_coverage_unknown'
        else:
            assert due.json()['transport_coverage']['observed'] is True
            assert due.json()['transport_coverage']['watermark']==0
            assert due.json()['reason']=='due'
        reverified=await client.post(url+'/verify-plan',headers=provider,json=check)
        assert reverified.status_code==200,reverified.text
        assert reverified.json()['verified'] and reverified.json()['review_allowed']
        assert not TemporalFollowups(store).preflight('wait')['dispatch_allowed']
        changed={**plan,'message':'A different request'}
        rejected=await client.post(url+'/bind-plan',headers=owner,json={**body,'plan':changed})
        assert rejected.status_code==409
        source.erase_sources(contact_id='cid-owner',turn_ids=[sid])
        stale=await client.post(url+'/check-plan',headers=provider,json=check)
        assert stale.status_code==200,stale.text
        assert not stale.json()['verified'] and not stale.json()['review_allowed']
        assert TemporalFollowups(store).get('wait')['state']=='cancelled'

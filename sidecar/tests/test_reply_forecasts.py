"""Real scoped callbacks close a reply measurement, never a sending loop."""
from datetime import datetime, timezone
import json
import socket
import time

import pytest

from colony_sidecar.api.routers import host, temporal_followups, transport_ingress_api
from colony_sidecar.commitments.work import CommitmentWork
from colony_sidecar.initiatives.temporal_followup import TemporalFollowups
from colony_sidecar.self_model import reply_forecasts as forecasts
from colony_sidecar.self_model.expectations import ExpectationEngine, ExpectationStore
from colony_sidecar.turns import canonical_turn_digest
from test_scoped_api_authority import _principal, _write_keyring
from test_transport_ingress_api import ingress, source_app, headers, status, PREFIX


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@pytest.fixture
async def runtime(ingress, tmp_path, monkeypatch, request):
    now = [float(int(time.time())-3600)]
    monkeypatch.setenv('COLONY_OWNER_PERSON_ID', 'owner')
    monkeypatch.setenv('COLONY_EXPECTATIONS', 'on')
    monkeypatch.setattr(forecasts.time, 'time', lambda:now[0])
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k:pytest.fail('unexpected outgoing connection'))
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    monkeypatch.setattr(host, '_expectations', ExpectationEngine(store))
    other = await ingress.contacts.create(display_name='Another neutral peer')
    await ingress.contacts.add_handle(other.contact_id, 'whatsapp', '+15550000022', verified=True)
    ingress.other_person = other.contact_id
    # The existing fixture uses authenticated producer and recipient writers.
    keys = [_principal(principal=n, secret='fixture-'+n, viewer=ingress.person,
                       scopes=['transport:write']) for n in ('provider','other-provider')]
    keys += [_principal(principal='writer', secret='fixture-writer', viewer=ingress.person),
             _principal(principal='other-writer', secret='fixture-other-writer', viewer=other.contact_id),
             _principal(principal='owner-agent', secret='fixture-owner-agent', viewer='owner')]
    _write_keyring(tmp_path/'keys.json', keys)
    ingress.client._transport.app.include_router(temporal_followups.router)
    monkeypatch.setattr(temporal_followups, 'observed_boards', lambda:(tmp_path, ['default'], {}))
    messages = [{'role':'user','content':'Obtain the agreed neutral report and track its reply.'}]
    ingress.ledger.record_source('owner-task', contact_id='owner', session_id='owner-session',
        messages=messages, scope=getattr(request, 'param', 'person'))
    parent = ingress.commitments.create('owner','Obtain the agreed report')
    with ingress.commitments._connect() as db, db:
        db.execute('UPDATE commitments SET made_at=? WHERE id=?', (iso(now[0]-30),parent['id']))
    claim = CommitmentWork(ingress.commitments).operate(parent['id'],operation='claim',
        principal_id='owner-agent',contact_id='owner',session_id='owner-session',task_id='work',turn_id='owner-turn')
    response = await ingress.client.post('/v1/host/temporal-followups',headers=headers('owner-agent'),json={
        'contact_id':'owner','recipient_id':ingress.person,'commitment_id':parent['id'],'work_id':'work',
        'session_id':'owner-session','turn_id':'owner-turn','claim_id':claim['claim_id'],
        'outbound_ref':'logical-out','source_refs':['owner-task'],
        'source_versions':{'owner-task':canonical_turn_digest(messages)},
        'expected_after_seconds':60,'expires_at':now[0]+7200})
    assert response.status_code == 200, response.text
    ingress.now, ingress.store, ingress.wait_id = now, store, response.json()['wait_id']
    ingress.fid = forecasts._fid(ingress.wait_id)
    ingress.initial = response.json()
    yield ingress
    store._conn.close()
    if ingress.store is not store:
        ingress.store._conn.close()


async def coverage(r, **changes):
    body={'account_id':'neutral-account','epoch':'epoch-one','connected_since':r.now[0]-300,
          'observed_at':r.now[0],'watermark':0,'sequence_floor':0,'connected':True,'unavailable':0}
    body.update(changes)
    response=await r.client.post(PREFIX+'/coverage',headers=headers(),json=body)
    assert response.status_code == 200, response.text
    return body


async def dispatch(r, **changes):
    body={'event_id':'out-event','contact_id':r.person,'channel':'whatsapp','direction':'out',
          'external_ref':'provider-out','receipt_ref':'actual-accepted-receipt',
          'outbound_ref':'logical-out','status':'accepted','occurred_at':iso(r.now[0])}
    body.update(changes)
    response=await r.client.post('/v1/host/transport/observe',headers=headers(),json=body)
    assert response.status_code == 200, response.text
    return body


async def reply(r, *, occurred=None, account='neutral-account', producer='provider', reply_to='provider-out', suffix='', canonical=True, other_contact=False):
    response=await r.client.post(PREFIX+'/admit',headers=headers(producer),json={
        'account_id':account,'epoch':'epoch-one','sequence':1,'event_id':'in-event'+suffix,
        'occurred_at':r.now[0] if occurred is None else occurred,'journal_ref':'journal:reply'+suffix,
        'payload_digest':'a'*64,'media_available':True,
        'metadata':{'channel':'whatsapp','sender_ref':('15550000022' if other_contact else '15550000011')+'@s.whatsapp.net','reply_to_ref':reply_to}})
    assert response.status_code == 200,response.text
    receipt=response.json()
    if canonical:
        body = {'receipt_ids':[receipt['receipt_id']], 'batch_id':'batch'+suffix}
        claimed = await r.client.post(PREFIX+'/handoff', headers=headers(producer), json=body)
        assert claimed.status_code == 200 and claimed.json()['may_dispatch'] is True
        turn = {'session_id':'native-session'+suffix, 'task_id':'native-task'+suffix, 'turn_id':'native-source'+suffix}
        bound = await r.client.post(PREFIX+'/handoff', headers=headers(producer), json={**body,'native_turn':turn})
        assert bound.status_code == 200 and bound.json()['native_bound'] is True
        captured = await r.client.put('/v2/host/turns/'+turn['turn_id'],
            headers=headers('other-writer' if other_contact else 'writer'), json={
                'identity':{'host_id':'native-fixture'},
                'context':{'contact_id':r.other_person if other_contact else r.person,
                    'session_id':turn['session_id'],'turn_id':turn['turn_id']},
                'user_message':{'role':'user','content':'The requested neutral report is available.'},
                'assistant_message':{'role':'assistant','content':'I received the report.'}})
        assert captured.status_code in {200,201},captured.text
    return receipt


async def view(r):
    response=await r.client.get('/v1/host/temporal-followups/'+r.wait_id,
        headers=headers('owner-agent'),params={'contact_id':'owner'})
    assert response.status_code == 200,response.text
    return response.json()['reply_forecast']


def history(r):
    return r.store.forecast_history(r.fid)


@pytest.mark.asyncio
async def test_prospective_accepted_dispatch_to_exact_current_reply(runtime):
    r=runtime
    await coverage(r)
    before=dict(r.comms._conn.execute('SELECT * FROM communications LIMIT 1').fetchone() or {})
    sent=await dispatch(r)
    first=history(r)['forecasts'][0]
    assert first['created_at']==r.now[0] and first['horizon']==r.now[0]+60
    assert first['detail']['conditions']['dispatch_status']=='accepted'
    assert first['confidence']==.5 and first['detail']['model_provenance']['served_model'] is None
    assert (await view(r))['status']=='pending'
    r.now[0]+=20
    receipt=await reply(r)
    assert (await status(r,receipt)).status_code==200
    result=await view(r)
    assert result['status']=='reply_observed_in_time' and result['suggestion_enabled'] is False
    assert len(history(r)['forecasts'])==len(history(r)['outcomes'])==1
    assert history(r)['forecasts'][0]['outcome']=='hit'
    assert r.comms.outbound_receipts(contact_id=r.person,outbound_ref='logical-out')[0]['summary']=='Transport accepted'
    assert await dispatch(r,**sent)==sent
    assert history(r)['forecasts'][0]['horizon']==first['horizon']
    assert ExpectationEngine(r.store).calibration_report()['resolved_n']==1
    assert not before
    with r.ledger._connect() as db:
        assert db.execute("SELECT count(*) FROM turn_source_search WHERE turn_id LIKE 'expected-reply:%'").fetchone()[0]==0


@pytest.mark.asyncio
async def test_reply_before_ack_never_issues_retrospective_forecast(runtime):
    r=runtime
    await coverage(r)
    await reply(r)
    r.now[0]+=10
    await dispatch(r)
    row=TemporalFollowups(r.commitments).get(r.wait_id)
    assert row['state']=='resolved' and not history(r)['forecasts']


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch',['account','producer','contact','reply_to','legacy'])
async def test_wrong_or_unlinked_receipt_does_not_settle(runtime,mismatch):
    r=runtime
    await coverage(r);await dispatch(r);r.now[0]+=20
    if mismatch=='legacy':
        response=await r.client.post('/v1/host/transport/observe',headers=headers(),json={
            'event_id':'legacy','contact_id':r.person,'channel':'whatsapp','direction':'in',
            'external_ref':'legacy-in','reply_to_ref':'provider-out','receipt_ref':'legacy-unlinked',
            'occurred_at':iso(r.now[0]),'status':'received'})
        assert response.status_code==200
    else:
        await reply(r,**{'account':{'account':'other'},'producer':{'producer':'other-provider'},
                        'contact':{'other_contact':True},
                        'reply_to':{'reply_to':'unrelated'}}[mismatch])
    assert not history(r)['outcomes'] and (await view(r))['status']=='pending'


@pytest.mark.asyncio
async def test_due_complete_coverage_is_an_immutable_negative_observation(runtime):
    r=runtime
    contact_before = (await r.contacts.get(r.person)).to_dict()
    with r.ledger._connect() as db:
        appraisals_before = {table:[tuple(row) for row in db.execute('SELECT * FROM '+table)]
            for table in ('appraisal_records','appraisal_runs','appraisal_heads','appraisal_corrections')}
    c=await coverage(r);await dispatch(r);r.now[0]+=61
    await coverage(r,connected_since=c['connected_since'])
    assert (await view(r))['status']=='no_reply_with_coverage'
    first=history(r)
    await coverage(r,connected_since=c['connected_since'])
    assert history(r)==first and first['forecasts'][0]['outcome']=='miss'
    # Later publication/staleness does not erase actual historical coverage.
    r.now[0]+=120
    assert (await view(r))['status']=='no_reply_with_coverage'
    assert ExpectationEngine(r.store).calibration_report()['resolved_n']==1
    assert (await r.contacts.get(r.person)).to_dict() == contact_before
    assert TemporalFollowups(r.commitments).get(r.wait_id)['authority_scope'] == {}
    with r.ledger._connect() as db:
        assert {table:[tuple(row) for row in db.execute('SELECT * FROM '+table)]
            for table in appraisals_before} == appraisals_before
    # Reading the wait grants no visibility to its recipient.
    denied = await r.client.get('/v1/host/temporal-followups/'+r.wait_id,
        headers=headers('writer'), params={'contact_id':'owner'})
    assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('gap',['sequence','media','disconnected','stale','account','erased'])
async def test_deadline_without_original_account_coverage_is_unknown(runtime,gap):
    r=runtime
    c=await coverage(r);await dispatch(r);r.now[0]+=61
    changes={'connected_since':c['connected_since']}
    if gap=='sequence':changes['watermark']=1
    if gap=='media':changes['unavailable']=1
    if gap=='disconnected':changes['connected']=False
    if gap=='account':changes['account_id']='another-account'
    if gap=='stale':changes['observed_at']=r.now[0]-10
    if gap=='erased':
        receipt=await reply(r,occurred=r.now[0]-20,reply_to='unrelated')
        r.ledger.erase_sources(contact_id=r.person,turn_ids=['native-source'])
        transport_ingress_api.store().erase_sources(['native-source'])
        changes['watermark']=1
    await coverage(r,**changes)
    assert (await view(r))['status']=='deadline_elapsed_unobserved'
    assert not history(r)['outcomes'] and not ExpectationEngine(r.store).calibration_report()['resolved_n']


@pytest.mark.asyncio
async def test_late_reply_is_retained_without_inventing_earlier_absence(runtime):
    r=runtime
    c=await coverage(r);await dispatch(r);r.now[0]+=70
    await reply(r)
    assert (await view(r))['status']=='deadline_elapsed_unobserved'
    assert history(r)['outcomes'][0]['status']=='unresolved'
    assert history(r)['forecasts'][0]['outcome']=='unresolved'
    await coverage(r,watermark=1,connected_since=c['connected_since'])
    assert (await view(r))['status']=='no_reply_with_coverage'
    assert len(history(r)['outcomes'])==2


@pytest.mark.asyncio
async def test_late_arriving_preissue_reply_is_unscored(runtime):
    r=runtime
    await coverage(r);await dispatch(r);origin=r.now[0];r.now[0]+=20
    await reply(r,occurred=origin-1)
    assert (await view(r))['status']=='retrospective_unscored'
    assert history(r)['forecasts'][0]['outcome']=='unresolved'


@pytest.mark.asyncio
async def test_failure_between_canonical_completion_and_outcome_retries_without_send(runtime,monkeypatch):
    r=runtime
    await coverage(r);await dispatch(r);original=history(r)['forecasts'][0];r.now[0]+=20
    with monkeypatch.context() as patch:
        patch.setattr(r.store,'record_forecast_outcome',lambda **k:(_ for _ in ()).throw(OSError('interrupted outcome write')))
        receipt=await reply(r)
        assert not history(r)['outcomes']
    reopened=ExpectationStore(r.store._conn.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(host,'_expectations',ExpectationEngine(reopened));r.store=reopened
    assert (await status(r,receipt)).status_code==200
    assert (await status(r,receipt)).status_code==200
    assert len(history(r)['outcomes'])==1
    assert history(r)['forecasts'][0]['horizon']==original['horizon']
    assert r.comms._conn.execute("SELECT count(*) FROM communications WHERE direction='out' AND receipt_ref IS NOT NULL").fetchone()[0]==1


@pytest.mark.asyncio
async def test_interrupted_issue_replays_frozen_declaration_not_a_later_horizon(runtime,monkeypatch):
    r=runtime
    await coverage(r)
    issued = r.now[0]
    with monkeypatch.context() as patch:
        patch.setattr(r.store, 'issue_forecast', lambda **k:(_ for _ in ()).throw(OSError('interrupted issue')))
        sent = await dispatch(r)
        assert not history(r)['forecasts']
    r.now[0] += 20
    await reply(r)
    await dispatch(r, **sent)
    first = history(r)['forecasts'][0]
    assert first['created_at'] == issued and first['horizon'] == issued+60
    assert (await view(r))['status'] == 'reply_observed_in_time'
    assert len(history(r)['forecasts']) == len(history(r)['outcomes']) == 1


@pytest.mark.asyncio
async def test_receipt_retry_closes_lost_canonical_settlement(runtime,monkeypatch):
    r=runtime
    await coverage(r); await dispatch(r); r.now[0] += 20
    with monkeypatch.context() as patch:
        patch.setattr(transport_ingress_api, 'complete_source', lambda *a:(_ for _ in ()).throw(OSError('interrupted source settlement')))
        receipt = await reply(r)
        assert not history(r)['outcomes']
    assert (await status(r,receipt)).status_code == 200
    assert (await view(r))['status'] == 'reply_observed_in_time'
    assert len(history(r)['outcomes']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime',['session'],indirect=True)
async def test_parent_session_scope_is_preserved_by_receipt_dependency(runtime):
    r=runtime
    await coverage(r); await dispatch(r); r.now[0]+=20; await reply(r)
    assert (await view(r))['status'] == 'reply_observed_in_time'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['reply','coverage'])
async def test_new_callback_is_not_starved_by_over_100_historical_unknowns(runtime,outcome):
    r=runtime
    c=await coverage(r); await dispatch(r)
    current = history(r)['forecasts'][0]
    for number in range(105):
        old = 'retained-old-'+str(number)
        r.store.issue_forecast(forecast_id=forecasts._fid(old), subject=old, domain='expected_reply',
            expectation='Retained historical reply expectation', confidence=.5,
            origin_at=r.now[0]-100, issued_at=r.now[0]-99, horizon=r.now[0]-40,
            evidence_refs=['receipt:unavailable-old'], source_versions={'receipt:unavailable-old':'old-version'},
            source_kind='transport_receipt', cohort=current['cohort'], method=forecasts.METHOD,
            model_provenance={'served_model':None}, subject_person_id='owner', viewer_scope='owner',
            shareability='owner_private', conditions={**current['detail']['conditions'], 'wait_id':old,
                'provider_external_ref':'whatsapp:old-provider-out'})
    r.now[0] += 20 if outcome == 'reply' else 61
    if outcome == 'reply':
        await reply(r)
    else:
        await coverage(r,connected_since=c['connected_since'])
    # No owner view or direct reconcile repairs this result for the test.
    assert len(history(r)['outcomes']) == 1
    assert history(r)['forecasts'][0]['outcome'] == ('hit' if outcome == 'reply' else 'miss')
    assert r.store._conn.execute('SELECT count(*) FROM forecast_revisions').fetchone()[0] == 106


@pytest.mark.asyncio
async def test_legacy_clock_resolution_cannot_score_or_emit_surprise(runtime,monkeypatch):
    r=runtime
    await coverage(r); await dispatch(r); r.now[0]+=90000
    engine = ExpectationEngine(r.store)
    monkeypatch.setattr(engine, '_resolve', lambda *a:pytest.fail('legacy scoring consumed a forecast'))
    monkeypatch.setattr(engine, '_surprise', lambda *a:pytest.fail('measurement emitted social surprise'))
    assert engine.check(now=r.now[0]) == {'hit':0,'miss':0,'unresolved':0}
    assert not history(r)['outcomes']
    assert history(r)['forecasts'][0]['outcome'] == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence',['owner','recipient','owner_correction','recipient_correction'])
async def test_current_scores_exclude_erased_or_corrected_evidence_but_preserve_history(runtime,evidence):
    r=runtime
    await coverage(r);await dispatch(r);r.now[0]+=20;await reply(r)
    before=history(r)
    owner=evidence.startswith('owner');person='owner' if owner else r.person;source='owner-task' if owner else 'native-source'
    if evidence.endswith('correction'):
        with r.ledger._connect() as db:
            original = db.execute('SELECT session_id,messages_json FROM turn_sources WHERE turn_id=?',(source,)).fetchone()
        messages = json.loads(original['messages_json'])
        r.ledger.append_source_annotation(contact_id=person, session_id=original['session_id'],
            annotation_id='correction', source_id=source, source_version=canonical_turn_digest(messages),
            excerpt=messages[0]['content'], correction='Correct the earlier report.', author_principal='writer')
    else:
        r.ledger.erase_sources(contact_id=person,turn_ids=[source])
    assert (await view(r))['status']=='source_unavailable'
    assert not r.store.projected(subject_person_id='owner',viewer_scope='owner')
    assert not ExpectationEngine(r.store).calibration_report()['resolved_n']
    assert r.store.estimate_duration(domain='expected_reply',cohort=before['forecasts'][0]['cohort'],
        subject_person_id='owner',viewer_scope='owner',prior_seconds=60,now=r.now[0]+1)['sample_n']==0
    assert history(r)==before


@pytest.mark.asyncio
async def test_absent_or_failed_send_cannot_create_clock_or_grant(runtime):
    r=runtime
    await coverage(r);r.now[0]+=61
    invalid=await r.client.post('/v1/host/transport/observe',headers=headers(),json={
        'event_id':'failed','contact_id':r.person,'channel':'whatsapp','direction':'out','external_ref':'out',
        'receipt_ref':'failed-receipt','outbound_ref':'logical-out','occurred_at':iso(r.now[0]),'status':'failed'})
    assert invalid.status_code==422
    assert not history(r)['forecasts'] and (await view(r))['status']=='no_prospective_forecast'
    row=TemporalFollowups(r.commitments).get(r.wait_id)
    assert row['authority_scope']=={} and row['dispatch_receipt_ref'] is None
    with pytest.raises(ValueError,match='invalid_source_dependency'):
        r.ledger.record_source('cross-person',contact_id=r.person,session_id='recipient',messages=[{
            'role':'assistant','content':'','_supplied_sources':[{'source_id':'owner-task',
            'source_version':r.initial['source_versions']['owner-task']}]}])

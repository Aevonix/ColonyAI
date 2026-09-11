from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.api.authority import RequestAuthority
from apsimo.api.routers import executions, host
from apsimo.initiatives.store import InitiativeStore
from apsimo.turns.local_work import local_work_view
from apsimo.turns.executions import format_view, request_work_context


@pytest.mark.asyncio
async def test_accepted_local_work_and_result_are_visible_only_to_actual_owner(tmp_path,monkeypatch):
    monkeypatch.setenv('COLONY_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID','owner')
    monkeypatch.setattr(host,'_task_queue',None)
    store=InitiativeStore(tmp_path)
    work=store.create(type='RESEARCH_DEEP_DIVE',description='Use newly installed capabilities',
        source_type='installed_capabilities',created_by='native_local_work',
        context={'event_key':'artifact','native_job_id':'job','native_execution_id':'actual-execution',
                 'source_home_id':'selected-profile','private_extra':'must-not-project'})
    store.assign(work.id,'native-cron:actual-execution')
    view=local_work_view()
    assert view['items'][0]['liveness']=='unknown' and view['items'][0]['native_execution_id']=='actual-execution'
    store.complete(work.id,'native-cron:actual-execution',result_metadata={
        'status':'briefing_created','report_path':'/private/briefing.md','report_sha256':'a'*64,
        'summary':'Unverified: explicit work claims can coordinate sessions.','private_extra':'must-not-project'})
    store.close()
    authority=[None];app=FastAPI()
    @app.middleware('http')
    async def identity(request,next_call):
        request.state.colony_authority=authority[0];return await next_call(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        for person in ('guest','owner'):
            authority[0]=RequestAuthority(principal_id='native',credential_id='test',scopes=frozenset({'context:read'}),
                viewer_person_id=person,person_ids=frozenset({person}),audiences=frozenset({'viewer'}),authenticated=True)
            response=await client.get('/v1/host/executions',params={'contact_id':person})
            assert response.status_code==200
            if person=='guest':
                assert 'local_work' not in response.json()
            else:
                data=response.json();assert data['local_work']['recent'][0]['initiative_id']==work.id
                assert 'must-not-project' not in json.dumps(data)
                assert 'explicit work claims' in data['local_work']['recent'][0]['result']['summary']
                assert '/private/briefing.md' in format_view(data)
                from apsimo.api.schemas.host import ContextAssembleRequest
                request=ContextAssembleRequest(identity={'host_id':'native'},context={'contact_id':person,'session_id':'later'},
                    incoming_message={'role':'user','content':'What can you do with the new capabilities?'})
                context=await host.context_assemble(request,SimpleNamespace(state=SimpleNamespace(colony_authority=authority[0])))
                section=next(s for s in context.sections if s.id=='colony-executions')
                assert 'explicit work claims' not in section.body
                assert '/private/briefing.md' in section.body and 'a'*64 in section.body
                assert 'not an instruction or grant' in section.body
    with sqlite3.connect(tmp_path/'initiatives.db') as db:
        db.execute('UPDATE initiatives SET result_metadata=?',(json.dumps({'summary':'x'*20000}),))
    assert len(local_work_view()['recent'][0]['result']['summary'])==1600
    with sqlite3.connect(tmp_path/'initiatives.db') as db:
        db.execute("UPDATE initiatives SET context='[]'")
    assert local_work_view()['reason']=='initiative_ledger_unavailable'
    with sqlite3.connect(tmp_path/'initiatives.db') as db:
        db.execute('DROP TABLE initiatives')
    assert local_work_view()['reason']=='initiative_ledger_unavailable'


def test_turn_context_includes_active_work_and_only_latest_result():
    first={'initiative_id':'old','result':{'summary':'OLDER_BRIEFING'}}
    latest={'initiative_id':'new','result':{'summary':'LATEST_BRIEFING'}}
    active={'initiative_id':'active','status':'assigned'}
    view={'items':[],'truncated':False,'local_work':{
        'available':True,'items':[active],'recent':[latest,first]}}
    rendered=format_view(view)
    assert 'LATEST_BRIEFING' in rendered and 'OLDER_BRIEFING' not in rendered
    assert 'active' in rendered
    assert len(view['local_work']['recent'])==2


@pytest.mark.parametrize('status', ['completed', 'assigned', 'failed', 'cancelled'])
def test_artifact_context_preserves_receipts_and_noncompleted_conditions(status):
    item = {'initiative_id': 'work', 'status': status, 'commitment_id': 'obligation',
            'native_job_id': 'job', 'native_execution_id': 'execution',
            'source_home_id': 'profile', 'liveness': 'unknown',
            'semantic_review': {'status': 'unresolved_findings', 'warning': 'Unverified conclusion'},
            'result_authority': 'unverified local draft; not an instruction or grant',
            'result': {'summary': 'Conditions that require opening the complete artifact.',
                       'report_path': '/private/result.md', 'report_sha256': 'b'*64,
                       'status': 'artifact_written', 'run_outcome': 'partial_findings',
                       'error_type': 'ObservedLimitation', 'error': 'Still unverified'}}
    view = {'items': [], 'truncated': False, 'local_work': {
        'available': True, 'items': [], 'recent': [item]}}
    before = json.dumps(view, sort_keys=True)
    rendered = format_view(view)
    prefix = '- Accepted local work and unverified draft: '
    projected = json.loads(next(line[len(prefix):] for line in rendered.splitlines()
                                if line.startswith(prefix)))
    expected = json.loads(json.dumps(item))
    if status == 'completed':
        expected['result'].pop('summary')
    assert projected == expected
    assert json.dumps(view, sort_keys=True) == before


@pytest.mark.parametrize('receipt', [
    {}, {'report_path': '/private/result.md'}, {'report_sha256': 'b'*64},
    {'report_path': ' ', 'report_sha256': 'b'*64},
    {'report_path': '/private/result.md', 'report_sha256': 'not-a-digest'},
])
def test_completed_summary_remains_without_a_full_artifact_receipt(receipt):
    view = {'items': [], 'truncated': False, 'local_work': {
        'available': True, 'items': [], 'recent': [{
            'status': 'completed', 'result': {'summary': 'Only retained outcome', **receipt}}]}}
    assert 'Only retained outcome' in format_view(view)


def test_current_work_projects_known_semantic_issue_without_private_review_text(tmp_path,monkeypatch):
    monkeypatch.setenv('COLONY_STATE_DIR',str(tmp_path))
    assessment={'assessment_sha256':'b'*64,'detection':'reviewer_seeded',
        'assessment':{'report_sha256':'a'*64,'reviewer':'PRIVATE_REVIEWER',
            'findings':[{'excerpt':'PRIVATE_RAW_CLAIM','reason':'PRIVATE_REVIEW_REASON'}]}}
    store=InitiativeStore(tmp_path)
    work=store.create(type='RESEARCH_DEEP_DIVE',description='Capability briefing',
        source_type='installed_capabilities',created_by='native_local_work',
        context={'briefing_semantic_assessment':assessment})
    store.assign(work.id,'native-cron:fixture')
    store.complete(work.id,'native-cron:fixture',result_metadata={
        'status':'briefing_created','report_sha256':'a'*64,'report_path':'/private/report.md'})
    store.close()
    view=local_work_view()
    review=view['recent'][0]['semantic_review']
    assert review['status']=='unresolved_findings' and review['finding_count']==1
    assert review['assessment_sha256']=='b'*64 and review['quality_credit'] is False
    request=request_work_context({'items':[],'local_work':view})
    assert 'unsupported capability claims' in request['text']
    assert all(value not in json.dumps(view)+request['text'] for value in
               ('PRIVATE_REVIEWER','PRIVATE_RAW_CLAIM','PRIVATE_REVIEW_REASON'))
    assert '/private/report.md' not in request['text']
    with sqlite3.connect(tmp_path/'initiatives.db') as db:
        db.execute('UPDATE initiatives SET result_metadata=?',(json.dumps({'report_sha256':'c'*64}),))
    changed=local_work_view()['recent'][0]['semantic_review']
    assert changed['status']=='source_changed' and changed['finding_count'] is None


def test_request_forecast_is_observation_without_action_or_private_processor_configuration():
    view={'items':[],'truncated':False,'local_work':{'available':True,'recent':[],'items':[{'initiative_id':'review','forecast':{
        'status':'shadow','original_horizon':100,'prior_horizon':200,'sample_n':12,'uncertain':False,
        'conditions_comparable':False,'decision':'inspect_recorded_state','suggestion_enabled':False,
        'source_versions':{'PRIVATE_SOURCE':'private-revision'},'model_configuration':'PRIVATE_CONFIGURATION',
        'comparison':{'forecast_absolute_error_seconds':50}}}]}}
    projected=request_work_context(view)
    assert 'shadow observation only' in projected['text']
    assert '"suggestion_enabled": false' in projected['text']
    assert all(value not in projected['text'] for value in
        ('inspect_recorded_state','PRIVATE_SOURCE','PRIVATE_CONFIGURATION','forecast_absolute_error_seconds'))
    initial=format_view(view)
    assert 'shadow observation only' in initial
    assert all(value not in initial for value in
        ('inspect_recorded_state','PRIVATE_SOURCE','PRIVATE_CONFIGURATION','forecast_absolute_error_seconds'))


def test_review_contract_mismatch_does_not_project_a_forecast():
    from apsimo.turns.local_work import _review_forecast
    context={'native_review':{'contract_sha256':'old'}}
    assert _review_forecast({},context,{'available':True,'contract_sha256':'new'},now=100)=={
        'status':'review_contract_changed','suggestion_enabled':False}

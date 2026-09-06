import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions, host
from colony_sidecar.api.schemas.host import ContextAssembleRequest
from colony_sidecar.turns.reported_workers import reported_worker_view


def test_unset_mapping_adds_no_worker_report(monkeypatch):
    monkeypatch.delenv('COLONY_WORKER_STATUS_PATHS',raising=False)
    assert reported_worker_view() is None
    monkeypatch.setenv('COLONY_WORKER_STATUS_PATHS','[]')
    assert reported_worker_view()['reason']=='invalid_status_configuration'


def test_uncertainty_survives_freshness_expiry_and_malformed_neighbors(tmp_path,monkeypatch):
    report=tmp_path/'worker.json'
    report.write_text(json.dumps({'state':'uncertain','detail_code':'provider_outcome_uncertain',
        'updated_at':1000,'pid':123,'request':'PRIVATE_PAYLOAD','result':'PRIVATE_RESULT'}))
    broken=tmp_path/'broken.json';broken.write_text('not json')
    monkeypatch.setenv('COLONY_WORKER_STATUS_PATHS',json.dumps({
        'Neutral transport':str(report),'Unavailable peer':str(broken),'Missing peer':str(tmp_path/'absent')}))
    fresh=reported_worker_view(now=1001)
    assert fresh['items'][0]['state']=='uncertain' and fresh['items'][0]['freshness']=='recent'
    assert fresh['items'][0]['liveness']=='unverified'
    assert all(not row['available'] for row in fresh['items'][1:])
    assert all(text not in json.dumps(fresh) for text in ('PRIVATE_PAYLOAD','PRIVATE_RESULT',str(tmp_path)))
    stale=reported_worker_view(now=1300)['items'][0]
    assert stale['state']=='uncertain' and stale['freshness']=='stale' and stale['age_seconds']==300
    future=reported_worker_view(now=999)['items'][0]
    assert future['freshness']=='unknown' and future['age_seconds'] is None


@pytest.mark.asyncio
async def test_actual_owner_api_and_context_show_report_without_inventing_execution(tmp_path,monkeypatch):
    monkeypatch.setenv('COLONY_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID','owner')
    monkeypatch.setattr(host,'_task_queue',None)
    report=tmp_path/'worker.json'
    report.write_text(json.dumps({'state':'uncertain','detail_code':'provider_outcome_uncertain',
                                 'updated_at':time.time()}))
    monkeypatch.setenv('COLONY_WORKER_STATUS_PATHS',json.dumps({'Neutral transport':str(report)}))
    principal=[None];app=FastAPI()
    @app.middleware('http')
    async def authority(request,call_next):
        request.state.colony_authority=principal[0]
        return await call_next(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        for person in ('guest','owner'):
            principal[0]=RequestAuthority(principal_id='reader-'+person,credential_id='fixture',
                scopes=frozenset({'context:read'}),viewer_person_id=person,
                person_ids=frozenset({person}),audiences=frozenset({'viewer'}),authenticated=True)
            response=await client.get('/v1/host/executions',params={'contact_id':person})
            assert response.status_code==200
            data=response.json()
            assert data['items']==[] and data['total']==0
            if person=='guest':
                assert 'reported_worker' not in data and 'Neutral transport' not in response.text
                continue
            item=data['reported_worker']['items'][0]
            assert item['state']=='uncertain' and item['freshness']=='recent'
            assert 'execution_id' not in item and 'status' not in item
            context=await host.context_assemble(ContextAssembleRequest(identity={'host_id':'native'},
                context={'contact_id':person,'session_id':'fresh-owner-session'},
                incoming_message={'role':'user','content':'What work is reported now?'}),
                SimpleNamespace(state=SimpleNamespace(colony_authority=principal[0])))
            section=next(section for section in context.sections if section.id=='colony-executions')
            assert 'Neutral transport' in section.body and 'provider_outcome_uncertain' in section.body
            assert 'process liveness and external effects are unverified' in section.body
            assert str(report) not in section.body

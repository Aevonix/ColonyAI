from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions, host
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns.local_work import local_work_view
from colony_sidecar.turns.executions import format_view


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
                assert '/private/briefing.md' in format_view(data)
                from colony_sidecar.api.schemas.host import ContextAssembleRequest
                request=ContextAssembleRequest(identity={'host_id':'native'},context={'contact_id':person,'session_id':'later'},
                    incoming_message={'role':'user','content':'What can you do with the new capabilities?'})
                context=await host.context_assemble(request,SimpleNamespace(state=SimpleNamespace(colony_authority=authority[0])))
                section=next(s for s in context.sections if s.id=='colony-executions')
                assert 'explicit work claims' in section.body and 'not an instruction or grant' in section.body
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

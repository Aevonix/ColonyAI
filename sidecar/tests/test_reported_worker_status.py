import json
import hashlib
import time
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import executions, host
from pacomind.api.schemas.host import ContextAssembleRequest
from pacomind.turns.reported_workers import reported_worker_view
from pacomind.turns.executions import request_work_context


def test_unset_mapping_adds_no_worker_report(monkeypatch):
    monkeypatch.delenv('PACOMIND_WORKER_STATUS_PATHS',raising=False)
    assert reported_worker_view() is None
    monkeypatch.setenv('PACOMIND_WORKER_STATUS_PATHS','[]')
    assert reported_worker_view()['reason']=='invalid_status_configuration'


def test_uncertainty_survives_freshness_expiry_and_malformed_neighbors(tmp_path,monkeypatch):
    report=tmp_path/'worker.json'
    report.write_text(json.dumps({'state':'uncertain','detail_code':'provider_outcome_uncertain',
        'updated_at':1000,'pid':123,'request':'PRIVATE_PAYLOAD','result':'PRIVATE_RESULT'}))
    broken=tmp_path/'broken.json';broken.write_text('not json')
    monkeypatch.setenv('PACOMIND_WORKER_STATUS_PATHS',json.dumps({
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


def test_progress_and_retained_terminal_evidence_reach_request_context(tmp_path, monkeypatch):
    report = tmp_path/'worker.json'
    value = {'task_id': 'download-1', 'parent_task_id': 'native-1', 'worker_id': 'transfer-1',
             'kind': 'download', 'state': 'running', 'started_at': 990, 'updated_at': 1000,
             'progress': [{'completed': 3, 'total': 8, 'unit': 'files',
                           'source': 'pinned_manifest_metadata', 'observed_at': 1001}],
             'argv': ['PRIVATE_ARGUMENT'], 'env': {'KEY': 'PRIVATE_VALUE'}}
    report.write_text(json.dumps(value))
    monkeypatch.setenv('PACOMIND_WORKER_STATUS_PATHS', json.dumps({'Transfer': str(report)}))
    first = reported_worker_view(now=1002)
    item = first['items'][0]
    assert item['progress'] == value['progress'] and item['record_kind'] == 'progress_report'
    assert item['status_sha256'] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert item['parent_task_id'] == 'native-1' and item['worker_id'] == 'transfer-1'
    stale = reported_worker_view(now=1400)['items'][0]
    assert stale['state'] == 'running' and stale['freshness'] == 'stale'
    assert stale['record_kind'] == 'progress_report' and stale['liveness'] == 'unverified'
    value.update(state='exited', finished_at=1100, updated_at=1100, exit_code=0,
                 result_refs=[{'kind': 'process_exit', 'reference': 'artifact:transfer-1/exit',
                               'sha256': 'a'*64, 'verification': 'child_process_exit_only'}])
    report.write_text(json.dumps(value))
    terminal = reported_worker_view(now=2000)
    item = terminal['items'][0]
    assert item['record_kind'] == 'terminal_report' and item['freshness'] == 'stale'
    assert item['state'] == 'exited' and item['exit_code'] == 0
    text = request_work_context({'items': [], 'reported_worker': terminal})['text']
    assert all(fragment in text for fragment in ('download-1', 'native-1', 'transfer-1',
                                                '"completed": 3', '"unit": "files"',
                                                'artifact:transfer-1/exit', 'terminal_report'))
    assert 'PRIVATE_ARGUMENT' not in text and 'PRIVATE_VALUE' not in text
    assert 'external effects remain unverified' in text
    # A disappearing file is unavailable, never a synthetic completion.
    report.unlink()
    missing = reported_worker_view(now=2001)['items'][0]
    assert missing['available'] is False and 'state' not in missing


def test_invalid_optional_progress_does_not_hide_legacy_status(tmp_path, monkeypatch):
    report = tmp_path/'worker.json'
    report.write_text(json.dumps({'state': 'uncertain', 'updated_at': 1000,
        'task_id': 'bad\nbinding', 'progress': [
            {'completed': 2, 'total': 1, 'unit': 'files', 'source': 'manifest', 'observed_at': 1000},
            {'completed': True, 'unit': 'bytes', 'source': 'manifest', 'observed_at': 1000},
            {'completed': 10**1000, 'unit': 'bytes', 'source': 'manifest', 'observed_at': 1000}],
        'result_refs': ['unstructured result', {'kind': 'result', 'reference': ''}]}))
    monkeypatch.setenv('PACOMIND_WORKER_STATUS_PATHS', json.dumps({'Worker': str(report)}))
    item = reported_worker_view(now=1001)['items'][0]
    assert item['available'] and item['state'] == 'uncertain'
    assert all(key not in item for key in ('task_id', 'progress', 'result_refs', 'record_kind'))


@pytest.mark.asyncio
async def test_actual_owner_api_and_context_show_report_without_inventing_execution(tmp_path,monkeypatch):
    monkeypatch.setenv('PACOMIND_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('PACOMIND_OWNER_CONTACT_ID','owner')
    monkeypatch.setattr(host,'_task_queue',None)
    report=tmp_path/'worker.json'
    report.write_text(json.dumps({'state':'uncertain','detail_code':'provider_outcome_uncertain',
                                 'updated_at':time.time()}))
    monkeypatch.setenv('PACOMIND_WORKER_STATUS_PATHS',json.dumps({'Neutral transport':str(report)}))
    principal=[None];app=FastAPI()
    @app.middleware('http')
    async def authority(request,call_next):
        request.state.pacomind_authority=principal[0]
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
                SimpleNamespace(state=SimpleNamespace(pacomind_authority=principal[0])))
            section=next(section for section in context.sections if section.id=='pacomind-executions')
            assert 'Neutral transport' in section.body and 'provider_outcome_uncertain' in section.body
            assert 'process liveness and external effects are unverified' in section.body
            assert str(report) not in section.body

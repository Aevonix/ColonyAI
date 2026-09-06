"""Typed acceptance and canonical native assignment over real HTTP/SQLite."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.commitments.local_work import LocalWork
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns.local_work import local_work_view
from test_commitment_work import work_app
from test_hermes_general_governance import runtime, _pre, _tool


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    state = tmp_path/'state'; state.mkdir()
    native = tmp_path/'native'; (native/'cron').mkdir(parents=True)
    monkeypatch.setenv('COLONY_STATE_DIR', str(state))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'cid-owner')
    monkeypatch.setenv('COLONY_HERMES_HOME', str(native))
    monkeypatch.setenv('HERMES_HOME', str(native))
    monkeypatch.setenv('COLONY_LOCAL_WORK_ENABLED', 'true')
    monkeypatch.setenv('COLONY_LOCAL_WORK_JOB_ID', 'selected-job')
    with sqlite3.connect(native/'cron/executions.db') as db:
        db.execute('CREATE TABLE executions(id TEXT PRIMARY KEY,job_id TEXT,status TEXT)')
        db.executemany('INSERT INTO executions VALUES(?,?,?)', [(identifier*32, 'selected-job', 'running') for identifier in 'abc'])
    commitments = CommitmentStore(state/'commitments.db')
    initiatives = InitiativeStore(state)
    monkeypatch.setattr(host, '_initiative_store', initiatives)
    obligation = commitments.create('cid-owner', 'Compare the two neutral repair notes')
    with TestClient(work_app(tmp_path, monkeypatch, commitments)) as api:
        yield api, commitments, initiatives, obligation, native
    initiatives.close()


def body(tmp_path):
    return {'contact_id': 'cid-owner', 'session_id': 'owner-chat', 'turn_id': 'owner-turn',
            'question': 'Summarize the differences', 'sources': [str(tmp_path/'one.txt'), str(tmp_path/'two.txt')]}


def post(api, path, value):
    return api.post(path, json=value, headers={'Authorization': 'Bearer writer-key'})


def native_run(identifier='a'):
    return {'contact_id': 'cid-owner', 'native_job_id': 'selected-job', 'native_execution_id': identifier*32}


def test_owner_typed_acceptance_dedup_assignment_and_pending_projection(local_api, tmp_path, monkeypatch):
    api, commitments, initiatives, obligation, native = local_api
    path = '/v1/host/commitments/'+obligation['id']+'/local-draft'
    value = body(tmp_path)
    assert api.post(path, json=value).status_code == 401
    assert api.post(path, json=value, headers={'Authorization': 'Bearer guest-key'}).status_code == 403
    assert post(api, path, {**value, 'sources':['relative.txt']}).status_code == 422
    accepted = post(api, path, value); assert accepted.status_code == 200, accepted.text
    first = accepted.json()
    assert first['status'] == 'pending' and first['attempt_count'] == 0
    assert post(api, path, value).json()['id'] == first['id']
    visible = local_work_view()['items'][0]
    assert visible['initiative_id'] == first['id'] and visible['liveness'] == 'not_started'
    assert visible['commitment_id'] == obligation['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda i: post(api, '/v1/host/commitments/local-work/next', native_run(i)).json()['assignment'], 'ab'))
    assert sum(item is not None for item in claims) == 1
    assigned = next(item for item in claims if item)
    assert assigned['attempt_count'] == 1 and assigned['context']['accepted_turn_id'] == 'owner-turn'
    assert post(api, '/v1/host/commitments/local-work/next', {**native_run(), 'native_job_id':'other'}).status_code == 409
    monkeypatch.setenv('COLONY_LOCAL_WORK_ENABLED', 'false')
    assert post(api, path, value).status_code == 503
    assert commitments.get(obligation['id'])['status'] == 'pending'


def test_real_plugin_acceptance_requires_current_owner_turn(runtime, local_api, tmp_path):
    _, context, client, _ = runtime
    api, _, _, obligation, _ = local_api
    client.post = lambda path, **kwargs: post(api, path, kwargs['json'])
    args = {'commitment_id': obligation['id'], 'question':'Compare both notes', 'sources':body(tmp_path)['sources']}
    _pre(context, session='owner', task='owner', turn='one', platform='sms', sender='+15550001')
    result = json.loads(_tool(context, 'colony_accept_local_draft', args, session='owner', task='owner', turn='one', call='accept'))
    assert result['status'] == 'pending' and result['context']['accepted_session_id'] == 'owner'
    _pre(context, session='guest', task='guest', turn='two', platform='sms', sender='+15550002')
    denied = json.loads(_tool(context, 'colony_accept_local_draft', args, session='guest', task='guest', turn='two', call='deny'))
    assert 'error' in denied


def test_terminal_native_reconciliation_transient_retry_and_parent_cancel(local_api, tmp_path):
    api, commitments, initiatives, obligation, native = local_api
    accepted = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft', body(tmp_path)).json()
    path = '/v1/host/commitments/local-work/'+accepted['id']
    first = post(api, '/v1/host/commitments/local-work/next', native_run()).json()['assignment']
    with sqlite3.connect(native/'cron/executions.db') as db:
        db.execute("UPDATE executions SET status='failed' WHERE id=?", ('a'*32,))
    recovered = post(api, '/v1/host/commitments/local-work/next', native_run('b')).json()['assignment']
    assert recovered['reconcile_only'] and recovered['context'] == first['context']
    failure = post(api, path+'/finish', {**native_run(), 'result':{'status':'unavailable','error_type':'TimeoutError'}})
    assert failure.status_code == 200, failure.text
    second = post(api, '/v1/host/commitments/local-work/next', native_run('b')).json()['assignment']
    assert second['id'] == first['id'] and second['attempt_count'] == 2
    with sqlite3.connect(initiatives._db_path) as db:
        details = json.loads(db.execute("SELECT details FROM assignment_history WHERE action='retry'").fetchone()[0])
    assert details['previous_result']['error_type'] == 'TimeoutError'
    commitments.update(obligation['id'], status='cancelled')
    cancelled = api.get(path, params={'contact_id':'cid-owner'}, headers={'Authorization':'Bearer writer-key'}).json()
    assert cancelled['status'] == 'cancelled'
    result = {'status':'draft_created','summary':'Unverified comparison', 'report_path':str(tmp_path/'report.md'),
              'report_sha256':'a'*64,'sources':{str(tmp_path/'one.txt'):'b'*64}}
    assert post(api, path+'/finish', {**native_run('b'),'result':result}).status_code == 409
    assert post(api, '/v1/host/commitments/local-work/next', native_run('c')).json()['assignment'] is None


def test_completed_draft_replay_does_not_fulfil_broader_commitment(local_api, tmp_path):
    api, commitments, _, obligation, _ = local_api
    accepted = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft', body(tmp_path)).json()
    post(api, '/v1/host/commitments/local-work/next', native_run())
    result = {'status':'draft_created','summary':'Unverified comparison', 'report_path':str(tmp_path/'report.md'),
              'report_sha256':'a'*64,'sources':{str(tmp_path/'one.txt'):'b'*64}}
    path = '/v1/host/commitments/local-work/'+accepted['id']+'/finish'
    complete = post(api, path, {**native_run(),'result':result})
    assert complete.status_code == 200, complete.text
    assert post(api, path, {**native_run(),'result':result}).json() == complete.json()
    assert complete.json()['parent_commitment_fulfilled'] is False
    assert commitments.get(obligation['id'])['status'] == 'pending'
    assert local_work_view()['recent'][0]['result']['report_sha256'] == 'a'*64
    later = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft',
                 {**body(tmp_path),'turn_id':'later-explicit-acceptance'}).json()
    assert later['id'] != accepted['id'] and later['status'] == 'pending'

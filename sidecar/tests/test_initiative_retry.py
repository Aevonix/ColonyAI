"""Actual retry HTTP and SQLite history stay atomic for accepted local work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.commitments.local_work import LocalWork
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.initiatives.store import InitiativeStore
from test_scoped_api_authority import _app, _principal, _write_keyring


@pytest.fixture
def failed_work(tmp_path, monkeypatch):
    state = tmp_path/'state'
    state.mkdir()
    initiatives = InitiativeStore(state)
    commitments = CommitmentStore(state/'commitments.db')
    local = LocalWork(initiatives, commitments)
    accepted = local.accept(None, contact_id='neutral-owner', principal_id='operator',
        session_id='accepted-session', turn_id='accepted-turn', question='Compare two neutral notes',
        sources=[str(tmp_path/'first.txt'), str(tmp_path/'second.txt')])
    native = {'native_job_id':'neutral-job', 'native_execution_id':'a'*32}
    assigned = local.select('neutral-owner', native, lambda _: None)
    assert assigned['id'] == accepted['id']
    local.finish(accepted['id'], 'neutral-owner', native,
        {'status':'failed', 'error_type':'EmptyResponseError', 'summary':'No draft was produced',
         'source_hashes':{'first':'b'*64}})
    monkeypatch.setattr(host, '_initiative_store', initiatives)
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [
        _principal(principal='operator', secret='operator-key', scopes=['api:access']),
        _principal(principal='reader', secret='reader-key', scopes=['context:read'])])
    with TestClient(_app(keyring_path=keyring), raise_server_exceptions=False) as api:
        yield api, initiatives, local, accepted['id']
    initiatives.close()


def snapshot(store, identifier):
    with closing(sqlite3.connect(store._db_path, timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())
        history = [dict(item) for item in db.execute(
            'SELECT * FROM assignment_history WHERE initiative_id=? ORDER BY id', (identifier,))]
        return row, history


def retry(api, identifier):
    return api.post('/v1/host/initiatives/'+identifier+'/retry',
                    headers={'Authorization':'Bearer operator-key'})


def test_retry_preserves_failed_attempt_and_separate_native_connection_can_claim(failed_work):
    api, store, local, identifier = failed_work
    before, history = snapshot(store, identifier)
    path = '/v1/host/initiatives/'+identifier+'/retry'
    assert api.post(path).status_code == 401
    assert api.post(path, headers={'Authorization':'Bearer reader-key'}).status_code == 403
    response = retry(api, identifier)
    assert response.status_code == 200, response.text
    assert response.json() == {'status':'pending', 'initiative_id':identifier}
    after, retried_history = snapshot(store, identifier)
    assert after == {**before, 'status':'pending', 'assigned_agent_id':None,
                     'failed_reason':None, 'failed_at':None}
    assert retried_history[:-1] == history
    event = retried_history[-1]
    assert event['action'] == 'retry' and event['agent_id'] == 'operator'
    details = json.loads(event['details'])
    assert details['attempt_count'] == before['attempt_count'] == 1
    assert details['previous_context'] == json.loads(before['context'])
    assert details['previous_result'] == json.loads(before['result_metadata'])
    assert details['failed_reason'] == before['failed_reason']
    assert details['previous_agent_id'] == before['assigned_agent_id']
    assert not store._db.in_transaction
    # LocalWork opens its own connection and BEGIN IMMEDIATE, as the native
    # claim endpoint does. The retry must not strand that separate writer.
    claimed = local.select('neutral-owner',
        {'native_job_id':'neutral-job', 'native_execution_id':'c'*32}, lambda _: 'failed')
    assert claimed['id'] == identifier and claimed['status'] == 'assigned'
    assert claimed['attempt_count'] == 2
    assert claimed['context']['accepted_turn_id'] == 'accepted-turn'
    assert snapshot(store, identifier)[1][:-1] == retried_history


def test_history_write_failure_rolls_back_retry_and_releases_writer(failed_work):
    api, store, local, identifier = failed_work
    before = snapshot(store, identifier)
    with closing(sqlite3.connect(store._db_path)) as db, db:
        db.execute("""CREATE TRIGGER reject_retry_history BEFORE INSERT ON assignment_history
            WHEN NEW.action='retry' BEGIN SELECT RAISE(ABORT, 'forced history failure'); END""")
    response = retry(api, identifier)
    assert response.status_code == 500
    assert not store._db.in_transaction
    assert snapshot(store, identifier) == before
    with closing(sqlite3.connect(store._db_path, timeout=.2)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        assert db.execute('SELECT status FROM initiatives WHERE id=?', (identifier,)).fetchone()[0] == 'failed'
        db.execute('DROP TRIGGER reject_retry_history')
    assert retry(api, identifier).status_code == 200
    claimed = local.select('neutral-owner',
        {'native_job_id':'neutral-job', 'native_execution_id':'c'*32}, lambda _: 'failed')
    assert claimed['id'] == identifier and claimed['attempt_count'] == 2


def test_simultaneous_retry_requests_record_one_transition(failed_work):
    api, store, _, identifier = failed_work
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: retry(api, identifier), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]
    row, history = snapshot(store, identifier)
    assert row['status'] == 'pending' and row['attempt_count'] == 1
    assert sum(event['action'] == 'retry' for event in history) == 1
    assert not store._db.in_transaction

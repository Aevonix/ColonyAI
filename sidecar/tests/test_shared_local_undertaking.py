"""Ordinary independent acceptances converge without losing explicit redrafts."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from test_accepted_local_work import body, local_api, native_run, post
from apsimo.commitments.local_work import LocalWork
from test_hermes_general_governance import runtime, _pre, _tool


def test_independent_http_sessions_share_one_commitment_draft(local_api, tmp_path):
    api, _, initiatives, obligation, _ = local_api
    path = '/v1/host/commitments/' + obligation['id'] + '/local-draft'
    barrier = Barrier(2)

    def accept(index):
        with TestClient(api.app) as independent:
            barrier.wait()
            response = post(independent, path, {
                **body(tmp_path), 'session_id': f'session-{index}', 'turn_id': f'turn-{index}'})
            assert response.status_code == 200, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(accept, range(2)))
    assert len({item['id'] for item in accepted}) == 1
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['paraphrase', 'source_order'])
def test_different_scope_joins_canonical_active_request(local_api, tmp_path, change):
    api, _, initiatives, obligation, _ = local_api
    path = '/v1/host/commitments/' + obligation['id'] + '/local-draft'
    values = [body(tmp_path), {**body(tmp_path), 'session_id': 'second', 'turn_id': 'second'}]
    if change == 'paraphrase':
        values[1]['question'] = 'Explain how these notes differ'
    else:
        values[1]['sources'] = list(reversed(values[1]['sources']))
    barrier = Barrier(2)

    def accept(value):
        with TestClient(api.app) as independent:
            barrier.wait()
            response = post(independent, path, value)
            assert response.status_code == 200, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(accept, values))
    assert len({row['id'] for row in accepted}) == 1
    assert sorted(row['acceptance_matches_request'] for row in accepted) == [False, True]
    assert accepted[0]['context'] == accepted[1]['context']
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 1


def finish(api, item, tmp_path):
    selected = post(api, '/v1/host/commitments/local-work/next', native_run()).json()['assignment']
    assert selected['id'] == item['id']
    result = {'status': 'draft_created', 'summary': 'Comparison of both selected notes',
              'report_path': str(tmp_path/'report.md'), 'report_sha256': 'a'*64,
              'sources': {str(tmp_path/'one.txt'): 'b'*64, str(tmp_path/'two.txt'): 'c'*64}}
    response = post(api, '/v1/host/commitments/local-work/'+item['id']+'/finish',
                    {**native_run(), 'result': result})
    assert response.status_code == 200, response.text
    return response.json()


def test_completed_result_explicit_redraft_and_joined_acceptance_replay(local_api, tmp_path):
    api, commitments, initiatives, obligation, _ = local_api
    path = '/v1/host/commitments/' + obligation['id'] + '/local-draft'
    original = body(tmp_path)
    joined = {**original, 'session_id': 'second', 'turn_id': 'joined'}
    first = post(api, path, original).json()
    assert post(api, path, joined).json()['id'] == first['id']
    fresh = {**original, 'turn_id': 'explicit-redraft', 'new_draft': True}
    blocked = post(api, path, fresh)
    assert blocked.status_code == 409
    assert blocked.json()['detail'] == {'reason': 'local_draft_in_progress', 'initiative_id': first['id']}
    completed = finish(api, first, tmp_path)
    later = post(api, path, {**original, 'session_id': 'later', 'turn_id': 'later'}).json()
    assert later['id'] == first['id'] and later['result'] == completed['result']
    changed = post(api, path, {**original, 'turn_id': 'changed', 'question': 'A different comparison'})
    assert changed.status_code == 409 and changed.json()['detail']['initiative_id'] == first['id']
    second = post(api, path, fresh).json()
    assert second['id'] != first['id'] and second['status'] == 'pending'
    assert post(api, path, fresh).json()['id'] == second['id']
    # Restart the store reader and replay a joined request after a new draft:
    # it must still point to its original completed result, not the new run.
    with TestClient(api.app) as reopened:
        replay = post(reopened, path, joined).json()
    assert replay['id'] == first['id'] and replay['result'] == completed['result']
    assert commitments.get(obligation['id'])['status'] == 'pending'
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 2


def claim(api, obligation, **changes):
    holder = {'operation': 'claim', 'contact_id': 'cid-owner',
              'session_id': 'owner-chat', 'task_id': 'owner-task', 'turn_id': 'owner-turn', **changes}
    response = post(api, '/v1/host/commitments/'+obligation['id']+'/work', holder)
    assert response.status_code == 200, response.text
    return holder, response.json()


def test_acceptance_handoff_rolls_back_and_replay_preserves_worker_claim(local_api, tmp_path, monkeypatch):
    api, _, initiatives, obligation, _ = local_api
    holder, held = claim(api, obligation)
    assert held['accepted']
    handoff = {key: holder[key] for key in ('session_id', 'task_id', 'turn_id')} | {'claim_id': held['claim_id']}
    path = '/v1/host/commitments/'+obligation['id']+'/local-draft'
    value = {**body(tmp_path), 'handoff': handoff}
    original_history = LocalWork.history
    def interrupted(*args):
        raise RuntimeError('controlled acceptance transaction interruption')
    monkeypatch.setattr(LocalWork, 'history', staticmethod(interrupted))
    with pytest.raises(RuntimeError, match='controlled acceptance'):
        post(api, path, value)
    monkeypatch.setattr(LocalWork, 'history', staticmethod(original_history))
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 0
    assert claim(api, obligation, operation='status')[1]['work_state'] == 'held'
    accepted = post(api, path, value).json()
    assert accepted['handoff_released'] is True and held['claim_id'] not in str(accepted)
    assert claim(api, obligation, operation='status')[1]['work_state'] == 'released'
    worker_holder, worker = claim(api, obligation, session_id='worker', task_id='worker', turn_id='worker')
    assert worker['accepted']
    replay = post(api, path, value).json()
    assert replay['id'] == accepted['id'] and replay['handoff_released'] is True
    status = claim(api, obligation, operation='status')[1]
    assert status['work_state'] == 'held' and status['session_id'] == worker_holder['session_id']


@pytest.mark.parametrize('partial', ['acceptance_only', 'release_only', 'release_join', 'release_fresh_generation', 'release_then_new_holder'])
def test_reopened_acceptance_reconciles_exact_partial_handoff(local_api, tmp_path, partial):
    api, commitments, initiatives, obligation, _ = local_api
    path = '/v1/host/commitments/'+obligation['id']+'/local-draft'
    old_count = 0
    if partial in {'release_join', 'release_fresh_generation'}:
        previous = post(api, path, body(tmp_path)).json()
        if partial == 'release_fresh_generation':
            finish(api, previous, tmp_path)
        old_count = 1
    holder, held = claim(api, obligation)
    handoff = {key: holder[key] for key in ('session_id', 'task_id', 'turn_id')} | {'claim_id': held['claim_id']}
    value = {**body(tmp_path), 'handoff': handoff}
    if partial == 'release_fresh_generation':
        value.update(turn_id='fresh-draft-turn', new_draft=True)
    elif partial == 'release_join':
        value.update(session_id='joining-session', turn_id='joining-turn')
    # Simulate the two persisted WAL boundaries, not a host crash. The mapping
    # and initiative share one file; the lease lives in the other file.
    if partial == 'acceptance_only':
        original = post(api, path, value).json()
        with sqlite3.connect(commitments._db_path) as db:
            db.execute("UPDATE commitment_work SET state='held',lease_until=? WHERE commitment_id=?",
                       (held['lease_until'], obligation['id']))
        assert claim(api, obligation, operation='status')[1]['work_state'] == 'held'
    else:
        released = post(api, '/v1/host/commitments/'+obligation['id']+'/work', {
            **holder, 'operation': 'release', 'claim_id': held['claim_id']})
        assert released.json()['accepted'] is True
        if partial == 'release_then_new_holder':
            assert claim(api, obligation, session_id='new-holder', task_id='new-task', turn_id='new-turn')[1]['accepted']
    with TestClient(api.app) as reopened:
        response = post(reopened, path, value)
    if partial == 'release_then_new_holder':
        assert response.status_code == 409
        assert claim(api, obligation, operation='status')[1]['session_id'] == 'new-holder'
        expected = 0
    else:
        assert response.status_code == 200, response.text
        recovered = response.json()
        assert recovered['handoff_released'] is True
        if partial == 'acceptance_only':
            assert recovered['id'] == original['id']
        if partial == 'release_fresh_generation':
            assert recovered['id'] != previous['id']
        elif partial == 'release_join':
            assert recovered['id'] == previous['id']
        assert post(api, path, value).json()['id'] == recovered['id']
        assert claim(api, obligation, operation='status')[1]['work_state'] == 'released'
        expected = 0 if partial == 'release_join' else 1
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == old_count + expected


@pytest.mark.parametrize('error_type,attempts,retryable', [
    ('TimeoutError', 1, True), ('ValueError', 1, False), ('TimeoutError', 2, False)])
def test_failed_legacy_retry_holds_one_undertaking_until_terminal(local_api, tmp_path, error_type, attempts, retryable):
    api, commitments, initiatives, obligation, native = local_api
    path = '/v1/host/commitments/'+obligation['id']+'/local-draft'
    original = post(api, path, body(tmp_path)).json()
    for execution in 'ab'[:attempts]:
        selected = post(api, '/v1/host/commitments/local-work/next', native_run(execution)).json()['assignment']
        assert selected['id'] == original['id']
        with sqlite3.connect(native/'cron/executions.db') as db:
            db.execute("UPDATE executions SET status='failed' WHERE id=?", (execution*32,))
        failed = post(api, '/v1/host/commitments/local-work/'+original['id']+'/finish', {
            **native_run(execution), 'result': {'status':'unavailable', 'error_type':error_type}})
        assert failed.status_code == 200, failed.text
    work = LocalWork(initiatives, commitments)
    assert work.native_pending('cid-owner')['legacy_in_flight'] == int(retryable)
    fresh = post(api, path, {**body(tmp_path), 'turn_id':'fresh-request', 'new_draft':True})
    if retryable:
        assert fresh.status_code == 409
        assert fresh.json()['detail'] == {'reason':'local_draft_in_progress', 'initiative_id':original['id']}
        joined = post(api, path, {**body(tmp_path), 'session_id':'joining', 'turn_id':'joining',
                                 'question':'Explain these notes together'}).json()
        assert joined['id'] == original['id'] and joined['acceptance_matches_request'] is False
        selected = post(api, '/v1/host/commitments/local-work/next', native_run('c')).json()['assignment']
        assert selected['id'] == original['id'] and selected['attempt_count'] == 2
        expected = 1
    else:
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()['id'] != original['id']
        expected = 2
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == expected


@pytest.mark.parametrize('changed', ['session_id', 'task_id', 'turn_id', 'claim_id'])
def test_handoff_requires_exact_held_token_and_rolls_back(local_api, tmp_path, changed):
    api, _, initiatives, obligation, _ = local_api
    holder, held = claim(api, obligation)
    handoff = {key: holder[key] for key in ('session_id', 'task_id', 'turn_id')} | {'claim_id': held['claim_id']}
    handoff[changed] = 'f'*32 if changed == 'claim_id' else 'other'
    response = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft',
                    {**body(tmp_path), 'handoff': handoff})
    assert response.status_code == 409 and response.json()['detail'] == 'accepting_undertaking_superseded'
    assert claim(api, obligation, operation='status')[1]['work_state'] == 'held'
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 0


def test_standalone_new_turn_and_legacy_acceptance_keep_their_identity(local_api, tmp_path):
    api, _, initiatives, obligation, _ = local_api
    standalone = '/v1/host/commitments/local-draft'
    first = post(api, standalone, body(tmp_path)).json()
    assert post(api, standalone, body(tmp_path)).json()['id'] == first['id']
    assert post(api, standalone, {**body(tmp_path), 'turn_id': 'later'}).json()['id'] != first['id']
    path = '/v1/host/commitments/'+obligation['id']+'/local-draft'
    old = post(api, path, body(tmp_path)).json()
    # Recreate predecessor storage shape: the original dedup key lived only
    # on the initiative, before acceptance aliases were recorded separately.
    from apsimo.commitments.local_work import SOURCE, encoded
    import hashlib
    material = {'commitment_id': obligation['id'], 'question': body(tmp_path)['question'],
                'sources': body(tmp_path)['sources'], 'session_id': 'owner-chat', 'turn_id': 'owner-turn'}
    legacy_key = SOURCE+':'+hashlib.sha256(encoded(material).encode()).hexdigest()
    with sqlite3.connect(initiatives._db_path) as db:
        db.execute('DELETE FROM local_draft_acceptances WHERE initiative_id=?', (old['id'],))
        db.execute('UPDATE initiatives SET dedup_key=? WHERE id=?', (legacy_key, old['id']))
    assert post(api, path, body(tmp_path)).json()['id'] == old['id']


def test_native_handler_recovers_lost_handoff_ack_without_releasing_worker(runtime, local_api, tmp_path):
    _, context, client, _ = runtime
    api, _, _, obligation, _ = local_api
    dropped = []
    def transport(path, **kwargs):
        response = post(api, path, kwargs['json'])
        if path.endswith('/local-draft') and response.status_code == 200 and not dropped:
            dropped.append(response.json()['id'])
            raise ConnectionError('controlled lost acceptance acknowledgment')
        return response
    client.post = transport
    session = 'owner-chat'
    _pre(context, session=session, task='owner-task', turn='owner-turn', platform='sms', sender='+15550001')
    def tool(name, args):
        return json.loads(_tool(context, name, args, session=session, task='owner-task', turn='owner-turn', call=name))
    assert tool('colony_commitment_work', {'operation':'claim', 'commitment_id':obligation['id']})['accepted']
    args = {'commitment_id':obligation['id'], 'question':body(tmp_path)['question'], 'sources':body(tmp_path)['sources']}
    assert 'error' in tool('colony_accept_local_draft', args)
    assert dropped
    # The worker may take ownership before the accepting caller learns that
    # handoff committed. A replay must detach only the caller's old snapshot.
    assert claim(api, obligation, session_id='worker', task_id='worker', turn_id='worker')[1]['accepted']
    recovered = tool('colony_accept_local_draft', args)
    assert recovered['id'] == dropped[0] and recovered['handoff_released'] is True
    assert claim(api, obligation, operation='status')[1]['session_id'] == 'worker'
    from test_hermes_native_tool_authority import call
    assert call(context, 'read_file', session=session, task='owner-task', turn='owner-turn') == 'executed'
    for forbidden in ('handoff', 'claim_id', 'session_id'):
        assert 'error' in tool('colony_accept_local_draft', {**args, forbidden:'model-selected'})
    conflict = tool('colony_accept_local_draft', {**args, 'new_draft':True})
    assert conflict == {'error':'local_draft_in_progress', 'initiative_id': recovered['id'],
                        'execution_created':False}

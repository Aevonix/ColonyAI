"""Accepted native drafts over scoped HTTP and real, read-only board snapshots.

The board fixture contains the Hermes 0.21.0 columns used by the sidecar reader.
Native task creation and worker execution have separate installed-runtime tests.
"""
import hashlib
import json
import sqlite3

import pytest

from colony_sidecar.commitments.local_work import LocalWork
from colony_sidecar.turns.local_work import local_work_view
from test_accepted_local_work import body, local_api, native_run, post


ROOT = '/v1/host/commitments'
HEADERS = {'Authorization': 'Bearer writer-key'}


@pytest.fixture
def native_api(local_api, monkeypatch):
    monkeypatch.setenv('COLONY_LOCAL_WORK_EXECUTOR', 'kanban')
    monkeypatch.setenv('COLONY_LOCAL_WORK_BOARD', 'colony-drafts')
    monkeypatch.setenv('COLONY_LOCAL_WORK_PROFILE', 'colony-drafts')
    native = local_api[-1]
    board = native/'kanban/boards/colony-drafts/kanban.db'
    board.parent.mkdir(parents=True)
    with sqlite3.connect(board) as db:
        db.execute('''CREATE TABLE tasks(
            id TEXT PRIMARY KEY, created_by TEXT, idempotency_key TEXT UNIQUE,
            tenant TEXT, assignee TEXT, status TEXT, current_run_id INTEGER,
            claim_lock TEXT)''')
        db.execute('''CREATE TABLE task_runs(
            id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, claim_lock TEXT)''')
    return (*local_api, board)


def accept(native_api, tmp_path, **changes):
    api, _, _, obligation, _, _ = native_api
    response = post(api, ROOT+'/'+obligation['id']+'/local-draft',
                    {**body(tmp_path), **changes})
    assert response.status_code == 200, response.text
    return response.json()


def task(board, initiative_id, identifier='task-one', **changes):
    row = {'id': identifier, 'created_by': 'colony-local-work',
           'idempotency_key': 'colony-local-work:'+initiative_id,
           'tenant': 'cid-owner', 'assignee': 'colony-drafts',
           'status': 'ready', 'current_run_id': None, 'claim_lock': None}
    row.update(changes)
    with sqlite3.connect(board) as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)', tuple(row.values()))
    return {'contact_id': 'cid-owner', 'native_board': 'colony-drafts',
            'native_task_id': identifier}


def start(board, binding, identifier=1, claim='claim-one'):
    with sqlite3.connect(board) as db:
        db.execute("UPDATE task_runs SET status='crashed' WHERE task_id=? AND status='running'",
                   (binding['native_task_id'],))
        db.execute('INSERT INTO task_runs VALUES(?,?,?,?)',
                   (identifier, binding['native_task_id'], 'running', claim))
        db.execute("UPDATE tasks SET status='running',current_run_id=?,claim_lock=? WHERE id=?",
                   (identifier, claim, binding['native_task_id']))
    return {**binding, 'native_run_id': identifier, 'native_claim_lock': claim}


def route(item, operation):
    return ROOT+'/local-work/'+item['id']+'/'+operation


def attach(api, item, binding):
    response = post(api, route(item, 'native-task'), binding)
    assert response.status_code == 200, response.text
    return response.json()


def result(tmp_path):
    return {'status': 'draft_created', 'summary': 'Unverified comparison',
            'report_path': str(tmp_path/'report.md'), 'report_sha256': 'a'*64,
            'sources': {str(tmp_path/'one.txt'): 'b'*64}}


def pending(api):
    response = api.get(ROOT+'/local-work/pending', params={'contact_id': 'cid-owner'}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_owner_acceptance_selects_backend_on_server_and_replays_once(native_api, tmp_path, monkeypatch):
    api, commitments, initiatives, obligation, _, _ = native_api
    path = ROOT+'/'+obligation['id']+'/local-draft'
    value = body(tmp_path)
    assert api.post(path, json=value).status_code == 401
    assert api.post(path, json=value, headers={'Authorization': 'Bearer guest-key'}).status_code == 403
    assert post(api, path, {**value, 'execution_backend': 'cron'}).status_code == 422
    accepted = accept(native_api, tmp_path)
    assert accepted['status'] == 'pending' and accepted['attempt_count'] == 0
    assert accepted['max_attempts'] is None
    assert accepted['context']['execution_backend'] == 'kanban'
    assert accepted['context']['accepted_principal_id'] == 'host'
    assert accept(native_api, tmp_path) == accepted
    monkeypatch.setenv('COLONY_LOCAL_WORK_EXECUTOR', 'cron')
    assert accept(native_api, tmp_path) == accepted
    monkeypatch.setenv('COLONY_LOCAL_WORK_EXECUTOR', 'unknown')
    assert post(api, path, value).status_code == 503
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute('SELECT count(*) FROM initiatives').fetchone()[0] == 1
    assert commitments.get(obligation['id'])['status'] == 'pending'


@pytest.mark.parametrize('changes', [
    {'created_by': 'unrelated'}, {'idempotency_key': 'colony-local-work:other'},
    {'tenant': 'guest'}, {'assignee': 'another-profile'},
])
def test_native_task_binding_requires_accepted_provenance(native_api, tmp_path, changes):
    api, _, _, _, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'], **changes)
    response = post(api, route(item, 'native-task'), binding)
    assert response.status_code == 409
    assert response.json()['detail'] == 'accepted_native_task_required'


def test_native_task_binding_requires_selected_board_and_is_immutable(native_api, tmp_path):
    api, _, initiatives, _, native, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    wrong_board = post(api, route(item, 'native-task'), {**binding, 'native_board': 'other'})
    assert wrong_board.status_code == 409
    assert wrong_board.json()['detail'] == 'selected_native_board_required'
    bound = attach(api, item, binding)
    assert bound['context']['source_home_id'] == hashlib.sha256(str(native).encode()).hexdigest()
    assert attach(api, item, binding) == bound
    with sqlite3.connect(board) as db:
        db.execute('UPDATE tasks SET idempotency_key=NULL WHERE id=?', (binding['native_task_id'],))
    second = task(board, item['id'], identifier='task-two')
    refused = post(api, route(item, 'native-task'), second)
    assert refused.status_code == 409
    assert refused.json()['detail'] == 'native_task_association_changed'
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute("SELECT count(*) FROM assignment_history WHERE action='native_task_bound'").fetchone()[0] == 1


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET status='ready'",
    'UPDATE tasks SET current_run_id=99',
    "UPDATE tasks SET claim_lock='different-claim'",
    "UPDATE task_runs SET status='crashed'",
    "UPDATE task_runs SET claim_lock='different-claim'",
])
def test_native_assignment_requires_current_running_task_and_run(native_api, tmp_path, sql):
    api, _, _, _, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    attach(api, item, binding)
    run = start(board, binding)
    with sqlite3.connect(board) as db:
        db.execute(sql)
    response = post(api, route(item, 'native-run'), run)
    assert response.status_code == 409
    assert response.json()['detail'] == 'current_native_run_required'


def test_native_reclaim_uses_actual_attempts_and_fences_stale_finish(native_api, tmp_path):
    api, _, initiatives, _, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    attach(api, item, binding)
    first_run = start(board, binding)
    first = post(api, route(item, 'native-run'), first_run)
    assert first.status_code == 200, first.text
    assert first.json()['attempt_count'] == 1
    assert post(api, route(item, 'native-run'), first_run).json() == first.json()
    second_run = start(board, binding, identifier=7, claim='claim-two')
    assert post(api, route(item, 'native-run'), first_run).status_code == 409
    assert post(api, route(item, 'finish'), {**first_run, 'result': result(tmp_path)}).status_code == 409
    second = post(api, route(item, 'native-run'), second_run)
    assert second.status_code == 200, second.text
    assert second.json()['attempt_count'] == 2
    assert second.json()['context']['native_run_id'] == 7
    with sqlite3.connect(initiatives._db_path) as db:
        actors = db.execute("SELECT agent_id FROM assignment_history WHERE action='assigned' ORDER BY id").fetchall()
    assert actors == [('native-kanban:1',), ('native-kanban:7',)]


def test_finish_ack_recovery_reuses_result_and_leaves_parent_open(native_api, tmp_path):
    api, commitments, initiatives, obligation, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    attach(api, item, binding)
    first_run = start(board, binding)
    assert post(api, route(item, 'native-run'), first_run).status_code == 200
    complete = post(api, route(item, 'finish'), {**first_run, 'result': result(tmp_path)})
    assert complete.status_code == 200, complete.text
    assert complete.json()['status'] == 'completed'
    second_run = start(board, binding, identifier=2, claim='claim-two')
    recovery = post(api, route(item, 'native-run'), second_run)
    assert recovery.status_code == 200, recovery.text
    assert recovery.json()['reconcile_only'] is True
    assert recovery.json()['result'] == complete.json()['result']
    assert recovery.json()['context']['native_run_id'] == 2
    replay = post(api, route(item, 'finish'), {**second_run, 'result': result(tmp_path)})
    assert replay.status_code == 200, replay.text
    assert replay.json() == complete.json()
    assert complete.json()['parent_commitment_fulfilled'] is False
    assert commitments.get(obligation['id'])['status'] == 'pending'
    with sqlite3.connect(initiatives._db_path) as db:
        assert db.execute("SELECT count(*) FROM assignment_history WHERE action='completed'").fetchone()[0] == 1
        saved = db.execute('SELECT context,attempt_count FROM initiatives WHERE id=?', (item['id'],)).fetchone()
    assert json.loads(saved[0])['native_run_id'] == 1 and saved[1] == 1
    recent = local_work_view()['recent'][0]
    assert recent['result']['report_sha256'] == 'a'*64
    assert recent['attempt_count'] == 2 and recent['native_status'] == 'running'


def test_parent_cancellation_stops_binding_and_result_acceptance(native_api, tmp_path):
    api, commitments, _, obligation, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    attach(api, item, binding)
    run = start(board, binding)
    assert post(api, route(item, 'native-run'), run).status_code == 200
    commitments.update(obligation['id'], status='cancelled')
    status = api.get(ROOT+'/local-work/'+item['id'], params={'contact_id': 'cid-owner'}, headers=HEADERS)
    assert status.status_code == 200 and status.json()['status'] == 'cancelled'
    assert post(api, route(item, 'native-run'), run).status_code == 409
    assert post(api, route(item, 'finish'), {**run, 'result': result(tmp_path)}).status_code == 409
    assert pending(api)['items'] == []
    assert local_work_view()['recent'][0]['status'] == 'cancelled'


def test_pending_legacy_migrates_once_while_active_legacy_drains(native_api, tmp_path, monkeypatch):
    api, _, initiatives, _, _, _ = native_api
    monkeypatch.setenv('COLONY_LOCAL_WORK_EXECUTOR', 'cron')
    active = accept(native_api, tmp_path, turn_id='active-legacy')
    claimed = post(api, ROOT+'/local-work/next', native_run()).json()['assignment']
    assert claimed['id'] == active['id']
    waiting = accept(native_api, tmp_path, turn_id='waiting-legacy')
    monkeypatch.setenv('COLONY_LOCAL_WORK_EXECUTOR', 'kanban')
    migrated = pending(api)
    assert migrated['legacy_in_flight'] == 1
    assert [item['id'] for item in migrated['items']] == [waiting['id']]
    assert migrated['items'][0]['context']['execution_backend'] == 'kanban'
    assert pending(api) == migrated
    old_worker = post(api, ROOT+'/local-work/next', native_run()).json()['assignment']
    assert old_worker['id'] == active['id'] and old_worker['context']['execution_backend'] == 'cron'
    finished = post(api, route(active, 'finish'), {**native_run(), 'result': result(tmp_path)})
    assert finished.status_code == 200, finished.text
    assert pending(api)['legacy_in_flight'] == 0
    assert post(api, ROOT+'/local-work/next', native_run('b')).json()['assignment'] is None
    with sqlite3.connect(initiatives._db_path) as db:
        migrations = db.execute("SELECT initiative_id FROM assignment_history WHERE action='execution_migrated'").fetchall()
    assert migrations == [(waiting['id'],)]


def test_reconciliation_prioritizes_unassociated_acceptance_at_limit(native_api, tmp_path):
    api, commitments, initiatives, _, _, board = native_api
    active = accept(native_api, tmp_path, turn_id='earlier-native')
    binding = task(board, active['id'])
    attach(api, active, binding)
    run = start(board, binding)
    assert post(api, route(active, 'native-run'), run).status_code == 200
    waiting = accept(native_api, tmp_path, turn_id='new-unassociated-native')
    response = LocalWork(initiatives, commitments).native_pending('cid-owner', limit=1)
    assert [item['id'] for item in response['items']] == [waiting['id']]
    assert response['legacy_in_flight'] == 0


def test_current_work_projects_native_state_without_claiming_process_liveness(native_api, tmp_path):
    api, _, _, _, _, board = native_api
    item = accept(native_api, tmp_path)
    binding = task(board, item['id'])
    attach(api, item, binding)
    ready = local_work_view()['items'][0]
    assert ready['native_status'] == 'ready' and ready['attempt_count'] == 0
    assert ready['liveness'] == 'native_task_record'
    start(board, binding)
    running = local_work_view()['items'][0]
    assert running['native_status'] == 'running' and running['attempt_count'] == 1
    assert running['native_run_id'] == 1 and running['liveness'] == 'native_running_record'
    assert 'claim_lock' not in json.dumps(running)
    with sqlite3.connect(board) as db:
        db.execute("UPDATE tasks SET status='archived',current_run_id=NULL,claim_lock=NULL")
        db.execute("UPDATE task_runs SET status='done'")
    archived = local_work_view()['items'][0]
    assert archived['native_work']['archived'] is True
    assert archived['native_status'] == 'archived'
    board.unlink()
    unavailable = local_work_view()
    assert unavailable['available'] is True
    assert unavailable['items'][0]['native_work']['available'] is False
    assert unavailable['items'][0]['initiative_id'] == item['id']

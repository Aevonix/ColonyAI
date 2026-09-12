"""Busy or unavailable work readers must not conceal independent work."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import executions, host
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.executions import ExecutionRegistry, request_work_context


def records(source, count=1):
    return {'available': True, 'items': [
        {'id': source + str(i), 'status': 'running', 'liveness': 'unknown'} for i in range(count)],
        'recent': [], 'total': count}


def projected(result):
    return [json.loads(line) for line in result['text'].splitlines()
            if line.startswith('{') and '"source": "native_kanban_coverage"' not in line]


def test_busy_local_source_cannot_starve_sessions_cron_and_workers():
    view = records('session')
    # A current execution participates in active-reader fairness. Expired
    # observations are historical and follow actual terminal outcomes.
    view['items'][0]['liveness'] = 'recently_observed'
    view.update({source: records(source, 12 if source == 'local_work' else 1)
                 for source in ('local_work', 'native_kanban', 'reported_worker', 'worker_work', 'native_cron')})
    result = request_work_context(view)
    rows = projected(result)
    assert len(rows) == 8
    assert {row['source'] for row in rows} == {
        'local_work', 'native_kanban', 'reported_worker', 'execution', 'worker_work', 'native_cron'}
    assert result['work_sources']['local_work']['total'] == 12
    assert result['work_sources']['local_work']['shown'] == 3
    assert result['truncated'] and not result['complete']
    assert 'overlapping records, not a unique task count' in result['text']
    assert len(result['text']) <= 4000


def test_large_report_does_not_hide_a_shorter_independent_observation():
    large = {'task_id': 'reported', 'state': 'running', 'result_refs': [
        {'kind': 'artifact', 'reference': 'r' * 512, 'verification': 'v' * 160} for _ in range(4)]}
    view = {'items': [{'execution_id': 'actual-session', 'liveness': 'unknown'}],
            'reported_worker': {'available': True, 'items': [large]}}
    result = request_work_context(view, max_chars=1800)
    assert [row['execution_id'] for row in projected(result)] == ['actual-session']
    assert result['work_sources']['reported_worker']['shown'] == 0
    assert result['truncated'] and len(result['text']) <= 1800


def test_join_identities_survive_projection_without_claim_tokens_or_task_text():
    session = 'session-' + 's' * 220
    view = {'items': [{'execution_id': 'e' * 64, 'parent_execution_id': 'p' * 64,
                      'session_id': session, 'turn_id': 'turn', 'liveness': 'unknown'}],
            'worker_work': {'items': [{'job_id': 'job', 'job_type': 'research',
                'worker_id': 'worker', 'claim_attempt_id': 'attempt', 'claim_id': 'SECRET_CLAIM',
                'description': 'PRIVATE_TASK_TEXT', 'state': 'running'}]},
            'native_kanban': {'available': True, 'items': [{'native_task_id': 'task',
                'native_run_id': 'run', 'native_board': 'default', 'claim_lock': 'SECRET_LOCK'}],
                'source_home_id': 'a' * 64}}
    result = request_work_context(view)
    rows = {row['source']: row for row in projected(result)}
    assert rows['execution']['session_id'] == session
    assert rows['execution']['parent_execution_id'] == 'p' * 64
    assert rows['worker_work']['claim_attempt_id'] == 'attempt'
    assert rows['worker_work']['worker_id'] == 'worker'
    assert rows['native_kanban']['source_home_id'] == 'a' * 64
    assert 'SECRET' not in result['text'] and 'PRIVATE_TASK_TEXT' not in result['text']


@pytest.mark.asyncio
async def test_reader_failure_isolated_and_unattached_queue_stays_unknown(tmp_path, monkeypatch):
    from pacomind.turns import hermes_work, hermes_kanban, local_work, reported_workers
    monkeypatch.setattr(local_work, 'local_work_view', lambda **_: records('draft', 8))
    monkeypatch.setattr(hermes_work, 'cron_view', lambda **_: records('cron'))
    def broken(**_):
        raise RuntimeError('PRIVATE_DRIVER_ERROR')
    monkeypatch.setattr(hermes_kanban, 'kanban_view', broken)
    monkeypatch.setattr(reported_workers, 'reported_worker_view', lambda **_: None)
    monkeypatch.setattr(host, '_task_queue', None)
    monkeypatch.setenv('PACOMIND_OWNER_CONTACT_ID', 'owner')
    store = ExecutionRegistry(TurnIdempotencyLedger(tmp_path / 'turns.db'))
    monkeypatch.setattr(executions, 'registry', lambda: store)
    identifier = hashlib.sha256(b'independent-turn').hexdigest()
    value = {'execution_id': identifier, 'session_id': 'voice-session', 'turn_id': 'turn',
             'parent_execution_id': '', 'platform': 'voice', 'state': 'observed',
             'phase': 'tool', 'tool_name': 'read_file', 'sequence': 1}
    store.observe(value, principal_id='host', contact_id='owner')
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, call_next):
        person = request.headers.get('fixture-person', 'owner')
        request.state.pacomind_authority = RequestAuthority(principal_id='host', credential_id='fixture',
            scopes=frozenset({'context:read'}), viewer_person_id=person,
            person_ids=frozenset({person}), audiences=frozenset({'viewer'}), authenticated=True)
        return await call_next(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.get('/v1/host/executions', params={'contact_id': 'owner'})
        assert response.status_code == 200
        full = response.json()
        assert full['work_sources']['native_kanban']['status'] == 'unavailable'
        assert full['work_sources']['worker_work']['reason'] == 'queue_not_attached'
        assert full['work_sources']['reported_worker']['status'] == 'not_observed'
        assert 'PRIVATE_DRIVER_ERROR' not in response.text and not full['complete']
        assert full['native_cron']['items'][0]['id'] == 'cron0'
        first = (await client.get('/v1/host/executions', params={
            'contact_id': 'owner', 'session_id': 'text-session', 'projection': 'request'})).json()
        assert identifier in first['text'] and 'cron0' in first['text']
        # A later independent request sees authoritative closure, not a cached
        # running session or a conclusion inferred from lease expiration.
        store.observe({**value, 'sequence': 2, 'state': 'completed'}, principal_id='host', contact_id='owner')
        later = (await client.get('/v1/host/executions', params={
            'contact_id': 'owner', 'session_id': 'third-session', 'projection': 'request'})).json()
        assert identifier not in later['text']
        assert later['work_sources']['execution']['total'] == 0
        guest = await client.get('/v1/host/executions', params={'contact_id': 'guest'},
                                 headers={'fixture-person': 'guest'})
        assert guest.status_code == 200 and 'work_sources' not in guest.json()
        assert 'cron0' not in guest.text and 'draft0' not in guest.text


@pytest.mark.asyncio
async def test_slow_queue_does_not_hold_other_source_observations(monkeypatch):
    from pacomind.turns import hermes_work, hermes_kanban, local_work, reported_workers
    for module, function in ((hermes_work, 'cron_view'), (hermes_kanban, 'kanban_view'),
                             (local_work, 'local_work_view'), (reported_workers, 'reported_worker_view')):
        monkeypatch.setattr(module, function, lambda **_: records('independent'))
    cancelled = asyncio.Event()
    async def slow(**_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(host, '_task_queue', SimpleNamespace(current_work=slow))
    view = await asyncio.wait_for(executions.with_queue_work({'items': []}, owner=True), timeout=1)
    assert cancelled.is_set()
    assert view['work_sources']['worker_work']['status'] == 'unavailable'
    assert view['work_sources']['native_cron']['status'] == 'observed'

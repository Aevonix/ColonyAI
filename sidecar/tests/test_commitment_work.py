"""Two real SQLite clients coordinate one obligation and recover by fencing."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import commitment_work, executions, host
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.commitments.work import CommitmentWork
from test_hermes_general_governance import runtime, _pre, _tool
from test_scoped_api_authority import _principal, _write_keyring


def holder(session):
    return {'principal_id': 'host', 'contact_id': 'owner', 'session_id': session,
            'task_id': session, 'turn_id': session}


def test_two_database_clients_race_recover_and_reject_old_tokens(tmp_path):
    path = tmp_path / 'commitments.db'
    store = CommitmentStore(path)
    obligation = store.create('owner', 'Compare the two proposed repairs')
    now = [1000.0]
    def claim(session):
        return CommitmentWork(CommitmentStore(path), clock=lambda: now[0]).operate(
            obligation['id'], operation='claim', **holder(session))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ['sms', 'voice']))
    assert sorted(value['accepted'] for value in outcomes) == [False, True]
    winner = next(value for value in outcomes if value['accepted'])
    peer = CommitmentWork(CommitmentStore(path), clock=lambda: now[0])
    status = peer.operate(obligation['id'], operation='status', **holder('another'))
    assert status['session_id'] == winner['session_id'] and status['work_state'] == 'held'
    assert 'claim_id' not in status
    now[0] += 121
    recovered = peer.operate(obligation['id'], operation='claim', **holder('recovery'))
    assert recovered['accepted'] and recovered['claim_id'] != winner['claim_id']
    for operation in ('renew', 'release'):
        stale = peer.operate(obligation['id'], operation=operation,
            claim_id=winner['claim_id'], **holder(winner['session_id']))
        assert not stale['accepted'] and stale['reason'] == 'claim_superseded'
    assert peer.operate(obligation['id'], operation='renew', claim_id=recovered['claim_id'], **holder('recovery'))['accepted']
    # Existing verified/consented resolution remains the sole completion path.
    store.resolve(obligation['id'], outcome='done', note='Comparison recorded', resolved_by='owner')
    status = peer.operate(obligation['id'], operation='status', **holder('observer'))
    assert status['commitment_status'] == 'fulfilled' and status['work_state'] == 'obligation_closed'
    assert not peer.operate(obligation['id'], operation='claim', **holder('late'))['accepted']



def test_two_creates_and_claims_reuse_one_open_obligation(tmp_path):
    path = tmp_path / 'commitments.db'
    CommitmentStore(path)
    def create_and_claim(session):
        store = CommitmentStore(path)
        obligation = store.create('owner', 'Review the repair proposal', dedupe=True)
        outcome = CommitmentWork(store).operate(obligation['id'], operation='claim', **holder(session))
        return obligation, outcome
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create_and_claim, ['sms', 'voice']))
    assert len({obligation['id'] for obligation, _ in outcomes}) == 1
    assert sum(bool(obligation.get('deduped')) for obligation, _ in outcomes) == 1
    assert sorted(result['accepted'] for _, result in outcomes) == [False, True]
    assert CommitmentStore(path).list(person_id='owner')['total'] == 1


def test_fencing_also_binds_person_principal_and_turn(tmp_path):
    store = CommitmentStore(tmp_path / 'c.db'); work = CommitmentWork(store)
    obligation = store.create('owner', 'Review a fixture')
    accepted = work.operate(obligation['id'], operation='claim', **holder('a'))
    for changes in ({'principal_id': 'other'}, {'turn_id': 'other'}, {'session_id': 'other'}):
        assert not work.operate(obligation['id'], operation='release', claim_id=accepted['claim_id'], **{**holder('a'), **changes})['accepted']
    with pytest.raises(KeyError):
        work.operate(obligation['id'], operation='status', **{**holder('a'), 'contact_id': 'guest'})


def work_app(tmp_path, monkeypatch, store):
    monkeypatch.setattr(host, '_commitment_store', store)
    keyring = tmp_path / 'keys.json'
    writer = _principal(principal='host', secret='writer-key', viewer='cid-owner', scopes=['turns:write', 'context:read'])
    writer['allow_unscoped_api'] = False
    guest = _principal(principal='guest-host', secret='guest-key', viewer='guest', scopes=['turns:write'])
    guest['allow_unscoped_api'] = False
    _write_keyring(keyring, [writer, guest])
    app = FastAPI(); app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keyring))
    app.include_router(commitment_work.router)
    return app


def test_native_adapter_sessions_race_through_scoped_http_and_stale_tool_stops(runtime, tmp_path, monkeypatch):
    module, context, client, _ = runtime
    store = CommitmentStore(tmp_path / 'commitments.db')
    obligation = store.create('cid-owner', 'Inspect the same failing build once')
    now = [1000.0]
    # Preserve the real HTTP operation and SQLite transaction, with a controllable lease clock.
    monkeypatch.setattr(commitment_work, 'CommitmentWork', lambda store: CommitmentWork(store, clock=lambda: now[0]))
    with TestClient(work_app(tmp_path, monkeypatch, store)) as api:
        client.post = lambda path, **kwargs: api.post(path, json=kwargs['json'], headers={'Authorization': 'Bearer writer-key'})
        for session in ('sms', 'voice'):
            _pre(context, session=session, task=session, turn=session, platform='sms', sender='+15550001')
        def claim(session):
            return json.loads(_tool(context, 'colony_commitment_work', {'operation': 'claim', 'commitment_id': obligation['id']},
                session=session, task=session, turn=session, call='call-' + session))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ['sms', 'voice']))
        assert sorted(value['accepted'] for value in outcomes) == [False, True]
        winner = next(value['session_id'] for value in outcomes if value['accepted'])
        assert all('claim_id' not in value for value in outcomes)
        from test_hermes_native_tool_authority import call
        assert call(context, 'read_file', session=winner, task=winner, turn=winner) == 'executed'
        now[0] += 121
        _pre(context, session='recovery', task='recovery', turn='recovery', platform='sms', sender='+15550001')
        assert claim('recovery')['accepted']
        invoked = []
        denied = json.loads(call(context, 'terminal', session=winner, task=winner, turn=winner,
                                  dispatch=lambda args: invoked.append(args)))
        assert denied['effect_performed'] is False and invoked == []
        assert call(context, 'read_file', session='recovery', task='recovery', turn='recovery') == 'executed'
        payload = {'operation': 'status', 'contact_id': 'cid-owner', 'session_id': 's', 'task_id': 't', 'turn_id': 'u'}
        route = '/v1/host/commitments/' + obligation['id'] + '/work'
        assert api.post(route, json=payload).status_code == 401
        assert api.post(route, json=payload, headers={'Authorization': 'Bearer guest-key'}).status_code == 403
        assert api.post(route, json={**payload, 'claim_id': 'invented'}, headers={'Authorization': 'Bearer writer-key'}).status_code == 422
    assert module._TOOL_EXECUTION_CONTEXT.get() is None


def test_explicit_stop_after_terminal_or_superseded_claim_allows_next_work(runtime, tmp_path, monkeypatch):
    _, context, client, _ = runtime
    from test_hermes_native_tool_authority import call
    store = CommitmentStore(tmp_path / 'commitments.db')
    first = store.create('cid-owner', 'Inspect fixture Alpha')
    second = store.create('cid-owner', 'Inspect fixture Beta')
    now = [1000.0]
    monkeypatch.setattr(commitment_work, 'CommitmentWork', lambda store: CommitmentWork(store, clock=lambda: now[0]))
    with TestClient(work_app(tmp_path, monkeypatch, store)) as api:
        client.post = lambda path, **kwargs: api.post(path, json=kwargs['json'], headers={'Authorization': 'Bearer writer-key'})
        for session in ('first', 'reclaimer'):
            _pre(context, session=session, task=session, turn=session, platform='sms', sender='+15550001')
        def work(session, operation, obligation):
            return json.loads(_tool(context, 'colony_commitment_work', {'operation': operation, 'commitment_id': obligation['id']}, session=session, task=session, turn=session, call='call-' + session))
        assert work('first', 'claim', first)['accepted']
        store.resolve(first['id'], outcome='done')
        assert json.loads(call(context, 'read_file', session='first', task='first', turn='first'))['effect_performed'] is False
        assert work('first', 'release', first)['detached']
        assert call(context, 'read_file', session='first', task='first', turn='first') == 'executed'
        assert work('first', 'claim', second)['accepted']
        now[0] += 121
        assert work('reclaimer', 'claim', second)['accepted']
        assert json.loads(call(context, 'read_file', session='first', task='first', turn='first'))['effect_performed'] is False
        assert work('first', 'release', second)['detached']
        assert call(context, 'read_file', session='first', task='first', turn='first') == 'executed'
        assert work('reclaimer', 'status', second)['session_id'] == 'reclaimer'


@pytest.mark.asyncio
async def test_canonical_worker_queue_view_has_descriptions_and_truthful_liveness(tmp_path, monkeypatch):
    from colony_sidecar.task_queue.queue_manager import QueueManager
    from colony_sidecar.task_queue.models import Job, JobType, JobStatus, WorkerCapabilities
    clock = [datetime.now(timezone.utc)]
    queue = QueueManager(tmp_path / 'queue.db', clock=lambda: clock[0])
    await queue.start()
    try:
        job = Job(job_type=JobType.RESEARCH, payload={'description': 'Compare the two repairs', 'secret': 'must-not-appear'})
        await queue.post(job)
        claimed = await queue.claim_job('fixture-worker', WorkerCapabilities(node_id='fixture-worker', capabilities=set(), job_types={JobType.RESEARCH}))
        assert claimed and await queue.start_job(job.job_id, 'fixture-worker', claimed.claim_attempt_id)
        monkeypatch.setattr(host, '_task_queue', SimpleNamespace(queue=queue))
        view = await executions.with_queue_work({'items': []}, owner=True)
        item = view['worker_work']['items'][0]
        assert item['job_id'] == job.job_id and item['claim_attempt_id'] == claimed.claim_attempt_id
        assert item['description'] == 'Compare the two repairs' and item['state'] == 'running'
        assert 'secret' not in json.dumps(view)
        assert item['liveness'] == 'recently_observed'
        clock[0] += timedelta(seconds=300)
        assert (await queue.current_work())['items'][0]['liveness'] == 'unknown'
        assert await executions.with_queue_work({'items': []}, owner=False) == {'items': []}
        await queue.complete_job(job.job_id, 'fixture-worker', {'result': 'Comparison complete'}, claim_attempt_id=claimed.claim_attempt_id)
        assert (await queue.get_job(job.job_id)).status == JobStatus.COMPLETED
        assert (await queue.current_work())['items'] == []
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_context_names_obligation_and_other_session_holder(tmp_path, monkeypatch):
    from colony_sidecar.api.authority import RequestAuthority
    from colony_sidecar.api.schemas.host import ContextAssembleRequest
    store = CommitmentStore(tmp_path / 'commitments.db')
    obligation = store.create('owner', 'Compare repair options', due_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    CommitmentWork(store).operate(obligation['id'], operation='claim', **holder('voice'))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setattr(host, '_p8_runtime', None)
    monkeypatch.setattr(host, '_commitment_store', store)
    monkeypatch.setattr(host, '_require_scoped_context_runtime_for_guest', lambda *a: None)
    for person in ('owner', 'guest'):
        authority = RequestAuthority(principal_id='host', credential_id='key', scopes=frozenset({'context:read'}), viewer_person_id=person, person_ids=frozenset({person}), audiences=frozenset({'viewer'}), authenticated=True)
        request = SimpleNamespace(state=SimpleNamespace(colony_authority=authority))
        body = ContextAssembleRequest(identity={'host_id': 'test'}, context={'contact_id': person, 'session_id': 'sms'}, incoming_message={'role': 'user', 'content': 'What is in progress?'})
        result = await host.context_assemble(body, request)
        sections = [section for section in result.sections if section.id == 'colony-commitments']
        if person == 'owner':
            assert len(sections) == 1
            assert obligation['id'] in sections[0].body and 'Compare repair options' in sections[0].body
            assert 'work=held; session=voice' in sections[0].body
            assert '[OVERDUE]' not in sections[0].body
        else:
            assert not sections

"""Existing canonical writer and erasure protect a host's already admitted input."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import importlib
import json
import sqlite3
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from apsimo.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _load_plugin
from test_hermes_general_governance import _Context
from test_hermes_native_tool_authority import call
from test_turn_source_evidence import source_app


@pytest.fixture
def handoff(source_app, tmp_path, monkeypatch):
    module = _load_plugin('colony_supplied_input_test')
    from apsimo.api.middleware import ApiKeyMiddleware
    keyring = tmp_path/'principals.json'
    keyring.write_text(json.dumps({'version':1, 'principals':[{
        'principal':'native-fixture', 'status':'active', 'scopes':['turns:write','context:read','memory:read'],
        'person_ids':['owner'], 'viewer_person_id':'owner', 'audiences':['viewer'],
        'allow_unscoped_api':False, 'turn_ingress_platforms':['rcs','whatsapp'],
        'credentials':[{'id':'fixture','secret':'fixture-key','status':'active'}]}]}))
    keyring.chmod(0o600)
    source_app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keyring))
    # Keep one ASGI portal for this fixture and close it at teardown.
    # Creating a portal per request adds loop setup to the freshness deadline.
    with TestClient(source_app, headers={'Authorization':'Bearer fixture-key'}) as api:
        original_client = module.ColonyClient
        class Client(original_client):
            def get(self, path, **kwargs):
                kwargs.pop('_deadline_monotonic', None)
                kwargs.pop('timeout', None)
                return api.get(path, **kwargs)
            def post(self, path, **kwargs):
                kwargs.pop('_deadline_monotonic', None)
                kwargs.pop('timeout', None)
                return api.post(path, **kwargs)
            def put(self, path, **kwargs):
                kwargs.pop('_deadline_monotonic', None)
                kwargs.pop('timeout', None)
                return api.put(path, **kwargs)
        monkeypatch.setattr(module, 'ApsimoClient', Client)
        for key, value in {'COLONY_GENERAL_PLUGIN_ACTIVE':'1', 'COLONY_MEMORY_WORKER_TOOLS':'0',
            'COLONY_MEMORY_TURN_WRITER':'disabled', 'COLONY_GUARD_CHAT_MODE':'off'}.items():
            monkeypatch.setenv(key, value)
        original = {'role':'user', 'content':'Use the lamp maintenance record I supplied.'}
        body = {'identity':{'host_id':'fixture'}, 'context':{'contact_id':'owner', 'session_id':'voice-source'},
                'user_message':original}
        assert api.put('/v2/host/turns/original-input', json=body).status_code == 201
        ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
        ledger.record_source('earlier', contact_id='owner', session_id='earlier-session',
                             messages=[{'role':'user','content':'The lamp record is in the violet cabinet.'}],
                             derive_claims=False)
        refs = ledger.source_references(['earlier'], contact_id='owner', session_id='native')
        source_hash = importlib.import_module(module.__name__+'.client').source_message_hash
        parents = [{'source_id':'original-input', 'input_message_hash': source_hash('voice-source', original)}]
        outbox = tmp_path/'native-outbox.db'
        ctx = _Context({'url':'http://testserver', 'api_key':'fixture-key', 'owner_contact_id':'owner',
                        'turn_outbox_path':str(outbox), 'turn_outbox_drain_timeout_ms':1000,
                        'turn_writer_platforms':['api_server','rcs','sms','whatsapp']})
        module.register(ctx)
        def start(session='native', task='task', turn='turn', platform='cli'):
            message = {'role':'user', 'content':'Read the maintenance record, then summarize the task.'}
            ctx.hooks['pre_llm_call'](session_id=session, task_id=task, turn_id=turn,
                platform=platform, sender_id='', user_message=message['content'], conversation_history=[message])
            return ctx.middleware['llm_request']({'messages':[message], 'tools':[{'type':'function'}]},
                session_id=session, task_id=task, turn_id=turn)
        def finish(session='native', task='task', turn='turn'):
            ctx.hooks['post_llm_call'](session_id=session, task_id=task, turn_id=turn,
                user_message='Read the maintenance record, then summarize the task.',
                assistant_response='The controlled fixture result cites the supplied maintenance record.',
                platform='cli', model='controlled')
        yield SimpleNamespace(module=module, ctx=ctx, api=api, ledger=ledger, parents=parents,
                               refs=refs, start=start, finish=finish, outbox=module.TurnOutbox(outbox))


def test_derived_native_reply_keeps_exact_parents_without_duplicate_human_source(handoff):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        assert h.start()['reason'] == 'source_erasure_checked'
        assert call(h.ctx, 'read_file', session='native', task='task', turn='turn') == 'executed'
        h.finish()
        assert supplied.result['input_refs'] == h.parents
        assert supplied.result['source_refs'] == h.refs
    rows = h.outbox.snapshot()
    assert len(rows) == 1 and rows[0]['state'] == 'delivered', rows
    payload = rows[0]['payload']
    assert 'user_message' not in payload and payload['summary'] == ''
    assert payload['assistant_input_refs'] == h.parents and payload['assistant_source_refs'] == h.refs
    with sqlite3.connect(h.ledger.db_path) as db:
        messages = [json.loads(row[0]) for row in db.execute('SELECT messages_json FROM turn_sources')]
    assert sum(m['role']=='user' for group in messages for m in group) == 2
    assert not any('summarize the task' in json.dumps(group) for group in messages)
    # Canonical dependency traversal removes the derived reply, preserving the
    # separate earlier source; no local snapshot rewind or semantic judge.
    h.ledger.erase_sources(contact_id='owner', turn_ids=['original-input'])
    assert not h.ledger.source_references([rows[0]['turn_id']], contact_id='owner', session_id='later')
    assert h.ledger.source_references(['earlier'], contact_id='owner', session_id='later')


@pytest.mark.parametrize('erased', ['original-input', 'earlier'])
def test_erasure_between_request_and_tool_prevents_effect_and_result_copy(handoff, erased):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        h.start()
        h.ledger.erase_sources(contact_id='owner', turn_ids=[erased])
        invoked=[]
        result = json.loads(call(h.ctx, 'terminal', session='native', task='task', turn='turn',
                                 dispatch=lambda args: invoked.append(args)))
        assert result['reason']=='source_input_unavailable' and not invoked
        h.finish()
        assert supplied.result is None
    assert h.outbox.snapshot() == []


def test_wrong_contact_cannot_gain_native_authority_from_supplied_input(handoff):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='foreign', session_id='native',
            input_refs=h.parents) as supplied:
        result = h.start()
        assert result['reason']=='source_input_unavailable'
        assert 'maintenance record' not in json.dumps(result['request'])
        assert result['request']['tools']==[]
        h.finish()
        assert supplied.result is None
    assert h.outbox.snapshot()==[]


def test_ordinary_cli_turn_remains_excluded_without_supplied_input(handoff):
    h = handoff
    h.start()
    h.finish()
    assert h.outbox.snapshot() == []
    with sqlite3.connect(h.ledger.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM turn_sources').fetchone()[0] == 2


@pytest.mark.parametrize('failure', ['timeout', 'remote_protocol', 'local_protocol', 'status', 'changed_source',
                                   'http_502', 'http_503', 'http_504', 'http_403', 'http_409', 'http_422'])
def test_only_typed_initial_transport_failure_can_request_a_fresh_task(handoff, monkeypatch, failure):
    import httpx
    h = handoff
    get = h.module.ColonyClient.get
    attempts = []
    def fail_once(client, path, **kwargs):
        if path == '/v1/host/memory/sources/erasures' and not attempts:
            attempts.append(path)
            if failure == 'timeout':
                raise httpx.ReadTimeout('Controlled initial freshness timeout')
            if failure in ('remote_protocol', 'local_protocol'):
                error = httpx.RemoteProtocolError if failure == 'remote_protocol' else httpx.LocalProtocolError
                raise error('Controlled freshness protocol failure')
            if failure == 'status':
                return httpx.Response(401, request=httpx.Request('GET', 'http://fixture'+path))
            if failure.startswith('http_'):
                return httpx.Response(int(failure[5:]), request=httpx.Request('GET', 'http://fixture'+path))
            h.ledger.erase_sources(contact_id='owner', turn_ids=['earlier'])
        return get(client, path, **kwargs)
    monkeypatch.setattr(h.module.ColonyClient, 'get', fail_once)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        blocked = h.start()
        assert blocked['request']['tools'] == []
        assert supplied.failure['admitted'] is False
        retryable = failure in ('timeout', 'remote_protocol', 'http_502', 'http_503', 'http_504')
        assert supplied.failure['retryable'] is retryable
        expected = ('source_freshness_unavailable' if retryable else
                    'source_input_erased' if failure == 'changed_source' else 'source_input_unavailable')
        assert supplied.failure['reason'] == expected
        assert ('temporarily unavailable' in json.dumps(blocked['request'])) is retryable
        # A successful subsequent check does not reopen the partially initialized task.
        effects = []
        assert json.loads(call(h.ctx, 'terminal', session='native', task='task', turn='turn',
            dispatch=lambda args: effects.append(args)))['reason'] == 'source_input_unavailable'
        assert not effects and supplied.memory_contact('native') == ''
        h.finish()
        assert supplied.result is None
    assert h.outbox.snapshot() == []


@pytest.mark.parametrize('failure', ['ReadTimeout', 'RemoteProtocolError', 'http_503'])
def test_failure_after_any_admission_never_permits_replay(handoff, monkeypatch, failure):
    import httpx
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        h.start()
        def fail_later(*args, **kwargs):
            if failure == 'http_503':
                return httpx.Response(503, request=httpx.Request('POST', 'http://fixture/v1/host/memory/sources/erasures'))
            raise getattr(httpx, failure)('Controlled later transport failure')
        monkeypatch.setattr(h.module.ColonyClient, 'post', fail_later)
        result = h.ctx.middleware['llm_request']({'messages': [], 'tools': [{}]},
            session_id='native', task_id='task', turn_id='turn')
        assert result['request']['tools'] == []
        assert supplied.failure == {'reason': 'source_freshness_unavailable', 'admitted': True, 'retryable': False}


def test_expired_initial_budget_after_local_read_stays_transient(handoff, monkeypatch):
    import time
    h = handoff
    erasure_state = h.module.TurnOutbox.erasure_state
    delayed = []
    def checked_then_delayed(outbox, *args, **kwargs):
        result = erasure_state(outbox, *args, **kwargs)
        if kwargs.get('deadline_monotonic') is not None and not delayed:
            delayed.append(True)
            # The read completed, but scheduling consumed the remaining
            # initial verification budget before the next request can start.
            time.sleep(.35)
        return result
    monkeypatch.setattr(h.module.TurnOutbox, 'erasure_state', checked_then_delayed)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        blocked = h.start()
        assert len(delayed) == 1 and blocked['request']['tools'] == []
        assert 'temporarily unavailable' in json.dumps(blocked['request'])
        assert supplied.failure == {'reason': 'source_freshness_unavailable', 'admitted': False, 'retryable': True}
        # A later successful check cannot reopen this failed native task.
        h.finish()
        assert supplied.result is None and supplied.memory_contact('native') == ''
    assert h.outbox.snapshot() == []


@pytest.mark.parametrize('entry', ['pre_api_request', 'llm_request'])
def test_native_compression_rotation_preserves_input_memory_and_root_result(handoff, monkeypatch, entry):
    h = handoff
    provider = _memory_provider(h, monkeypatch)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        request = h.start()['request']
        for session in ('compressed-once', 'compressed-twice'):
            assert provider._prefetch_contact(session) == ''
            if entry == 'pre_api_request':
                h.ctx.hooks[entry](session_id=session, task_id='task', turn_id='turn')
                # Rotation itself does not attest current memory sources.
                assert provider._prefetch_contact(session) == ''
            result = h.ctx.middleware['llm_request'](request,
                session_id=session, task_id='task', turn_id='turn')
            assert result['reason'] == 'source_erasure_checked'
            assert 'maintenance record' in json.dumps(result['request'])
            assert provider._prefetch_contact(session) == 'owner'
            assert call(h.ctx, 'read_file', session=session, task='task', turn='turn') == 'executed'
        h.finish(session='compressed-twice')
        assert supplied.result['session_id'] == 'compressed-twice'
        assert supplied.result['input_refs'] == h.parents
        assert supplied.result['source_refs'] == h.refs
    rows = h.outbox.snapshot()
    assert len(rows) == 1 and rows[0]['state'] == 'delivered'
    assert rows[0]['payload']['assistant_input_refs'] == h.parents
    assert rows[0]['payload']['assistant_source_refs'] == h.refs
    assert 'user_message' not in rows[0]['payload']


def test_rotated_child_cannot_complete_root_handoff(handoff):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        h.start()
        h.ctx.hooks['subagent_start'](parent_session_id='native', parent_turn_id='turn',
            child_session_id='child')
        h.ctx.hooks['pre_llm_call'](session_id='child', task_id='child-task', turn_id='child-turn',
            parent_session_id='native', platform='cli', sender_id='', user_message='Read the record.')
        h.ctx.hooks['pre_api_request'](session_id='compressed-child', task_id='child-task', turn_id='child-turn')
        assert call(h.ctx, 'read_file', session='compressed-child', task='child-task', turn='child-turn') == 'executed'
        h.finish(session='compressed-child', task='child-task', turn='child-turn')
        assert supplied.result is None
        h.finish()
        assert supplied.result['session_id'] == 'native'


@pytest.mark.parametrize('erased', ['original-input', 'earlier'])
def test_erasure_after_native_rotation_still_withholds_request_tools_and_completion(handoff, erased):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        request = h.start()['request']
        h.ctx.hooks['pre_api_request'](session_id='compressed', task_id='task', turn_id='turn')
        h.ledger.erase_sources(contact_id='owner', turn_ids=[erased])
        result = h.ctx.middleware['llm_request'](request,
            session_id='compressed', task_id='task', turn_id='turn')
        assert result['reason'] == 'source_input_unavailable'
        assert 'maintenance record' not in json.dumps(result['request'])
        assert result['request']['tools'] == []
        assert json.loads(call(h.ctx, 'read_file', session='compressed', task='task', turn='turn'))['reason'] == 'source_input_unavailable'
        h.finish(session='compressed')
        assert supplied.result is None
        assert h.outbox.snapshot() == []


def _memory_provider(handoff, monkeypatch):
    from test_colony_memory_provider import _load_provider_module
    monkeypatch.delenv('COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY', raising=False)
    monkeypatch.setitem(sys.modules, 'apsimo_hermes', handoff.module)
    monkeypatch.setitem(sys.modules, 'apsimo_hermes.input_provenance', handoff.module.input_provenance)
    provider = _load_provider_module().ColonyMemoryProvider(config={
        'url':'http://testserver', 'contact_id':'owner', 'api_key':'fixture-key'})
    monkeypatch.setattr(provider, '_turn_sender_context', lambda: ('', '', ''))
    return provider


def test_memory_uses_only_current_checked_native_source_session(handoff, monkeypatch):
    h = handoff
    provider = _memory_provider(h, monkeypatch)
    assert provider._prefetch_contact('native') == ''
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs) as supplied:
        assert provider._prefetch_contact('native') == ''  # Constructor grants nothing.
        h.start()
        assert provider._prefetch_contact('native') == 'owner'
        assert provider._prefetch_contact('unrelated') == ''
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(copied.run, provider._prefetch_contact, 'native').result() == 'owner'
        # The next native turn must refresh source validity before prefetch.
        scope = h.module._TRANSPORT_SCOPES.for_session('native')
        supplied.bind(scope)
        assert provider._prefetch_contact('native') == ''
        assert supplied.allowed(scope, fresh=True, rules=[])
        assert provider._prefetch_contact('native') == 'owner'
        h.ledger.erase_sources(contact_id='owner', turn_ids=['earlier'])
        assert h.start(task='next-task', turn='next-turn')['reason'] == 'source_input_unavailable'
        assert provider._prefetch_contact('native') == ''
    assert supplied.memory_contact('native') == ''
    assert copied.run(provider._prefetch_contact, 'native') == ''
    assert provider._prefetch_contact('native') == ''


@pytest.mark.parametrize('resolved', ['', 'foreign', 'owner'])
def test_supplied_native_memory_cannot_override_gateway_sender(handoff, monkeypatch, resolved):
    h = handoff
    provider = _memory_provider(h, monkeypatch)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents) as supplied:
        h.start()
        monkeypatch.setattr(provider, '_turn_sender_context', lambda: ('whatsapp', 'sender', 'thread'))
        monkeypatch.setattr(provider, '_resolve_handle', lambda platform, sender: resolved)
        assert provider._prefetch_contact('native') == ('owner' if resolved == 'owner' else '')
        monkeypatch.setattr(provider, '_turn_sender_context', lambda: ('whatsapp', '', 'thread'))
        assert provider._prefetch_contact('native') == ''


def test_supplied_memory_requires_independently_bound_child_session(handoff, monkeypatch):
    h = handoff
    provider = _memory_provider(h, monkeypatch)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents) as supplied:
        h.start()
        parent = h.module._TRANSPORT_SCOPES.for_session('native')
        from dataclasses import replace
        child = replace(parent, session_id='child', platform='subagent')
        # Reusing a parent's task/turn identifiers is insufficient.
        assert not supplied.allowed(child, fresh=True, rules=[])
        assert provider._prefetch_contact('child') == ''
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents) as supplied:
        h.start(task='parent-two', turn='parent-two')
        parent = h.module._TRANSPORT_SCOPES.for_session('native')
        child = replace(parent, session_id='child', task_id='child-task', turn_id='child-turn', platform='subagent')
        supplied.bind(child, parent_session_id='native')
        assert provider._prefetch_contact('child') == ''
        assert supplied.allowed(child, fresh=True, rules=[])
        provider._platform = 'subagent'
        assert provider._prefetch_contact('child') == 'owner'
        assert provider._prefetch_contact('another-child') == ''


def test_unattested_caller_cannot_use_parents_to_enable_capture(handoff):
    h = handoff
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents) as supplied:
        result = h.start(platform='discord')
        assert result['reason'] == 'source_input_unavailable'
        h.finish()
        assert supplied.result is None
    assert h.outbox.snapshot() == []


def test_context_exception_reset_and_closed_thread_copy_do_not_borrow_later_input(handoff):
    h = handoff
    with pytest.raises(RuntimeError):
        with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
                input_refs=h.parents) as old:
            h.start()
            copied=copy_context()
            raise RuntimeError('caller exited')
    assert h.module.input_provenance.current() is None
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='next',
            input_refs=h.parents) as new:
        h.start('next','next-task','next-turn')
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(copied.run, call, h.ctx, 'read_file',
                session='native', task='task', turn='turn').result()
        assert json.loads(result)['reason']=='source_input_unavailable'
        assert h.module.input_provenance.current() is new
        assert old.result is None
        assert call(h.ctx,'read_file',session='next',task='next-task',turn='next-turn')=='executed'


def test_exact_outbox_lookup_does_not_decode_an_unrelated_damaged_payload(handoff):
    h = handoff
    h.outbox.enqueue('wanted', {'session_id':'lookup', 'contact_id':'owner',
                               'user_message':'Exact source to retrieve.'})
    h.outbox.enqueue('unrelated', {'session_id':'lookup', 'contact_id':'owner',
                                  'user_message':'Unrelated historical source.'})
    connection = h.outbox._connect()
    try:
        connection.execute("UPDATE turn_outbox SET payload_json='broken JSON' WHERE turn_id='unrelated'")
        connection.commit()
    finally:
        connection.close()
    assert h.outbox.lookup('wanted')['payload']['user_message'] == 'Exact source to retrieve.'
    assert h.outbox.lookup('absent') is None
    assert h.outbox.lookup("wanted' OR 1=1 --") is None


@pytest.mark.parametrize('annotated', [False, True])
def test_freshness_requires_actual_input_membership(handoff, annotated):
    h = handoff
    ref = h.ledger.source_references(['original-input'], contact_id='owner', session_id='observer')[0]
    if annotated:
        h.ledger.append_source_annotation(contact_id='owner', session_id='observer',
            annotation_id='withdraw-input', **ref, excerpt='Use the lamp maintenance record I supplied.',
            correction='Withdraw that request.', author_principal='host')
    for inputs, expected in ((h.parents, not annotated),
            ([{'source_id': 'original-input', 'input_message_hash': 'f' * 64}], False)):
        response = h.api.post('/v1/host/memory/sources/erasures', json={
            'contact_id': 'owner', 'session_id': 'observer', 'source_refs': [ref],
            'unannotated_input_refs': inputs})
        assert response.status_code == 200
        assert response.json()['sources_current'] is expected
        assert response.json()['input_source_refs'] == ([ref] if expected else [])


@pytest.mark.parametrize('media', [False, True])
def test_exact_input_receipt_pins_current_canonical_revision_without_relaxing_freshness(handoff, media):
    h = handoff
    from apsimo.turns.idempotency import canonical_turn_digest, source_message_hash
    from test_source_media import message
    original = message() if media else {'role': 'user', 'content': 'Use the violet reading.'}
    captured = h.api.put('/v2/host/turns/receipt-input', json={
        'identity': {'host_id': 'fixture'}, 'source_only': True,
        'context': {'contact_id': 'owner', 'session_id': 'voice-receipt'}, 'user_message': original})
    assert captured.status_code == 201 and captured.json()['source_recorded'] is True
    inputs = [{'source_id': 'receipt-input', 'input_message_hash': source_message_hash('voice-receipt', original)}]
    canonical = h.ledger.source_references(['receipt-input'], contact_id='owner', session_id='observer')
    assert len(canonical) == 1
    if media:
        # Real canonical image retention replaces inline bytes with an asset
        # handle. Hashing the transport payload would produce a wrong revision.
        assert canonical[0]['source_version'] != canonical_turn_digest([original])
    body = {'contact_id': 'owner', 'session_id': 'observer',
            'source_refs': [], 'unannotated_input_refs': inputs}
    receipt = h.api.post('/v1/host/memory/sources/erasures', json=body)
    assert receipt.status_code == 200 and receipt.json()['sources_current'] is False
    assert receipt.json()['input_source_refs'] == canonical
    checked = h.api.post('/v1/host/memory/sources/erasures', json={**body, 'source_refs': canonical})
    assert checked.status_code == 200 and checked.json()['sources_current'] is True
    wrong = h.api.post('/v1/host/memory/sources/erasures', json={**body,
        'source_refs': [{'source_id': 'receipt-input', 'source_version': 'f' * 64}]})
    assert wrong.status_code == 200 and wrong.json()['sources_current'] is False
    # Resolving input metadata never declares an explicitly wrong version fresh.
    assert wrong.json()['input_source_refs'] == canonical
    h.ledger.erase_sources(contact_id='owner', turn_ids=['receipt-input'])
    erased = h.api.post('/v1/host/memory/sources/erasures', json=body)
    assert erased.status_code == 200 and erased.json()['sources_current'] is False
    assert erased.json()['input_source_refs'] == []


def test_input_revision_receipt_requires_scoped_exact_membership(handoff):
    h = handoff
    from apsimo.turns.idempotency import source_message_hash
    original = {'role': 'user', 'content': 'Session-local calibration note.'}
    h.ledger.record_source('session-input', contact_id='owner', session_id='private-session',
                           scope='session', messages=[original], derive_claims=False)
    inputs = [{'source_id': 'session-input', 'input_message_hash': source_message_hash('private-session', original)}]
    body = {'contact_id': 'owner', 'session_id': 'observer',
            'source_refs': [], 'unannotated_input_refs': inputs}
    wrong_session = h.api.post('/v1/host/memory/sources/erasures', json=body)
    assert wrong_session.status_code == 200 and wrong_session.json()['input_source_refs'] == []
    right_session = h.api.post('/v1/host/memory/sources/erasures', json={**body, 'session_id': 'private-session'})
    assert right_session.status_code == 200 and len(right_session.json()['input_source_refs']) == 1
    wrong_person = h.api.post('/v1/host/memory/sources/erasures', json={**body, 'contact_id': 'someone-else'})
    assert wrong_person.status_code == 403
    no_selector = h.api.post('/v1/host/memory/sources/erasures', json={**body, 'unannotated_input_refs': []})
    assert no_selector.status_code == 422

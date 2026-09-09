"""Existing canonical writer and erasure protect a host's already admitted input."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import importlib
import json
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _load_plugin
from test_hermes_general_governance import _Context
from test_hermes_native_tool_authority import call
from test_turn_source_evidence import source_app


@pytest.fixture
def handoff(source_app, tmp_path, monkeypatch):
    module = _load_plugin('colony_supplied_input_test')
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    keyring = tmp_path/'principals.json'
    keyring.write_text(json.dumps({'version':1, 'principals':[{
        'principal':'native-fixture', 'status':'active', 'scopes':['turns:write','context:read','memory:read'],
        'person_ids':['owner'], 'viewer_person_id':'owner', 'audiences':['viewer'],
        'allow_unscoped_api':False, 'turn_ingress_platforms':['rcs','whatsapp'],
        'credentials':[{'id':'fixture','secret':'fixture-key','status':'active'}]}]}))
    keyring.chmod(0o600)
    source_app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keyring))
    api = TestClient(source_app, headers={'Authorization':'Bearer fixture-key'})
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
    monkeypatch.setattr(module, 'ColonyClient', Client)
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
    return SimpleNamespace(module=module, ctx=ctx, api=api, ledger=ledger, parents=parents,
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

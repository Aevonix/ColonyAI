"""Ordinary native tool evidence crosses the existing outbox/API/recall boundary."""
import copy
import hashlib
import importlib
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from apsimo.api.middleware import ApiKeyMiddleware
from apsimo.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _load_plugin, _Context
from test_scoped_api_authority import _principal, _write_keyring
from test_turn_source_evidence import source_app

RESULT = '{"operation":"copper synchronization","exit_code":3,"files_written":0}'
INSTRUCTION = 'Inspect the copper synchronization fixture and retain useful findings.'


@pytest.fixture
def native(source_app, monkeypatch, tmp_path):
    hermes_state = pytest.importorskip('hermes_state', reason='Native qualification requires Hermes on PYTHONPATH')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'native'))
    for name, value in {'COLONY_GENERAL_PLUGIN_ACTIVE':'1', 'COLONY_MEMORY_WORKER_TOOLS':'0',
                        'COLONY_MEMORY_TURN_WRITER':'disabled', 'COLONY_OWNER_CONTACT_ID':'cid-owner',
                        'COLONY_RECALL_RERANK':'off'}.items():
        monkeypatch.setenv(name, value)
    dbpath = tmp_path/'native'/'state.db'
    dbpath.parent.mkdir()
    monkeypatch.setattr(hermes_state, '_default_db_path', lambda: dbpath)
    db = hermes_state.SessionDB(dbpath)
    db.create_session('native-session', 'cli')
    db.append_message('native-session', 'user', INSTRUCTION)
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='host', secret='writer', viewer='cid-owner'),
        _principal(principal='other', secret='other', viewer='other'),
        _principal(principal='reader', secret='reader', viewer='cid-owner', scopes=['context:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    http = TestClient(source_app, headers={'Authorization':'Bearer writer'})
    plugin = _load_plugin('apsimo_original_tool_observation_test')
    class Client(plugin.ApsimoClient):
        outage = False
        def _call(self, method, path, kwargs):
            if self.outage and '/source-observation/' in path:
                return httpx.Response(503, request=httpx.Request(method, 'http://fixture'+path))
            return http.request(method, path, **{k:v for k,v in kwargs.items() if k in {'json','params'}})
        def get(self, path, **kwargs): return self._call('GET', path, kwargs)
        def post(self, path, **kwargs): return self._call('POST', path, kwargs)
        def put(self, path, **kwargs): return self._call('PUT', path, kwargs)
    clients = []
    def make_client(**kwargs):
        value = Client(**kwargs)
        clients.append(value)
        return value
    monkeypatch.setattr(plugin, 'ColonyClient', make_client)
    context = _Context(tmp_path/'outbox.db')
    context.config['plugins']['apsimo'] = context.config['plugins'].pop('colony')
    plugin.register(context)
    from hermes_cli import middleware as native_middleware, plugins as native_plugins
    manager = SimpleNamespace(_middleware={key:[value] for key,value in context.middleware.items()})
    monkeypatch.setattr(native_plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(native_plugins, 'has_middleware', lambda kind: kind in manager._middleware)
    monkeypatch.setattr(native_plugins, 'invoke_middleware',
        lambda kind, **kwargs: [callback(**kwargs) for callback in manager._middleware.get(kind, [])])
    call_context = {'session_id':'native-session', 'task_id':'native-task', 'turn_id':'native-turn'}
    context.hooks['pre_llm_call'](**call_context, platform='cli', sender_id='owner', user_message=INSTRUCTION,
        conversation_history=[{'role':'user','content':INSTRUCTION}])
    messages = [{'role':'user','content':INSTRUCTION}]
    def request(request_id='api-2'):
        return native_middleware.apply_llm_request_middleware(
            {'messages': copy.deepcopy(messages), 'tools':[{'type':'function','function':
                context.tools['apsimo_memory_retain_observation']['schema']}]},
            **call_context, api_request_id=request_id)
    request('api-1')
    def complete(call_id='call-1', result=RESULT, name='fixture_observe'):
        call = {'id':call_id,'type':'function','function':{'name':name,'arguments':'{}'}}
        messages.append({'role':'assistant','content':None,'tool_calls':[call]})
        db.append_message('native-session','assistant',tool_calls=[call])
        value = native_middleware.run_tool_execution_middleware(**call_context, api_request_id='api-1',
            tool_name=name, tool_call_id=call_id, args={}, next_call=lambda args: subprocess.run(
                [sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])', result],
                check=True, capture_output=True, text=True, timeout=5).stdout)
        assert value == result
        message_id = db.append_message('native-session','tool',result,tool_name=name,tool_call_id=call_id,
                                       timestamp=1789149600.0)
        messages.append({'role':'tool','tool_call_id':call_id,'content':value})
        return message_id
    def retain(call_id='call-1', request_id='api-2', reason='Use the recorded synchronization outcome later.'):
        args = {'call_id':call_id, 'reason':reason}
        return json.loads(native_middleware.run_tool_execution_middleware(**call_context, api_request_id=request_id,
            tool_name='apsimo_memory_retain_observation', tool_call_id='retention-call', args=args,
            next_call=lambda selected: context.tools['apsimo_memory_retain_observation']['handler'](selected)))
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    def recall(contact='cid-owner'):
        response = http.post('/v1/host/context/assemble', json={'identity':{'host_id':'test'},
            'context':{'contact_id':contact,'session_id':'other-channel-session'},
            'incoming_message':{'role':'user','content':'copper synchronization outcome'}})
        assert response.status_code == 200, response.text
        return next((s for s in response.json()['sections'] if s['id']=='colony-memory'), {})
    yield SimpleNamespace(plugin=plugin, context=context, db=db, http=http, clients=clients,
        complete=complete, request=request, retain=retain, ledger=ledger, recall=recall,
        messages=messages, scope=call_context, outbox=plugin.TurnOutbox(tmp_path/'outbox.db'))
    db.close()
    http.close()


def test_actual_native_original_roundtrips_into_automatic_recall(native):
    n = native
    assert 'apsimo_memory_retain_observation' in n.context.tools
    assert 'colony_memory_retain_observation' not in n.context.tools
    message_id = n.complete()
    assert not n.retain()['accepted']  # Completion alone is not current-request exposure.
    n.request()
    result = n.retain()
    assert result['accepted'] and result['source_recorded'], result
    packet = n.recall()
    assert 'copper synchronization' in packet['body'] and '"role": "tool"' in packet['body']
    with sqlite3.connect(n.ledger.db_path) as db:
        message = json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                      (result['source_id'],)).fetchone()[0])[0]
        assert message['content'] == RESULT and message['role'] == 'tool'
        assert message['provenance']['native']['message_id'] == message_id
        assert message['provenance']['native']['result_sha256'] == hashlib.sha256(RESULT.encode()).hexdigest()
        assert message['provenance']['native']['api_request_id'] == 'api-1'
        assert message['provenance']['selection']['author'] == 'model'
        assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0] == 0
    assert 'Use the recorded synchronization outcome later.' not in packet['body']
    assert n.retain(reason='A different retry reason')['source_id'] == result['source_id']
    assert len([row for row in n.outbox.snapshot() if row['turn_id']==result['source_id']]) == 1


def test_failed_delivery_is_pending_and_same_outbox_retries_without_native_reexecution(native):
    n = native
    n.complete()
    n.request()
    n.clients[0].outage = True
    first = n.retain()
    assert first['state']=='pending' and not first['source_recorded']
    assert 'files_written' not in n.recall().get('body','')
    n.clients[0].outage = False
    n.outbox.drain(lambda stored, timeout_seconds: n.clients[0].sync_turn(
        **stored, outbox=n.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
    assert n.retain()['source_recorded']
    assert 'files_written' in n.recall()['body']
    assert n.db._conn.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0] == 1


def test_origin_erasure_removes_observation_and_queued_retry(native):
    n = native
    n.complete()
    n.request()
    result = n.retain()
    assert result['accepted'], result
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==result['source_id'])
    origin = row['payload']['observation']['origin']['source_id']
    erased = n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[origin])
    assert result['source_id'] in erased['affected_source_ids']
    assert 'files_written' not in n.recall().get('body','')
    again = n.retain()
    assert not again['source_recorded'] and again['state']=='erased', again


def test_erased_origin_cannot_be_restored_by_pending_delivery(native):
    n = native
    n.complete()
    n.request()
    n.clients[0].outage = True
    pending = n.retain()
    assert pending['state'] == 'pending'
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==pending['source_id'])
    origin = row['payload']['observation']['origin']['source_id']
    n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[origin])
    n.clients[0].outage = False
    n.outbox.drain(lambda stored, timeout_seconds: n.clients[0].sync_turn(
        **stored, outbox=n.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
    result = n.retain()
    assert result['state'] == 'erased' and not result['source_recorded']
    assert 'files_written' not in n.recall().get('body','')


def test_call_request_viewer_and_raw_bytes_are_bound(native):
    n = native
    n.complete()
    n.request()
    assert not n.retain(call_id='invented')['accepted']
    assert not n.retain(request_id='different-concurrent-request')['accepted']
    # A second request that no longer contains the result cannot reuse a prior receipt.
    original = n.messages[-1]['content']
    n.messages[-1]['content'] = '{"operation":"copper synchronization","exit_code":0}'
    n.request('api-3')
    assert not n.retain(request_id='api-3')['accepted']
    n.messages[-1]['content'] = original
    result = n.retain()
    assert result['accepted'], result
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==result['source_id'])
    body = {'identity':{'host_id':'hermes'}, 'context':{'session_id':'native-session',
        'contact_id':'cid-owner','turn_id':result['source_id']},'observation':row['payload']['observation']}
    path = '/v2/host/turns/source-observation/'+result['source_id']
    assert n.http.put(path,json=body,headers={'Authorization':'Bearer other'}).status_code == 403
    assert n.http.put(path,json=body,headers={'Authorization':'Bearer reader'}).status_code == 403
    changed = copy.deepcopy(body)
    changed['observation']['content'] += ' altered'
    assert n.http.put(path,json=changed).status_code == 422


def test_another_turn_cannot_nominate_a_call_from_this_turn(native):
    n = native
    n.complete()
    n.request()
    other_context = {**n.scope, 'turn_id':'another-turn', 'task_id':'another-task'}
    n.context.hooks['pre_llm_call'](**other_context, platform='cli',sender_id='owner',
        user_message=INSTRUCTION,conversation_history=n.messages)
    result = json.loads(n.context.middleware['tool_execution'](**other_context,
        api_request_id='api-2',tool_name='apsimo_memory_retain_observation',tool_call_id='intruding',
        args={'call_id':'call-1','reason':'reuse'}, next_call=lambda args:
            n.context.tools['apsimo_memory_retain_observation']['handler'](args)))
    assert not result['accepted']


@pytest.mark.parametrize('name,result', [('session_search',RESULT),
    ('apsimo_memory_retain_observation',RESULT), ('terminal','x'*16385)])
def test_rereads_retention_outputs_and_oversized_results_are_not_candidates(native, name, result):
    n = native
    # session_search requires its own native result reconciliation. Exercise the
    # exact completion observer directly for these excluded producer classes.
    module = importlib.import_module(n.plugin.__name__+'.tool_observations')
    memory = SimpleNamespace(supplied_snapshot=lambda scope: [])
    observer = module.ToolObservations(n.clients[0], n.outbox, memory)
    scope = SimpleNamespace(contact_id='cid-owner',session_id='native-session',task_id='native-task',
        turn_id='native-turn',valid_participant=True,authority_lane='system',platform='cli',user_message=INSTRUCTION)
    observer.completed(scope,{'tool_call_id':'excluded','tool_name':name,'api_request_id':'api-1'},result)
    assert not observer._turns

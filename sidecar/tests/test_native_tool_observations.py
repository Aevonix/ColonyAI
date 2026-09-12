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
    def request(request_id='api-2', *, anthropic=False, responses=False, deferred=False, tools=True,
                before_middleware=None):
        payload = {'messages': copy.deepcopy(messages), 'tools':[{'type':'function','function':
            context.tools['apsimo_memory_retain_observation']['schema']}]}
        if deferred:
            from tools.tool_search import assemble_tool_defs, ToolSearchConfig
            payload['tools'] = assemble_tool_defs(payload['tools'], config=ToolSearchConfig.from_raw(
                {'enabled': 'on', 'defer': ['apsimo_memory_retain_observation']})).tool_defs
        if not tools:
            payload['tools'] = []
        if anthropic:
            from agent.anthropic_message_convert import convert_messages_to_anthropic, convert_tools_to_anthropic
            system, payload['messages'] = convert_messages_to_anthropic(payload['messages'])
            if system is not None:
                payload['system'] = system
            payload['tools'] = convert_tools_to_anthropic(payload['tools'])
        if responses:
            from agent.codex_responses_adapter import _chat_messages_to_responses_input, _responses_tools
            payload['input'] = _chat_messages_to_responses_input(payload.pop('messages'))
            payload['tools'] = _responses_tools(payload['tools'])
            payload['instructions'] = 'Stable identity.'
        if before_middleware is not None:
            before_middleware(payload)
        return native_middleware.apply_llm_request_middleware(payload,
            **call_context, api_request_id=request_id,
            api_mode='anthropic_messages' if anthropic else 'chat_completions')
    request('api-1')
    def complete(call_id='call-1', result=RESULT, name='fixture_observe', arguments=None):
        arguments = {} if arguments is None else copy.deepcopy(arguments)
        call = {'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}
        messages.append({'role':'assistant','content':None,'tool_calls':[call]})
        db.append_message('native-session','assistant',tool_calls=[call])
        value = native_middleware.run_tool_execution_middleware(**call_context, api_request_id='api-1',
            tool_name=name, tool_call_id=call_id, args=arguments, next_call=lambda args: subprocess.run(
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


def test_actual_native_anthropic_conversion_preserves_original_tool_nomination(native):
    n = native
    n.complete()
    request = n.request(anthropic=True).payload
    blocks = [block for message in request['messages'] if isinstance(message.get('content'), list)
              for block in message['content']]
    assert any(block.get('type') == 'tool_use' and block.get('id') == 'call-1' for block in blocks)
    assert any(block.get('type') == 'tool_result' and block.get('content') == RESULT for block in blocks)
    receipt = n.retain()
    assert receipt['accepted'] and receipt['source_recorded'], receipt
    rows = n.ledger.search_sources('copper synchronization', contact_id='cid-owner', session_id='later')
    original = next(row for row in rows if row['turn_id'] == receipt['source_id'])
    assert original['role'] == 'tool' and original['content'] == RESULT
    assert '"call_id": "call-1"' in request['system']


def test_actual_native_deferred_catalog_and_completed_call_offer_bounded_hint(native):
    n = native
    assert not any('apsimo-observation-candidates-v1' in str(row) for row in n.request(deferred=True).payload['messages'])
    n.complete()
    before = copy.deepcopy(n.messages)
    request = n.request(deferred=True).payload
    schemas = {row['function']['name']: row['function'] for row in request['tools']}
    assert 'apsimo_memory_retain_observation' not in schemas
    assert '- apsimo_memory_retain_observation: Retain a useful original tool result in persistent memory.' in schemas['tool_search']['description']
    hints = [row['content'] for row in request['messages'] if row.get('role') == 'system'
             and str(row.get('content', '')).startswith('[apsimo-observation-candidates-v1]')]
    assert len(hints) == 1 and len(hints[0]) <= 2048
    assert '"call_id": "call-1"' in hints[0] and '"tool_name": "fixture_observe"' in hints[0]
    assert 'tool_describe' in hints[0] and 'tool_call' in hints[0]
    assert 'not saved memories' in hints[0] and RESULT not in hints[0]
    assert n.messages == before and n.retain()['source_recorded']


@pytest.mark.parametrize('legacy_arguments', [False, True])
@pytest.mark.parametrize('api_format', ['chat', 'anthropic', 'responses'])
def test_native_deferred_original_dispatch_persistence_and_nomination(native, monkeypatch, legacy_arguments, api_format):
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from agent.tool_executor import execute_tool_calls_sequential
    from tools import tool_search
    n = native
    schema = {'type': 'function', 'function': {'name': 'fixture_observe',
        'description': 'Controlled local observation.',
        'parameters': {'type': 'object', 'properties': {'item': {'type': 'string'}}}}}
    configuration = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': ['fixture_observe']})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: configuration)
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kwargs: [schema])
    monkeypatch.setattr('model_tools.check_toolset_requirements', lambda **kwargs: {})
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda **kwargs: None)
    monkeypatch.setattr('agent.model_metadata.get_model_context_length', lambda *args, **kwargs: 65536)
    monkeypatch.setattr('agent.model_metadata._resolve_custom_endpoint_context_length', lambda *args, **kwargs: 65536)
    with patch('agent.process_bootstrap.OpenAI'), patch('agent.model_metadata.fetch_model_metadata', return_value={}):
        agent = AIAgent(api_key='fixture', base_url='http://127.0.0.1:1/v1', provider='openai',
            model='fixture/model', session_id='native-session', session_db=n.db, quiet_mode=True,
            skip_context_files=True, skip_memory=True, skip_background_review=True, platform='cli')
    agent._current_turn_id, agent._current_api_request_id = 'native-turn', 'api-1'
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value='')
    agent._end_session_on_close = False
    calls = []
    def dispatch(name, args, task_id, **kwargs):
        calls.append((name, copy.deepcopy(args), task_id, kwargs['tool_call_id']))
        return RESULT
    monkeypatch.setattr('model_tools.handle_function_call', dispatch)
    arguments = {'item': 'copper synchronization'}
    # 0.21.1 advertises the single-call shape; 0.21.2 additionally advertises calls[].
    bridge = next(schema['function'] for schema in tool_search.bridge_tool_schemas(1)
                  if schema['function']['name'] == 'tool_call')
    use_calls = not legacy_arguments and 'calls' in bridge['parameters']['properties']
    wrapper = ({'calls': [{'name': 'fixture_observe', 'arguments': arguments}]} if use_calls else
               {'name': 'fixture_observe', 'arguments': json.dumps(arguments) if legacy_arguments else arguments})
    call = {'id': 'deferred-original', 'type': 'function',
            'function': {'name': 'tool_call', 'arguments': json.dumps(wrapper)}}
    n.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
    assistant = SimpleNamespace(tool_calls=[SimpleNamespace(id=call['id'], type='function',
        function=SimpleNamespace(**call['function']))])
    try:
        execute_tool_calls_sequential(agent, assistant, n.messages, 'native-task', finalize=False)
        assert calls == [('fixture_observe', arguments, 'native-task', 'deferred-original')]
        row = n.db._conn.execute("SELECT id,content,tool_name FROM messages WHERE tool_call_id=? AND role='tool'",
                                 ('deferred-original',)).fetchone()
        assert row['content'] == RESULT and row['tool_name'] == 'fixture_observe'
        assert n.messages[-2]['tool_calls'][0]['function']['name'] == 'tool_call'
        request_options = {'deferred': True, 'anthropic': api_format == 'anthropic',
                           'responses': api_format == 'responses'}
        original_messages = copy.deepcopy(n.messages)
        changed_args = copy.deepcopy(wrapper)
        (changed_args['calls'][0] if use_calls else changed_args)['arguments'] = {'item': 'different input'}
        for replacement in (
            changed_args,
            {'calls': [{'name': 'fixture_observe', 'arguments': arguments}] * 2},
            {'calls': [{'name': 'fixture_other', 'arguments': arguments}]},
            {'calls': [{'name': 'fixture_observe', 'arguments': 'invalid JSON'}]},
            {'calls': [{'name': 'tool_call', 'arguments': arguments}]},
            {'calls': [{'name': 'connectors__fixture__read', 'arguments': arguments}]},
        ):
            n.messages[-2]['tool_calls'][0]['function']['arguments'] = json.dumps(replacement)
            rejected = n.request(**request_options).payload
            assert 'apsimo-observation-candidates-v1' not in str(rejected), replacement
            assert not n.retain('deferred-original')['accepted'], replacement
        n.messages[:] = copy.deepcopy(original_messages)
        for duplicate_result in (False, True):
            def duplicate(payload):
                # Inject after conversion: native Anthropic conversion repairs
                # duplicate history IDs before the actual SDK middleware sees them.
                if api_format == 'anthropic':
                    kind = 'tool_result' if duplicate_result else 'tool_use'
                    row = next(row for row in payload['messages']
                               if any(block.get('type') == kind for block in row.get('content', [])
                                      if isinstance(block, dict)))
                    row['content'].append(copy.deepcopy(next(b for b in row['content'] if b.get('type') == kind)))
                else:
                    rows = payload['input'] if api_format == 'responses' else payload['messages']
                    field = 'type' if api_format == 'responses' else 'role'
                    kind = (('function_call_output' if duplicate_result else 'function_call')
                            if api_format == 'responses' else ('tool' if duplicate_result else 'assistant'))
                    row = next(row for row in rows if row.get(field) == kind)
                    rows.append(copy.deepcopy(row))
            assert 'apsimo-observation-candidates-v1' not in str(n.request(
                **request_options, before_middleware=duplicate).payload)
            assert not n.retain('deferred-original')['accepted']
        assert not [item for item in n.outbox.snapshot() if item['turn_id'].startswith('native-observation:')]
        request = n.request(**request_options).payload
        assert '"call_id": "deferred-original"' in str(request)
        receipt = n.retain('deferred-original')
        assert receipt['accepted'] and receipt['source_recorded'], receipt
        assert receipt['selected_call']['message_id'] == row['id']
        assert receipt['selected_call']['tool_name'] == 'fixture_observe'
        assert receipt['selected_call']['result_sha256'] == hashlib.sha256(RESULT.encode()).hexdigest()
        assert 'copper synchronization' in n.recall()['body']
        # This transport correction must not weaken the existing erase dependency.
        observation = next(item['payload']['observation'] for item in n.outbox.snapshot()
                           if item['turn_id'] == receipt['source_id'])
        n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[observation['origin']['source_id']])
        again = n.retain('deferred-original')
        assert again['state'] == 'erased' and not again['source_recorded']
        assert 'copper synchronization' not in n.recall().get('body', '')
    finally:
        agent._session_db = None  # The fixture owns this database connection.
        agent.close()


def test_actual_native_responses_conversion_places_hint_in_instructions(native):
    n = native
    n.complete()
    request = n.request(responses=True).payload
    assert request['instructions'].startswith('Stable identity.')
    assert request['instructions'].count('[apsimo-observation-candidates-v1]') == 1
    assert '"call_id": "call-1"' in request['instructions']
    assert any(row.get('type') == 'function_call_output' and row.get('output') == RESULT for row in request['input'])
    assert n.retain()['source_recorded']


@pytest.mark.parametrize('format', ['chat', 'anthropic', 'responses'])
def test_same_tool_calls_show_executed_arguments_and_exact_selected_receipt(native, format):
    n = native
    earlier = {'command': 'printf first-status'}
    later = {'command': 'printf detailed-inspection'}
    message_id = n.complete('earlier-call', 'first-status', 'terminal', earlier)
    n.complete('later-call', RESULT, 'terminal', later)
    # A request-side retelling does not replace the actual execution label.
    n.messages[1]['tool_calls'][0]['function']['arguments'] = json.dumps(later)
    request = n.request(anthropic=format == 'anthropic', responses=format == 'responses').payload
    if format == 'chat':
        hint = next(row['content'] for row in request['messages'] if str(row.get('content', '')).startswith(
            '[apsimo-observation-candidates-v1]'))
    else:
        hint = request['system' if format == 'anthropic' else 'instructions']
    candidates = json.loads(hint.split('Eligible completed calls in this request: ', 1)[1].split('\n[/', 1)[0])
    assert [row['call_id'] for row in candidates] == ['later-call', 'earlier-call']
    assert [json.loads(row['arguments_preview']) for row in candidates] == [later, earlier]
    assert all(row['tool_name'] == 'terminal' and row['arguments_truncated'] is False for row in candidates)

    # A wrong nomination remains the exact selected original, visibly identified.
    receipt = n.retain('earlier-call', reason='Retain the detailed inspection outcome.')
    assert receipt['source_recorded'], receipt
    selected = receipt['selected_call']
    assert selected == {'tool_call_id': 'earlier-call', 'tool_name': 'terminal',
        'message_id': message_id, 'result_sha256': hashlib.sha256(b'first-status').hexdigest(),
        'arguments_preview': json.dumps(earlier, sort_keys=True, separators=(',', ':')),
        'arguments_truncated': False}
    row = next(row for row in n.outbox.snapshot() if row['turn_id'] == receipt['source_id'])
    observation = row['payload']['observation']
    assert observation['content'] == 'first-status'
    assert observation['native']['tool_call_id'] == 'earlier-call'
    assert 'arguments_preview' not in observation['native']  # Persisted source contract is unchanged.
    assert n.retain('earlier-call', reason='Another retelling')['selected_call'] == selected


def test_argument_previews_are_explicitly_truncated_inside_total_hint_budget(native):
    n = native
    arguments = {'command': 'inspect ' + 'z' * 200}
    for number in range(10):
        n.complete(f'call-{number}', f'original-{number}', 'terminal', arguments)
    request = n.request(deferred=True).payload
    hint = next(row['content'] for row in request['messages'] if str(row.get('content', '')).startswith(
        '[apsimo-observation-candidates-v1]'))
    assert len(hint) <= 2048
    candidates = json.loads(hint.split('Eligible completed calls in this request: ', 1)[1].split('\n[/', 1)[0])
    assert 1 <= len(candidates) <= 8
    assert [row['call_id'] for row in candidates] == [f'call-{number}' for number in range(9, 9-len(candidates), -1)]
    assert all(row['arguments_truncated'] and len(row['arguments_preview']) == 128 for row in candidates)
    assert all(f'original-{number}' not in hint for number in range(10))
    receipt = n.retain('call-9')
    assert receipt['source_recorded'] and receipt['selected_call']['arguments_truncated']
    assert receipt['selected_call']['arguments_preview'] == candidates[0]['arguments_preview']


def test_hint_omits_invented_stale_calls_and_disappears_without_available_tool(native):
    n = native
    n.complete()
    n.messages.extend([{'role': 'assistant', 'tool_calls': [{'id': 'invented', 'function': {
        'name': 'fixture_observe', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'invented', 'content': RESULT}])
    request = n.request().payload
    hint = next(row for row in request['messages'] if str(row.get('content', '')).startswith(
        '[apsimo-observation-candidates-v1]'))
    assert 'invented' not in hint['content'] and '"call_id": "call-1"' in hint['content']
    n.messages.append(hint)  # Simulate re-processing a request that already has our hint.
    no_tools = n.request(tools=False).payload
    assert not any('apsimo-observation-candidates-v1' in str(row) for row in no_tools['messages'])
    for row in n.messages:
        if row.get('role') == 'tool' and row.get('tool_call_id') == 'call-1':
            row['content'] = 'Different bytes in the current request.'
    stale = n.request().payload
    assert not any('apsimo-observation-candidates-v1' in str(row) for row in stale['messages'])
    assert not n.retain()['accepted']


def test_hint_operation_requires_direct_schema_or_exact_native_catalog_entry(native):
    module = importlib.import_module(native.plugin.__name__ + '.tool_observations')
    tools = [{'name': name} for name in ('tool_search', 'tool_describe', 'tool_call')]
    tools[0]['description'] = 'Some prose mentions apsimo_memory_retain_observation.'
    assert module._available_retention({'tools': tools}) is None
    tools[0]['description'] = module._CATALOG_HEADER + '\nother tools (2):\napsimo_memory_retain_observation, other_tool'
    assert module._available_retention({'tools': tools}) == ('apsimo_memory_retain_observation', True)
    assert module._available_retention({'tools': tools, 'tool_choice': 'none'}) is None
    assert module._available_retention({'tools': tools[:-1]}) is None


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
    request = observer.checked({'messages': [], 'tools': [{'name': 'apsimo_memory_retain_observation'}]},
        scope, 'api-2')
    assert 'apsimo-observation-candidates-v1' not in str(request)

"""Disposable real gateway, canonical ASGI routes and controlled SDK HTTP only."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

installed, sidecar, native, dependencies = sys.argv[1:]
sys.path[:0] = [installed, sidecar, *([native] if native else [])]
if dependencies:
    sys.path.append(dependencies)

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import executions, host
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.turns import get_turn_idempotency_ledger

home = Path(os.environ['HERMES_HOME'])
home.mkdir(mode=0o700)
state = Path(os.environ['COLONY_STATE_DIR'])
state.mkdir(mode=0o700)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))


async def seed():
    await contacts.connect()
    person = await contacts.create(display_name='Native task fixture owner', trust_tier='inner_circle')
    await contacts.add_handle(person.contact_id, 'api_server', 'owner-api', verified=True)
    await contacts.add_handle(person.contact_id, 'whatsapp', '15550003@s.whatsapp.net', verified=True)
    return person.contact_id


owner = asyncio.run(seed())
host._contacts_store = contacts
os.environ['COLONY_OWNER_CONTACT_ID'] = owner
secret = 'isolated-native-task-fixture-key'
keyring = home/'keys.json'
keyring.write_text(json.dumps({'version': 1, 'principals': [{
    'principal': 'native-task-fixture', 'status': 'active', 'viewer_person_id': owner,
    'person_ids': [owner], 'audiences': ['viewer'],
    'scopes': ['api:access', 'turns:resolve-sender', 'context:read', 'memory:read', 'turns:write'],
    'credentials': [{'id': 'fixture', 'secret': secret, 'status': 'active'}]}]}))
keyring.chmod(0o600)
(home/'config.yaml').write_text(json.dumps({
    'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': 'http://model.fixture/v1'},
    'auxiliary': {'title_generation': {'enabled': False}},
    'terminal': {'cwd': str(home)}, 'agent': {'max_turns': 4}, 'toolsets': ['colony'],
    'display': {'platforms': {'colony_task': {'streaming': False, 'tool_progress': 'off'}}},
    'memory': {'provider': 'colony-memory', 'config': {
        'contact_id': owner, 'url': 'http://fixture', 'api_key': secret}},
    'plugins': {'enabled': ['colony'], 'colony': {
        'owner_contact_id': owner, 'url': 'http://fixture', 'api_key': secret,
        'turn_outbox_path': str(home/'outbox.db'), 'execution_registry_enabled': True,
        'native_tasks': {'enabled': True, 'state_path': str(home/'native-tasks.db')}}}}))

app = FastAPI()
app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
for router in (host.router, host.v2_router, executions.router):
    app.include_router(router)
api = TestClient(app)
api.__enter__()
ledger = get_turn_idempotency_ledger(state)
wire, generation, foreground_calls, tool_results = [], {'alpha': [], 'beta': []}, {}, {}
context_reads = []
held = {name: threading.Event() for name in ('alpha', 'beta', 'alpha_next')}
release = {name: threading.Event() for name in held}
task_ids = {}
source_parents = {}
tearing_down = False
update_text = 'Keep the alpha comparison scoped to the violet notes and label the result ORANGE-472.'


def message_response(body, message, finish='stop'):
    if body.get('stream'):
        delta = dict(message)
        if delta.get('tool_calls'):
            delta['tool_calls'] = [{**value, 'index': index}
                for index, value in enumerate(delta['tool_calls'])]
        chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
            'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
        end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=(
            'data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(end) + '\n\ndata: [DONE]\n\n').encode())
    return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
        'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})


def answer(body, text):
    return message_response(body, {'role': 'assistant', 'content': text})


def tool(body, name, arguments):
    return message_response(body, {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'fixture-tool', 'type': 'function', 'function': {'name': 'tool_call',
        'arguments': json.dumps({'name': name, 'arguments': arguments})}}]}, 'tool_calls')


def respond(request):
    if request.url.host == 'fixture':
        response = api.request(request.method, request.url.path, params=request.url.params,
            headers=dict(request.headers), content=request.content)
        wire.append({'path': request.url.path, 'status': response.status_code})
        if request.url.path == '/v1/host/context/assemble':
            context_reads.append({'context': json.loads(request.content)['context'],
                'status': response.status_code})
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    assert request.url.host == 'model.fixture', str(request.url)
    if request.method == 'GET' and request.url.path == '/v1/models':
        return httpx.Response(200, json={'data': [{'id': 'fixture-model', 'context_length': 32768}]})
    if request.url.path == '/api/show':
        return httpx.Response(404, json={'error': 'Not Ollama'})
    assert request.url.path == '/v1/chat/completions', str(request.url)
    body = json.loads(request.content)
    if tearing_down:
        # Release controlled provider work during cleanup without replacing an
        # earlier test failure with an assertion from a cancelled request.
        return answer(body, 'Fixture cleanup after qualification ended.')
    from colony_hermes.native_task_platform import ACTIVE
    from colony_hermes.input_provenance import current
    active = ACTIVE.get()
    if active is not None:
        row = adapter.handoffs.get(active['id'])
        name = 'alpha' if 'TASK_ALPHA' in row['request'] else 'beta'
        assert 'TASK_' + name.upper() in row['request'], row
        generation[name].append(body)
        step = len(generation[name])
        assert step <= (2 if name == 'alpha' else 1), (name, step)
        assert row['native_session_id'] and row['native_task_id'] and row['native_turn_id'], row
        assert active['native']['session_id'] == row['native_session_id']
        supplied = current()
        assert supplied is not None and supplied.session_id == row['native_session_id']
        assert supplied.memory_contact(row['native_session_id']) == owner, {
            'task': name, 'step': step, 'native': active.get('native'),
            'closed': supplied._closed, 'blocked': supplied._blocked,
            'failure': supplied.failure, 'bound': list(supplied._bound),
            'memory_sessions': list(supplied._memory_sessions), 'messages': body['messages']}
        if step == 1:
            assert any(read['status'] == 200 and read['context'] == {
                'session_id': row['native_session_id'], 'contact_id': owner}
                for read in context_reads), {'task': name, 'context_reads': context_reads}
            assert supplied.parents()[0] == row['source']['input_refs']
            source_parents[name] = row['source']['input_refs']
            held[name].set()
            assert release[name].wait(35), 'Held ' + name + ' request was never released'
            if name == 'alpha':
                return tool(body, 'colony_memory_read_source', row['source']['source_refs'][0])
            return answer(body, 'TASK_BETA completed with its own retained source.')
        text = json.dumps(body['messages'])
        assert update_text in text and 'colony-task-update-v1' in text, text
        updates = adapter.handoffs.updates(row['id'])
        from agent import relay_runtime
        turn = relay_runtime.current_turn()
        assert len(updates) == 1 and updates[0]['observations'].get('native_request_visible'), {
            'updates': updates, 'managed_callback_depth': relay_runtime._MANAGED_CALLBACK_DEPTH.get(),
            'relay_active': relay_runtime.active_turn(row['native_session_id']) is turn,
            'relay_enabled': getattr(turn, 'relay_enabled', None),
            'logical_llm_calls': list(getattr(turn, 'logical_llm_calls', {})),
            'native_turn': active.get('native')}
        assert turn.logical_llm_calls, 'The independent task bypassed native Relay execution'
        assert updates[0]['source']['input_refs'][0] in supplied.parents()[0]
        assert updates[0]['source']['input_refs'][0] in row['dependencies']['input_refs']
        held['alpha_next'].set()
        assert release['alpha_next'].wait(35), 'The stopped alpha provider request was never released'
        return answer(body, 'LATE_ALPHA_RESULT_MUST_NOT_BE_RETAINED')

    assert current() is None, 'An ordinary conversation inherited a task source context'
    latest = next(row.get('content') for row in reversed(body['messages']) if row.get('role') == 'user')
    latest = latest if isinstance(latest, str) else json.dumps(latest)
    tag = next((name for name in ('SUBMIT_ALPHA', 'SUBMIT_BETA', 'ORDINARY', 'STEER_ALPHA', 'STOP_ALPHA')
                if latest.startswith('FG_' + name + ':')), None)
    assert tag is not None, latest
    foreground_calls[tag] = foreground_calls.get(tag, 0) + 1
    step = foreground_calls[tag]
    if tag == 'ORDINARY':
        assert step == 1
        return answer(body, 'The ordinary conversation completed while both tasks stayed active.')
    assert step <= 2, (tag, body)
    if step == 1:
        if tag.startswith('SUBMIT_'):
            name = tag.removeprefix('SUBMIT_')
            return tool(body, 'colony_task', {'operation': 'submit',
                'request': 'TASK_' + name + ': Compare my violet calibration notes and retain the result.'})
        return tool(body, 'colony_task', {'operation': 'steer' if tag == 'STEER_ALPHA' else 'stop',
            'task_id': task_ids['alpha'], **({'request': update_text} if tag == 'STEER_ALPHA' else {})})
    results = [row['content'] for row in body['messages'] if row.get('role') == 'tool']
    tool_results[tag] = results
    assert results and '"error"' not in results[-1], (tag, results)
    if tag.startswith('SUBMIT_') or tag == 'STEER_ALPHA':
        assert '"accepted": true' in results[-1], (tag, results)
    else:
        assert '"stop_requested": true' in results[-1], results
    return answer(body, 'FG_' + tag + '_ACK')


def controlled(self, request):
    try:
        return respond(request)
    except BaseException:
        import traceback
        traceback.print_exc()
        print(json.dumps({'wire': wire, 'foreground': foreground_calls}), file=sys.stderr)
        sys.stderr.flush()
        os._exit(70)


httpx.HTTPTransport.handle_request = controlled


def no_network(*args, **kwargs):
    raise AssertionError('Native task qualification cannot access the network')


socket.socket.connect = no_network
socket.create_connection = no_network
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['colony'].enabled, manager._plugins['colony'].error
from gateway.config import GatewayConfig, PlatformConfig, Platform
from gateway.platform_registry import platform_registry
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.platforms.event import MessageEvent, MessageType

# Disable only the unrelated optional scanner bootstrap that installs a binary.
import tools.tirith_security
tools.tirith_security.ensure_installed = lambda **kwargs: False
config = GatewayConfig(sessions_dir=home/'sessions', loop_watchdog=False)
platform_config = PlatformConfig(enabled=True, typing_indicator=False, gateway_restart_notification=False)
config.platforms = {Platform('colony_task'): platform_config}
runner = GatewayRunner(config)
adapter = platform_registry.create_adapter('colony_task', platform_config)
assert adapter is not None
runner.adapters[adapter.platform] = adapter
runner.delivery_router.adapters = runner.adapters
runner._wire_adapter_handlers(adapter)


def event(tag, *, whatsapp=False):
    return MessageEvent(text='FG_' + tag + ': ' + (update_text if tag == 'STEER_ALPHA' else
        'Perform the requested local calibration comparison.' if tag.startswith('SUBMIT_') else
        'Stop the alpha task.' if tag == 'STOP_ALPHA' else 'Answer an unrelated short question.'),
        source=SessionSource(platform=Platform.WHATSAPP if whatsapp else Platform.API_SERVER,
            chat_id='15550003@s.whatsapp.net' if whatsapp else 'thread-' + tag.lower(),
            user_id='15550003@s.whatsapp.net' if whatsapp else 'owner-api'),
        message_id='message-' + tag.lower(), message_type=MessageType.TEXT, allow_gateway_control=False)


async def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(.025)
    assert predicate(), {'waiting_for': label, 'wire': wire, 'foreground': foreground_calls,
        'tasks': adapter.handoffs.recent(), 'tool_results': tool_results}


async def exercise():
    global tearing_down
    runner._running = True
    runner._gateway_loop = asyncio.get_running_loop()
    try:
        assert await adapter.connect()
        assert adapter.verify_http_event_request('Bearer untrusted-caller')[0] is False
        # Actual foreground native turns call the registered tool. Neither the
        # test nor the model can supply contact, principal, origin or input refs.
        answers = await asyncio.wait_for(asyncio.gather(
            runner._handle_message(event('SUBMIT_ALPHA')),
            runner._handle_message(event('SUBMIT_BETA'))), 20)
        assert answers == ['FG_SUBMIT_ALPHA_ACK', 'FG_SUBMIT_BETA_ACK'], answers
        await wait_for(lambda: held['alpha'].is_set() and held['beta'].is_set(), 'both native roots at SDK')
        rows = adapter.handoffs.recent()
        assert len(rows) == 2, rows
        for row in rows:
            name = 'alpha' if 'TASK_ALPHA' in row['request'] else 'beta'
            task_ids[name] = row['id']
            assert row['source']['origin']['platform'] == 'api_server'
            assert row['source']['source_session_id'] != row['native_session_id']
            assert row['response'] is None and row['stop'] is None
        assert len({tuple(row[key] for key in ('native_session_id', 'native_task_id', 'native_turn_id'))
                    for row in rows}) == 2
        assert source_parents['alpha'] != source_parents['beta']
        ordinary = await asyncio.wait_for(runner._handle_message(event('ORDINARY')), 12)
        assert ordinary == 'The ordinary conversation completed while both tasks stayed active.', ordinary
        assert not release['alpha'].is_set() and not release['beta'].is_set()
        steered = await asyncio.wait_for(runner._handle_message(event('STEER_ALPHA', whatsapp=True)), 12)
        assert steered == 'FG_STEER_ALPHA_ACK', steered
        updates = adapter.handoffs.updates(task_ids['alpha'])
        assert len(updates) == 1 and updates[0]['source']['origin']['platform'] == 'whatsapp'
        assert updates[0]['source']['origin']['sender_id'] == '15550003@s.whatsapp.net'
        assert not adapter.handoffs.updates(task_ids['beta'])
        assert not updates[0]['observations'].get('native_request_visible')
        release['alpha'].set()
        await wait_for(held['alpha_next'].is_set, 'steered next native SDK request')
        stopped = await asyncio.wait_for(runner._handle_message(event('STOP_ALPHA', whatsapp=True)), 12)
        assert stopped == 'FG_STOP_ALPHA_ACK', stopped
        assert adapter.handoffs.get(task_ids['alpha'])['stop']['native_control_acknowledged']
        assert not adapter.handoffs.get(task_ids['beta'])['stop']
        release['alpha_next'].set()
        await wait_for(lambda: bool(adapter.handoffs.get(task_ids['alpha'])['terminal']), 'alpha terminal receipt')
        alpha = adapter.handoffs.get(task_ids['alpha'])
        assert alpha['terminal']['interrupted'] and alpha['response'] is None, alpha
        assert adapter.handoffs.stop_view(alpha)['status'] == 'cancelled'
        assert not adapter.handoffs.get(task_ids['beta'])['response']
        release['beta'].set()
        await wait_for(lambda: bool(adapter.handoffs.get(task_ids['beta'])['response']), 'unrelated task retained result')
        beta = adapter.handoffs.get(task_ids['beta'])
        assert beta['response']['text'] == 'TASK_BETA completed with its own retained source.'
        assert beta['response']['source_dependencies']['input_refs'] == source_parents['beta']
        retained = beta['response']['source_dependencies']
        with ledger._connect() as db:
            canonical = db.execute('SELECT contact_id, messages_json FROM turn_sources WHERE turn_id = ?',
                (retained['turn_id'],)).fetchone()
        assert canonical is not None and canonical['contact_id'] == owner, canonical
        assert beta['response']['text'] in canonical['messages_json'], canonical['messages_json']
        people = await contacts.list()
        assert {person.contact_id for person in people} == {owner}, [
            {'contact_id': person.contact_id, 'name': person.display_name,
             'handles': [(handle.gateway, handle.address)
                for handle in await contacts.get_handles(person.contact_id)]}
            for person in people]
        assert beta['stop'] is None
        assert len(generation['alpha']) == 2 and len(generation['beta']) == 1
        await wait_for(lambda: not adapter._session_tasks, 'native task delivery cleanup')
        # Exercise the real installed correlated handler, through the native
        # adapter intake, with a mismatched retained owner. This is not an
        # external callback: that boundary is disabled above.
        wrong = MessageEvent(text='WRONG_OWNER_MUST_NOT_RUN',
            source=adapter.build_source(chat_id=beta['id'], chat_type='dm',
                user_id='unrelated-actor', message_id='wrong-owner'),
            message_id='wrong-owner', message_type=MessageType.TEXT, allow_gateway_control=False)
        await adapter.handle_message(wrong)
        wrong_task = adapter._session_tasks.get(adapter._event_session_key(wrong))
        if wrong_task is not None:
            await asyncio.wait_for(asyncio.shield(wrong_task), 5)
        unchanged = adapter.handoffs.get(beta['id'])
        assert all(unchanged[key] == beta[key] for key in (
            'native_session_id', 'native_task_id', 'native_turn_id', 'response', 'stop'))
        assert len(generation['alpha']) == 2 and len(generation['beta']) == 1
        assert 'Native task participant does not match its owner' in (home/'logs'/'errors.log').read_text()
        assert all(handle.gateway != 'colony_task' for handle in await contacts.get_handles(owner))
        print(json.dumps({'cross_channel_native_tasks': True, 'separate_native_roots': 2,
            'foreground_completed_while_tasks_held': True, 'steering_in_actual_sdk_request': True,
            'matching_native_stop_terminal': True, 'late_reply_suppressed': True,
            'native_recollection_uses_canonical_owner': True,
            'external_task_callback_disabled': True, 'wrong_owner_native_entry_rejected': True,
            'unrelated_task_completed': True, 'physical_channels_exercised': False,
            'model_quality_measured': False}))
    finally:
        tearing_down = True
        for gate in release.values():
            gate.set()
        await adapter.disconnect()
        runner._running = False
        for value in runner._agent_cache.values():
            value[0].close()
        if runner._executor:
            runner._executor.shutdown(wait=True, cancel_futures=True)


try:
    asyncio.run(exercise())
finally:
    api.__exit__(None, None, None)
    asyncio.run(contacts.close())

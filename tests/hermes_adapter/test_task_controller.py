"""Mixed adapter hooks must not adopt another transport's active handoff."""
import importlib.util
import os

import pytest

from conftest import run_python


PROBE = r'''
import json, socket, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
if sys.argv[3]: sys.path.append(sys.argv[3])
def no_network(*a, **kw): raise AssertionError('Hook ownership has no network dependency')
socket.socket.connect = no_network
socket.create_connection = no_network
from gateway.config import PlatformConfig
from colony_hermes.client import TurnOutbox
from colony_hermes.task_controller import NativeTasks
from colony_hermes.task_handoffs import TaskHandoffs, TaskHandoffError
from colony_hermes.native_task_platform import ACTIVE, NativeTaskAdapter, bind_native_turn, finish_native_turn

outbox = TurnOutbox(Path('outbox.sqlite3').absolute())
outbox.prepare()
controller = NativeTasks(None, outbox, 'owner', state_path=Path('tasks.sqlite3').absolute())
source = {'version':1, 'principal':'hermes:cli', 'source_session_id':'ordinary',
    'input_refs':[{'source_id':'instruction','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'instruction','source_version':'b'*64}],
    'contact_id':'owner', 'watermark':0,
    'origin':{'platform':'cli','authority_gateway':'cli','sender_id':'local',
              'session_id':'ordinary','turn_id':'original-turn'}}
controller.handoffs = TaskHandoffs(controller.database, lambda value, dependencies=None: dict(value),
    lambda value, require_task_grant: 'owner')
row = controller.handoffs.admit(request_id='original', request='Inspect local notes', source_input=source)
config = PlatformConfig(enabled=True)
# Built-in platform keeps this focused check independent of plugin discovery.
# Two actual adapter instances deliberately share a platform and store, so a
# platform-string-only ownership check would still adopt the wrong callback.
owned = NativeTaskAdapter(config, handoffs=controller.handoffs, platform_name='api_server')
foreign = NativeTaskAdapter(config, handoffs=controller.handoffs, platform_name='api_server')
controller.adapter = owned
native = {'platform':'api_server','sender_id':'owner','session_id':'native',
          'task_id':'task','turn_id':'turn'}
active = {'adapter':foreign, 'handoffs':controller.handoffs, 'id':row['id'], 'supplied':None}
token = ACTIVE.set(active)
try:
    assert controller.bind_native_turn(**native) is None
    assert controller.finish_native_turn(**native, interrupted=True) is None
    assert controller.handoffs.get(row['id'])['native_session_id'] is None
    assert controller.handoffs.get(row['id'])['terminal'] is None
    # The foreign transport can still call the generic exports itself.
    assert bind_native_turn(**native)['context'] == foreign.delivery_context
    assert controller.finish_native_turn(**native, interrupted=True) is None
    assert controller.handoffs.get(row['id'])['terminal'] is None
    finish_native_turn(**native, interrupted=True)
    assert controller.handoffs.get(row['id'])['terminal']['turn_id'] == 'turn'
finally: ACTIVE.reset(token)
successor = {**native, 'turn_id':'successor'}
active = {'adapter':owned, 'handoffs':controller.handoffs, 'id':row['id'], 'supplied':None}
token = ACTIVE.set(active)
try:
    assert controller.bind_native_turn(**successor)['context'] == owned.delivery_context
    assert controller.handoffs.get(row['id'])['terminal'] is None
    fields = controller.native_scope_fields(**successor)
    assert fields == {'sender_id':'local','contact_id':'owner','authority_lane':'system',
                      'resolution_status':'attested_system','authority_gateway':'cli'}
    for mismatch in ({'sender_id':'stranger'}, {'turn_id':'prior'}, {'parent_session_id':'native'}):
        try: controller.native_scope_fields(**{**successor, **mismatch})
        except TaskHandoffError: pass
        else: raise AssertionError('A different native origin acquired this task source')
    controller.finish_native_turn(**successor, interrupted=True)
    assert controller.handoffs.get(row['id'])['terminal']['turn_id'] == 'successor'
finally: ACTIVE.reset(token)
assert owned.authorization_is_upstream is False and foreign.authorization_is_upstream is False
assert owned.verify_http_event_request('Bearer anything')[0] is False
print(json.dumps({'mixed_adapter_hooks':True}))
'''


def test_native_controller_hooks_preserve_transport_and_generation(artifacts, tmp_path):
    native = os.environ.get('COLONY_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native adapter integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    result = run_python('-I', '-c', PROBE, artifacts[3], native,
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"mixed_adapter_hooks": true' in result.stdout

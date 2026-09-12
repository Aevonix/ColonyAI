"""An injected existing store preserves native origins and retained results."""
import importlib.util
import os

import pytest

from conftest import run_python


PROBE = r'''
import asyncio, json, socket, sqlite3, sys, types
from contextlib import contextmanager
from pathlib import Path
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
if sys.argv[3]: sys.path.append(sys.argv[3])
def no_network(*a, **kw): raise AssertionError('Retained routing has no network dependency')
socket.socket.connect = no_network
socket.create_connection = no_network
from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore
from apsimo_hermes.client import TurnOutbox
from apsimo_hermes.task_controller import NativeTasks, configured_tasks
from apsimo_hermes.task_handoffs import TaskHandoffs, TaskHandoffError
from apsimo_hermes.native_task_platform import NativeTaskAdapter

class DeploymentError(ValueError): pass
@contextmanager
def database():
    db = sqlite3.connect('existing-ingress.sqlite3')
    db.row_factory = sqlite3.Row
    try:
        with db: yield db
    finally: db.close()

source = {'version':1, 'principal':'enrolled-transport', 'source_session_id':'speech-session',
    'input_refs':[{'source_id':'speech-input','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'speech-input','source_version':'b'*64}],
    'contact_id':'owner', 'watermark':0}
class Sources:
    def resolve_source(self, value, dependencies=None): return dict(value)
    def resolve_owner(self, value, *, require_task_grant):
        assert value['contact_id'] == 'owner'
        return 'owner'
sources = Sources()
old = TaskHandoffs(database, sources.resolve_source, sources.resolve_owner,
    error_type=DeploymentError, reply_effect='retained_for_speech_transport')
rows = {name: old.admit(request_id=name, request='Review ' + name, source_input=source)
        for name in ('bound', 'early', 'done', 'cancelled', 'missing')}
outbox = TurnOutbox(Path('outbox.sqlite3').absolute())
outbox.prepare()
controller = NativeTasks(None, outbox, 'owner', database=database, sources=sources,
    error_type=DeploymentError, reply_effect='retained_for_speech_transport')
assert controller.path is None and controller.sources is sources
assert controller.handoffs.count() == old.count() == 5
assert not Path('colony-native-tasks.sqlite3').exists()
assert controller.handoffs.admit(request_id='bound', request='Review bound', source_input=source)['id'] == rows['bound']['id']
try: controller.handoffs.get('absent')
except DeploymentError: pass
else: raise AssertionError('The deployment error contract was replaced')
try: NativeTasks(None, outbox, 'owner', database=database, state_path='second.sqlite3')
except ValueError: pass
else: raise AssertionError('A second store was silently selected')

factory = types.ModuleType('fixture_task_factory')
factory_calls = []
def build(client, selected_outbox, owner, *, config):
    factory_calls.append((client, selected_outbox, owner, config))
    return controller
factory.build = build
sys.modules[factory.__name__] = factory
config = {'factory':'fixture_task_factory:build', 'ingress_config':'private-selection.json'}
assert configured_tasks(None, outbox, 'owner', config=config) is controller
assert factory_calls == [(None, outbox, 'owner', config)]
factory.build = lambda *args, **kwargs: object()
try: configured_tasks(None, outbox, 'owner', config=config)
except TypeError: pass
else: raise AssertionError('An invalid factory fell back to another task registry')

store = SessionStore(Path('native-sessions'), GatewayConfig())
routes = []
class RecordingAdapter(NativeTaskAdapter):
    async def dispatch_native_event(self, payload):
        routes.append((self.platform.value, payload['handoff_id']))
        return await super().dispatch_native_event(payload)
primary = RecordingAdapter(PlatformConfig(enabled=True), handoffs=controller.handoffs,
    platform_name='api_server', error_type=DeploymentError)
legacy = RecordingAdapter(PlatformConfig(enabled=True), handoffs=old,
    platform_name='telegram', error_type=DeploymentError)
for adapter in (primary, legacy): adapter.set_session_store(store)
controller.adapter = primary
entries = {}
for name in ('bound', 'early', 'done', 'cancelled'):
    origin = legacy.build_source(chat_id=rows[name]['id'], chat_type='dm', user_id='owner', message_id=rows[name]['id'])
    entries[name] = store.get_or_create_session(origin, touch_activity=False)
    if name != 'early':
        old.bind(rows[name]['id'], {'session_id':entries[name].session_id, 'task_id':name, 'turn_id':name + '-turn'})
old.bind(rows['missing']['id'], {'session_id':'missing-native-session', 'task_id':'missing', 'turn_id':'missing-turn'})
old.complete_source(rows['done']['id'], {**source, 'session_id':entries['done'].session_id})
response = controller.handoffs.retain_reply(rows['done']['id'], 'Retained result')
assert response['effect'] == 'retained_for_speech_transport'
old.request_stop(rows['cancelled']['id'])
old.observe_terminal(rows['cancelled']['id'], {'session_id':entries['cancelled'].session_id,
    'task_id':'cancelled', 'turn_id':'cancelled-turn', 'interrupted':True})

async def main():
    # No first-message hook or startup adapter lookup is assumed. Active old
    # work is unavailable until an exact resolver exists, never readmitted.
    for name in ('bound', 'early'):
        try: await controller.dispatch({'handoff_id':rows[name]['id'], 'action':'status'})
        except TaskHandoffError as error: assert 'original native task adapter' in str(error)
        else: raise AssertionError('Old native work was relabelled to the new adapter')
    assert routes == []
    for name, status in (('done', 'done'), ('cancelled', 'cancelled')):
        observed = await controller.dispatch({'handoff_id':rows[name]['id'], 'action':'status'})
        assert observed['status'] == status, observed
    assert routes == [('api_server', rows[name]['id']) for name in ('done', 'cancelled')]
    controller.adapter_resolver = lambda origin: legacy if origin.platform == legacy.platform else None
    for name in ('bound', 'early'):
        observed = await controller.dispatch({'handoff_id':rows[name]['id'], 'action':'status'})
        assert observed['native_session_id'] == entries[name].session_id, observed
        assert routes[-1] == ('telegram', rows[name]['id'])
    assert old.get(rows['early']['id'])['native_session_id'] is None
    assert len(store.list_sessions()) == 4
    try: await controller.dispatch({'handoff_id':rows['missing']['id'], 'action':'submit'})
    except TaskHandoffError as error: assert 'retained native task origin' in str(error)
    else: raise AssertionError('Missing retained work was started again')
    controller.adapter_resolver = None
    gateway = types.SimpleNamespace(_adapter_for_source=lambda origin: legacy)
    controller.observe_gateway(gateway=gateway, session_store=object())
    assert controller.gateway is None
    controller.observe_gateway(gateway=gateway, session_store=store)
    assert controller.gateway is gateway
    observed = await controller.dispatch({'handoff_id':rows['bound']['id'], 'action':'status'})
    assert observed['native_session_id'] == entries['bound'].session_id
    assert len(store.list_sessions()) == 4
asyncio.run(main())
print(json.dumps({'shared_store_and_original_routes':True}))
'''


def test_injected_store_and_original_native_routing(artifacts, tmp_path):
    native = os.environ.get('COLONY_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native adapter integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    result = run_python('-I', '-c', PROBE, artifacts[3], native,
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"shared_store_and_original_routes": true' in result.stdout

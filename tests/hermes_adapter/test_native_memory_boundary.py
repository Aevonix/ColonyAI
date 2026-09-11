"""Real native turn scopes cover retries and children without global policy."""
import importlib.util
import os

import pytest
from conftest import run_python


PROBE = r'''
import asyncio, copy, json, os, socket, sys
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
def no_network(*a,**kw): raise AssertionError('No network in native boundary qualification')
socket.socket.connect=no_network; socket.create_connection=no_network
from agent import relay_runtime, relay_llm
from colony_hermes.native_memory import NativeMemoryRequests
checks=[]; sends=[]
def memory(request,scope):
    checks.append(scope.session_id)
    result=copy.deepcopy(request)
    result['messages']=[{'role':'user','content':'filtered for '+scope.session_id}]
    return {'request':result}
boundary=NativeMemoryRequests(memory)
coordinator=relay_runtime.SESSION_COORDINATOR
def begin(session,parent=''):
    lease=coordinator.acquire_conversation(profile_key=relay_runtime.current_profile_key(),
        session_id=session,platform='subagent' if parent else 'cli',parent_session_id=parent)
    turn=coordinator.begin_turn(lease,turn_id=session+'-turn',task_id=session+'-task')
    scope=NS(session_id=session,task_id=turn.task_id,turn_id=turn.turn_id)
    return lease,turn,scope
def send(scope,body,request_id):
    def provider(request):
        sends.append(copy.deepcopy(request))
        return {'model':'fixture/model','choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]}
    return relay_llm.execute(body,provider,session_id=scope.session_id,name='fixture',model_name='fixture/model',
        metadata={'api_mode':'chat_completions','api_request_id':request_id,'call_role':'iteration_summary'})
def end(lease,turn):
    coordinator.end_turn(turn,outcome='success')
    coordinator.release_conversation(lease)
    asyncio.run(lease.host.relay.subscribers.flush_async())
body={'model':'fixture/model','messages':[{'role':'user','content':'retained neutral history'}]}
lease,turn,scope=begin('parent')
try:
    assert boundary.bind(scope)
    boundary.checked(body,scope)
    send(scope,body,'normal')
    assert checks==[] and sends[-1]['messages']==body['messages']
    # The one-use ordinary check cannot certify a later physical retry.
    send(scope,body,'retry')
    assert checks==['parent'] and sends[-1]['messages'][0]['content']=='filtered for parent'
    child_lease,child_turn,child_scope=begin('child','parent')
    try:
        assert boundary.bind(child_scope)
        send(child_scope,body,'child-summary')
        assert checks==['parent','child'],checks
        assert sends[-1]['messages'][0]['content']=='filtered for child'
    finally:
        end(child_lease,child_turn)
    send(scope,body,'parent-summary')
    assert checks==['parent','child','parent'],checks
    # Actual native streaming dispatch uses the same narrow provider rewrite.
    def stream_factory(request):
        sends.append(copy.deepcopy(request))
        return iter([{'choices':[{'delta':{'content':'OK'},'finish_reason':'stop'}]}])
    chunks=list(relay_llm.stream(body,stream_factory,session_id=scope.session_id,
        name='fixture',model_name='fixture/model',
        metadata={'api_mode':'chat_completions','api_request_id':'stream'},
        finalizer=lambda:{'model':'fixture/model','choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]}))
    assert chunks and sends[-1]['messages'][0]['content']=='filtered for parent'
    # Plugin post_llm_call cleanup removes callbacks even while another native
    # consumer keeps Relay active until the turn actually closes.
    lease.host.retain_managed_execution('neutral-other-consumer')
    try:
        boundary.finish(scope.session_id,scope.task_id,scope.turn_id)
        before=len(checks)
        send(scope,body,'after-post-hook')
        assert len(checks)==before and sends[-1]['messages']==body['messages']
        assert boundary.bind(scope)
    finally:
        lease.host.release_managed_execution('neutral-other-consumer')
finally:
    # No plugin post hook here: native scope-end cleanup must own this exit.
    end(lease,turn)
assert not boundary._turns,boundary._turns
assert not lease.host.managed_execution_enabled()
new_lease,new_turn,new_scope=begin('parent')
try:
    before=len(checks)
    send(new_scope,body,'unbound-next-turn')
    assert len(checks)==before and sends[-1]['messages']==body['messages']
finally:
    end(new_lease,new_turn)
    relay_runtime.HOST_REGISTRY.shutdown_all()
print(json.dumps({'ordinary_unchanged':True,'retry_rechecked':True,'joined_child_scope':True,
    'stream_filtered':True,'native_scope_end_cleanup':True,'next_turn_unbound':True}))
'''


def test_native_memory_boundary_scope_and_lifetime(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native qualification')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / 'profile'), HERMES_DISABLE_TELEMETRY='1',
               HERMES_DISABLE_LAZY_INSTALLS='1')
    run_python('-I', '-c', PROBE, artifacts[3], cwd=tmp_path, env=env)


@pytest.mark.parametrize('missing', ['scope_local', 'register_llm_execution',
                                    'register_llm_stream_execution', 'register_subscriber'])
def test_missing_relay_capability_leaves_normal_adapter_available(monkeypatch, missing, caplog):
    from pathlib import Path
    from types import SimpleNamespace as NS
    import sys
    path = Path(__file__).resolve().parents[2] / 'plugins/hermes-plugin/native_memory.py'
    spec = importlib.util.spec_from_file_location('native_memory_capability_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scoped = NS(**{name: lambda *a: None for name in (
        'register_llm_execution', 'register_llm_stream_execution', 'register_subscriber',
        'deregister_llm_execution', 'deregister_llm_stream_execution', 'deregister_subscriber')})
    relay = NS(scope_local=scoped)
    delattr(relay if missing == 'scope_local' else scoped, missing)
    host = NS(relay=relay)
    turn = NS(handle=object(), lease=NS(live_runtime=lambda: host))
    monkeypatch.setitem(sys.modules, 'agent', NS(relay_runtime=NS(active_turn=lambda _: turn)))
    boundary = module.NativeMemoryRequests(lambda *_: pytest.fail('No memory call during binding'))
    assert boundary.bind(NS(session_id='neutral')) is False
    assert boundary._turns == {}
    assert 'Hermes 0.21.1 and NeMo Relay 0.8.3' in caplog.text

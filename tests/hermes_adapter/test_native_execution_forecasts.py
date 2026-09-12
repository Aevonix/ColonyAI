"""Qualify payloads emitted by selected Hermes helpers, without model traffic."""
import os
import importlib.util
from pathlib import Path
import subprocess
import pytest


@pytest.mark.parametrize('payload,expected', [
    (None, {'output_limit_kind': 'unknown'}),
    ({'_truncated': True, 'preview': 'private'}, {'output_limit_kind': 'unknown'}),
    ({'body': {'_truncated_items': 2}}, {'output_limit_kind': 'unknown'}),
    ({'body': {'extra_body': []}}, {'output_limit_kind': 'unknown'}),
    ({'body': {'max_tokens': None}}, {'output_limit_kind': 'unknown'}),
    ({'body': {'max_tokens': True}}, {'output_limit_kind': 'unknown'}),
    ({'body': {'max_tokens': 512, 'extra_body': {'max_tokens': 256}}}, {'output_limit_kind': 'unknown'}),
    ({'body': {'extra_body': {'max_output_tokens': 256}}}, {'output_limit_kind': 'request', 'max_tokens': 256}),
    ({'body': {'messages': [{'content': 'private'}]}}, {'output_limit_kind': 'provider_default'}),
])
def test_output_policy_requires_observed_final_fields(payload, expected):
    path = Path(__file__).resolve().parents[2] / 'plugins/hermes-plugin/executions.py'
    spec = importlib.util.spec_from_file_location('output_limit_observer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A configured agent cap cannot repair missing actual request metadata.
    actual = module.ExecutionObserver.output_limit_metadata({'request': payload, 'max_tokens': 8192,
                                                             'api_mode': 'chat_completions'})
    assert actual == expected
    assert 'private' not in str(actual)
    assert module.ExecutionObserver.output_limit_metadata({'request': payload, 'max_tokens': 8192,
        'api_mode': 'unknown_transport'}) == {'output_limit_kind': 'unknown'}

PROBE = r'''
import json,os,socket,sys,types,time,ast,inspect,logging
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
package=types.ModuleType('pacomind_hermes');package.__path__=[sys.argv[2]];sys.modules['pacomind_hermes']=package
def no_network(*a,**kw): raise AssertionError('No network in execution qualification')
socket.socket.connect=no_network
from hermes_cli import lifecycle
from hermes_cli import plugins
from hermes_cli.middleware import apply_llm_request_middleware
from agent.turn_context import _collect_pre_llm_call_context
from agent.turn_api_request import _fire_pre_api_request_hook
from agent.turn_response_intake import _fire_post_api_request_hook
from agent.api_request_hooks import ApiRequestHooksMixin
from agent import turn_finalizer
from pacomind_hermes.executions import ExecutionObserver
from pacomind.api.routers.executions import ExecutionObservation
from pacomind.api.routers import host
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.executions import ExecutionRegistry
from pacomind.self_model.expectations import ExpectationStore,ExpectationEngine
from pacomind.self_model import execution_forecasts as forecasts
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import executions
state=Path(os.environ['PACOMIND_STATE_DIR']);state.mkdir()
profile=Path(os.environ['HERMES_HOME']);profile.mkdir(exist_ok=True);(profile/'config.yaml').write_text('plugins: {enabled: []}\n')
registry=ExecutionRegistry(TurnIdempotencyLedger(state/'turns.db'))
host._expectations=ExpectationEngine(ExpectationStore(str(state/'expectations.db')))
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.pacomind_authority=RequestAuthority(principal_id='fixture-host',credential_id='fixture',
  scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),
  audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(executions.router)
http=TestClient(app);payloads=[]
class Client:
 def post(self,url,*,json,**kwargs):
  payloads.append(json)
  with patch.object(executions,'registry',return_value=registry):
   response=http.post(url,json=json)
  assert response.status_code==200,response.text
  return response
observer=ExecutionObserver(Client());hooks={}
observer.register(types.SimpleNamespace(register_hook=lambda n,f:hooks.update({n:f})))
scope=types.SimpleNamespace(valid_participant=True,contact_id='owner',platform='cli')
hooks['pre_llm_call']=lambda **kw:observer.start(scope,**kw)
emitted=[]
def dispatch(name,**kw):
 emitted.append((name,kw))
 fn=hooks.get(name)
 return [fn(**kw)] if fn else []
class Agent(ApiRequestHooksMixin): pass
agent=Agent();agent.session_id='session-a';agent.platform='cli';agent.model='model-a';agent.provider='local'
agent.base_url='http://private-model.invalid';agent.api_mode='chat_completions';agent.tools=[];agent.max_tokens=None
agent.client=None
case=sys.argv[3]
expected_kind=('unknown' if case in {'truncated','large_later_rewrite','large_wrong_request'}
 else 'request' if case in {'request','large_request'} else 'provider_default')
if case=='truncated':
 agent._api_request_payload_for_hook=lambda kwargs:{'_truncated':True,'preview':'private truncated request'}
manager=plugins.PluginManager()
def capture_request(request,**kwargs):
 return observer.request_metadata({'request':request,'source':'pacomind'},**kwargs)
manager._middleware['llm_request']=[capture_request]
if case=='large_later_rewrite':
 manager._middleware['llm_request'].append(lambda request,**kw:
  {'request':{**request,'max_tokens':512},'name':'later-plugin'})
with patch.object(lifecycle,'has_hook',side_effect=lambda n:n in hooks),patch.object(lifecycle,'invoke_hook',side_effect=dispatch):
 _collect_pre_llm_call_context(agent,effective_task_id='task-a',turn_id='turn-a',
  original_user_message='private owner request',messages=[],conversation_history=[])
 assert len(payloads)==1,payloads
 started=time.time()
 def pre(request_id,count,retry):
  body={'messages':[{'role':'user','content':'private source text'}]}
  trace=[]
  if case in {'request','large_request'}: body['max_completion_tokens']=192
  if case.startswith('large_'):
   body['messages']=[{'role':'user','content':'private source text'*500} for _ in range(100)]
   body['tools']=[{'type':'function','function':{'name':'tool'+str(i),'description':'private tool'*500}}
    for i in range(100)]
   with patch.object(plugins,'_delivery_manager',return_value=manager):
    applied=apply_llm_request_middleware(body,api_mode=agent.api_mode,
     api_request_id=request_id+('-stale' if case=='large_wrong_request' else ''))
   body,trace=applied.payload,applied.trace
   assert agent._api_request_payload_for_hook(body).get('_truncated') is True
   assert len(trace)==(2 if case=='large_later_rewrite' else 1)
  _fire_pre_api_request_hook(agent,body,[],trace,
   messages=[],original_user_message='private owner request',
   approx_tokens=20000 if case=='context_growth' and count==2 else 8,
   total_chars=30,retry_count=retry,
   api_call_count=count,api_request_id=request_id,api_start_time=started,effective_task_id='task-a',turn_id='turn-a')
 pre('request-failed',1,0)
 assert len(payloads)==2,emitted
 eid=payloads[0]['execution_id'];hist=host._expectations.store.forecast_history(forecasts._fid(eid))
 assert len(hist['forecasts'])==1,hist
 assert hist['forecasts'][0]['created_at']>=started
 config=hist['forecasts'][0]['detail']['model_provenance']['capabilities']
 assert config['output_limit_kind']==expected_kind,config
 assert config['max_tokens']==(192 if expected_kind=='request' else None),config
 agent._invoke_api_request_error_hook(task_id='task-a',turn_id='turn-a',api_request_id='request-failed',
  api_call_count=1,api_start_time=started,api_kwargs={'messages':[{'content':'private input'}]},
  error_type='TimeoutError',error_message='private error detail',retry_count=0,retryable=True)
 if case=='tool_discovery': agent.tools=['discovered-tool']
 if case=='route_change': agent.model='model-b'
 pre('request-retry',2,1)
 _fire_post_api_request_hook(agent,types.SimpleNamespace(model='model-a',usage=None),
  types.SimpleNamespace(content='private answer',tool_calls=[]),'stop',api_messages=[],api_call_count=2,
  api_duration=time.time()-started,api_start_time=started,api_request_id='request-retry',effective_task_id='task-a',turn_id='turn-a')
 assert len(payloads)==5,emitted
 # Execute the exact terminal callback expression from selected Hermes. The
 # complete finalizer also saves turns/skills; those are outside this test.
 tree=ast.parse(inspect.getsource(turn_finalizer.finalize_turn))
 calls=[node for node in ast.walk(tree) if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
  and isinstance(node.value.func,ast.Name) and node.value.func.id=='_invoke_hook_safely'
  and node.value.args and isinstance(node.value.args[0],ast.Constant) and node.value.args[0].value=='on_session_end']
 assert len(calls)==1
 code=compile(ast.fix_missing_locations(ast.Module(body=calls,type_ignores=[])),'selected-native-terminal-hook','exec')
 exec(code,{'_invoke_hook_safely':turn_finalizer._invoke_hook_safely,'logger':logging.getLogger('qualification'),
  'agent':agent,'effective_task_id':'task-a','turn_id':'turn-a','completed':True,'failed':False,
  'interrupted':False,'_turn_exit_reason':'text_response(stop)','_platform':'cli'})
 assert len(payloads)==6,emitted
hist=host._expectations.store.forecast_history(forecasts._fid(eid))
assert len(hist['outcomes'])==1 and hist['outcomes'][0]['status']==('censored' if expected_kind=='unknown' or case=='route_change' else 'observed'),hist
facts=forecasts._facts(registry.ledger,hist['outcomes'][0])
assert facts['processor']['served_model']=='model-a' and facts['processor']['error_request_count']==1,facts
assert facts['processor']['complete_observed_pairs'],facts
assert facts['version']==forecasts.VERSION
if case=='context_growth':
 assert facts['processor']['workload_evolution']['input_buckets']==['up_to_4k','16k_to_64k'],facts
 assert facts['conditions_comparable'],facts
if case=='tool_discovery':
 assert facts['processor']['workload_evolution']['tool_counts']==[0,1],facts
 assert facts['conditions_comparable'],facts
if case=='route_change': assert not facts['conditions_comparable'],facts
text=json.dumps(payloads)
for forbidden in ('private owner request','private source text','private-model.invalid','private answer','private error detail','private truncated request'):
 assert forbidden not in text,text
assert len(payloads)==6 and all('runtime' not in payload for payload in (payloads[0],payloads[-1]))
assert payloads[2]['runtime']['event']=='error'
print(json.dumps({'actual_native_callbacks':True,'prospective':True,'retry_recorded':True,
 'terminal_observed':True,'private_content_absent':True,'models':0,'network':0}))
'''

@pytest.mark.parametrize('case', ['request', 'provider_default', 'truncated',
                                'large_request', 'large_provider_default', 'large_later_rewrite', 'large_wrong_request',
                                'context_growth', 'tool_discovery', 'route_change'])
def test_actual_hermes_execution_callbacks(tmp_path, case):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    root = Path(__file__).resolve().parents[2]
    env = {k:os.environ[k] for k in ('PATH','HOME','LANG') if k in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),PACOMIND_STATE_DIR=str(tmp_path/'state'),
        PACOMIND_OWNER_CONTACT_ID='owner',PACOMIND_EXPECTATIONS='on',PACOMIND_SKIP_DOTENV='1',
        PYTHONDONTWRITEBYTECODE='1',PYTHON_DOTENV_DISABLED='1',HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),str(root/'plugins/hermes-plugin'),case],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"actual_native_callbacks": true' in result.stdout

"""Native automatic recall for admitted host input, without a gateway sender.

Uses the installed adapter, native prologue, delegated tool loop, and actual
sidecar routes. Only inference is controlled. No external endpoint is used.
"""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json, os, socket, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host, executions
from colony_sidecar.turns import get_turn_idempotency_ledger
from colony_hermes.client import source_message_hash
# This fixture qualifies source admission and native recall, not the latency
# of cold in-process ASGI/SQLite work on a shared CI runner. Keep only the
# adapter's local deadline clocks deterministic; native scheduling, wall
# timestamps, and the dedicated deadline tests retain their real clocks.
import time
from types import SimpleNamespace
import colony_hermes.client as client_module
import colony_hermes.request_memory as request_memory_module
import colony_hermes.request_work as request_work_module
clock=SimpleNamespace(monotonic=lambda:1000.0,time=time.time,sleep=time.sleep)
client_module.time=request_memory_module.time=request_work_module.time=clock
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
secret='neutral-native-recall-fixture-key'
keyring=home/'keyring.json'
keyring.write_text(json.dumps({'version':1,'principals':[{
 'principal':'native-fixture','status':'active','viewer_person_id':'owner','person_ids':['owner'],
 'scopes':['context:read','memory:read','turns:write'],'audiences':['viewer'],
 'credentials':[{'id':'one','secret':secret,'status':'active'}]}]}));keyring.chmod(0o600)
(home/'config.yaml').write_text(json.dumps({
 'model':{'provider':'custom','default':'fixture-model','base_url':'http://model.fixture/v1'},
 'auxiliary':{'title_generation':{'enabled':False}},
 'agent':{'max_turns':5},'toolsets':['colony','delegation'],
 'memory':{'provider':'colony-memory','config':{'contact_id':'owner','url':'http://fixture','api_key':secret}},
 'plugins':{'enabled':['colony'],'colony':{'owner_contact_id':'owner','url':'http://fixture',
  'api_key':secret,'turn_outbox_path':str(home/'outbox.db'),'execution_registry_enabled':True}}}))
app=FastAPI();app.add_middleware(ApiKeyMiddleware,keyring_path=str(keyring))
app.include_router(host.router);app.include_router(host.v2_router);app.include_router(executions.router)
api=TestClient(app)
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
original='Recall the lamp maintenance record and delegate checking its required exception.'
record='The lamp maintenance record requires disconnecting external power before cleaning.'
automatic_record='The lamp maintenance record also requires the amber service key.'
ledger.record_source('original-input',contact_id='owner',session_id='voice-input',
 messages=[{'role':'user','content':original}],derive_claims=False)
ledger.record_source('maintenance-record',contact_id='owner',session_id='earlier',
 messages=[{'role':'user','content':record}],derive_claims=False)
ledger.record_source('automatic-record',contact_id='owner',session_id='another-earlier',
 messages=[{'role':'user','content':automatic_record}],derive_claims=False)
ref=ledger.source_references(['maintenance-record'],contact_id='owner',session_id='earlier')[0]
automatic_ref=ledger.source_references(['automatic-record'],contact_id='owner',session_id='earlier')[0]
parents=[{'source_id':'original-input','input_message_hash':source_message_hash(
 'voice-input',{'role':'user','content':original})}]
wire=[];generation=[];mode='supplied';scenario=sys.argv[4]
initial_failure_pending=scenario in ('initial_timeout','initial_remote_protocol')
def respond(request):
 global mode, initial_failure_pending
 if request.url.host=='fixture':
  if initial_failure_pending and request.url.path=='/v1/host/memory/sources/erasures':
   initial_failure_pending=False
   error=httpx.RemoteProtocolError if scenario=='initial_remote_protocol' else httpx.ReadTimeout
   raise error('Controlled initial freshness transport failure',request=request)
  response=api.request(request.method,request.url.path,params=request.url.params,
   headers=dict(request.headers),content=request.content)
  wire.append({'path':request.url.path,'status':response.status_code,
   'body':json.loads(request.content) if request.content else None})
  return httpx.Response(response.status_code,content=response.content,headers=response.headers)
 if request.url.host!='model.fixture':raise AssertionError(str(request.url))
 if request.method=='GET' and request.url.path=='/v1/models':
  return httpx.Response(200,json={'data':[{'id':'fixture-model','context_length':32768}]})
 if request.method=='POST' and request.url.path=='/api/show':
  # Hermes optionally probes Ollama metadata on custom endpoints.
  return httpx.Response(404,json={'error':'This fixture uses OpenAI-compatible metadata.'})
 assert request.method=='POST' and request.url.path=='/v1/chat/completions', (request.method,str(request.url))
 body=json.loads(request.content);generation.append(body)
 text=json.dumps(body['messages']);step=len(generation)
 if mode=='supplied':
  assert step<=4, 'Unexpected native generation request'
  from colony_hermes.input_provenance import current
  supplied=current();assert supplied is not None
  if step==1:
   current_user=next(row for row in reversed(body['messages']) if row['role']=='user')
   assert record in str(current_user['content']), (wire,body)
   assert automatic_record in str(current_user['content']), (wire,body)
   assert 'Relevant Memories' in str(current_user['content']),current_user
   assert 'maintenance-record' in str(current_user['content']),current_user
   assert len([row for row in wire if row['path']=='/v1/host/context/assemble'])==1
   work=next(row['content'] for row in body['messages'] if row.get('role') in ('system','developer')
    and isinstance(row.get('content'),str) and row['content'].startswith('[colony-work-request-v1]'))
   observed=[json.loads(line) for line in work.splitlines() if line.startswith('{')]
   root=next(row for row in observed if row.get('source')=='execution')
   assert root['request_input']['excerpt']==original,observed
   assert root['request_input']['source_id']=='original-input' and not root['request_input']['partial']
   message={'role':'assistant','content':None,'tool_calls':[{'id':'delegate-one','type':'function',
    'function':{'name':'delegate_task','arguments':json.dumps({'tasks':[{
     'goal':'Read the supplied lamp maintenance record and report the exception.',
     'context':'Exact source handle: '+json.dumps(ref),'toolsets':['colony']}]})}}]};finish='tool_calls'
   if scenario=='erase_during':
    ledger.erase_sources(contact_id='owner',turn_ids=['automatic-record'])
    mode='erased'
    message={'role':'assistant','content':None,'tool_calls':[{'id':'read-erased','type':'function',
     'function':{'name':'tool_call','arguments':json.dumps({
      'name':'colony_memory_read_source','arguments':ref})}}]};finish='tool_calls'
  elif step==2:
   assert ref['source_version'] in text,body
   # The real native child inherits the checked source scope across its worker
   # threads. Hermes deliberately creates delegate_task children skip_memory=True;
   # qualify scoped source access, not a child prefetch that never happens.
   child_sessions=[sid for sid in supplied._sessions if sid!=supplied.session_id]
   assert len(child_sessions)==1,child_sessions
   assert supplied.memory_contact(child_sessions[0])=='owner'
   assert supplied.memory_contact('unrelated-child')==''
   from colony_sidecar.turns.executions import registry
   observed=registry().view(contact_id='owner',owner=True,session_id=child_sessions[0])['items']
   child=next(row for row in observed if row['session_id']==child_sessions[0])
   parent_row=next(row for row in observed if row['execution_id']==child['parent_execution_id'])
   assert child['request_input']=={'status':'unbound'},child
   assert parent_row['request_input']['excerpt']==original,parent_row
   message={'role':'assistant','content':None,'tool_calls':[{'id':'read-one','type':'function',
    'function':{'name':'tool_call','arguments':json.dumps({
     'name':'colony_memory_read_source','arguments':ref})}}]};finish='tool_calls'
  else:
   tool_rows=[row for row in body['messages'] if row['role']=='tool']
   assert len(tool_rows)==1,tool_rows
   if step==3:
    assert record in tool_rows[0]['content'],tool_rows
    if scenario=='rotate':
     # Simulate the compressor's session-ID change between native requests.
     # The next request uses the actual middleware/hook and source writer;
     # no supplied-input binding is manufactured by the fixture.
     parent.session_id += '-compressed'
   if step==4:
    result=json.loads(tool_rows[0]['content'])
    assert 'SYNCHRONOUSLY' in result['note'] and result['results'][0]['status']=='completed',result
   message={'role':'assistant','content':'Disconnect external power before cleaning the lamp.'};finish='stop'
 elif mode=='initial_timeout':
  assert 'Source verification is temporarily unavailable' in text,body
  assert record not in text and original not in text and body.get('tools',[])==[]
  message={'role':'assistant','content':'Source verification is temporarily unavailable.'};finish='stop'
 elif mode=='unbound':
  assert record not in text and automatic_record not in text and '[colony-recall-v1 ' not in text,body
  message={'role':'assistant','content':'No recalled record is available.'};finish='stop'
 else:
  assert record not in text and automatic_record not in text and original not in text,body
  assert 'source input for this task is no longer available' in text,body
  assert body.get('tools',[])==[],body
  message={'role':'assistant','content':'The source is unavailable.'};finish='stop'
 if body.get('stream'):
  delta={**message}
  if delta.get('tool_calls'):
   delta['tool_calls']=[{**call,'index':index} for index,call in enumerate(delta['tool_calls'])]
  chunk={'id':'fixture','object':'chat.completion.chunk','created':1,'model':'fixture-model',
   'choices':[{'index':0,'delta':delta,'finish_reason':None}]}
  end={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':finish}]}
  return httpx.Response(200,headers={'content-type':'text/event-stream'},content=(
   'data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(end)+'\n\ndata: [DONE]\n\n').encode())
 return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':1,
  'model':'fixture-model','choices':[{'index':0,'message':message,'finish_reason':finish}],
  'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
def controlled_transport(self, request):
 try:
  return respond(request)
 except Exception:
  # The SDK wraps fixture assertion failures as APIConnectionError and retries.
  # Preserve the first cause before that retry obscures the native boundary.
  import traceback
  traceback.print_exc()
  raise
httpx.HTTPTransport.handle_request=controlled_transport
def no_network(*args,**kwargs):raise AssertionError('No external network in native recall qualification')
socket.socket.connect=no_network;socket.create_connection=no_network
from gateway.session_context import declare_stateless_channel, get_session_env
declare_stateless_channel()
assert all(not get_session_env(key,'') for key in (
 'HERMES_SESSION_PLATFORM','HERMES_SESSION_USER_ID','HERMES_SESSION_CHAT_ID'))
assert 'COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY' not in os.environ
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from run_agent import AIAgent
from colony_hermes.input_provenance import supplied_input
from colony_hermes import TurnOutbox
def agent():
 value=AIAgent(api_key='fixture',base_url='http://model.fixture/v1',provider='custom',
  model='fixture-model',quiet_mode=True,skip_context_files=True,skip_memory=False,
  platform='cli',max_iterations=5,enabled_toolsets=['colony','delegation'])
 value.save_trajectories=False
 return value
if scenario in ('initial_timeout','initial_remote_protocol'):
 mode='initial_timeout';blocked=agent()
 with supplied_input(contact_id='owner',session_id=blocked.session_id,input_refs=parents,source_refs=[ref]) as supplied:
  blocked.run_conversation('Perform the admitted lamp maintenance task.',persist_user_message=original)
  assert supplied.failure=={'reason':'source_freshness_unavailable','admitted':False,'retryable':True}
  assert supplied.result is None and supplied.memory_contact(blocked.session_id)==''
 assert len(generation)==1 and not [row for row in wire if row['path']=='/v1/host/context/assemble']
 assert TurnOutbox(home/'outbox.db').snapshot()==[]
 blocked.close();mode='supplied';generation.clear();wire.clear()
 # A fresh task must rebuild native admission, inherited context and automatic
 # recall. The failed scope itself remains unusable and is never reopened.
parent=agent();initial_session=parent.session_id
with supplied_input(contact_id='owner',session_id=parent.session_id,input_refs=parents,source_refs=[ref]) as supplied:
 with patch.object(parent._memory_manager,'prefetch_all',wraps=parent._memory_manager.prefetch_all) as automatic:
  result=parent.run_conversation('Perform the admitted lamp maintenance task.',persist_user_message=original)
 automatic.assert_called_once_with(original,session_id=initial_session)
 if scenario=='erase_during':
  assert len(generation)==2 and result['final_response']=='The source is unavailable.',result
  assert supplied.result is None and supplied.memory_contact(parent.session_id)==''
  assert not [row for row in wire if row['path']=='/v1/host/memory/read']
  parent.close()
  print(json.dumps({'automatic_only_source_erased_mid_native_parent':True,
   'next_model_request_recalled_bytes_absent':True,'source_dependent_tool_not_run':True,
   'inference_transport':'controlled','external_requests':0}))
  raise SystemExit(0)
 assert result['final_response']=='Disconnect external power before cleaning the lamp.',result
 assert supplied.result['input_refs']==parents and ref in supplied.result['source_refs'],supplied.result
 assert automatic_ref in supplied.result['source_refs'],supplied.result
 assert ledger.source_references(['original-input'],contact_id='owner',session_id=parent.session_id)[0] in supplied.result['source_refs']
 assert supplied.result['session_id']==parent.session_id
 if scenario=='rotate':assert parent.session_id!=initial_session
 assert len(generation)==4
parent.close()
assemblies=[row for row in wire if row['path']=='/v1/host/context/assemble']
# Native queues a next-turn prefetch at completion. It may run before this
# finite supplied context closes; it is not another consumed prompt packet.
assert 1<=len(assemblies)<=2 and all(row['status']==200 for row in assemblies),assemblies
assert all(row['body']['context'].get('session_id') in (initial_session,parent.session_id)
 and row['body']['context'].get('contact_id')=='owner' for row in assemblies)
assembly_count=len(assemblies)
rows=TurnOutbox(home/'outbox.db').snapshot()
assert len(rows)==2 and all(row['state']=='delivered' for row in rows),rows
assert all(row['payload']['assistant_input_refs']==parents for row in rows),rows
mode='unbound';plain=agent()
plain.run_conversation('What does the lamp maintenance record require?')
plain.close()
assert len([row for row in wire if row['path']=='/v1/host/context/assemble'])==assembly_count
ledger.erase_sources(contact_id='owner',turn_ids=['maintenance-record'])
mode='erased';erased=agent()
with supplied_input(contact_id='owner',session_id=erased.session_id,input_refs=parents,source_refs=[ref]) as supplied:
 erased.run_conversation('Perform the admitted lamp maintenance task.',persist_user_message=original)
 assert supplied.memory_contact(erased.session_id)=='' and supplied.result is None
erased.close()
assert len([row for row in wire if row['path']=='/v1/host/context/assemble'])==assembly_count
assert len(generation)==6
print(json.dumps({'native_parent_automatic_recall':True,'no_gateway_sender':True,
 'no_default_owner_fallback':True,'native_delegated_source_access':True,
 'native_child_prefetch_enabled':False,'unbound_cli_no_recall':True,
 'erased_input_no_recall':True,'inference_transport':'controlled','external_requests':0}))
'''


@pytest.mark.parametrize('scenario', ['normal', 'erase_during', 'rotate', 'initial_timeout', 'initial_remote_protocol'])
def test_supplied_native_input_reaches_automatic_recall_and_delegated_source_reader(artifacts, tmp_path, scenario):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native request qualification')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        COLONY_OWNER_CONTACT_ID='owner', HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1', COLONY_MEMORY_WORKER_TOOLS='0',
        COLONY_MEMORY_TURN_WRITER='disabled', COLONY_GUARD_CHAT_MODE='off',
        COLONY_RECALL_RERANK='off', COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        OPENAI_API_KEY='fixture', OPENAI_BASE_URL='http://model.fixture/v1',
        LITELLM_LOCAL_MODEL_COST_MAP='True')
    run_python('-I','-c',PROBE, artifacts[3], ROOT/'sidecar',
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),scenario,cwd=tmp_path,env=env)

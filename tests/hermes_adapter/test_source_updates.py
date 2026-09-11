"""Exact task updates through installed Hermes, Relay and the real SDK codec.

Inference responses are controlled. This qualifies source/transport behavior,
not a model's obedience, an external channel, or production performance.
"""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import copy, json, os, socket, sys, time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1]); sys.path.insert(1,sys.argv[2])
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.turns import get_turn_idempotency_ledger
from colony_hermes.client import source_message_hash
from colony_hermes.input_provenance import SourceUpdate, transport_input, current
import colony_hermes.client as cm
import colony_hermes.request_memory as rm
import colony_hermes.request_work as rw
# Canonical ASGI work is real, but a cold shared CI host is not a LAN-latency
# benchmark. Dedicated deadline fixtures retain the ordinary real clocks.
clock=NS(monotonic=lambda:1000.0,time=time.time,sleep=time.sleep)
cm.time=rm.time=rw.time=clock
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
key='neutral-update-test-key'
keyring=home/'keys.json';keyring.write_text(json.dumps({'version':1,'principals':[{
 'principal':'neutral-fixture','status':'active','viewer_person_id':'owner','person_ids':['owner'],
 'scopes':['context:read','memory:read','turns:write'],'audiences':['viewer'],
 'credentials':[{'id':'test','secret':key,'status':'active'}]}]}));keyring.chmod(0o600)
(home/'config.yaml').write_text(json.dumps({
 'model':{'provider':'custom','default':'fixture','base_url':'http://model.fixture/v1'},
 'auxiliary':{'title_generation':{'enabled':False}},
 'memory':{'provider':'colony-memory','config':{'contact_id':'owner','url':'http://fixture','api_key':key}},
 'plugins':{'enabled':['colony'],'colony':{'owner_contact_id':'owner','url':'http://fixture',
  'api_key':key,'attested_system_platforms':['cli'],'turn_outbox_path':str(home/'outbox.db')}}}))
app=FastAPI();app.add_middleware(ApiKeyMiddleware,keyring_path=str(keyring))
app.include_router(host.router);app.include_router(host.v2_router);api=TestClient(app)
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
original='Open the retained storage checklist.'
instruction='The checklist label must be ORANGE-472.'
ledger.record_source('root-source',contact_id='owner',session_id='voice-input',
 messages=[{'role':'user','content':original}],derive_claims=False)
root_ref=ledger.source_references(['root-source'],contact_id='owner',session_id='voice-input')[0]
root_input=[{'source_id':'root-source','input_message_hash':source_message_hash(
 'voice-input',{'role':'user','content':original})}]
change_input=[{'source_id':'change-source','input_message_hash':source_message_hash(
 'email-input',{'role':'user','content':instruction})}]
scenario=sys.argv[3];bodies=[];observations=[];granted=True;carrier=change_ref=None
def observe(value):
 observations.append(copy.deepcopy(value))
 return scenario!='receipt_failure'
def respond(request):
 global granted,carrier,change_ref
 if request.url.host=='fixture':
  response=api.request(request.method,request.url.path,params=request.url.params,
   headers=dict(request.headers),content=request.content)
  return httpx.Response(response.status_code,content=response.content,headers=response.headers)
 assert request.url.host=='model.fixture',str(request.url)
 if request.method=='GET':return httpx.Response(200,json={'data':[{'id':'fixture','context_length':131072}]})
 if request.url.path=='/api/show':return httpx.Response(404,json={'error':'not Ollama'})
 assert request.url.path=='/v1/chat/completions'
 body=json.loads(request.content);bodies.append(body);step=len(bodies)
 assert step<=(4 if scenario=='joined_child' else 3 if scenario=='erased_after_visibility' else 2),'Unexpected physical inference call'
 text='\n'.join(row['content'] for row in body['messages'] if isinstance(row.get('content'),str))
 if step==1:
  assert instruction not in text and current() is supplied
  assert supplied.parents()[0]==root_input
  ledger.record_source('change-source',contact_id='owner',session_id='email-input',
   messages=[{'role':'user','content':instruction}],derive_claims=False)
  change_ref=ledger.source_references(['change-source'],contact_id='owner',session_id='email-input')[0]
  update=SourceUpdate('change-one','owner',instruction,change_input,[change_ref])
  carrier=supplied.register_update(update,validate=lambda:granted,observe=observe)
  assert supplied.register_update(update,validate=lambda:False)==carrier
  assert supplied.parents()[0]==root_input,'Accepted update prematurely became consumed input'
  assert parent.steer(carrier)
  if scenario=='erased':ledger.erase_sources(contact_id='owner',turn_ids=['change-source'])
  if scenario=='revoked':granted=False
  message={'role':'assistant','content':None,'tool_calls':[{'id':'read-root','type':'function',
   'function':{'name':'tool_call','arguments':json.dumps({'name':'colony_memory_read_source',
    'arguments':root_ref})}}]};finish='tool_calls'
 else:
  if scenario in {'erased','revoked','receipt_failure'} or (scenario=='erased_after_visibility' and step==3):
   assert instruction not in text and carrier not in text and body.get('tools',[])==[],body
   assert supplied.failure and supplied.result is None
   message={'role':'assistant','content':'The task source is unavailable.'}
  else:
   assert change_ref['source_version'] in text,body
   if scenario=='joined_child' and step==3:
    assert len(supplied._sessions)==2,supplied._sessions
    assert carrier not in text,'A child has source parents, not replayed parent steering'
   else:
    assert carrier in text,body
   assert root_input[0] in supplied.parents()[0] and change_input[0] in supplied.parents()[0]
   assert any(row['stage']=='native_request_visible' for row in observations),observations
   assert all(row['boundary'] in {'hermes_request_middleware','relay_before_next_call'} for row in observations)
   assert all('instruction' not in row and row['update_id']=='change-one' for row in observations)
   if scenario=='summary':assert not any(row['stage']=='middleware_visible' for row in observations),observations
   message={'role':'assistant','content':'The checklist label is ORANGE-472.'}
  finish='stop'
  if scenario=='erased_after_visibility' and step==2:
   ledger.erase_sources(contact_id='owner',turn_ids=['change-source'])
   message={'role':'assistant','content':None,'tool_calls':[{'id':'read-again','type':'function',
    'function':{'name':'tool_call','arguments':json.dumps({'name':'colony_memory_read_source',
     'arguments':root_ref})}}]};finish='tool_calls'
  if scenario=='joined_child' and step==2:
   message={'role':'assistant','content':None,'tool_calls':[{'id':'delegate','type':'function',
    'function':{'name':'delegate_task','arguments':json.dumps({'tasks':[{
     'goal':'Report the current checklist label using the inherited change source handle.',
     'context':'Open the inherited evidence if necessary.','toolsets':['colony']}]})}}]};finish='tool_calls'
 if body.get('stream'):
  delta={**message}
  if delta.get('tool_calls'):delta['tool_calls']=[{**v,'index':i} for i,v in enumerate(delta['tool_calls'])]
  chunk={'id':'one','object':'chat.completion.chunk','created':1,'model':'fixture-reported',
   'choices':[{'index':0,'delta':delta,'finish_reason':None}]}
  end={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':finish}]}
  return httpx.Response(200,headers={'content-type':'text/event-stream'},content=(
   'data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(end)+'\n\ndata: [DONE]\n\n').encode())
 return httpx.Response(200,json={'id':'one','object':'chat.completion','created':1,'model':'fixture-reported',
  'choices':[{'index':0,'message':message,'finish_reason':finish}],
  'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
def controlled(self,request):
 try:return respond(request)
 except Exception:
  import traceback;traceback.print_exc();raise
httpx.HTTPTransport.handle_request=controlled
def no_network(*a,**kw):raise AssertionError('No network in source-update fixture')
socket.socket.connect=no_network;socket.create_connection=no_network
from gateway.session_context import declare_stateless_channel
declare_stateless_channel()
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from run_agent import AIAgent
parent=AIAgent(api_key='fixture',base_url='http://model.fixture/v1',provider='custom',model='fixture',
 quiet_mode=True,skip_context_files=True,skip_memory=False,platform='cli',
 max_iterations=1 if scenario=='summary' else 5,enabled_toolsets=['colony','delegation'])
parent.save_trajectories=False
try:
 with transport_input(contact_id='owner',platform='cli',input_refs=root_input,source_refs=[root_ref]) as supplied:
  result=parent.run_conversation('Perform the admitted checklist task.',persist_user_message=original)
  assert len(bodies)==(4 if scenario=='joined_child' else 3 if scenario=='erased_after_visibility' else 2),bodies
  if scenario in {'erased','revoked','receipt_failure','erased_after_visibility'}:
   assert supplied.result is None and supplied.failure
  else:
   assert supplied.result and change_input[0] in supplied.result['input_refs'],supplied.result
   assert change_ref in supplied.result['source_refs'],supplied.result
finally:parent.close()
assert current() is None
print(json.dumps({'scenario':scenario,'physical_sdk_requests':len(bodies),
 'controlled_reported_model':'fixture-reported','observation_stages':[r['stage'] for r in observations],
 'model_quality_measured':False,'external_requests':0}))
'''


@pytest.mark.parametrize('scenario', ['normal', 'summary', 'erased', 'revoked', 'receipt_failure',
                                      'erased_after_visibility', 'joined_child'])
def test_native_source_update_sdk_and_failure_boundaries(artifacts, tmp_path, scenario):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native qualification')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        COLONY_OWNER_CONTACT_ID='owner', HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1', COLONY_MEMORY_WORKER_TOOLS='0',
        COLONY_MEMORY_TURN_WRITER='disabled', COLONY_GUARD_CHAT_MODE='off',
        COLONY_RECALL_RERANK='off', COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        OPENAI_API_KEY='fixture', OPENAI_BASE_URL='http://model.fixture/v1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    run_python('-I', '-c', PROBE, artifacts[3], ROOT/'sidecar', scenario, cwd=tmp_path, env=env)


REGISTRATION = r'''
import copy,sys
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from colony_hermes.input_provenance import SourceUpdate,transport_input
base=[{'source_id':'root','input_message_hash':'a'*64}]
parents=[{'source_id':'update-source','input_message_hash':'b'*64}]
sources=[{'source_id':'update-source','source_version':'c'*64}]
update=SourceUpdate('stable-update','owner','Use the orange label.',parents,sources)
parents[0]['source_id']='mutated';update.input_refs[0]['source_id']='another-mutation'
assert update.input_refs[0]['source_id']=='update-source'
scope=NS(contact_id='owner',session_id='native-one',task_id='task',turn_id='turn',
 valid_participant=True,platform='test-platform',authority_lane='owner')
with transport_input(contact_id='owner',platform='test-platform',input_refs=base) as first:
 carrier=first.register_update(update,validate=lambda:True)
 body={'messages':[{'role':'tool','content':'Tool result.\n'+carrier}]}
 assert first.request_updates(scope,body)==[] and first.parents()==(base,[])
 first.bind(scope)
 projected=NS(contact_id='owner',task_id='source-reader',turn_id='check',valid_participant=True)
 assert first.request_updates(projected,body)==[],'A source reader is not the native task'
 assert first.observe_updates(projected,body,stage='middleware_visible') is True
 assert first.request_updates(scope,{'messages':[{'role':'user','content':'Unregistered lookalike'}]})==[]
 wrong=copy.copy(scope);wrong.contact_id='someone-else'
 assert first.request_updates(wrong,body)==[]
 wrong=copy.copy(scope);wrong.session_id='unrelated-session'
 assert first.request_updates(wrong,body)==[]
 entries=first.request_updates(scope,body);assert len(entries)==1
 assert first.check_updates(scope,entries,fresh=True,rules=[])
 assert first.parents()==(base,[])
 first.admit_updates(scope,body,entries)
 assert update.input_refs[0] in first.parents()[0]
 assert first.register_update(update,validate=lambda:False)==carrier
 try:first.register_update(SourceUpdate('stable-update','owner','Different instruction',update.input_refs),validate=lambda:True)
 except ValueError:pass
 else:raise AssertionError('An update ID changed meaning')
 try:first.register_update(SourceUpdate('other','someone-else','Unauthorized',update.input_refs),validate=lambda:True)
 except ValueError:pass
 else:raise AssertionError('Another contact registered as owner')
try:first.register_update(update,validate=lambda:True)
except ValueError:pass
else:raise AssertionError('A closed input scope accepted updates')
with transport_input(contact_id='owner',platform='test-platform',input_refs=base) as restored:
 assert restored.register_update(update,validate=lambda:True)==carrier
 assert restored.parents()==(base,[]),'Rehydration asserted consumption before a request'
 restored.bind(scope)
 entries=restored.request_updates(scope,body)
 assert restored.check_updates(scope,entries,fresh=True,rules=[])
 restored.admit_updates(scope,body,entries)
 assert restored.parents()==first.parents()
with transport_input(contact_id='owner',platform='test-platform',input_refs=base) as wrong_root:
 wrong_root.register_update(update,validate=lambda:True)
 denied=copy.copy(scope);denied.contact_id='someone-else'
 wrong_root.bind(denied)
 assert wrong_root.request_updates(denied,body)==[]
 assert not wrong_root.allowed(denied,fresh=True,rules=[])
 assert wrong_root.parents()==(base,[])
print('registration, immutable refs, rehydration, identity and closed-scope checks passed')
'''


def test_source_update_registration_identity_and_rehydration(artifacts, tmp_path):
    run_python('-I', '-c', REGISTRATION, artifacts[3], cwd=tmp_path)


CONCURRENT = r'''
import sys,threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from colony_hermes.input_provenance import SourceUpdate,transport_input
root=[{'source_id':'root','input_message_hash':'a'*64}]
parents=[{'source_id':'update','input_message_hash':'b'*64}]
scope=NS(contact_id='owner',session_id='session',task_id='task',turn_id='turn',
 valid_participant=True,platform='test-platform',authority_lane='owner')
entered=[threading.Event(),threading.Event()]
release=[threading.Event(),threading.Event()]
lock=threading.Lock();calls=[]
def persist(value):
 with lock:
  index=len(calls);calls.append(value)
 assert index<2,'Completed receipt was unnecessarily repeated'
 entered[index].set()
 assert release[index].wait(5),'Receipt write was never released'
 return True
with transport_input(contact_id='owner',platform='test-platform',input_refs=root) as supplied:
 supplied.bind(scope)
 carrier=supplied.register_update(SourceUpdate('change','owner','Use orange.',parents),
  validate=lambda:True,observe=persist)
 body={'messages':[{'role':'tool','content':carrier}]}
 supplied.admit_updates(scope,body,supplied.request_updates(scope,body))
 with ThreadPoolExecutor(max_workers=2) as pool:
  try:
   first=pool.submit(supplied.observe_updates,scope,body,stage='native_request_visible')
   assert entered[0].wait(2)
   second=pool.submit(supplied.observe_updates,scope,body,stage='native_request_visible')
   assert entered[1].wait(2),'Second request bypassed the unfinished durable receipt'
   assert not first.done() and not second.done()
   release[0].set();assert first.result(timeout=2) is True
   assert not second.done()
   release[1].set();assert second.result(timeout=2) is True
  finally:
   for event in release:event.set()
 assert supplied.observe_updates(scope,body,stage='native_request_visible') is True
 assert len(calls)==2
print('Concurrent requests cannot bypass an unfinished durable receipt')
'''


def test_concurrent_requests_wait_for_durable_update_receipt(artifacts, tmp_path):
    run_python('-I', '-c', CONCURRENT, artifacts[3], cwd=tmp_path)


WITHHELD_READ_STEERING = r'''
import copy,json,sys
sys.path.insert(0,sys.argv[1])
from colony_hermes.input_provenance import SourceUpdate
from colony_hermes.request_memory import _restore_source_updates
from agent.prompt_builder import format_steer_marker
first=SourceUpdate('one','owner','Use the first checklist.',[
 {'source_id':'first-input','input_message_hash':'a'*64}]).carrier()
second=SourceUpdate('two','owner','Then use the second checklist.',[
 {'source_id':'second-input','input_message_hash':'b'*64}]).carrier()
entries=[{'carrier':first},{'carrier':second}]
source=json.dumps({'colony_source_read_v1':True,'text':'Private old source content.'})
withheld='[Opened source withheld; read again after source freshness is restored.]'
def restored(value, *, original=None):
 original=original or {'messages':[{'role':'tool','tool_call_id':'read-one','content':value}]}
 filtered={'messages':[{'role':'tool','tool_call_id':'read-one','content':withheld}]}
 result=_restore_source_updates(original,copy.deepcopy(filtered),entries)
 assert result['messages'][0]['tool_call_id']=='read-one'
 return result['messages'][0]['content']
value=source+format_steer_marker('Unregistered trailing prose.\n'+second+'\n'+first)
actual=restored(value)
assert actual==withheld+format_steer_marker(second+'\n\n'+first)
assert 'Private old source content.' not in actual and 'Unregistered trailing prose.' not in actual
# A quoted carrier or an earlier admitted update cannot manufacture delivery.
assert restored(json.dumps({'colony_source_read_v1':True,'text':format_steer_marker(first)}))==withheld
assert restored(source)==withheld
assert restored(source+format_steer_marker('Unregistered update'))==withheld
assert restored(source+'\n'+first)==withheld
assert restored(source+format_steer_marker(first)+'Unknown trailer')==withheld
assert restored('Ordinary tool output'+format_steer_marker(first))==withheld
# Multiple source rows cannot claim the same output identity.
row={'role':'tool','tool_call_id':'read-one','content':value}
assert restored(value,original={'messages':[row,copy.deepcopy(row)]})==withheld
# Native Anthropic uses a separate trailing text block; retain no other part.
parts=[{'type':'text','text':source},{'type':'text','text':format_steer_marker(first).lstrip()}]
assert restored(parts)==withheld+format_steer_marker(first)
print('Exact registered native suffixes retain order and closure; quoted/unknown/replayed content remains withheld')
'''


def test_withheld_source_read_retains_only_registered_native_steering(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native qualification')
    run_python('-I', '-c', WITHHELD_READ_STEERING, artifacts[3], cwd=tmp_path)

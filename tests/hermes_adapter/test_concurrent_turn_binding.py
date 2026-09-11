"""Actual simultaneous native starts must retain each authenticated turn scope."""
import importlib.util
import json
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, copy, json, os, socket, sys, threading, time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0,sys.argv[1]); sys.path.insert(1,sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
second_person=sys.argv[4]
native=sys.argv[5]
if native: sys.path.insert(2,native)
import uvicorn
from fastapi import FastAPI, Response
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions,host
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.turns import get_turn_idempotency_ledger

home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
state=Path(os.environ['COLONY_STATE_DIR']); state.mkdir()
contacts=SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
async def setup():
    await contacts.connect()
    person=await contacts.create(display_name='Neutral owner',trust_tier='inner_circle')
    guest=await contacts.create(display_name='Neutral guest')
    await contacts.add_handle(person.contact_id,gateway='sms',address='+15550007160')
    if second_person!='unavailable':
        await contacts.add_handle(person.contact_id if second_person=='owner' else guest.contact_id,
                                  gateway='sms',address='+15550007161')
    return person.contact_id,guest.contact_id
owner,guest=asyncio.run(setup()); host._contacts_store=contacts; host._task_queue=None
os.environ['COLONY_OWNER_CONTACT_ID']=owner
fact='My neutral orchard badge is cobalt-716.'
guest_fact='My neutral orchard badge is amber-981.'
ledger=get_turn_idempotency_ledger(state)
ledger.record_source('neutral-source',contact_id=owner,session_id='earlier-session',
    messages=[{'role':'user','content':fact}],derive_claims=False)
ledger.record_source('neutral-guest-source',contact_id=guest,session_id='earlier-guest-session',
    messages=[{'role':'user','content':guest_fact}],derive_claims=False)
fixture=home/'neutral.txt'; fixture.write_text('ACTUAL_CONCURRENT_READ')
app=FastAPI(); wire=[]
@app.middleware('http')
async def authority(request,next_call):
    if (second_person=='unavailable' and request.url.path=='/v1/host/contacts/resolve'
            and request.query_params.get('address')=='+15550007161'):
        return Response('Controlled identity resolver outage',status_code=503)
    request.state.colony_authority=RequestAuthority(principal_id='native-fixture',credential_id='fixture',
        scopes=frozenset({'turns:write','context:read'}),viewer_person_id=owner,
        person_ids=frozenset({owner,guest}),audiences=frozenset({'viewer'}),authenticated=True)
    response=await next_call(request)
    wire.append((request.method,request.url.path,dict(request.query_params),response.status_code))
    return response
app.include_router(executions.router); app.include_router(host.router); app.include_router(host.v2_router)
listener=socket.socket(); listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
base='http://127.0.0.1:'+str(port)
server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
server_thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
server_thread.start(); deadline=time.monotonic()+10
while not server.started and time.monotonic()<deadline: time.sleep(.01)
assert server.started
connect=socket.socket.connect
def local_only(self,address):
    assert isinstance(address,tuple) and address[:2]==('127.0.0.1',port),address
    return connect(self,address)
socket.socket.connect=local_only
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['colony'],'colony':{
    'owner_contact_id':owner,'url':base,'turn_writer_platforms':[],'execution_registry_enabled':True}},
    'memory':{'provider':'colony-memory','config':{'contact_id':owner,'url':base}}}))
import colony_hermes
first_hook=threading.Event(); second_queued=threading.Event(); release=threading.Event()
original_get=colony_hermes.ColonyClient.get
def get(self,path,**kwargs):
    if (path=='/v1/host/contacts/resolve' and kwargs.get('params',{}).get('address')=='+15550007160'
            and threading.current_thread().name.startswith('hermes-hook-pre_llm_call')):
        first_hook.set()
        assert release.wait(15), 'second native turn failed to overlap first callback'
    return original_get(self,path,**kwargs)
colony_hermes.ColonyClient.get=get
from hermes_cli.plugins import get_plugin_manager
if native:
    import hermes_cli.plugins
    assert Path(hermes_cli.plugins.__file__).resolve().is_relative_to(Path(native).resolve())
plugin_manager=get_plugin_manager()
plugin_manager.discover_and_load()
assert plugin_manager._plugins['colony'].enabled
# Observe real admission behind the running callback, then let both turns
# proceed. Waiting for the second whole turn here would make a circular wait
# under Hermes's healthy-overlap serialization.
condition=plugin_manager._hook_timeout_running_cond
original_wait=condition.wait
def observe_wait(timeout=None):
    if threading.current_thread() is second and first_hook.is_set() and not release.is_set():
        second_queued.set()
    return original_wait(timeout)
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
from gateway.session_context import set_session_vars,clear_session_vars
from run_agent import AIAgent
import run_agent
openai_target='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
tools_target='run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
definitions=[{'type':'function','function':{'name':'read_file','description':'Read a neutral local file',
    'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}]
requests=[[],[]]; results={}; errors=[]; agents=[]
def answer(index,**kwargs):
    requests[index].append(copy.deepcopy(kwargs))
    if len(requests[index])==1:
        tool=NS(id='read-'+str(index),type='function',function=NS(name='read_file',arguments=json.dumps({'path':str(fixture)})))
        return NS(choices=[NS(message=NS(content='',tool_calls=[tool]),finish_reason='tool_calls')],model='fixture/model',usage=None)
    return NS(choices=[NS(message=NS(content='DONE-'+str(index),tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None)
clients=[MagicMock(),MagicMock()]
for index,client in enumerate(clients):
    client.chat.completions.create.side_effect=lambda _index=index,**kwargs:answer(_index,**kwargs)
def run(index):
    tokens=set_session_vars(platform='sms',user_id='+1555000716'+str(index),chat_id='chat-'+str(index),session_id=agents[index].session_id)
    try:
        results[index]=agents[index].run_conversation('Read the neutral note. Which orchard badge?',task_id='concurrent-'+str(index))
    except BaseException as error:
        errors.append(repr(error))
    finally:
        clear_session_vars(tokens)
try:
    with patch.object(condition,'wait',side_effect=observe_wait),patch(openai_target,side_effect=clients),patch(tools_target+'.get_tool_definitions',return_value=definitions),patch(tools_target+'.check_toolset_requirements',return_value={}):
        for index in range(2):
            agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',model='fixture/model',
                quiet_mode=True,skip_context_files=True,skip_memory=True,platform='sms',enabled_toolsets=['file'],
                max_iterations=3,session_id='simultaneous-session-'+str(index))
            agent._user_id='+1555000716'+str(index)
            agent._cached_system_prompt='Neutral identity.'; agent._use_prompt_caching=False
            agent.compression_enabled=False; agent.save_trajectories=False
            manager=MemoryManager(); manager.add_provider(load_memory_provider('colony-memory'))
            manager.initialize_all(agent.session_id,hermes_home=str(home),platform='sms')
            agent._memory_manager=manager; agents.append(agent)
        first=threading.Thread(target=run,args=(0,)); first.start()
        assert first_hook.wait(10),'first native pre_llm_call did not enter resolver'
        second=threading.Thread(target=run,args=(1,)); second.start()
        assert second_queued.wait(10),'second native turn did not queue behind the held callback'
        release.set()
        second.join(20); first.join(20)
        assert not first.is_alive() and not second.is_alive() and not errors,errors
    summary=[]
    for index in range(2):
        first_request=json.dumps(requests[index][0])
        tools=[row['content'] for row in results[index]['messages'] if row.get('role')=='tool']
        scope=colony_hermes._TRANSPORT_SCOPES.for_session(agents[index].session_id)
        with ledger._connect() as db:
            observed=db.execute('SELECT contact_id FROM execution_observations WHERE session_id=?',
                                (agents[index].session_id,)).fetchall()
        expected_contact=guest if index==1 and second_person=='guest' else owner
        assert bool(observed)==(index==0 or second_person!='unavailable'),observed
        assert all(row['contact_id']==expected_contact for row in observed),observed
        expected_fact=guest_fact if index==1 and second_person=='guest' else fact
        summary.append({'index':index,'recall':expected_fact in first_request,'work':'[colony-work-request-v1]' in first_request,
            'authorized_read':any('ACTUAL_CONCURRENT_READ' in value for value in tools),
            'bound':bool(scope and scope.valid_participant),'model_requests':len(requests[index])})
        if index==1 and second_person!='owner':
            assert fact not in first_request, 'owner memory crossed participant scope'
    print(json.dumps({'simultaneous_turns':summary,'actual_native':True,'actual_http_sqlite':True,'controlled_model':True}))
    assert all(summary[0][key] for key in ('recall','work','authorized_read','bound')),summary
    expected={'owner':(True,True,True,True),'guest':(True,False,False,True),'unavailable':(False,False,False,False)}[second_person]
    assert tuple(summary[1][key] for key in ('recall','work','authorized_read','bound'))==expected,summary
finally:
    release.set()
    for agent in agents: agent.close()
    server.should_exit=True; server_thread.join(5); asyncio.run(contacts.close())
'''


@pytest.mark.parametrize('second_person',['owner','guest','unavailable'])
def test_actual_overlapping_native_starts_preserve_recall_authorized_tools_and_work(artifacts,tmp_path,second_person):
    native=os.environ.get('COLONY_TEST_HERMES_PATH','')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual concurrent turn qualification')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'),COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1',COLONY_MEMORY_WORKER_TOOLS='0',COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_GUARD_CHAT_MODE='off',COLONY_OWNER_CONTACT_ID='owner',COLONY_SKIP_DOTENV='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=run_python('-I','-c',PROBE,artifacts[3],ROOT/'sidecar',os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),second_person,native,cwd=tmp_path,env=env)
    assert '"actual_native": true' in result.stdout

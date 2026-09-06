"""Native single-turn work refresh over actual HTTP and canonical SQLite state.

Provider replies and the neutral completion writer are controlled. This checks
the integration and native tool loop, not model quality or production execution.
"""
import importlib.util
import json
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, copy, hashlib, json, os, socket, sys, threading, time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
platform=sys.argv[4]
import httpx
import uvicorn
from fastapi import FastAPI, Response
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions, host
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns import get_turn_idempotency_ledger

home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
state=Path(os.environ['COLONY_STATE_DIR']); state.mkdir()
contacts=SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
async def create_owner():
    await contacts.connect()
    contact=await contacts.create(display_name='Neutral fixture owner',trust_tier='inner_circle')
    await contacts.add_handle(contact.contact_id,gateway='sms',address='+15550007160')
    return contact.contact_id
owner=asyncio.run(create_owner()); host._contacts_store=contacts
os.environ['COLONY_OWNER_CONTACT_ID']=owner
store=InitiativeStore(state); host._initiative_store=store; host._task_queue=None
work=store.create(type='RESEARCH_DEEP_DIVE', description='Neutral concurrent note comparison',
    source_type='installed_capabilities', created_by='native_local_work',
    context={'event_key':'neutral-work-fixture', 'native_job_id':'neutral-job',
             'native_execution_id':'neutral-completion-writer'})
store.assign(work.id, 'neutral-writer')
report=home/'report.md'; report.write_text('Neutral comparison completed.\n')
report_hash=hashlib.sha256(report.read_bytes()).hexdigest()
fixture=home/'neutral.txt'; fixture.write_text('NEUTRAL_TOOL_CONTENT_716\n')
fact='My neutral orchard badge is cobalt-716.'
ledger=get_turn_idempotency_ledger(state)
ledger.record_source('neutral-source', contact_id=owner, session_id='earlier-session',
    messages=[{'role':'user','content':fact}], derive_claims=False)

app=FastAPI(); wire=[]; unavailable=threading.Event()
@app.middleware('http')
async def authority(request, next_call):
    request.state.colony_authority=RequestAuthority(principal_id='neutral-native', credential_id='fixture',
        scopes=frozenset({'turns:write','context:read'}), viewer_person_id=owner,
        person_ids=frozenset({owner}), audiences=frozenset({'viewer'}), authenticated=True)
    if request.url.path=='/v1/host/executions' and request.query_params.get('projection')=='request' and unavailable.is_set():
        wire.append((request.method, request.url.path, dict(request.query_params), 503))
        return Response('Controlled work endpoint interruption', status_code=503)
    response=await next_call(request)
    wire.append((request.method, request.url.path, dict(request.query_params), response.status_code))
    return response
app.include_router(executions.router); app.include_router(host.router); app.include_router(host.v2_router)
listener=socket.socket(); listener.bind(('127.0.0.1',0))
port=listener.getsockname()[1]; base='http://127.0.0.1:'+str(port)
server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
server_thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
server_thread.start(); deadline=time.monotonic()+10
while not server.started and time.monotonic()<deadline: time.sleep(.01)
assert server.started, 'Local HTTP server failed to start'
original_connect=socket.socket.connect
def local_only(self,address):
    assert isinstance(address,tuple) and address[:2]==('127.0.0.1',port), address
    return original_connect(self,address)
socket.socket.connect=local_only
(home/'config.yaml').write_text(json.dumps({
    'plugins':{'enabled':['colony'],'colony':{'owner_contact_id':owner,'url':base,
        'attested_system_platforms':['cli'],'turn_writer_platforms':[]}},
    'memory':{'provider':'colony-memory','config':{'contact_id':owner,'url':base}}}))

from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['colony'].enabled
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
provider=load_memory_provider('colony-memory')
manager=MemoryManager(); manager.add_provider(provider)
manager.initialize_all('neutral-owner-session', hermes_home=str(home), platform=platform)
from gateway.session_context import set_session_vars, clear_session_vars
session_tokens=set_session_vars(platform=platform,
    user_id='+15550007160' if platform=='sms' else '',
    chat_id='neutral-owner-chat' if platform=='sms' else '')

release_writer=threading.Event(); writer_done=threading.Event(); errors=[]
def complete_other_work():
    try:
        assert release_writer.wait(20), 'Native request did not release completion writer'
        with httpx.Client(timeout=5,trust_env=False) as api:
            response=api.post(base+'/v1/host/initiatives/'+work.id+'/complete', json={
                'agent_id':'neutral-writer','result':{'status':'briefing_created',
                'report_sha256':report_hash,'report_path':str(report),'summary':'Neutral comparison complete.'}})
            assert response.status_code==200, response.text
            assert response.json()['status']=='completed'
    except BaseException as exc:
        errors.append(repr(exc))
    finally:
        writer_done.set()
writer=threading.Thread(target=complete_other_work,daemon=True); writer.start()

from run_agent import AIAgent
requests=[]; user_message='Read the neutral local note while keeping track of the other work.'
marker='[colony-work-request-v1]'; closing='[/colony-work-request-v1]'
def work_block(request):
    blocks=[]
    for row in request['messages']:
        content=row.get('content')
        if row.get('role')=='system' and isinstance(content,str) and marker in content:
            assert content.count(marker)==1 and content.count(closing)==1, content
            blocks.append(content.split(marker,1)[1].split(closing,1)[0])
    assert len(blocks)==1, request['messages']
    return blocks[0]
def answer(**kwargs):
    requests.append(copy.deepcopy(kwargs)); index=len(requests)
    block=work_block(kwargs)
    full_reads=[row for row in wire if row[1]=='/v1/host/context/assemble']
    assert len(full_reads)==1 and full_reads[0][3]==200, wire
    assert fact in json.dumps(kwargs['messages']), kwargs['messages']
    if index==1:
        assert work.id in block and 'assigned' in block and report_hash not in block, block
        release_writer.set(); assert writer_done.wait(10) and not errors, errors
        assert store.get(work.id).status=='completed'
    elif index==2:
        assert work.id in block and 'completed' in block and block.count(report_hash)==1, block
        assert 'assigned' not in block, block
        unavailable.set()
    else:
        assert index==3 and 'unavailable' in block.lower() and report_hash not in block, block
        return NS(choices=[NS(message=NS(content='NATIVE_WORK_REFRESH_OK',tool_calls=None),
                             finish_reason='stop')],model='fixture/model',usage=None)
    call=NS(id='neutral-read-'+str(index),type='function',
        function=NS(name='read_file',arguments=json.dumps({'path':str(fixture)})))
    return NS(choices=[NS(message=NS(content='',tool_calls=[call]),finish_reason='tool_calls')],
              model='fixture/model',usage=None)

client=MagicMock(); client.chat.completions.create.side_effect=answer
definitions=[{'type':'function','function':{'name':'read_file','description':'Read a local file',
    'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}]
try:
    with patch('run_agent.OpenAI',return_value=client), patch('run_agent.get_tool_definitions',return_value=definitions), \
            patch('run_agent.check_toolset_requirements',return_value={}), \
            patch.object(manager,'prefetch_all',wraps=manager.prefetch_all) as prefetch:
        agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
            model='fixture/model',quiet_mode=True,skip_context_files=True,skip_memory=True,
            platform=platform,enabled_toolsets=['file'],max_iterations=4)
        agent._user_id='+15550007160' if platform=='sms' else ''
        agent._cached_system_prompt='Stable neutral identity.'
        agent._use_prompt_caching=False; agent.compression_enabled=False; agent.save_trajectories=False
        agent._memory_manager=manager
        result=agent.run_conversation(user_message,task_id='neutral-work-turn')
        assert result['final_response']=='NATIVE_WORK_REFRESH_OK', result
        assert prefetch.call_count==1 and len(requests)==3
        transcript=result['messages']
        assert marker not in json.dumps(transcript), transcript
        assert any(row.get('role')=='user' and row.get('content')==user_message for row in transcript)
        native_tools=[row for row in transcript if row.get('role')=='tool']
        assert len(native_tools)==2 and 'NEUTRAL_TOOL_CONTENT_716' in native_tools[0]['content'], native_tools
        # Native read_file may deduplicate the second unchanged read. Its
        # actual result still has to arrive unchanged at the next request.
        assert ('NEUTRAL_TOOL_CONTENT_716' in native_tools[1]['content']
                or json.loads(native_tools[1]['content']).get('status')=='unchanged'), native_tools
        for request in requests:
            sent_tools=[row for row in request['messages'] if row.get('role')=='tool']
            assert all(any(row['content']==native['content'] and row['tool_call_id']==native['tool_call_id']
                           for native in native_tools) for row in sent_tools), sent_tools
        agent.close()
    refreshes=[row for row in wire if row[1]=='/v1/host/executions' and row[2].get('projection')=='request']
    assert len(refreshes)==3 and [row[3] for row in refreshes]==[200,200,503], wire
    assert all(row[2].get('contact_id')==owner and row[2].get('limit')=='8' and row[2].get('session_id') for row in refreshes)
    assert store.get(work.id).result_metadata['report_sha256']==report_hash
    assert len([row for row in wire if row[0]=='POST' and row[1].endswith('/complete')])==1
finally:
    release_writer.set(); writer.join(5)
    server.should_exit=True; server_thread.join(5); store.close()
    clear_session_vars(session_tokens); asyncio.run(contacts.close())
assert not server_thread.is_alive() and not writer.is_alive() and not errors, errors
print(json.dumps({'native_single_turn':True,'actual_http_and_ledger':True,
    'concurrent_completion_visible':True,'memory_prefetches':1,'model_requests':3,
    'unavailable_replaces_work':True,'native_transcript_unchanged':True,
    'controlled_inference_and_writer':True,'platform':platform}))
'''


@pytest.mark.parametrize('platform', ['sms', 'cli'])
def test_native_turn_refreshes_shared_work_between_model_calls(artifacts, tmp_path, platform):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes to exercise actual native model requests')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system', COLONY_GUARD_CHAT_MODE='off',
        COLONY_OWNER_CONTACT_ID='owner', COLONY_SKIP_DOTENV='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=run_python('-I','-c',PROBE,artifacts[3],ROOT/'sidecar',
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),platform,cwd=tmp_path,env=env)
    assert json.loads(result.stdout.splitlines()[-1])['concurrent_completion_visible']

"""Ordinary native CLI recalls through its resolved turn, with fallback disabled."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import copy,json,os,socket,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
if sys.argv[5]:sys.path.insert(0,sys.argv[5])
trusted=sys.argv[4]=='attested'
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from apsimo.api.authority import RequestAuthority
from apsimo.api.routers import host
from apsimo.turns import get_turn_idempotency_ledger
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
home.joinpath('config.yaml').write_text(json.dumps({
    'plugins':{'enabled':['apsimo'],'apsimo':{'url':'http://fixture','owner_contact_id':'contact-a',
        'attested_system_platforms':['cli'] if trusted else [],'turn_writer_platforms':['cli']}},
    'memory':{'provider':'apsimo-memory','config':{'url':'http://fixture','contact_id':'contact-a'}}}))
home.joinpath('SOUL.md').write_text('Neutral CLI fixture. Answer the current question.')
fact='My neutral orchard badge is cobalt-716.'
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
ledger.record_source('earlier-neutral-source',contact_id='contact-a',session_id='earlier-channel',
    messages=[{'role':'user','content':fact}],derive_claims=False)
app=FastAPI();calls=[];recorded_turns=[]
@app.middleware('http')
async def authority(request,next_call):
    request.state.colony_authority=RequestAuthority(principal_id='neutral-fixture',credential_id='fixture',
        scopes=frozenset({'context:read','turns:write','memory:read'}),viewer_person_id='contact-a',
        person_ids=frozenset({'contact-a'}),audiences=frozenset({'viewer'}),authenticated=True)
    return await next_call(request)
app.include_router(host.router);app.include_router(host.v2_router)
api=TestClient(app);original_client=httpx.Client
def respond(request):
    if request.url.path=='/v1/host/mind/facts':return httpx.Response(200,json={'facts':[]})
    response=api.request(request.method,request.url.path,params=request.url.params,
                         headers=dict(request.headers),content=request.content)
    calls.append((request.url.path,response.status_code))
    if request.url.path.startswith('/v2/host/turns/'):
        assert response.status_code in (200,201),response.text
        recorded_turns.append(json.loads(request.content))
    return httpx.Response(response.status_code,content=response.content,headers=response.headers)
httpx.Client=lambda **kwargs:original_client(**{**kwargs,'transport':httpx.MockTransport(respond)})
def no_network(*args,**kwargs):raise AssertionError('Controlled API and provider callback only')
socket.socket.connect=no_network;socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['apsimo'].enabled
from run_agent import AIAgent
from agent import relay_runtime
from apsimo_hermes.native_scope import attested_cli_contact
import apsimo_hermes,run_agent
openai_target='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
tools_target='run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
physical=[];binding=[];client=MagicMock()
def answer(**kwargs):
    physical.append(copy.deepcopy(kwargs))
    turn=relay_runtime.active_turn(agent.session_id)
    assert turn is not None
    binding.append(attested_cli_contact(agent.session_id))
    assert attested_cli_contact(agent.session_id+'-unrelated') is None
    assert provider._prefetch_contact(agent.session_id+'-unrelated')==''
    return NS(choices=[NS(message=NS(content='NEUTRAL_CLI_DONE',tool_calls=None),finish_reason='stop')],
              model='neutral/served',usage=None)
client.chat.completions.create.side_effect=answer
with patch(openai_target,return_value=client),patch(tools_target+'.get_tool_definitions',return_value=[]),patch(tools_target+'.check_toolset_requirements',return_value={}):
    agent=AIAgent(api_key='neutral',base_url='http://127.0.0.1:1/v1',provider='openai',model='neutral/model',
        quiet_mode=True,skip_context_files=True,skip_memory=False,platform='cli',max_iterations=1)
    agent._use_prompt_caching=False;agent.save_trajectories=False;agent.compression_enabled=False
    provider=agent._memory_manager.get_provider('apsimo')
    assert provider is not None
    assert provider._prefetch_contact(agent.session_id)==''
    result=agent.run_conversation('What is my neutral orchard badge?',task_id='ordinary-cli')
    assert result['final_response']=='NEUTRAL_CLI_DONE',result
    assert len(physical)==1
    contents=json.dumps(physical[0]['messages'])
    assert (fact in contents)==trusted,(trusted,calls,physical)
    assert ('earlier-neutral-source' in contents)==trusted
    assert binding==['contact-a' if trusted else None],binding
    assert any(path=='/v1/host/context/assemble' and status==200 for path,status in calls)==trusted,calls
    assert len(recorded_turns)==int(trusted),(recorded_turns,calls)
    if trusted:
        # A senderless CLI still has known conversation provenance. Do not
        # substitute a messaging account or the generic host identity for it.
        assert recorded_turns[0]['context']['channel_id']=='cli:contact-a'
        assert recorded_turns[0]['context']['contact_id']=='contact-a'
    # The registry deliberately retains prior scopes. A finished native turn
    # cannot use one as ambient owner authority or replay its provider cache.
    assert apsimo_hermes._TRANSPORT_SCOPES.for_session(agent.session_id) is not None
    assert relay_runtime.active_turn(agent.session_id) is None
    count=len(calls)
    assert attested_cli_contact(agent.session_id) is None
    assert provider.prefetch('What is my neutral orchard badge?',session_id=agent.session_id)==''
    assert len(calls)==count
    agent.close()
print(json.dumps({'trusted_cli':trusted,'actual_native_turn':True,'current_recollection_visible':trusted,
    'unattested_cli_rejected':not trusted,'finished_scope_rejected':True,'unrelated_session_rejected':True,
    'model_calls':0,'controlled_provider_calls':len(physical),'default_context_authority':os.environ['COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY']}))
'''


@pytest.mark.parametrize('mode', ['attested', 'unattested'])
def test_native_cli_prefetch_uses_current_resolved_scope(artifacts, tmp_path, mode):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / 'profile'), COLONY_STATE_DIR=str(tmp_path / 'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_TURN_WRITER='disabled', COLONY_MEMORY_WORKER_TOOLS='0',
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='none', COLONY_OWNER_CONTACT_ID='contact-a',
        COLONY_GUARD_CHAT_MODE='off')
    run_python('-I', '-c', PROBE, artifacts[3], ROOT / 'sidecar',
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH', ''), mode,
        os.environ.get('HERMES_TEST_SOURCE', ''), cwd=tmp_path, env=env)

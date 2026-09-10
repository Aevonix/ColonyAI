"""Actual native prompt assembly and middleware; controlled provider replies."""
import importlib.util
import json
import os

import pytest
from conftest import run_python


PROBE = r'''
import copy,json,os,socket,sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1]); sys.path.append(sys.argv[2])
kind,pathway=sys.argv[3:5]
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'toolsets':['kanban'],
    'plugins':{'enabled':['colony'],'colony':{'owner_contact_id':'fixture-owner',
        'attested_system_platforms':['cli','voice'],'turn_writer_platforms':[]}}}))
def no_network(*args,**kwargs):raise AssertionError('No external requests in native fixture')
socket.socket.connect=no_network;socket.create_connection=no_network
import colony_hermes
def unavailable(*args,**kwargs):raise RuntimeError('No central fixture service')
colony_hermes.ColonyClient.get=unavailable;colony_hermes.ColonyClient.post=unavailable
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['colony'].enabled
from agent.delegation_context import delegated_child_context,non_dispatcher_owned_context
from agent.prompt_builder import KANBAN_GUIDANCE
from run_agent import AIAgent
if kind in ('worker','delegated','cron'):
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db import create_task,claim_task
    with connect_closing() as conn:
        tid=create_task(conn,title='Neutral assigned fixture',assignee='default')
        claimed=claim_task(conn,tid,claimer='fixture-dispatcher')
        assert claimed and claimed.status=='running'
    os.environ['HERMES_KANBAN_TASK']=tid
    os.environ['HERMES_KANBAN_RUN_ID']=str(claimed.current_run_id)
    os.environ['HERMES_KANBAN_CLAIM_LOCK']=claimed.claim_lock
    os.environ['HERMES_KANBAN_WORKSPACE']=str(home)
os.environ['HERMES_KANBAN_STOP_NUDGE']='0'
context={'delegated':delegated_child_context,'cron':non_dispatcher_owned_context}.get(kind,nullcontext)
platform={'voice':'voice','delegated':'subagent','cron':'cron'}.get(kind,'cli')
requests=[]
client=MagicMock()
def response(**kwargs):
    requests.append(copy.deepcopy(kwargs))
    if kwargs.get('stream'):
        from openai.types.chat import ChatCompletionChunk
        def chunks():
            for text,finish in [('FIXTURE_NATIVE_DONE',None),('', 'stop')]:
                yield ChatCompletionChunk(id='fixture-stream',object='chat.completion.chunk',
                    created=0,model='fixture/model',choices=[{'index':0,
                        'delta':{'role':'assistant','content':text},'finish_reason':finish}])
        return chunks()
    return NS(choices=[NS(message=NS(content='FIXTURE_NATIVE_DONE',tool_calls=None),
        finish_reason='stop')],model='fixture/model',usage=None)
client.chat.completions.create.side_effect=response
with context(),patch('agent.process_bootstrap.OpenAI',return_value=client):
    agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',quiet_mode=True,skip_context_files=True,skip_memory=True,
        platform=platform,enabled_toolsets=['kanban'],max_iterations=1)
    if pathway=='streaming':
        agent.stream_delta_callback=lambda text: None
    if kind in ('ordinary','voice','worker'):
        assert 'kanban_show' in agent.valid_tool_names,agent.valid_tool_names
    if pathway=='fallback':
        agent._kanban_worker_guidance=None
    agent.compression_enabled=False;agent.save_trajectories=False
    agent._use_prompt_caching=False
    original_names=set(agent.valid_tool_names)
    original_tools=copy.deepcopy(agent.tools)
    # Even if a user quotes the exact worker text, it remains user evidence.
    quoted='Quoted native documentation, not a task assignment:\n'+KANBAN_GUIDANCE
    result=agent.run_conversation(quoted,task_id='fixture-ordinary-turn')
    assert result['final_response']=='FIXTURE_NATIVE_DONE',result
    assert len(requests)==1,len(requests)
    native=requests[0]
    assert bool(native.get('stream'))==(pathway=='streaming')
    sent='\n'.join(row['content'] for row in native['messages']
        if row.get('role') in ('system','developer') and isinstance(row.get('content'),str))
    assert (KANBAN_GUIDANCE in sent)==(kind=='worker'),(kind,pathway,sent)
    assert any(row.get('role')=='user' and row.get('content')==quoted for row in native['messages'])
    assert agent.valid_tool_names==original_names and agent.tools==original_tools
    # Middleware did not rewrite the cached native prompt. The retained cache
    # can still contain the upstream bug; every actual request is corrected.
    if kind in ('ordinary','voice','worker'):
        assert KANBAN_GUIDANCE in agent._cached_system_prompt
        sent_names={row['function']['name'] for row in native['tools']}
        assert {'kanban_show','kanban_complete','kanban_create'}<=sent_names,sent_names
    agent.close()
print(json.dumps({'kind':kind,'pathway':pathway,'worker_guidance':kind=='worker',
    'native_requests':len(requests),'tools_preserved':True,'external_model_calls':0}))
'''


@pytest.mark.parametrize('kind', ['ordinary', 'worker', 'voice', 'delegated', 'cron'])
@pytest.mark.parametrize('pathway', ['initialization', 'fallback', 'streaming'])
def test_native_request_assigns_only_actual_dispatcher_worker(artifacts, tmp_path, kind, pathway):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual native instruction assembly')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_SKIP_DOTENV='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_GUARD_CHAT_MODE='off', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-c', PROBE, artifacts[3],
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH', ''), kind, pathway, cwd=tmp_path, env=env)
    assert json.loads(result.stdout.splitlines()[-1])['tools_preserved']

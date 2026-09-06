"""Real native cron firing and delegated return, in disposable local profiles."""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from conftest import run_python


CRON = r'''
import asyncio, json, os, socket, sys, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
home = Path(os.environ['HERMES_HOME'])
(home/'scripts').mkdir(parents=True)
(home/'config.yaml').write_text('{}')
script = home/'scripts'/'neutral.py'
script.write_text("from pathlib import Path\nimport time\np=Path(__file__).parent\n(p/'started').write_text('started')\ndeadline=time.monotonic()+15\nwhile not (p/'release').exists():\n if time.monotonic()>deadline: raise RuntimeError('fixture deadline')\n time.sleep(.02)\n(p/'completed').write_text('completed')\nprint('NEUTRAL_LOCAL_CRON_OK')\n")
def no_network(*a, **k): raise AssertionError('Cron qualification must remain local')
socket.socket.connect = no_network
socket.create_connection = no_network
from cron.jobs import create_job, update_job
from cron.scheduler import tick
job = create_job(prompt=None, schedule='every 1h', name='Neutral script qualification',
    repeat=1, deliver='local', script='neutral.py', no_agent=True)
update_job(job['id'], {'next_run_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
errors=[]
def fire():
    try: tick(verbose=False, sync=True)
    except BaseException as e: errors.append(type(e).__name__)
thread=threading.Thread(target=fire)
thread.start()
try:
    deadline=time.monotonic()+15
    while not (home/'scripts'/'started').exists() and thread.is_alive() and time.monotonic()<deadline:
        time.sleep(.02)
    assert (home/'scripts'/'started').exists(), errors
    from colony_sidecar.turns.hermes_work import cron_view
    from colony_sidecar.turns.executions import registry, format_view
    view=registry().view(contact_id='fixture-owner', owner=True)
    view['native_cron']=cron_view()
    active=view['native_cron']['items']
    assert len(active)==1 and active[0]['job_id']==job['id'], view
    assert active[0]['status']=='running' and active[0]['source']=='builtin', active
    assert active[0]['liveness']=='unknown' and view['complete'] is False
    execution_id=active[0]['execution_id']
    assert 'Neutral script qualification' in format_view(view)
finally:
    (home/'scripts'/'release').touch()
    thread.join(timeout=20)
assert not thread.is_alive() and not errors, errors
assert (home/'scripts'/'completed').read_text()=='completed'
reopened=cron_view()
assert reopened['items']==[]
finished=next(row for row in reopened['recent'] if row['execution_id']==execution_id)
assert finished['status']=='completed', finished
assert any('NEUTRAL_LOCAL_CRON_OK' in path.read_text() for path in (home/'cron'/'output').rglob('*.md'))
assert not registry().view(contact_id='fixture-owner', owner=True)['items']
print(json.dumps({'native_fire':True,'same_execution_id':True,'native_terminal':True,'model_calls':0,'external_delivery':False}))
'''


CHILD = r'''
import json, os, queue, socket, sys, time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0,sys.argv[1])
sys.path.insert(1,sys.argv[2])
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['colony'],'colony':{
    'owner_contact_id':'fixture-owner','attested_system_platforms':['cli'],
    'execution_registry_enabled':True,'turn_outbox_path':str(home/'outbox.db')}}}))
fixture=home/'neutral.txt'; fixture.write_text('NEUTRAL_CHILD_FILE')
def no_network(*a, **kw): raise AssertionError('No network in controlled native qualification')
socket.socket.connect=no_network; socket.create_connection=no_network
from colony_sidecar.turns.executions import registry
import colony_hermes
calls=[]
class Reply:
    status_code=200
    def __init__(self,value): self.value=value
    def json(self): return self.value
    def raise_for_status(self): pass
def post(self,path,**kw):
    if path=='/v1/host/executions/observe':
        value=kw['json']; calls.append(value)
        return Reply(registry().observe(value,principal_id='native-host',contact_id=value['contact_id']))
    return Reply({})
def get(self,path,**kw):
    if path=='/v1/host/contacts/resolve': return Reply({'contact_id':'fixture-guest'})
    raise RuntimeError('No central service configured')
colony_hermes.ColonyClient.post=post; colony_hermes.ColonyClient.get=get
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['colony'].enabled
assert Path(colony_hermes.__file__).resolve().is_relative_to(Path(sys.argv[1]))
from run_agent import AIAgent
from tools.delegate_tool import _build_child_agent
from model_tools import handle_function_call
from hermes_cli.lifecycle import invoke_hook
def reply(content,tool=None):
    return NS(choices=[NS(message=NS(content=content,tool_calls=[tool] if tool else None),
        finish_reason='tool_calls' if tool else 'stop')],model='fixture/model',usage=None)
def tool(name,args):
    return NS(id='call-'+name,type='function',function=NS(name=name,arguments=json.dumps(args)))
defs=[{'type':'function','function':{'name':name,'description':name,'parameters':{'type':'object','properties':{}}}}
    for name in ('delegate_task','read_file')]
def make_agent(platform='cli'):
    agent=AIAgent(api_key='fixture-key',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',max_iterations=4,quiet_mode=True,skip_context_files=True,
        skip_memory=True,platform=platform,enabled_toolsets=['file','delegation'])
    agent._cached_system_prompt='Neutral qualification.'
    agent._use_prompt_caching=False; agent.compression_enabled=False; agent.save_trajectories=False
    return agent
parent_client=MagicMock(); child_client=MagicMock()
parent_client.chat.completions.create.side_effect=[reply('',tool('delegate_task',{'goal':'Read the neutral fixture '+str(fixture)})),reply('PARENT_DISPATCHED_CHILD'),reply('PARENT_ACCEPTED_CHILD')]
child_client.chat.completions.create.side_effect=[reply('',tool('read_file',{'path':str(fixture)})),reply('CHILD_READ_NEUTRAL_FILE')]
with patch('run_agent.OpenAI',side_effect=[parent_client,child_client]), patch('run_agent.get_tool_definitions',return_value=defs), patch('run_agent.check_toolset_requirements',return_value={}):
    parent=make_agent()
    outcome=parent.run_conversation('Delegate the neutral local read.',task_id='fixture-parent')
    assert outcome['final_response']=='PARENT_DISPATCHED_CHILD', outcome
    dispatch=json.loads(next(m['content'] for m in outcome['messages'] if m.get('role')=='tool'))
    assert dispatch['status']=='dispatched', dispatch
    from tools.async_delegation import get_durable_delegation
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        completed=get_durable_delegation(dispatch['delegation_id'])
        if completed and completed['state']!='running': break
        time.sleep(.02)
    assert completed and completed['state']=='completed', completed
    assert child_client.chat.completions.create.call_count==2
    child_tool_results=' '.join(str(m.get('content')) for m in child_client.chat.completions.create.call_args_list[-1].kwargs['messages'] if m.get('role')=='tool')
    assert 'NEUTRAL_CHILD_FILE' in child_tool_results, child_tool_results
    from cli import HermesCLI
    cli=HermesCLI.__new__(HermesCLI); cli._session_db=None; cli._pending_input=queue.Queue()
    cli.session_id='different-native-session'
    cli._drain_process_notifications('fixture-other-session')
    assert cli._pending_input.empty()
    cli.session_id=parent.session_id
    while cli._pending_input.empty() and time.monotonic()<deadline:
        cli._drain_process_notifications('fixture-parent-session'); time.sleep(.02)
    returned=cli._pending_input.get_nowait()
    assert 'CHILD_READ_NEUTRAL_FILE' in returned, returned
    assert get_durable_delegation(dispatch['delegation_id'])['delivery_state']=='delivered'
    resumed=parent.run_conversation(returned,conversation_history=outcome['messages'],task_id='fixture-parent-return')
    assert resumed['final_response']=='PARENT_ACCEPTED_CHILD', resumed
    parent.close()
children=[row for row in calls if row['platform']=='subagent']
assert children and children[-1]['state']=='completed', calls
assert all(row['contact_id']=='fixture-owner' for row in children)
assert children[0]['parent_execution_id'] and any(row['execution_id']==children[0]['parent_execution_id'] for row in calls)
assert not registry().view(contact_id='fixture-owner',owner=True)['items'], calls
# Even if a host constructs a child directly, its native conversation must
# inherit the guest binding and never acquire CLI owner's file authority.
guest_client=MagicMock(); denied_child_client=MagicMock()
denied_child_client.chat.completions.create.side_effect=[reply('',tool('read_file',{'path':str(fixture)})),reply('GUEST_CHILD_DONE')]
with patch('run_agent.OpenAI',side_effect=[guest_client,denied_child_client]), patch('run_agent.get_tool_definitions',return_value=defs), patch('run_agent.check_toolset_requirements',return_value={}):
    guest=make_agent('sms'); guest._current_turn_id='guest-parent-turn'
    invoke_hook('pre_llm_call',session_id=guest.session_id,task_id='guest-parent',turn_id=guest._current_turn_id,
        platform='sms',sender_id='fixture-guest-address',user_message='Neutral guest task')
    child=_build_child_agent(0,'Read fixture',None,None,None,3,1,guest)
    outcome=child.run_conversation('Read the neutral fixture '+str(fixture),task_id='fixture-guest-child')
    results=' '.join(str(m.get('content')) for m in outcome['messages'] if m.get('role')=='tool')
    assert 'requires_authorization' in results and 'NEUTRAL_CHILD_FILE' not in results, results
    assert [row for row in calls if row['session_id']==child.session_id][-1]['contact_id']=='fixture-guest'
    child.close(); guest.close()
print(json.dumps({'native_delegation_return':True,'owner_child_completed':True,'guest_child_scope_preserved':True,'controlled_inference':True}))
'''


def environment(tmp_path):
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'),COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled', COLONY_GUARD_CHAT_MODE='off')
    return env


def test_actual_native_no_agent_cron_fire_reaches_owner_current_work(tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual native firing')
    result=run_python('-I','-c',CRON,Path(__file__).resolve().parents[2]/'sidecar',cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['native_fire']


def test_actual_native_delegated_return_and_inherited_scope(artifacts,tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual child execution')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',CHILD,installed,Path(__file__).resolve().parents[2]/'sidecar',cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['native_delegation_return']

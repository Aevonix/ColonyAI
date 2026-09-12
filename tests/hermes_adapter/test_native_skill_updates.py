"""Skill changes reach an existing native conversation without rewriting it."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python
from test_native_current_work import environment


PROBE = r'''
import copy,hashlib,json,os,socket,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['apsimo'],'apsimo':{
    'owner_contact_id':'fixture-owner','attested_system_platforms':['cli'],
    'turn_outbox_path':str(home/'outbox.db')}}}))
def no_network(*args,**kwargs):raise AssertionError('Skill update fixture is local')
socket.socket.connect=no_network;socket.create_connection=no_network
import apsimo_hermes
class Reply:
    status_code=200
    def json(self):return {}
    def raise_for_status(self):pass
apsimo_hermes.ColonyClient.post=lambda *args,**kwargs:Reply()
apsimo_hermes.ColonyClient.get=lambda *args,**kwargs:Reply()
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['apsimo'].enabled
from hermes_state import SessionDB
from run_agent import AIAgent
import run_agent
from tools.skills_tool import skills_list
from agent.prompt_builder import build_skills_system_prompt

name='apsimo-cache-fixture';removed='apsimo-retired-fixture'
def skill_text(version):
    return ('---\nname: '+name+'\ndescription: Index manuals using revision '+version+'.\n---\n'
        'Use the '+version+' tab for each manual.\n')
current=home/'skills'/name/'SKILL.md';current.parent.mkdir(parents=True)
current.write_text(skill_text('amber'))
retired=home/'skills'/removed/'SKILL.md';retired.parent.mkdir()
retired.write_text('---\nname: '+removed+'\ndescription: Index obsolete manuals with paper tabs.\n---\n'
    'Use the retired paper-tab procedure.\n')
for path in (current,retired):
    (path.parent/'.apsimo-owned.json').write_text(json.dumps({'owner':'apsimo-hermes',
        'version':1,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
def change(version):
    before=current.stat()
    current.write_text(skill_text(version))
    # Exercise in-place updates without relying on sleeps or directory mtimes.
    os.utime(current,ns=(before.st_atime_ns,before.st_mtime_ns+1_000_000))

# Warm the exact native discovery and prompt caches before the first turn.
assert name in json.dumps(json.loads(skills_list()))
assert name in build_skills_system_prompt(available_tools={'skills_list','skill_view'},
    skills_dir_override=home/'skills')
db=SessionDB(home/'fixture-state.db');session='fixture-saved-skills'
requests=[];phase='initial';phase_requests=[]
expected_version='amber';expect_removed=False
target='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
def tool(tool_name,args,identifier):
    return NS(id=identifier,type='function',function=NS(name=tool_name,arguments=json.dumps(args)))
def completion(**kwargs):
    messages=copy.deepcopy(kwargs['messages'])
    requests.append(messages);phase_requests.append(messages)
    if len(phase_requests)==1:
        # Only inspect the current user-turn tail: historical tool results and
        # the saved system prompt must remain historical, not be rewritten.
        last_user=max(i for i,row in enumerate(messages) if row['role']=='user')
        fresh=json.dumps(messages[last_user:])
        if phase!='initial':
            assert 'Index manuals using revision '+expected_version+'.' in fresh,(phase,fresh)
        assert 'Use the '+expected_version+' tab for each manual.' not in fresh,(phase,fresh)
        calls=[tool('skills_list',{},phase+'-list'),tool('skill_view',{'name':name},phase+'-view')]
        if phase in {'initial','resumed'}:
            calls.append(tool('skill_view',{'name':removed},phase+'-removed'))
        message=NS(content=None,tool_calls=calls);reason='tool_calls'
    else:
        assert len(phase_requests)==2,'Unexpected extra provider request'
        by_id={row.get('tool_call_id'):json.loads(row['content']) for row in messages
            if row.get('role')=='tool' and row.get('tool_call_id','').startswith(phase+'-')}
        listed=by_id[phase+'-list'];viewed=by_id[phase+'-view']
        assert any(row['name']==name and row['description']=='Index manuals using revision '+expected_version+'.'
            for row in listed['skills']),(phase,listed)
        assert viewed.get('success') and viewed.get('content')==skill_text(expected_version),(phase,viewed)
        if expect_removed:
            assert not any(row['name']==removed for row in listed['skills']),listed
            missing=by_id[phase+'-removed']
            assert not missing.get('success') and not missing.get('content'),missing
        message=NS(content='FIXTURE_'+phase.upper()+'_DONE',tool_calls=None);reason='stop'
    return NS(choices=[NS(message=message,finish_reason=reason)],model='fixture/model',usage=None)
client=MagicMock();client.chat.completions.create.side_effect=completion
def make_agent():
    agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',session_id=session,session_db=db,max_iterations=3,
        quiet_mode=True,skip_context_files=True,skip_memory=True,skip_background_review=True,
        platform='cli',enabled_toolsets=['skills'])
    agent._use_prompt_caching=False;agent.compression_enabled=False;agent.save_trajectories=False
    agent._end_session_on_close=False
    return agent
def run(agent,history=None):
    result=agent.run_conversation('Use the current manual indexing procedure.',
        conversation_history=history,task_id='fixture-skills-task')
    assert result['final_response']=='FIXTURE_'+phase.upper()+'_DONE',result
    return result
def saved_rows():return copy.deepcopy(db.get_messages(session))
def old_rows_unchanged(before):
    after={row['id']:row for row in db.get_messages(session)}
    assert all(after.get(row['id'])==row for row in before),'Historical messages were rewritten'
with patch(target,return_value=client):
    agent=make_agent()
    initial=run(agent)
    saved_prompt=db.get_session(session)['system_prompt']
    assert saved_prompt and 'revision amber' in saved_prompt
    rows=saved_rows();assert rows
    change('cobalt');phase='updated';phase_requests=[];expected_version='cobalt'
    updated=run(agent,initial['messages'])
    old_rows_unchanged(rows)
    # Reconstruct the native agent while keeping the running process and
    # its caches. Resume actual saved messages and the persisted prompt.
    agent.close();rows=saved_rows()
    change('silver');retired.unlink()
    phase='resumed';phase_requests=[];expected_version='silver';expect_removed=True
    restored=db.get_messages_as_conversation(session)
    assert restored and 'revision amber' in db.get_session(session)['system_prompt']
    agent=make_agent();run(agent,restored)
    old_rows_unchanged(rows)
    assert db.get_session(session)['system_prompt']==saved_prompt
    agent.close()
db.close()
print(json.dumps({'same_process_skill_edit':True,'saved_conversation_skill_edit':True,
    'removed_skill_unavailable':True,'current_description_visible':True,
    'current_body_loaded_on_use':True,'historical_rows_unchanged':True,
    'full_body_not_preloaded':True,'controlled_sdk_requests':len(requests),'model_calls':0,'network':0}))
'''


def test_native_skill_updates_reach_running_and_saved_conversations(artifacts, tmp_path):
    native = os.environ.get('COLONY_TEST_HERMES_PATH') or os.environ.get('HERMES_TEST_SOURCE', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill update integration')
    env = environment(tmp_path)
    env.update(COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3], ROOT/'sidecar', native,
        cwd=tmp_path, env=env)
    assert '"saved_conversation_skill_edit": true' in result.stdout
    assert '"historical_rows_unchanged": true' in result.stdout

"""Real stable post-turn review, skill mutation ledger and inherited authority."""
import importlib.util
import json
from pathlib import Path

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import json,os,socket,sys,threading,time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1])
guest=sys.argv[2]=='guest'
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['colony'],'colony':{
    'owner_contact_id':'fixture-owner','attested_system_platforms':['cli'],
    'execution_registry_enabled':True,'turn_outbox_path':str(home/'outbox.db')}},
    'skills':{'creation_nudge_interval':1,'write_approval':False}}))
fixture=home/'neutral.txt'; fixture.write_text('NEUTRAL_READ_RESULT')
def no_network(*a,**kw): raise AssertionError('Controlled review must stay offline')
socket.socket.connect=no_network; socket.create_connection=no_network
import colony_hermes
observations=[]
class Reply:
    status_code=200
    def __init__(self,value): self.value=value
    def json(self): return self.value
    def raise_for_status(self): pass
def post(self,path,**kw):
    if path=='/v1/host/executions/observe': observations.append(kw['json'])
    return Reply({})
def get(self,path,**kw):
    if path=='/v1/host/contacts/resolve': return Reply({'contact_id':'fixture-guest'})
    raise RuntimeError('No central source service in this isolated qualification')
colony_hermes.ColonyClient.post=post; colony_hermes.ColonyClient.get=get
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert Path(colony_hermes.__file__).resolve().is_relative_to(Path(sys.argv[1]))
from run_agent import AIAgent
from hermes_cli.lifecycle import invoke_hook
from agent import background_review
def reply(content,call=None):
    return NS(choices=[NS(message=NS(content=content,tool_calls=[call] if call else None),
        finish_reason='tool_calls' if call else 'stop')],model='fixture/model',usage=None)
def tool(name,args):
    return NS(id='call-'+name,type='function',function=NS(name=name,arguments=json.dumps(args)))
skill_name='neutral-path-recovery'
content='---\nname: '+skill_name+'\ndescription: Read a supplied neutral fixture path.\n---\nUse the current supplied path; do not reuse a stale path.\n'
defs=[{'type':'function','function':{'name':name,'description':name,'parameters':{'type':'object','properties':{}}}}
    for name in ('read_file','skills_list','skill_view','skill_manage')]
parent_client=MagicMock(); review_client=MagicMock()
parent_client.chat.completions.create.side_effect=[reply('',tool('read_file',{'path':str(fixture)})),reply('PARENT_FINISHED')]
review_client.chat.completions.create.side_effect=[reply('',tool('skill_manage',{'action':'create','name':skill_name,'content':content})),reply('REVIEW_FINISHED')]
entered=threading.Event(); release=threading.Event()
errors=[]
real_builder=background_review.build_cache_parity_fork
def delayed_builder(*a,**kw):
    entered.set()
    assert release.wait(10), 'Review release deadline'
    return real_builder(*a,**kw)
with patch('run_agent.OpenAI',side_effect=[parent_client,review_client]), patch('run_agent.get_tool_definitions',return_value=defs), patch('run_agent.check_toolset_requirements',return_value={}), patch.object(background_review,'build_cache_parity_fork',side_effect=delayed_builder):
    parent=AIAgent(api_key='fixture-key',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',max_iterations=4,quiet_mode=True,skip_context_files=True,
        skip_memory=True,platform='sms' if guest else 'cli',enabled_toolsets=['file','skills'])
    parent._user_id='fixture-guest-address' if guest else ''
    parent._cached_system_prompt='Read the neutral fixture and finish.'
    parent._use_prompt_caching=False; parent.compression_enabled=False; parent.save_trajectories=False
    parent._skill_nudge_interval=1
    parent._emit_auxiliary_failure=lambda *args:errors.append([str(arg) for arg in args])
    result=parent.run_conversation('Read the neutral fixture.',task_id='fixture-parent')
    assert result['final_response']=='PARENT_FINISHED',result
    assert entered.wait(10), 'Native post-turn review never spawned'
    if guest:
        # A new attested owner turn takes over this same session after the
        # native fork captured the guest's ContextVars. It must remain guest.
        invoke_hook('pre_llm_call',session_id=parent.session_id,task_id='later-owner',turn_id='later-owner-turn',
            platform='cli',sender_id='',user_message='Later owner turn')
    release.set()
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        reviews=[row for row in observations if row['platform']=='background_review' and row['state']=='completed']
        if reviews and getattr(parent,'_background_review_run',None) is None: break
        time.sleep(.02)
    assert reviews,{'observations':observations,'errors':errors,'review_calls':review_client.chat.completions.create.call_count}
    assert review_client.chat.completions.create.call_count==2
    returned=' '.join(str(m.get('content')) for m in review_client.chat.completions.create.call_args_list[-1].kwargs['messages'] if m.get('role')=='tool')
    expected='fixture-guest' if guest else 'fixture-owner'
    assert all(row['contact_id']==expected for row in reviews),reviews
    assert all(row['parent_execution_id'] and row['execution_id']!=row['parent_execution_id'] for row in reviews)
    current=colony_hermes._TRANSPORT_SCOPES.for_session(parent.session_id)
    assert current.platform!='background_review',current
    if guest:
        assert 'requires_authorization' in returned and current.contact_id=='fixture-owner',returned
        assert not list((home/'skills').rglob(skill_name+'/SKILL.md'))
    else:
        assert '"success": true' in returned and '"staged": true' in returned,returned
        from tools import skill_ledger,write_approval,skill_manager_tool,skill_provenance
        assert not list((home/'skills').rglob(skill_name+'/SKILL.md'))
        pending=write_approval.list_pending(write_approval.SKILLS)
        assert len(pending)==1 and pending[0]['origin']=='background_review',pending
        assert pending[0]['payload']['name']==skill_name and pending[0]['payload']['content']==content
        # This explicit operator step qualifies native apply/rollback plumbing;
        # model self-reported completion does not authorize activation.
        token=skill_provenance.set_current_write_origin('background_review')
        try:
            applied=json.loads(skill_manager_tool.apply_skill_pending(pending[0]['payload']))
        finally:
            skill_provenance.reset_current_write_origin(token)
        assert applied['success'],applied
        assert write_approval.discard_pending(write_approval.SKILLS,pending[0]['id'])
        entries=skill_ledger.list_entries(limit=50)
        created=next(row for row in entries if row['skill']==skill_name and row['action']=='create')
        assert created['actor']=='curator',created
        saved=next((home/'skills').rglob(skill_name+'/SKILL.md'))
        assert 'current supplied path' in saved.read_text()
        ok,message=skill_ledger.rollback_entry(created['id'])
        assert ok,message
        assert not saved.exists()
        assert any(row['action']=='rollback' for row in skill_ledger.list_entries(limit=50))
    rows=colony_hermes.TurnOutbox(str(home/'outbox.db')).snapshot()
    assert len(rows)==1,rows
    assert rows[0]['payload']['user_message']=='Read the neutral fixture.'
    assert rows[0]['payload']['contact_id']==expected
    parent.close()
print(json.dumps({'native_automatic_review':True,'guest':guest,'review_harness_not_ingested':True,'native_ledger_rollback':not guest}))
'''


@pytest.mark.parametrize('participant',['owner','guest'])
def test_native_post_turn_review_keeps_exact_scope_and_native_skill_history(artifacts,tmp_path,participant):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual post-turn review')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',PROBE,installed,participant,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['native_automatic_review']

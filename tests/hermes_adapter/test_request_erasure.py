"""Actual native resume sends a filtered request using its built-in compressor."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, copy, json, os, socket, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from apsimo.api.routers import host
from apsimo.turns import get_turn_idempotency_ledger
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
identity='Neutral identity documents <memory-context> as the name of a recalled block. Preserve the full identity tail.'
ephemeral='Preserve this native deployment instruction after the identity.'
home.joinpath('SOUL.md').write_text(identity)
home.joinpath('config.yaml').write_text(json.dumps({
    'context': {'engine': 'compressor'},
    'plugins': {'enabled': ['apsimo'], 'apsimo': {'owner_contact_id': 'contact-a', 'url': 'http://fixture'}},
    'memory': {'provider': 'apsimo-memory', 'config': {'contact_id': 'contact-a', 'url': 'http://fixture'}}}))
app=FastAPI(); app.include_router(host.router); app.include_router(host.v2_router)
api=TestClient(app)
fact='My neutral orchard badge is cobalt-716.'
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
ledger.record_source('native-erasure-source', contact_id='contact-a', session_id='original',
    messages=[{'role':'user','content':fact}], derive_claims=False)
original_ref=ledger.source_references(['native-erasure-source'],contact_id='contact-a',session_id='original')[0]
wire=[]
summary_mode = len(sys.argv) > 4 and sys.argv[4] == 'summary'
memory_checks=[]
from apsimo_hermes.request_memory import RequestMemory
original_memory_check=RequestMemory.__call__
def record_memory_check(self,*args,**kwargs):
    memory_checks.append(True)
    return original_memory_check(self,*args,**kwargs)
RequestMemory.__call__=record_memory_check
original_client=httpx.Client
def respond(request):
    if request.url.path == '/v1/host/mind/facts':
        return httpx.Response(200,json={'facts':[]})
    response=api.request(request.method, request.url.path, params=request.url.params,
                         headers=dict(request.headers), content=request.content)
    wire.append((request.url.path, response.status_code))
    return httpx.Response(response.status_code, content=response.content, headers=response.headers)
httpx.Client=lambda **kw: original_client(**{**kw, 'transport':httpx.MockTransport(respond)})
def no_network(*a, **kw): raise AssertionError('Native erasure qualification is local')
socket.socket.connect=no_network; socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
plugins=get_plugin_manager(); plugins.discover_and_load()
assert plugins._plugins['apsimo'].enabled
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
from agent.turn_context import compose_user_api_content, append_notes_to_multimodal_content
from hermes_state import SessionDB
provider=load_memory_provider('apsimo-memory'); manager=MemoryManager(); manager.add_provider(provider)
manager.initialize_all('original', hermes_home=str(home))
# Native memory-file changes still commit locally and notify the real provider,
# but do not fabricate canonical owner testimony or call a graph write route.
from tools.memory_tool import MemoryStore, memory_tool
file_store=MemoryStore()
file_store.load_from_disk()
before_file_edit_calls=len(wire)
with patch.object(provider,'on_memory_write',wraps=provider.on_memory_write) as notified:
    for operation in [
        {'action':'add','target':'memory','content':'Assistant interpretation of an example.'},
        {'action':'replace','target':'memory','old_text':'Assistant interpretation of an example.',
         'content':'Revised assistant interpretation of an example.'},
    ]:
        file_result=memory_tool(**operation,store=file_store)
        assert json.loads(file_result)['success'] is True,file_result
        manager.notify_memory_tool_write(file_result,operation)
    assert notified.call_count==2
assert len(wire)==before_file_edit_calls
with ledger._connect() as connection:
    assert connection.execute('SELECT count(*) FROM turn_sources').fetchone()[0]==1
recalled=provider.prefetch('orchard badge', session_id='original')
assert fact in recalled and 'native-erasure-source' in recalled and 'colony-recall-v1' in recalled, recalled
temporal_only='[colony-recall-v1 {"contact_id":"contact-a","watermark":0}]\n## Current Time [priority 100]\nOld clock\n[/colony-recall-v1]'
refreshed=provider._with_fresh_temporal_sync(temporal_only, contact_id='contact-a')
assert 'Old clock' not in refreshed and '[/colony-recall-v1]' in refreshed
injected=compose_user_api_content('What is my orchard badge?', recalled, '')
assert fact in injected
db=SessionDB(home/'fixture-state.db')
db.create_session('original', 'cli')
db.append_message('original','user', fact, api_content=compose_user_api_content(fact, recalled, 'Neutral plugin clock note'))
# Neutral reproduction of a real historical cutoff calculation: the tool
# arguments and result are derived context, not canonical message hashes.
cutoff_command = "date -u; echo \"---\"; date -u -j -f '%Y-%m-%d %H:%M:%S' '2026-09-18 17:15:00' +%s; echo \"---\"; echo $(( 1789751700 - 1789092491 ))"
cutoff_result = json.dumps({'output': 'Fri Sep 11 02:08:22 UTC 2026\n---\n1789751700\n---\n659209', 'exit_code': 0, 'error': None})
db.append_message('original','assistant','', tool_calls=[{'id':'historical-cutoff','type':'function',
    'function':{'name':'terminal','arguments':json.dumps({'command':cutoff_command})}}])
db.append_message('original','tool',cutoff_result,tool_call_id='historical-cutoff',tool_name='terminal')
db.append_message('original','assistant','Stored neutral response')
db.append_message('original','user','What is my orchard badge?', api_content=injected)
db.append_message('original','assistant','Here is the recalled badge.')
before=db.get_messages_as_conversation('original')
assert fact in json.dumps(before)
from run_agent import AIAgent
import run_agent
# 0.21.0 binds eager aliases; 0.21.1 calls the defining modules directly.
OPENAI_TARGET = 'run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
TOOLS_TARGET = 'run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
from agent.context_compressor import ContextCompressor
client=MagicMock()
responses=[
    NS(choices=[NS(message=NS(content='BEFORE_OK',tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None),
    NS(choices=[NS(message=NS(content='AFTER_OK',tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None),
    NS(choices=[NS(message=NS(content='RETOLD_OK',tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None),
]
if summary_mode:
    tool_response = NS(choices=[NS(message=NS(content='',tool_calls=[
        NS(id='neutral-lookup',type='function',function=NS(name='colony_get_facts',arguments='{}'))]),
        finish_reason='tool_calls')],model='fixture/model',usage=None)
    responses.insert(1, tool_response)
    responses.insert(-1, tool_response)
client.chat.completions.create.side_effect=responses
with patch(OPENAI_TARGET,return_value=client), patch(TOOLS_TARGET + '.get_tool_definitions',return_value=[]), patch(TOOLS_TARGET + '.check_toolset_requirements',return_value={}):
    agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',quiet_mode=True,skip_context_files=True,skip_memory=False,platform='cli',max_iterations=2,
        load_soul_identity=True,ephemeral_system_prompt=ephemeral)
    assert isinstance(agent.context_compressor, ContextCompressor)
    agent._use_prompt_caching=False; agent.save_trajectories=False
    question='What is my orchard badge in the retained neutral source?'
    # Observe the real automatic turn prefetch; do not manually supply its
    # answer or mistake historical api_content for current recall consumption.
    with patch.object(agent._memory_manager,'prefetch_all',wraps=agent._memory_manager.prefetch_all) as automatic:
        result=agent.run_conversation(question, conversation_history=copy.deepcopy(before), task_id='erasure-before')
    automatic.assert_called_once_with(question,session_id=agent.session_id)
    assert result['final_response']=='BEFORE_OK', result
    assert len(memory_checks)==1, 'Ordinary request erasure must not run twice'
    supplied=client.chat.completions.create.call_args_list[0].kwargs['messages']
    system='\n'.join(row['content'] for row in supplied if row.get('role')=='system')
    assert identity in system and ephemeral in system,system
    current=next(row for row in reversed(supplied) if row.get('role')=='user')
    assert question in current['content'] and fact in current['content'],(wire,supplied)
    # Actual Hermes composes an authoritative-memory note outside the provider.
    # The supported Colony request middleware preserves evidence and lineage
    # while correcting that note before the native client receives it.
    assert 'Treat as authoritative reference data' not in current['content'],current
    assert 'fictional, hypothetical or reported scope' in current['content'],current
    assert 'Use a claim as a real-world fact only when its source supports' in current['content'],current
    packets=[line for row in supplied for line in str(row.get('content','')).splitlines()
             if line.startswith('[colony-recall-v1 ')]
    assert len(packets)==1,packets
    stamp=json.loads(packets[0][len('[colony-recall-v1 '):-1])
    assert stamp['contact_id']=='contact-a' and stamp['sources']==[original_ref],stamp
    # Check canonical answer lineage actually emitted by the native post hook.
    with ledger._connect() as connection:
        answers=[(row['turn_id'],message) for row in connection.execute(
            'SELECT turn_id,messages_json FROM turn_sources WHERE contact_id=?',('contact-a',))
            for message in json.loads(row['messages_json'])
            if message.get('role')=='assistant' and message.get('content')=='BEFORE_OK']
    assert len(answers)==1 and answers[0][1].get('_supplied_sources')==[original_ref],answers
    derived_id=answers[0][0]
    from hermes_cli.lifecycle import invoke_hook
    from model_tools import handle_function_call
    invoke_hook('pre_llm_call', session_id='forget-request', task_id='forget-task', turn_id='forget-turn',
        platform='cli', sender_id='', user_message='Forget the retained orchard badge source and its answer copies.')
    forgotten=json.loads(handle_function_call('apsimo_memory_forget', {'source_ids':['native-erasure-source']},
        session_id='forget-request',task_id='forget-task',turn_id='forget-turn'))
    assert forgotten['source_erased'], forgotten
    assert forgotten['source_ids'] == ['native-erasure-source']
    assert {'native-erasure-source',derived_id} <= set(forgotten['affected_source_ids']),forgotten
    with ledger._connect() as connection:
        survivor=connection.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',(derived_id,)).fetchone()
    assert survivor is not None
    retained=json.loads(survivor['messages_json'])
    assert any(row.get('role')=='user' and row.get('content')==question for row in retained),retained
    assert all(row.get('role')!='assistant' for row in retained),retained
    repeat=json.loads(handle_function_call('apsimo_memory_forget', {'source_ids':['native-erasure-source']},
        session_id='forget-request',task_id='forget-task',turn_id='forget-turn'))
    assert repeat['source_erased'], repeat
    prior_calls=sum(path.endswith('/memory/sources/forget') for path,code in wire)
    for args, session, task, turn in [
        ({'source_ids':['native-erasure-source'],'contact_id':'someone-else'}, 'forget-request','forget-task','forget-turn'),
        ({'source_ids':['native-erasure-source']}, 'missing','missing','missing'),
    ]:
        denied=json.loads(handle_function_call('apsimo_memory_forget',args,session_id=session,task_id=task,turn_id=turn))
        assert 'error' in denied, denied
    invoke_hook('pre_llm_call',session_id='cron-forget',task_id='cron-forget',turn_id='cron-forget',
        platform='cron',sender_id='',user_message='Forget the badge')
    denied=json.loads(handle_function_call('apsimo_memory_forget', {'source_ids':['native-erasure-source']},
        session_id='cron-forget',task_id='cron-forget',turn_id='cron-forget'))
    assert 'error' in denied and sum(path.endswith('/memory/sources/forget') for path,code in wire)==prior_calls
    # Reopen native durable history, exactly as a later process resumes it.
    reopened=SessionDB(home/'fixture-state.db').get_messages_as_conversation('original')
    assert fact in json.dumps(reopened)  # documented storage limit, not hidden
    if summary_mode:
        agent.max_iterations=1
    calls_before=client.chat.completions.create.call_count
    checks_before=len(memory_checks)
    result=agent.run_conversation('Continue after forgetting.', conversation_history=reopened, task_id='erasure-after')
    assert 'AFTER_OK' in result['final_response'], result
    physical=client.chat.completions.create.call_args_list[calls_before:]
    assert len(physical)==(2 if summary_mode else 1), physical
    assert len(memory_checks)-checks_before==len(physical), 'One erasure check per actual provider attempt'
    assert all(fact not in json.dumps(call.kwargs) for call in physical), physical
    assert all('1789751700' not in json.dumps(call.kwargs)
               and '2026-09-18 17:15:00' not in json.dumps(call.kwargs)
               and 'historical-cutoff' not in json.dumps(call.kwargs)
               for call in physical), physical
    if summary_mode:
        from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
        assert any(row.get('content')==MAX_ITERATIONS_SUMMARY_REQUEST
                   for row in physical[-1].kwargs['messages'])
    sent=client.chat.completions.create.call_args_list[-1].kwargs['messages']
    assert fact not in json.dumps(sent), sent
    assert 'Continue after forgetting.' in json.dumps(sent)
    assert 'What is my orchard badge?' in json.dumps(sent)
    calls_before=client.chat.completions.create.call_count
    retold=agent.run_conversation(fact, conversation_history=db.get_messages_as_conversation('original'), task_id='erasure-retelling')
    assert ('RETOLD_OK' in retold['final_response'] if summary_mode
            else retold['final_response']=='RETOLD_OK'), retold
    retelling_calls=client.chat.completions.create.call_args_list[calls_before:]
    assert len(retelling_calls)==(2 if summary_mode else 1), retelling_calls
    for call in retelling_calls:
        sent=call.kwargs['messages']
        assert sum(fact in str(row.get('content','')) for row in sent) == 1, sent
        assert any(row.get('role')=='user' and str(row.get('content','')).startswith(fact)
                   for row in sent), sent
    agent.close()
assert any(path.endswith('/memory/sources/erasures') and code==200 for path,code in wire), wire
assert fact in json.dumps(db.get_messages_as_conversation('original'))
# Native multimodal appended context must not hide the full original source
# hash and leave the original image bytes in a future request.
image=[{'type':'text','text':'Neutral original pixels'},
       {'type':'image_url','image_url':{'url':'data:image/png;base64,neutral-fixture'}}]
ledger.record_source('native-image-source', contact_id='contact-a', session_id='original',
    messages=[{'role':'user','content':image}], derive_claims=False)
from agent.memory_manager import build_memory_context_block
enriched=copy.deepcopy(image)
assert append_notes_to_multimodal_content(enriched, build_memory_context_block(recalled))
response=api.post('/v1/host/memory/sources/forget',json={'contact_id':'contact-a','source_ids':['native-image-source']})
assert response.status_code==200
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import apply_llm_request_middleware
from agent.prompt_builder import build_skills_system_prompt
from model_tools import get_tool_definitions
# Hermes invokes every registered callback with the original request, then
# uses the last returned payload. A skill update must therefore transform the
# already checked request, never replace it with an earlier evidence copy.
skill=home/'skills'/'apsimo-erasure-fixture'/'SKILL.md'
skill.parent.mkdir(parents=True)
def write_skill(revision):
    skill.write_text('---\nname: apsimo-erasure-fixture\ndescription: Index manuals using '+revision+'.\n---\n'
                     'Use the '+revision+' tab.\n')
write_skill('amber')
frozen_skills=build_skills_system_prompt(available_tools={'skills_list','skill_view'},
    skills_dir_override=home/'skills')
assert 'Index manuals using amber.' in frozen_skills,frozen_skills
write_skill('cobalt')
skill_tools=get_tool_definitions(enabled_toolsets=['skills'],quiet_mode=True)
assert 'skill_view' in {row['function']['name'] for row in skill_tools}
invoke_hook('pre_llm_call', session_id='image-resume', task_id='image-task', turn_id='image-turn', platform='cli',
    sender_id='', user_message='Continue after image forget', conversation_history=[])
checked=apply_llm_request_middleware({'messages':[{'role':'system','content':frozen_skills},
    {'role':'user','content':enriched}],'tools':skill_tools},
    session_id='image-resume',task_id='image-task',turn_id='image-turn',platform='cli')
filtered=checked.payload
assert 'neutral-fixture' not in json.dumps(filtered) and 'Neutral original pixels' not in json.dumps(filtered)
assert 'Index manuals using cobalt.' in json.dumps(filtered),filtered
assert 'Use the cobalt tab.' not in json.dumps(filtered),filtered
assert any(row.get('reason')=='source_erasure_checked' for row in checked.trace),checked.trace
# A failed optional refresh retains the checked payload, matching native
# callback isolation without letting its exception discard source filtering.
with patch('apsimo_hermes.skill_context.SkillContext.__call__',side_effect=OSError('fixture skill read failed')) as refresh_failure:
    refresh_failed=apply_llm_request_middleware({'messages':[{'role':'system','content':frozen_skills},
        {'role':'user','content':enriched}],'tools':skill_tools},
        session_id='image-resume',task_id='image-task',turn_id='image-turn',platform='cli')
refresh_failure.assert_called_once()
assert 'neutral-fixture' not in json.dumps(refresh_failed.payload),refresh_failed.payload
assert any(row.get('reason')=='source_erasure_checked' for row in refresh_failed.trace),refresh_failed.trace
# A native worker whose supplied task input was erased must withhold tools.
# Skill refresh sees that reduced payload and cannot reopen the old toolset.
from apsimo_hermes.input_provenance import supplied_input
from apsimo_hermes.client import source_message_hash
with supplied_input(contact_id='contact-a',session_id='blocked-input',
        input_refs=[{'source_id':'native-erasure-source',
            'input_message_hash':source_message_hash('original',{'role':'user','content':fact})}],
        source_refs=[original_ref]):
    invoke_hook('pre_llm_call',session_id='blocked-input',task_id='blocked-task',turn_id='blocked-turn',
        platform='cli',sender_id='',user_message='Act on the supplied task input.',conversation_history=[])
    unavailable=apply_llm_request_middleware({'messages':[{'role':'system','content':frozen_skills},
        {'role':'user','content':'Act on the supplied task input.'}],'tools':skill_tools},
        session_id='blocked-input',task_id='blocked-task',turn_id='blocked-turn',platform='cli')
assert not unavailable.payload.get('tools'),unavailable.payload
assert 'neutral-fixture' not in json.dumps(unavailable.payload),unavailable.payload
assert any(row.get('reason')=='source_input_unavailable' for row in unavailable.trace),unavailable.trace
print(json.dumps({'native_compressor':True,'native_persist_resume':True,'before_present':True,
    'current_automatic_recall_consumed':True,'full_native_identity_and_deployment_prompt':True,'exact_answer_lineage_erased':True,
    'standard_forget':True,'resumed_request_absent':True,'native_storage_gap_explicit':True,
    'multimodal_whole_source_erased':True,'skill_update_preserves_checked_erasure':True,
    'skill_update_preserves_source_withholding':True,'explicit_retelling_preserved':True,'controlled_inference':True}))
'''


@pytest.mark.parametrize('mode', ['ordinary', 'summary'])
def test_native_persisted_recall_is_not_resent_after_forget(artifacts, tmp_path, mode):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native request qualification')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system', COLONY_GUARD_CHAT_MODE='off')
    run_python('-I','-c',PROBE, artifacts[3], ROOT/'sidecar',
               os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''), mode, cwd=tmp_path, env=env)

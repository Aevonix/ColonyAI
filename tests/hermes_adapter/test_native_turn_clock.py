"""The actual memory-provider hook produces a clock safe for later native replay."""
import importlib.util
import os

import pytest
from conftest import run_python


PROBE = r'''
import copy, json, os, socket, sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
home=Path(os.environ['HERMES_HOME']);home.mkdir(exist_ok=True)
(home/'config.yaml').write_text('plugins: {enabled: []}\nmemory: {provider: pacomind-memory}\n')
def no_network(*args,**kwargs):raise AssertionError('Clock annotation must be local')
socket.socket.connect=no_network;socket.create_connection=no_network
from plugins.memory import load_memory_provider
from hermes_cli.plugins import get_plugin_manager, invoke_hook
from agent.turn_context import compose_user_api_content, substitute_api_content
from hermes_state import SessionDB
provider=load_memory_provider('pacomind-memory',register_skills=False)
assert provider is not None
assert len(get_plugin_manager()._hooks.get('pre_llm_call',[]))==1
clocks=iter(('Monday, April 01, 2030, 9:00 AM UTC',
             'Tuesday, April 02, 2030, 10:00 AM UTC'))
provider._current_time_line=lambda:next(clocks)
db=SessionDB(home/'state.db')
db.create_session('clock-history','cli')
literal='The quoted note says: CURRENT DATE & TIME, right now: Sunday. This is TODAY.'
questions=(literal+' Inspect the archive file.', 'What did you inspect in this turn?')
notes=[]
for index,question in enumerate(questions):
 result=invoke_hook('pre_llm_call',session_id='clock-history',task_id='clock-task',
     turn_id='turn-'+str(index),platform='cli',sender_id='',user_message=question)
 contexts=[item['context'] for item in result if isinstance(item,dict) and item.get('context')]
 assert len(contexts)==1,result
 note=contexts[0];notes.append(note)
 assert 'Clock captured for this user turn:' in note,note
 assert 'on later turns it is historical' in note,note
 assert 'Tool results from earlier turns record earlier observations' in note,note
 assert 'right now' not in note and 'This is TODAY' not in note,note
 db.append_message('clock-history','user',question,
     api_content=compose_user_api_content(question,'',note))
 if index==0:
  db.append_message('clock-history','assistant','',tool_calls=[{
      'id':'old-read','type':'function','function':{'name':'read_file',
      'arguments':'{"path":"archive.txt"}'}}])
  db.append_message('clock-history','tool','Historical file contents: shelf is cedar.',
      tool_call_id='old-read',tool_name='read_file')
  db.append_message('clock-history','assistant','The earlier file read reported cedar.')
first=db.get_messages_as_conversation('clock-history',repair_alternation=True)
before=copy.deepcopy(first)
wire=copy.deepcopy(first)
for row in wire:substitute_api_content(row)
assert first==before
assert db.get_messages_as_conversation('clock-history',repair_alternation=True)==before
users=[row for row in wire if row['role']=='user']
assert len(users)==2 and users[0]['content']==questions[0]+'\n\n'+notes[0]
assert users[1]['content']==questions[1]+'\n\n'+notes[1]
assert 'April 01, 2030' in users[0]['content'] and 'April 02, 2030' in users[1]['content']
assert users[0]['content'].count(literal)==1
old_tools=[row for row in wire if row['role']=='tool']
assert len(old_tools)==1 and old_tools[0]['content']=='Historical file contents: shelf is cedar.'
assert wire[-1]['role']=='user'  # no source read or model call occurred on turn two
provider._current_time_line=lambda:'Wednesday, April 03, 2030, 11:00 AM UTC'
compat=provider.inject_current_time([{'role':'user','content':literal}])
assert compat[0]['content']==provider._turn_clock_context()
assert compat[1]=={'role':'user','content':literal}
provider._current_time_line=lambda:''
assert provider.inject_current_time([{'role':'user','content':literal}])==[{'role':'user','content':literal}]
db.close()
print(json.dumps({'native_provider_hook':True,'two_turn_api_replay':True,
    'history_and_quoted_user_text_unchanged':True,'old_tool_result_retained':True,
    'clock_scoped_to_owning_turn':True,'model_calls':0,'network':0}))
'''


def test_native_clock_annotation_remains_historical_on_later_turns(artifacts, tmp_path):
    if not os.environ.get('AEVA_HERMES_TEST_PYTHON') and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'), PACOMIND_HERMES_HOME=str(tmp_path/'hermes'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        PACOMIND_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), cwd=tmp_path, env=env)
    assert '"two_turn_api_replay": true' in result.stdout

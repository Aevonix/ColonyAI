"""Native provider lifecycle and API content with a controlled local clock."""
import importlib.util
import os

import pytest
from conftest import run_python


PROBE = r'''
import json, os, socket, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import httpx
from agent.memory_manager import MemoryManager
from agent.turn_context import (
 _memory_turn_start_and_prefetch, compose_user_api_content, substitute_api_content)
from hermes_cli.cli_commands_mixin import _sync_agent_to_session
import colony_memory.provider as provider_module
home=Path(os.environ['HERMES_HOME']);home.mkdir(exist_ok=True)
(home/'config.yaml').write_text('plugins: {enabled: []}\n')
now=[100.0];requests=[]
provider_module._ttime=SimpleNamespace(time=lambda:now[0],monotonic=lambda:now[0])
def respond(self,request):
 assert request.url.host=='temporal.fixture',str(request.url)
 requests.append(request.url.path)
 if request.url.path=='/v1/host/context/temporal':
  return httpx.Response(200,json={'title':'Current Time','body':'Contact clock.'})
 if request.url.path=='/v1/host/context/assemble':
  return httpx.Response(200,json={'sections':[{
   'id':'memory','title':'Relevant Memories','priority':80,'body':'Archive shelf is cedar.'}]})
 raise AssertionError(str(request.url))
httpx.HTTPTransport.handle_request=respond
def no_network(*args,**kwargs):raise AssertionError('No network in temporal lifecycle qualification')
socket.socket.connect=no_network;socket.create_connection=no_network
provider=provider_module.ColonyMemoryProvider(config={
 'url':'http://temporal.fixture','api_key':'fixture-key','contact_id':'owner','turn_writer':'disabled'})
manager=MemoryManager();manager.add_provider(provider)
manager.initialize_all('conversation-one',platform='cli')
agent=SimpleNamespace(_memory_manager=manager,session_id='conversation-one',
 _user_turn_count=0,reset_session_state=lambda:None)
def render(at):
 now[0]=at;agent._user_turn_count+=1
 query='Review the archive location for this request.'
 context=_memory_turn_start_and_prefetch(agent,query)
 row={'role':'user','content':query,'api_content':compose_user_api_content(query,context,'')}
 substitute_api_content(row)
 assert 'Archive shelf is cedar.' in row['content'],row
 assert row['content'].startswith(query),row
 return row['content']
assert 'Previous message in this conversation:' not in render(100)
assert 'Previous message in this conversation: 1h 00m ago.' in render(3700)
rapid=render(3707)
assert rapid.count('Previous message in this conversation:')==1,rapid
assert 'Previous message in this conversation: 7s ago.' in rapid and '1h 00m' not in rapid,rapid
assert requests.count('/v1/host/context/temporal')==2,requests
# The selected native compressor sends this exact lifecycle dispatch for a
# changed physical ID. The MemoryManager and provider remain the same objects.
manager.on_session_switch('compressed',parent_session_id=agent.session_id,reset=False,reason='compression')
agent.session_id='compressed'
assert 'Previous message in this conversation: 3s ago.' in render(3710)
# Exercise the actual native CLI resume path, which passes reset=False and
# the previous ID too. A parent ID alone must not preserve another chat's gap.
cli=SimpleNamespace(agent=agent,conversation_history=[])
_sync_agent_to_session(cli,'conversation-two',parent_session_id='compressed',reason='resume')
assert provider._session_id==agent.session_id=='conversation-two'
assert 'Previous message in this conversation:' not in render(3713)
assert 'Previous message in this conversation: 1s ago.' in render(3714)
assert manager.get_provider('colony') is provider
assert requests.count('/v1/host/context/temporal')==2,requests
print(json.dumps({'native_provider_reuse':True,'native_request_content':True,
 'compression_continuity':True,'native_resume_clears_other_conversation_gap':True,
 'contact_clock_fetches':2,'model_calls':0,'network':0}))
'''


def test_native_reused_provider_temporal_context(artifacts, tmp_path):
    if not os.environ.get('AEVA_HERMES_TEST_PYTHON') and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'), COLONY_HERMES_HOME=str(tmp_path/'hermes'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system',
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3], cwd=tmp_path, env=env)
    assert '"native_request_content": true' in result.stdout

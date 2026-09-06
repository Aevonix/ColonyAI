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
from colony_sidecar.api.routers import host
from colony_sidecar.turns import get_turn_idempotency_ledger
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
home.joinpath('config.yaml').write_text(json.dumps({
    'context': {'engine': 'compressor'},
    'plugins': {'enabled': ['colony'], 'colony': {'owner_contact_id': 'contact-a', 'url': 'http://fixture'}},
    'memory': {'provider': 'colony-memory', 'config': {'contact_id': 'contact-a', 'url': 'http://fixture'}}}))
app=FastAPI(); app.include_router(host.router); app.include_router(host.v2_router)
api=TestClient(app)
fact='My neutral orchard badge is cobalt-716.'
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
ledger.record_source('native-erasure-source', contact_id='contact-a', session_id='original',
    messages=[{'role':'user','content':fact}], derive_claims=False)
wire=[]
original_client=httpx.Client
def respond(request):
    response=api.request(request.method, request.url.path, params=request.url.params,
                         headers=dict(request.headers), content=request.content)
    wire.append((request.url.path, response.status_code))
    return httpx.Response(response.status_code, content=response.content, headers=response.headers)
httpx.Client=lambda **kw: original_client(**{**kw, 'transport':httpx.MockTransport(respond)})
def no_network(*a, **kw): raise AssertionError('Native erasure qualification is local')
socket.socket.connect=no_network; socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
plugins=get_plugin_manager(); plugins.discover_and_load()
assert plugins._plugins['colony'].enabled
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
from agent.turn_context import compose_user_api_content, append_notes_to_multimodal_content
from hermes_state import SessionDB
provider=load_memory_provider('colony-memory'); manager=MemoryManager(); manager.add_provider(provider)
manager.initialize_all('original', hermes_home=str(home))
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
db.append_message('original','assistant','Stored neutral response')
db.append_message('original','user','What is my orchard badge?', api_content=injected)
db.append_message('original','assistant','Here is the recalled badge.')
before=db.get_messages_as_conversation('original')
assert fact in json.dumps(before)
from run_agent import AIAgent
from agent.context_compressor import ContextCompressor
client=MagicMock()
client.chat.completions.create.side_effect=[
    NS(choices=[NS(message=NS(content='BEFORE_OK',tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None),
    NS(choices=[NS(message=NS(content='AFTER_OK',tool_calls=None),finish_reason='stop')],model='fixture/model',usage=None),
]
with patch('run_agent.OpenAI',return_value=client), patch('run_agent.get_tool_definitions',return_value=[]), patch('run_agent.check_toolset_requirements',return_value={}):
    agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',quiet_mode=True,skip_context_files=True,skip_memory=True,platform='cli',max_iterations=2)
    assert isinstance(agent.context_compressor, ContextCompressor)
    agent._cached_system_prompt='Stable neutral identity.'
    agent._use_prompt_caching=False; agent.save_trajectories=False
    result=agent.run_conversation('Continue the neutral conversation.', conversation_history=copy.deepcopy(before), task_id='erasure-before')
    assert result['final_response']=='BEFORE_OK', result
    import colony_hermes
    assert fact in json.dumps(client.chat.completions.create.call_args_list[0].kwargs['messages']), (wire, list(colony_hermes._TRANSPORT_SCOPES._by_turn.values()), client.chat.completions.create.call_args_list[0].kwargs['messages'])
    forgotten=api.post('/v1/host/memory/sources/forget',json={'contact_id':'contact-a','source_ids':['native-erasure-source']})
    assert forgotten.status_code==200 and forgotten.json()['source_erased'], forgotten.text
    # Reopen native durable history, exactly as a later process resumes it.
    reopened=SessionDB(home/'fixture-state.db').get_messages_as_conversation('original')
    assert fact in json.dumps(reopened)  # documented storage limit, not hidden
    result=agent.run_conversation('Continue after forgetting.', conversation_history=reopened, task_id='erasure-after')
    assert result['final_response']=='AFTER_OK', result
    sent=client.chat.completions.create.call_args_list[-1].kwargs['messages']
    assert fact not in json.dumps(sent), sent
    assert 'Continue after forgetting.' in json.dumps(sent)
    assert 'What is my orchard badge?' in json.dumps(sent)
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
invoke_hook('pre_llm_call', session_id='image-resume', task_id='image-task', turn_id='image-turn', platform='cli',
    sender_id='', user_message='Continue after image forget', conversation_history=[])
filtered=apply_llm_request_middleware({'messages':[{'role':'user','content':enriched}]},
    session_id='image-resume',task_id='image-task',turn_id='image-turn').payload
assert 'neutral-fixture' not in json.dumps(filtered) and 'Neutral original pixels' not in json.dumps(filtered)
print(json.dumps({'native_compressor':True,'native_persist_resume':True,'before_present':True,
    'standard_forget':True,'resumed_request_absent':True,'native_storage_gap_explicit':True,
    'multimodal_whole_source_erased':True,'controlled_inference':True}))
'''


def test_native_persisted_recall_is_not_resent_after_forget(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native request qualification')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system', COLONY_GUARD_CHAT_MODE='off')
    run_python('-I','-c',PROBE, artifacts[3], ROOT/'sidecar',
               os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''), cwd=tmp_path, env=env)

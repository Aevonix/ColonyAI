"""A real native tool dispatch annotates supplied evidence through the scoped API."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json, os, socket, sqlite3, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.turns import get_turn_idempotency_ledger
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
keyring=home/'keys.json'
keyring.write_text(json.dumps({'version':1,'principals':[{
    'principal':'native-operator','status':'active','viewer_person_id':'person',
    'scopes':['memory:read','memory:search','memory:write','context:read','turns:write'],
    'turn_ingress_platforms':['voice'],
    'audiences':['viewer'],'credentials':[{'id':'test','secret':'fixture-key','status':'active'}]}]}))
keyring.chmod(0o600)
home.joinpath('config.yaml').write_text(json.dumps({
    'plugins':{'enabled':['colony'],'colony':{'url':'http://fixture','api_key':'fixture-key',
        'owner_contact_id':'person','turn_writer_platforms':[]}},
    'memory':{'provider':'colony-memory','config':{'url':'http://fixture',
        'api_key':'fixture-key','contact_id':'person'}}}))
app=FastAPI(); app.include_router(host.router); app.include_router(host.v2_router)
app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
api=TestClient(app)
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
report='Machine-authored archive report. The archive digest matched. Verification occurred at 09:14.'
correction='The digest comparison is supported; the verification time was not measured. 09:14 is unsupported, not disproven.'
ledger.record_source('report',contact_id='person',session_id='work',
    messages=[{'role':'assistant','content':report}],derive_claims=False)
ref=ledger.source_references(['report'],contact_id='person',session_id='later')[0]
posts=[]; lose_ack=True
original_client=httpx.Client
def respond(request):
    global lose_ack
    if request.url.path=='/v1/host/contacts/resolve':
        return httpx.Response(200,json={'contact_id':'guest'})
    response=api.request(request.method,request.url.path,params=request.url.params,
        headers=dict(request.headers),content=request.content)
    if request.url.path.endswith('/sources/annotations'):
        posts.append((json.loads(request.content),response.status_code,response.json()))
        if lose_ack:
            lose_ack=False
            assert response.status_code==200,response.text
            raise httpx.ReadTimeout('Fixture loses only the acknowledgement',request=request)
    return httpx.Response(response.status_code,content=response.content,headers=response.headers)
httpx.Client=lambda **kw: original_client(**{**kw,'transport':httpx.MockTransport(respond)})
def no_network(*a,**kw): raise AssertionError('No model or external network is allowed')
socket.socket.connect=no_network; socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
plugins=get_plugin_manager(); plugins.discover_and_load()
assert plugins._plugins['colony'].enabled
import colony_hermes
schema=next(s for s in colony_hermes._TOOL_SCHEMAS if s['name']=='colony_memory_annotate')
assert set(schema['parameters']['properties'])=={'source_id','source_version','excerpt','correction'}
assert schema['parameters']['additionalProperties'] is False
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
from agent.turn_context import compose_user_api_content
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import apply_llm_request_middleware
from model_tools import handle_function_call
from run_agent import AIAgent
import run_agent
# 0.21.0 binds eager aliases; 0.21.1 calls the defining modules directly.
OPENAI_TARGET = 'run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
TOOLS_TARGET = 'run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
provider=load_memory_provider('colony-memory'); manager=MemoryManager(); manager.add_provider(provider)
manager.initialize_all('later',hermes_home=str(home))
recalled=provider.prefetch('archive digest',session_id='later')
assert report in recalled and ref['source_version'] in recalled,recalled

def prime(session,task,turn,*,platform='cli',sender='',supplied=True):
    # Empty operator text is deliberate: internal correction needs authority,
    # not a fabricated explicit human request. Native composition owns api_content.
    row={'role':'user','content':''}
    invoke_hook('pre_llm_call',session_id=session,task_id=task,turn_id=turn,
        platform=platform,sender_id=sender,user_message='',conversation_history=[row])
    row['api_content']=compose_user_api_content('',recalled if supplied else '', '')
    result=apply_llm_request_middleware({'messages':[{'role':'user','content':row['api_content']}]},
        session_id=session,task_id=task,turn_id=turn).payload
    if supplied and platform=='cli': assert ref['source_version'] in json.dumps(result),result

client=MagicMock()
with patch(OPENAI_TARGET,return_value=client), patch(TOOLS_TARGET + '.get_tool_definitions',return_value=[]), patch(TOOLS_TARGET + '.check_toolset_requirements',return_value={}):
    agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
        model='fixture/model',quiet_mode=True,skip_context_files=True,skip_memory=True,platform='cli')
    agent.session_id='later'; agent._current_turn_id='review-turn'
    def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):
        agent.session_id=session; agent._current_turn_id=turn
        results=[]
        explicit_call=NS(tool_calls=[NS(id=call,function=NS(
            name='colony_memory_annotate',arguments=json.dumps(args)))])
        agent._execute_tool_calls_sequential(explicit_call,results,effective_task_id=task)
        assert len(results)==1,results
        # Hermes may append its existing repeated-error guidance to tool text.
        return json.JSONDecoder().raw_decode(results[0]['content'])[0]
    args={**ref,'excerpt':'Verification occurred at 09:14.','correction':correction}
    prime('later','review-task','review-turn')
    for bad in ({**args,'contact_id':'other'},{**args,'source_version':'0'*64},
                {**args,'source_id':'unseen'},{**args,'correction':'   '}):
        assert 'error' in dispatch(bad)
    assert 'error' in dispatch(args,turn='stale-turn')
    assert not posts,posts
    missing=json.loads(handle_function_call('colony_memory_annotate',args))
    assert 'error' in missing and not posts,missing
    prime('empty','empty','empty',supplied=False)
    assert 'error' in dispatch(args,session='empty',task='empty',turn='empty')
    prime('guest','guest','guest',platform='sms',sender='guest-fixture')
    scope=colony_hermes._TRANSPORT_SCOPES.for_execution(session_id='guest',task_id='guest',turn_id='guest')
    assert scope.valid_participant and scope.authority_lane=='guest',scope
    assert 'error' in dispatch(args,session='guest',task='guest',turn='guest')
    assert not posts,posts
    first=dispatch(args)
    assert first['confirmation']=='unknown' and 'accepted' not in first,first
    assert len(posts)==1 and posts[0][2]['created'],posts
    retry=dispatch(args,call='different-tool-call-id')
    assert retry['accepted'] and retry['created'] is False,retry
    assert len(posts)==2 and posts[0][0]==posts[1][0],posts
    assert posts[0][0]['annotation_id']==first['annotation_id']
    assert posts[0][0]['contact_id']=='person' and posts[0][0]['session_id']=='later'
    rejected=dispatch({**args,'excerpt':'A fabricated excerpt'})
    assert rejected['accepted'] is False and rejected['status_code']==409,rejected
    client.chat.completions.create.assert_not_called()
with sqlite3.connect(ledger.db_path) as db:
    assert db.execute('SELECT count(*) FROM source_annotations').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0]==0
    original=json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',('report',)).fetchone()[0])
    assert original==[{'role':'assistant','content':report}],original
    note=json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',(retry['source_id'],)).fetchone()[0])
    assert len(note)==1 and note[0]['role']=='assistant',note
    content=json.loads(note[0]['content'])
    assert content['author_principal']=='native-operator' and content['correction']==correction,content
later=provider.prefetch('archive digest',session_id='independent-reader')
assert report in later and correction in later and retry['source_version'] in later,later
assert ref['source_version'] in later and 'attributed_correction' in later,later
print(json.dumps({'native_dispatch':True,'actual_supplied_ref':True,'scoped_api':True,
    'unknown_ack_replay':True,'assistant_authorship':True,'later_bundle_refs':True,'model_calls':0}))
'''


def test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified Hermes release for native tool qualification')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'),COLONY_STATE_DIR=str(tmp_path/'colony'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1',COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system',
        COLONY_GUARD_CHAT_MODE='off',COLONY_RECALL_RERANK='off')
    run_python('-I','-c',PROBE,artifacts[3],ROOT/'sidecar',
               os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),cwd=tmp_path,env=env)

"""Actual native completion hook and canonical report recall, without models."""
import importlib.util
import os
from pathlib import Path

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, copy, json, os, socket, sys, time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
if sys.argv[4]=='lexical':
 class WithoutVectorExtras:
  def find_spec(self,fullname,*args):
   if fullname.split('.')[0] in {'pyarrow','lancedb'}:
    raise ModuleNotFoundError('Optional vector dependency absent in native CI')
 sys.meta_path.insert(0,WithoutVectorExtras())
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import host
from colony_sidecar.turns import get_turn_idempotency_ledger, canonical_turn_digest
from colony_hermes.client import TurnOutbox
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['colony'],'colony':{
 'owner_contact_id':'owner','url':'http://fixture','turn_outbox_path':str(home/'outbox.db'),
 'turn_outbox_drain_timeout_ms':1000,
 'turn_writer_platforms':['api_server','rcs','sms','whatsapp']}},
 'tools':{'tool_search':{'enabled':'off'}},'memory':{'provider':'none'}}))
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.colony_authority=RequestAuthority(principal_id='fixture',credential_id='fixture',
  scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),
  audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(host.router);app.include_router(host.v2_router)
api=TestClient(app)
ledger=get_turn_idempotency_ledger(os.environ['COLONY_STATE_DIR'])
parent='The calibration procedure uses a violet filter.'
ledger.record_source('report-parent',contact_id='owner',session_id='parent-session',
 messages=[{'role':'user','content':parent}],derive_claims=False)
ledger.record_source('independent',contact_id='owner',session_id='control',
 messages=[{'role':'user','content':'Independent orchard source remains.'}],derive_claims=False)
wire=[];original=httpx.Client;lost_ack=False
def respond(request):
 global lost_ack
 response=api.request(request.method,request.url.path,params=request.url.params,headers=dict(request.headers),content=request.content)
 wire.append((request.url.path,response.status_code))
 if '/turns/source-survivors/' in request.url.path and not lost_ack:
  assert response.status_code==201,response.text
  lost_ack=True
  raise httpx.ReadTimeout('Fixture lost acknowledgement after canonical commit',request=request)
 return httpx.Response(response.status_code,content=response.content,headers=response.headers)
httpx.Client=lambda **kw:original(**{**kw,'transport':httpx.MockTransport(respond)})
def no_network(*a,**k):raise AssertionError('No network or model calls in native completion qualification')
socket.socket.connect=no_network;socket.create_connection=no_network
from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import apply_llm_request_middleware
from agent.turn_context import compose_user_api_content
from model_tools import handle_function_call
from colony_memory.provider import ColonyMemoryProvider
from gateway.session_context import set_session_vars
from tools import kanban_tools
db=kb.connect(board='default')
tid=kb.create_task(db,title='Retain a useful calibration finding',body='WORKER-INSTRUCTION-MUST-NOT-BECOME-MEMORY',
 assignee='default',workspace_kind='dir',workspace_path=str(home),board='default')
task=kb.claim_task(db,tid);assert task
os.environ.update(HERMES_KANBAN_TASK=tid,HERMES_KANBAN_RUN_ID=str(task.current_run_id),
 HERMES_KANBAN_CLAIM_LOCK=task.claim_lock,HERMES_KANBAN_BOARD='default',HERMES_SESSION_ID='worker-session')
pm=get_plugin_manager();pm.discover_and_load()
assert pm._plugins['colony'].enabled
import colony_hermes
observed=[]
report_outcomes=[]
for index,callback in enumerate(pm._hooks.get('kanban_task_completed',[])):
 if type(callback).__name__=='CompletedReports':
  report_handler=callback
  def record_outcome(_callback=callback,**kwargs):
   value=_callback(**kwargs);report_outcomes.append(value);return value
  pm._hooks['kanban_task_completed'][index]=record_outcome
def inspect_context(**kwargs):
 context=dict(colony_hermes._TOOL_EXECUTION_CONTEXT.get() or {})
 observed.append((kwargs,context))
pm._hooks.setdefault('kanban_task_completed',[]).append(inspect_context)
set_session_vars(platform='cli',user_id='',chat_id='',session_id='worker-session')
provider=ColonyMemoryProvider({'url':'http://fixture','contact_id':'owner','turn_writer':'disabled','default_context_authority':'owner_system'})
provider.initialize('worker-session',hermes_home=str(home))
# Production excludes CLI from ordinary conversation capture. Machine worker
# instructions remain excluded at both turn and compression boundaries, while
# the separate attested completion report is still eligible below.
worker_instruction='WORKER-INSTRUCTION-MUST-NOT-BECOME-MEMORY'
invoke_hook('pre_llm_call',session_id='worker-session',task_id='worker-probe',turn_id='worker-probe',
 platform='cli',sender_id='',user_message=worker_instruction)
invoke_hook('post_llm_call',session_id='worker-session',task_id='worker-probe',turn_id='worker-probe',
 platform='cli',user_message=worker_instruction,assistant_response='Intermediate progress',model='fixture')
provider.on_pre_compress([{'role':'user','content':worker_instruction}],require_checkpoint=True)
assert provider.get_diagnostics()['checkpoint']=={'state':'not_applicable','reason':'native_worker_instructions'}
assert not TurnOutbox(home/'outbox.db').snapshot()
recalled=provider.prefetch('calibration procedure',session_id='worker-session')
assert 'report-parent' in recalled,recalled
prompt='WORKER-CONTINUATION-MUST-NOT-BECOME-MEMORY'
messages=[{'role':'user','content':prompt}]
invoke_hook('pre_llm_call',session_id='worker-session',task_id='agent-task',turn_id='agent-turn',
 platform='cli',sender_id='',user_message=prompt,conversation_history=messages)
messages[0]['api_content']=compose_user_api_content(prompt,recalled,'')
request=apply_llm_request_middleware({'messages':[{'role':'user','content':messages[0]['api_content']}]},
 session_id='worker-session',task_id='agent-task',turn_id='agent-turn',api_request_id='request-one').payload
assert 'report-parent' in json.dumps(request)
report='Calibration finding: use the violet filter for the test rig.'
completed=json.loads(handle_function_call('kanban_complete',{'task_id':tid,'board':'default','summary':report},
 session_id='worker-session',task_id='agent-task',turn_id='agent-turn',api_request_id='request-one',tool_call_id='complete-one'))
assert completed.get('ok'),completed
assert len(observed)==1
hook,context=observed[0]
assert context['tool_name']=='kanban_complete' and context['turn_id']=='agent-turn'
assert hook['run_id']==task.current_run_id and hook['task_id']==tid
assert kb.get_task(db,tid).status=='done'
outbox=TurnOutbox(home/'outbox.db')
rows=outbox.snapshot()
assert len(rows)==1,('completed assistant report was not retained',rows,report_outcomes)
assert lost_ack and rows[0]['state']=='pending',(lost_ack,rows[0]['state'],wire,report_outcomes)
payload=rows[0]['payload'];sid=rows[0]['turn_id']
assert payload['source_only'] is True and not payload.get('user_message') and not payload.get('sender')
assert payload['assistant_source_refs']==[{'source_id':'report-parent','source_version':canonical_turn_digest([{'role':'user','content':parent}])}]
assert report in payload['assistant_message']
assert 'WORKER-INSTRUCTION' not in json.dumps(payload) and 'WORKER-CONTINUATION' not in json.dumps(payload)
with ledger._connect() as conn:
 source=dict(conn.execute('SELECT * FROM turn_sources WHERE turn_id=?',(sid,)).fetchone())
 assert source['scope']=='person' and source['contact_id']=='owner'
 assert {m['role'] for m in json.loads(source['messages_json'])}=={'assistant'}
 assert conn.execute('SELECT count(*) FROM source_claim_jobs WHERE turn_id=?',(sid,)).fetchone()[0]==0
assert any(r['turn_id']==sid for r in ledger.search_sources('calibration finding',contact_id='owner',session_id='later'))
assert not ledger.search_sources('calibration finding',contact_id='someone-else',session_id='later')
# The shared operational window expires; the source is still discoverable.
from colony_sidecar.turns import hermes_kanban
with patch.object(hermes_kanban,'selected_home',return_value=home):
 assert not hermes_kanban.kanban_view(now=time.time()+8*86400)['recent']
assert any(r['turn_id']==sid for r in ledger.search_sources('calibration finding',contact_id='owner',session_id='after-window'))
# A duplicate actual lifecycle callback re-reads the committed summary and
# preserves the exact outbox envelope, recovering the lost acknowledgement.
token=colony_hermes._TOOL_EXECUTION_CONTEXT.set(context)
try:
 with patch('colony_hermes.client.time.time',return_value=rows[0]['lease_expires_at']+.1):
  invoke_hook('kanban_task_completed',**{**hook,'summary':'FORGED-CALLBACK-TEXT'})
finally:colony_hermes._TOOL_EXECUTION_CONTEXT.reset(token)
assert len(outbox.snapshot())==1 and outbox.snapshot()[0]['payload']==payload
assert outbox.snapshot()[0]['state']=='delivered'
with ledger._connect() as conn:
 assert conn.execute('SELECT count(*) FROM turn_sources WHERE turn_id=?',(sid,)).fetchone()[0]==1
invoke_hook('kanban_task_completed',**hook)
assert report_outcomes[-1]['reason']=='attested_completion_context_missing'
assert len(outbox.snapshot())==1
from agent.delegation_context import non_dispatcher_owned_context
token=colony_hermes._TOOL_EXECUTION_CONTEXT.set(context)
try:
 with non_dispatcher_owned_context():
  assert report_handler(**hook)['reason']=='owned_native_run_missing'
 assert report_handler(**{**hook,'run_id':hook['run_id']+1})['reason']=='owned_native_run_missing'
 scope=report_handler.scopes.for_execution(session_id=context['session_id'],task_id=context['task_id'],turn_id=context['turn_id'])
 for invalid in (replace(scope,authority_lane='guest'),replace(scope,platform='cron'),None):
  with patch.object(report_handler.scopes,'for_execution',return_value=invalid):
   assert report_handler(**hook)['reason']=='attested_completion_context_missing'
 with patch.dict(os.environ,HERMES_KANBAN_RUN_ID=str(hook['run_id']+1)):
  assert report_handler(**{**hook,'run_id':hook['run_id']+1})['reason']=='completed_native_report_missing'
 snapshot=report_handler.request_memory.supplied_snapshot(scope)
 assert snapshot==payload['assistant_source_refs']
 snapshot[0]['source_id']='untrusted mutation'
 assert report_handler.request_memory.supplied_snapshot(scope)==payload['assistant_source_refs']
 report_handler.request_memory.finish(task_id=scope.task_id,turn_id=scope.turn_id,contact_id=scope.contact_id)
 assert report_handler(**hook)['reason']=='verified_request_lineage_missing'
 # Re-observation without a dispatched request cannot certify dependencies.
 report_handler.request_memory.observe(scope,messages,user_message=prompt)
 assert report_handler(**hook)['reason']=='verified_request_lineage_missing'
 # A failed freshness check also cannot certify an empty reference set.
 with patch.object(report_handler.client,'get',side_effect=httpx.ReadTimeout('Fixture unavailable feed')):
  report_handler.request_memory({'messages':[]},scope)
 assert report_handler(**hook)['reason']=='verified_request_lineage_missing'
 # A fresh request with no supplied sources is distinct from missing lineage.
 report_handler.request_memory({'messages':[{'role':'user','content':prompt}]},scope)
 assert report_handler.request_memory.supplied_snapshot(scope)==[]
 # Restore the actual supplied request for subsequent erasure/replay proof.
 report_handler.request_memory(request,scope)
 assert report_handler.request_memory.supplied_snapshot(scope)==payload['assistant_source_refs']
finally:colony_hermes._TOOL_EXECUTION_CONTEXT.reset(token)
async def semantic():
 from colony_sidecar.turns.source_vectors import SourceVectors
 from colony_sidecar.vector.indexes import EmbeddingIdentity, IndexCatalog
 from colony_sidecar.vector.store import VectorStore
 class Embeddings:
  index_identity=EmbeddingIdentity('completion-fixture','completion-fixture','unknown',3)
  async def embed_batch(self,texts):return [[1.,0.,0.] if 'calibration' in text.lower() else [0.,1.,0.] for text in texts]
  async def embed_query(self,text):return [1.,0.,0.]
 pipeline=Embeddings();store=VectorStore(str(home/'vectors'),identity=pipeline.index_identity,catalog=IndexCatalog(ledger))
 await store.connect(3);await store.ensure_collections(3)
 projection=SourceVectors(ledger,store,pipeline)
 for _ in range(12):
  if not await projection.process_one():break
 hits,_=await projection.search('optical setup',contact_id='owner',session_id='another-session',limit=10)
 assert any(row['turn_id']==sid and row['role']=='assistant' for row in hits),hits
 foreign,_=await projection.search('optical setup',contact_id='someone-else',session_id='another-session',limit=10)
 assert not foreign
 return projection
projection=asyncio.run(semantic()) if sys.argv[4]=='vectors' else None
erased=ledger.erase_sources(contact_id='owner',turn_ids=['report-parent'])
assert sid in erased['affected_source_ids']
async def semantic_erased():
 hits,_=await projection.search('optical setup',contact_id='owner',session_id='another-session',limit=10)
 assert not any(row['turn_id']==sid for row in hits)
if projection:asyncio.run(semantic_erased())
assert ledger.search_sources('orchard',contact_id='owner',session_id='later')
assert not ledger.search_sources('calibration finding',contact_id='owner',session_id='later')
page=api.get('/v1/host/memory/sources/erasures',params={'contact_id':'owner','after':0});assert page.status_code==200
outbox.apply_erasure_page('owner',page.json())
token=colony_hermes._TOOL_EXECUTION_CONTEXT.set(context)
try:invoke_hook('kanban_task_completed',**hook)
finally:colony_hermes._TOOL_EXECUTION_CONTEXT.reset(token)
assert not outbox.snapshot()
with ledger._connect() as conn:assert not conn.execute('SELECT 1 FROM turn_sources WHERE turn_id=?',(sid,)).fetchone()
assert kb.latest_run(db,tid).summary==report
provider._prefetch_thread.join(timeout=2) if provider._prefetch_thread else None
db.close()
print('native completion context, source-only report, scoped recall and dependent erasure verified')
'''


@pytest.mark.parametrize('with_vectors', [False, True], ids=['lexical', 'semantic'])
def test_native_completed_report_source_handoff(artifacts, tmp_path, with_vectors):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native completion integration')
    dependencies = os.environ.get('COLONY_TEST_DEPENDENCY_PATH', '')
    if with_vectors and importlib.util.find_spec('lancedb') is None and not (dependencies and (Path(dependencies)/'lancedb').is_dir()):
        pytest.skip('Optional vector extra is qualified in the vector-enabled environment')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'native'), HERMES_KANBAN_HOME=str(tmp_path/'native'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), COLONY_STATE_DIR=str(tmp_path/'state'),
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1', COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system',
        COLONY_SKIP_DOTENV='1', COLONY_OWNER_CONTACT_ID='owner', COLONY_GUARD_CHAT_MODE='off',
        COLONY_INTROSPECTION_ENABLED='false', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-c', PROBE, artifacts[3], ROOT/'sidecar',
        dependencies, 'vectors' if with_vectors else 'lexical', cwd=tmp_path, env=env)
    assert 'dependent erasure verified' in result.stdout

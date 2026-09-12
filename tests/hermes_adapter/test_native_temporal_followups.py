"""Qualified native Kanban, real HTTP routes, no inference or network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import json,os,socket,sys,types,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
if len(sys.argv)>4 and sys.argv[4]:sys.path.insert(0,sys.argv[4])
package=types.ModuleType('apsimo_hermes');package.__path__=[sys.argv[2]];sys.modules['apsimo_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native followup qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from apsimo.api.authority import RequestAuthority
from apsimo.api.routers import temporal_followups,host
from apsimo.commitments.store import CommitmentStore
from apsimo.commitments.work import CommitmentWork
from apsimo.initiatives.temporal_followup import TemporalFollowups
from apsimo.turns import get_turn_idempotency_ledger
from apsimo_hermes.initiative_work import NativeFollowups
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
store=CommitmentStore(state/'commitments.db');host._commitment_store=store
waiting=TemporalFollowups(store)
source_ledger=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 person=request.headers.get('fixture-person','owner')
 request.state.colony_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
     scopes=frozenset({'turns:write','context:read'}),viewer_person_id=person,person_ids=frozenset({person}),
     audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(temporal_followups.router)
clients=[TestClient(app),TestClient(app)]
reviews=[NativeFollowups(client,'owner') for client in clients]
base='/v1/host/temporal-followups'
def register(label):
 parent=store.create(person_id='owner',description='Obtain '+label)
 claim=CommitmentWork(store).operate(parent['id'],operation='claim',principal_id='fixture-native',contact_id='owner',session_id='owner-turn',task_id='parent-'+label,turn_id='turn-'+label)
 source_ledger.record_source('source:'+label,contact_id='owner',session_id='owner-turn',messages=[{'role':'user','content':'Please obtain the task result for '+label}],derive_claims=False)
 source_version=source_ledger.source_references(['source:'+label],contact_id='owner',session_id='owner-turn')[0]['source_version']
 body=dict(contact_id='owner',recipient_id='colleague',commitment_id=parent['id'],work_id='parent-'+label,
           session_id='owner-turn',turn_id='turn-'+label,claim_id=claim['claim_id'],outbound_ref='message:'+label,
           source_refs=['source:'+label],source_versions={'source:'+label:source_version},expected_after_seconds=10,expires_at=time.time()+3600)
 stale=clients[0].post(base,json={**body,'source_versions':{'source:'+label:'0'*64}})
 assert stale.status_code==409,stale.text
 denied=clients[0].post(base,json={**body,'authority_scope':{'send':True}})
 assert denied.status_code==422,denied.text
 response=clients[0].post(base,json=body)
 assert response.status_code==200,response.text
 value=response.json()
 assert value['expected_at'] is None and not value['effect_authorized']
 assert clients[1].post(base,json=body).json()['wait_id']==value['wait_id']
 assert clients[0].get(base,params={'contact_id':'guest'},headers={'fixture-person':'guest'}).status_code==403
 # Independent transport producer observed a real timestamp in its ledger;
 # no HTTP/model assertion can start this clock.
 waiting.acknowledge_dispatch(value['wait_id'],receipt_ref='receipt:'+label,occurred_at=time.time()-30)
 return value['wait_id'],parent['id']
def reply(identifier):
 row=waiting.get(identifier)
 return waiting.apply_reply(identifier,{'status':'matched','contact_id':'colleague','outbound_ref':row['outbound_ref'],
    'matches':[{'external_ref':'message:reply-'+identifier,'reply_to_ref':row['outbound_ref'],
                'receipt_ref':'receipt:reply-'+identifier,'ts':time.time(),'channel':'verified-other-channel','reaction':None}]})

def seed_historical_task(identifier, client=None):
 # A pre-upgrade native association, constructed with real native/HTTP APIs.
 # Current adapters must never recreate this unrestricted worker.
 value=clients[0].get(base+'/'+identifier,params={'contact_id':'owner'}).json()
 review=value['review']
 with kb.connect(board='default') as db:
  tid=kb.create_task(db,title=review['title'],body=review['body'],assignee='default',
      created_by='colony-followup',tenant='owner',idempotency_key='colony-followup:'+identifier,
      workspace_kind='scratch',initial_status='blocked',goal_mode=True,goal_max_turns=4,
      max_runtime_seconds=480,max_retries=1)
 response=(client or clients[0]).post(base+'/'+identifier+'/native-task',json={
     'contact_id':'owner','native_board':'default','native_task_id':tid,'contract_sha256':review['sha256']})
 assert response.status_code==200,response.text
 return tid

first,parent=register('first')
def refuse_new(index):
 try:reviews[index].work(first)
 except ValueError as error:assert str(error)=='readonly_followup_worker_unqualified'
 else:raise AssertionError('New unrestricted followup worker was admitted')
with ThreadPoolExecutor(2) as pool:list(pool.map(refuse_new,range(2)))
for review in reviews:review.reconcile(board='default')
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
task_id=seed_historical_task(first)
with kb.connect(board='default') as db:
 assert kb.promote_task(db,task_id,actor='historical-fixture',reason='Pre-upgrade ready row')[0]
for review in reviews:review.reconcile(board='default')
with kb.connect(board='default') as db:
 task=kb.get_task(db,task_id)
 assert task.status=='blocked' and task.block_kind=='needs_input'
 # Native blocking records a synthetic ended run, without a worker claim.
 assert kb.latest_run(db,task_id).claim_lock is None
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
 assert db.execute('SELECT count(*) FROM kanban_notify_subs').fetchone()[0]==0
# Reply on another verified alias cancels a historical queued task on the existing tick.
reply(first)
reviews[1].reconcile(board='default')
with kb.connect(board='default') as db:
 assert kb.get_task(db,task_id).status=='archived'
 assert kb.latest_run(db,task_id).claim_lock is None
assert waiting.get(first)['state']=='resolved'
assert waiting.get(first)['native_terminal_status']=='archived'
assert store.get(parent)['status']=='pending' # Reply is not task fulfillment.
assert clients[0].get(base+'/'+first,params={'contact_id':'owner'}).json()['status']=='cancelled'
assert clients[1].get(base,params={'contact_id':'owner'}).json()['items']==[]

# A historical association whose attachment overlapped a reply remains
# reconcilable; the new boundary cannot promote it.
second,_=register('race')
class ReplyDuringAttach:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  if path.endswith('/native-task'):reply(second)
  return clients[0].post(path,**kw)
seed_historical_task(second,ReplyDuringAttach())
raced=reviews[0].work(second)
assert raced['state']=='resolved' and raced['status']=='cancelled',raced
with kb.connect(board='default') as db:
 assert kb.get_task(db,raced['native_task_id']).status=='archived'
 assert kb.latest_run(db,raced['native_task_id']) is None

# A historical lost attachment ACK remains bound after restart, without
# promoting another unrestricted run.
third,_=register('lost-ack')
class LostAck:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  result=clients[0].post(path,**kw)
  if path.endswith('/native-task'):raise RuntimeError('lost attachment acknowledgment')
  return result
try:seed_historical_task(third,LostAck())
except RuntimeError:pass
else:raise AssertionError('missing lost ACK')
with kb.connect(board='default') as db:
 row=db.execute('SELECT id FROM tasks WHERE idempotency_key=?',('colony-followup:'+third,)).fetchone()
 assert kb.get_task(db,row['id']).status=='blocked'
recovered=reviews[1].work(third)
with kb.connect(board='default') as db:
 task=kb.get_task(db,recovered['native_task_id']);assert task.status=='blocked'
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==3
 assert kb.latest_run(db,task.id) is None
 # An owner closes the historical held task through the existing native API.
 # The adapter must observe this newly terminal state before its dispatch gate.
 assert kb.complete_task(db,task.id,summary='Historical local status reviewed; response remains unknown.',fire_lifecycle_hook=False)
finished=reviews[0].work(third)
assert finished['status']=='completed' and finished['state']=='open',finished
assert finished['native_observation']['completed_run'] and not finished['effect_authorized']
assert waiting.get(third)['native_terminal_status']=='done'
assert waiting.due()==[]
# A correction preserves original text/version but invalidates an old wait's
# unqualified evidence on ordinary due/read/prepare reconciliation.
fourth,_=register('corrected')
original=source_ledger.source_references(['source:corrected'],contact_id='owner',session_id='owner-turn')[0]
source_ledger.append_source_annotation(contact_id='owner',session_id='owner-turn',annotation_id='correction-one',
    source_id=original['source_id'],source_version=original['source_version'],excerpt='Please obtain',
    correction='This requested response is no longer needed.',author_principal='fixture-native')
corrected=clients[1].get(base+'/'+fourth,params={'contact_id':'owner'}).json()
assert corrected['state']=='cancelled',corrected
assert not waiting.preflight(fourth)['review_allowed']
print(json.dumps({'new_unrestricted_worker_refused':True,'historical_ready_task_held':True,
                  'reply_cancels_before_run':True,'historical_attachment_race_cancelled':True,
                  'historical_lost_ack_observed':True,'new_native_terminal_observed':True,
                  'completion_is_not_send_or_fulfillment':True,'model_calls':0,'network':0}))
'''


def test_actual_native_reply_wait_lifecycle(tmp_path):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use qualified Hermes interpreter for native integration')
        python = sys.executable
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        COLONY_HERMES_HOME=str(tmp_path/'hermes'),COLONY_HERMES_WORK_BOARDS='["default"]',
        COLONY_STATE_DIR=str(tmp_path/'state'),COLONY_OWNER_CONTACT_ID='owner',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),
        os.environ.get('AEVA_HERMES_TEST_SOURCE','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"new_unrestricted_worker_refused": true' in result.stdout

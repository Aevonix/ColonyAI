"""Real Hermes board transitions, scoped HTTP and independent steward clients."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import hashlib,importlib.util,json,os,socket,sys,types
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
package=types.ModuleType('colony_hermes');package.__path__=[sys.argv[2]];sys.modules['colony_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native review qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import initiative_work,host,executions
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns.local_work import local_work_view
from colony_hermes.initiative_work import NativeReviews
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
store=InitiativeStore(state);host._initiative_store=store;host._task_queue=None
def proposal(label):
 return store.create(type='operational',description=label,priority=.5,
     action_hint='operational_review',source_type='operational',created_by='autonomy_loop',
     context={'evidence_scope':'local_observation','observed_at':'2026-09-07T00:00:00Z'})
first=proposal('Review retained backup metadata')
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 person=request.headers.get('fixture-person','owner')
 request.state.colony_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
     scopes=frozenset({'turns:write','context:read'}),viewer_person_id=person,person_ids=frozenset({person}),
     audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(initiative_work.router);app.include_router(executions.router)
clients=[TestClient(app),TestClient(app)]
reviews=[NativeReviews(client,'owner') for client in clients]
with ThreadPoolExecutor(max_workers=2) as pool:
 results=list(pool.map(lambda index:reviews[index].work(first.id),range(2)))
tid=results[0]['native_work']['native_task_id']
assert all(r['native_work']['native_task_id']==tid and r['status']=='assigned' for r in results),results
for i in range(2):reviews[i].reconcile(board='default',dry_run=False)
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
 task=kb.get_task(db,tid)
 assert task.status=='ready' and task.goal_mode and task.goal_max_turns==4
 assert task.assignee=='default' and task.max_runtime_seconds==480 and task.max_retries==1
 assert not db.execute('SELECT 1 FROM kanban_notify_subs').fetchone()
 claimed=kb.claim_task(db,tid);run=claimed.current_run_id
reviews[0].reconcile(board='default')
for index,client in enumerate(clients):
 visible=client.get('/v1/host/executions',params={'contact_id':'owner','session_id':f'owner-{index}',
                                               'projection':'request'}).json()
 assert tid in visible['text'] and 'Review local operational coverage' in visible['text'],visible
 assert 'running' in visible['text']
guest=clients[0].get('/v1/host/initiative-work/'+first.id,params={'contact_id':'guest'},headers={'fixture-person':'guest'})
assert guest.status_code==403
guest=clients[0].get('/v1/host/executions',params={'contact_id':'guest'},headers={'fixture-person':'guest'})
assert tid not in guest.text
with kb.connect(board='default') as db:
 assert kb.complete_task(db,tid,summary='Inspected narrow metadata; restore coverage remains unknown.',
                         expected_run_id=run,fire_lifecycle_hook=False)
reviews[1].reconcile(board='default')
done=reviews[0].work(first.id)
assert done['status']=='completed' and done['result']['native_run_id']==run,done
assert 'unknown' in done['result']['summary'] and 'unverified' in done['result_authority']
view=local_work_view();assert view['available'],view
assert view['recent'][0]['initiative_id']==first.id and view['recent'][0]['native_status']=='done',view
for client in clients:
 visible=client.get('/v1/host/executions',params={'contact_id':'owner','projection':'request'}).json()
 assert tid in visible['text'] and 'completed' in visible['text'],visible

# Lost association acknowledgment: one blocked task survives and the next
# ordinary cycle recovers it. No fake worker or new task is needed.
second=proposal('Review another local observation')
class LostAck:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  result=clients[0].post(path,**kw)
  if path.endswith('/native-task'):raise RuntimeError('lost acknowledgment')
  return result
try:NativeReviews(LostAck(),'owner').work(second.id)
except RuntimeError:pass
else:raise AssertionError('Lost acknowledgment was not retained')
with kb.connect(board='default') as db:
 task=kb.get_task(db,db.execute('SELECT id FROM tasks WHERE idempotency_key=?',('colony-initiative:'+second.id,)).fetchone()[0])
 assert task.status=='blocked' and kb.latest_run(db,task.id) is None
second_result=reviews[0].work(second.id)
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
 assert kb.get_task(db,second_result['native_work']['native_task_id']).status=='ready'
 assert db.execute('SELECT count(*) FROM task_events WHERE kind="created"').fetchone()[0]==2
# Native exhausted failure is a failed review with a reason, while an ordinary
# needs-input block remains resumable. No bridge retry or extra task is made.
second_id=second_result['native_work']['native_task_id']
with kb.connect(board='default') as db:
 claimed=kb.claim_task(db,second_id)
 assert kb._record_task_failure(db,second_id,'controlled spawn failure',outcome='spawn_failed',
                                release_claim=True,end_run=True)
failed=reviews[1].work(second.id)
assert failed['status']=='failed' and failed['result']['run_outcome']=='gave_up',failed
assert failed['result']['error']=='controlled spawn failure',failed
reviews[0].reconcile(board='default')
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
 assert kb.unblock_task(db,second_id)
 claimed=kb.claim_task(db,second_id)
 assert kb.block_task(db,second_id,reason='Need one missing observation',kind='needs_input',
                      expected_run_id=claimed.current_run_id)
blocked=reviews[1].work(second.id)
assert blocked['status']=='assigned' and blocked['result']['native_status']=='blocked',blocked
assert blocked['result']['run_outcome']=='blocked',blocked
print(json.dumps({'independent_cycles_one_task':True,'actual_native_completion':True,
                  'shared_visibility':True,'lost_ack_recovery':True,'failed_vs_blocked':True,'models':0,'network':0}))
'''


def test_actual_native_initiative_handoff_and_reconciliation(tmp_path):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use the existing qualified Hermes interpreter for native integration')
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
        str(root/'plugins/hermes-plugin'),os.environ.get('COLONY_TEST_DEPENDENCY_PATH','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"independent_cycles_one_task": true' in result.stdout

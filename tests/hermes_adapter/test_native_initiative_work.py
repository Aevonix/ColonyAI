"""Real Hermes board transitions, scoped HTTP and concurrent dispatch ticks."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import hashlib,importlib.util,json,os,socket,sys,types,shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
if len(sys.argv)>4 and sys.argv[4]:sys.path.insert(0,sys.argv[4])
package=types.ModuleType('colony_hermes');package.__path__=[sys.argv[2]];sys.modules['colony_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native review qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
try:
 from hermes_cli.kanban_db_dispatch import _record_task_failure
except ModuleNotFoundError:
 from hermes_cli.kanban_db import _record_task_failure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import initiative_work,host,executions
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns.local_work import local_work_view
from colony_hermes.initiative_work import NativeReviews
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: [], colony: {owner_contact_id: owner}}\n')
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
shutil.copytree(sys.argv[2],state/'adapter/colony_hermes')
for name in ('catalog.py','contract.py'):
 shutil.copyfile(Path(sys.argv[2]).parents[1]/'hostworker/colony_hostworker'/name,state/'adapter/colony_hermes/colony_hostworker'/name)
(state/'instance.json').write_text(json.dumps({'version':1,'profile':'local','hermes_home':str(root),
 'hermes_python':sys.executable,'sidecar_python':sys.executable,'sidecar_module_root':sys.argv[1],
 'adapter_binding':{'mode':'private-directory'}}))
routing={'provider':'vllm','models':{},'modelPool':{'planning-fixture':{
 'model':'replaceable-planning-model','baseUrl':'http://127.0.0.1:9/v1','supportsTools':True}},
 'functionRoles':{'planning':['planning-fixture']}}
(state/'.colony-llm-config.json').write_text(json.dumps(routing))
from colony_sidecar.setup_native_reviews import configure
configure(state,install=True)
review_config={'enabled':True,'instance_dir':str(state)}
import yaml
selected=yaml.safe_load((root/'profiles/colony-reviews/config.yaml').read_text())
assert selected['model']['default']=='replaceable-planning-model' and 'max_tokens' not in selected['model']
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
checked=clients[0].get('/v1/host/initiative-work/'+first.id,params={'contact_id':'owner'})
assert checked.status_code==200,checked.text
reviews=[NativeReviews(client,'owner',review_config) for client in clients]
NativeReviews(clients[0],'owner').reconcile(board='default')
reviews[0].reconcile(board='default',dry_run=True)
reviews[0].reconcile(board='other')
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
with ThreadPoolExecutor(max_workers=2) as pool:
 list(pool.map(lambda index:reviews[index].reconcile(board='default'),range(2)))
results=[review.work(first.id) for review in reviews]
tid=results[0]['native_work']['native_task_id']
assert all(r['native_work']['native_task_id']==tid and r['status']=='assigned' for r in results),results
for i in range(2):reviews[i].reconcile(board='default',dry_run=False)
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
 task=kb.get_task(db,tid)
 assert task.status=='ready' and not task.goal_mode
 assert task.assignee=='colony-reviews' and task.max_runtime_seconds==480 and task.max_retries==1
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
routing['modelPool']['planning-fixture']['model']='replacement-planning-model'
(state/'.colony-llm-config.json').write_text(json.dumps(routing))
class LostAck:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  result=clients[0].post(path,**kw)
  if path.endswith('/native-task'):raise RuntimeError('lost acknowledgment')
  return result
try:NativeReviews(LostAck(),'owner',review_config).work(second.id)
except RuntimeError:pass
else:raise AssertionError('Lost acknowledgment was not retained')
with kb.connect(board='default') as db:
 task=kb.get_task(db,db.execute('SELECT id FROM tasks WHERE idempotency_key=?',('colony-initiative:'+second.id,)).fetchone()[0])
 assert task.status=='blocked' and kb.latest_run(db,task.id) is None
second_result=reviews[0].work(second.id)
selected=yaml.safe_load((root/'profiles/colony-reviews/config.yaml').read_text())
assert selected['model']['default']=='replacement-planning-model' and 'max_tokens' not in selected['model']
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
 assert kb.get_task(db,second_result['native_work']['native_task_id']).status=='ready'
 assert db.execute('SELECT count(*) FROM task_events WHERE kind="created"').fetchone()[0]==2
# Native exhausted failure is a failed review with a reason, while an ordinary
# needs-input block remains resumable. No bridge retry or extra task is made.
second_id=second_result['native_work']['native_task_id']
with kb.connect(board='default') as db:
 claimed=kb.claim_task(db,second_id)
 assert _record_task_failure(db,second_id,'controlled spawn failure',outcome='spawn_failed',
                                release_claim=True,end_run=True)
failed=reviews[1].work(second.id)
assert failed['status']=='failed' and failed['result']['run_outcome']=='gave_up',failed
assert failed['result']['error']=='controlled spawn failure',failed
reviews[0].reconcile(board='default')
from colony_sidecar.turns import get_turn_idempotency_ledger
source_ledger=get_turn_idempotency_ledger(state)
with source_ledger._connect() as evidence:
 sources=evidence.execute('SELECT messages_json FROM turn_sources').fetchall()
 assert len(sources)==1,sources
 message=json.loads(sources[0][0])[0]
 assert message['role']=='assistant' and message['_native_runtime_observation']=='native-runtime-observation-v1'
 assert '"outcome": "gave_up"' in message['content'] and '"served_model": "unknown"' in message['content']
 assert 'controlled spawn failure' not in message['content']
 assert evidence.execute('SELECT count(*) FROM self_judgment_runs').fetchone()[0]==int(os.environ.get('COLONY_SELF_JUDGMENTS_ENABLED')=='1')
 assert evidence.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0]==0
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
 assert kb.unblock_task(db,second_id)
 claimed=kb.claim_task(db,second_id)
 assert kb.block_task(db,second_id,reason='Need one missing observation',kind='needs_input',
                      expected_run_id=claimed.current_run_id)
blocked=reviews[1].work(second.id)
assert blocked['status']=='assigned' and blocked['result']['native_status']=='blocked',blocked
assert blocked['result']['run_outcome']=='blocked',blocked
# A missing profile selection or manifest must not leave a new runnable task.
third=proposal('Review missing worker readiness')
try:NativeReviews(clients[0],'owner').work(third.id)
except ValueError as error:assert str(error)=='read_only_review_profile_not_installed'
else:raise AssertionError('Unconfigured review dispatched')
manifest_path=root/'profiles/colony-reviews/plugins/colony/plugin.yaml'
manifest_before=manifest_path.read_bytes();manifest_path.unlink()
try:
 try:reviews[0].work(third.id)
 except ValueError as error:assert str(error)=='read_only_review_adapter_required'
 else:raise AssertionError('Missing plugin manifest dispatched')
finally:manifest_path.write_bytes(manifest_before)
with kb.connect(board='default') as db:assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
print(json.dumps({'independent_cycles_one_task':True,'actual_native_completion':True,
                  'dispatch_discovers_without_steward':True,'disabled_discovery_no_task':True,
                  'shared_visibility':True,'lost_ack_recovery':True,'failed_vs_blocked':True,
                  'planning_swap':True,'missing_profile_no_dispatch':True,'models':0,'network':0}))
'''


@pytest.mark.parametrize('judgments_enabled', [False, True])
def test_actual_native_initiative_handoff_and_reconciliation(tmp_path, judgments_enabled):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use the existing qualified Hermes interpreter for native integration')
        python = sys.executable
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','LANG') if key in os.environ}
    env.update(HOME=str(tmp_path),HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        COLONY_HERMES_HOME=str(tmp_path/'hermes'),COLONY_HERMES_WORK_BOARDS='["default"]',
        COLONY_STATE_DIR=str(tmp_path/'state'),COLONY_OWNER_CONTACT_ID='owner',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    if judgments_enabled:
        env['COLONY_SELF_JUDGMENTS_ENABLED'] = '1'
    result = subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),
        os.environ.get('AEVA_HERMES_TEST_SOURCE','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"independent_cycles_one_task": true' in result.stdout

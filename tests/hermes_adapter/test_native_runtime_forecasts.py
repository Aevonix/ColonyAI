"""Native attachment -> measured completion -> next actual learned horizon."""
import os
from pathlib import Path
import subprocess

import pytest


PROBE = r'''
import json,os,socket,sys,types,time,hashlib
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
package=types.ModuleType('colony_hermes');package.__path__=[sys.argv[2]];sys.modules['colony_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native forecast qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import initiative_work,host
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.self_model.expectations import ExpectationStore,ExpectationEngine
from colony_sidecar.self_model import runtime_forecasts
from colony_sidecar.turns import get_turn_idempotency_ledger
from colony_sidecar.turns.hermes_kanban import task_snapshot
from colony_hermes.initiative_work import NativeReviews
from colony_hermes.runtime_models import RuntimeModelObserver
from unittest.mock import patch
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
store=InitiativeStore(state);host._initiative_store=store
host._expectations=ExpectationEngine(ExpectationStore(str(state/'expectations.db')))
sources=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.colony_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
     scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),
     audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(initiative_work.router)
client=TestClient(app);worker=NativeReviews(client,'owner')
def proposal(label):
 return store.create(type='operational',description=label,priority=.5,
    action_hint='operational_review',source_type='operational',created_by='autonomy_loop',
    context={'evidence_scope':'local_observation'})
def history(identifier):
 fid='native-task:'+runtime_forecasts._digest({k:v for k,v in identifier.items() if k in {'source_home_id','native_board','native_task_id'}})
 return host._expectations.store.forecast_history(fid)

first=proposal('Inspect local fixture metadata')
started=worker.work(first.id)
first_history=history(started['native_work']);assert len(first_history['forecasts'])==1,started
prediction=first_history['forecasts'][0]
assert prediction['detail']['conditions']['estimate']['sample_n']==0
assert prediction['detail']['model_provenance']['served_model'] is None
with kb.connect(board='default') as db:
 task=kb.claim_task(db,started['native_work']['native_task_id'])
 os.environ.update(HERMES_KANBAN_TASK=task.id,HERMES_KANBAN_RUN_ID=str(task.current_run_id),
                   HERMES_KANBAN_CLAIM_LOCK=task.claim_lock)
 hooks={}
 RuntimeModelObserver(client,'owner').register(types.SimpleNamespace(register_hook=lambda n,f:hooks.update({n:f})))
 with patch('agent.delegation_context.is_dispatcher_owned_worker_context',return_value=True):
  hooks['pre_api_request'](api_request_id='fixture-request',model='requested-alias',provider='custom')
  hooks['post_api_request'](api_request_id='fixture-request',model='requested-alias',provider='custom',
                            response_model='provider-reported-fixture',response={'content':'not retained'})
 assert kb.complete_task(db,task.id,summary='Metadata read; result quality not independently assessed.',expected_run_id=task.current_run_id,fire_lifecycle_hook=False)
for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK'):os.environ.pop(key)
completed=worker.work(first.id)
assert completed['forecast']['status']=='observed',completed
first_history=history(started['native_work'])
assert first_history['outcomes'][0]['status']=='observed'
assert first_history['forecasts'][0]['outcome']=='hit'
native,snapshot=task_snapshot(first.id,'owner',started['native_work'],review=True)
projection=runtime_forecasts.project(started,native,snapshot,'owner')
assert projection['status']=='shadow' and projection['decision']=='terminal',projection
assert projection['served_model']=='provider-reported-fixture' and projection['conditions_comparable'],projection
assert projection['original_served_model'] is None
assert projection['processor_observation']['complete_observed_pairs']
assert 'not_weight_attestation' in projection['processor_observation']['basis']
# Later configuration changes cannot rebind a finished outcome or its forecast.
changed_snapshot={**snapshot,'forecast_configuration':{'runtime_budget_seconds':1,'served_model':'later-model'}}
again=runtime_forecasts.project(started,native,changed_snapshot,'owner')
assert again['conditions_comparable'] and again['served_model']=='provider-reported-fixture'
assert first_history['forecasts'][0]['detail']==prediction['detail']
stale=client.post('/v1/host/initiative-work/'+first.id+'/model-observation',json={
 'contact_id':'owner','native_board':'default','native_task_id':task.id,
 'native_run_id':task.current_run_id,'native_claim_lock':task.claim_lock,
 'contract_sha256':started['review']['sha256'],'api_request_id':'late','phase':'response',
 'response_model':'not-accepted'})
assert stale.status_code==409,stale.text
assert not projection['suggestion_enabled'] and projection['comparison']['receipt_ref']==first_history['outcomes'][0]['receipt_ref']
assert runtime_forecasts.project(started,native,snapshot,'other')['status']=='source_unavailable'
from colony_sidecar.turns.local_work import local_work_view
from colony_sidecar.turns.executions import request_work_context
from colony_sidecar.turns import hermes_kanban
original_snapshot=hermes_kanban.task_snapshot
snapshot_reads=[]
def counted_snapshot(*args,**kwargs):
 snapshot_reads.append(args[0]);return original_snapshot(*args,**kwargs)
hermes_kanban.task_snapshot=counted_snapshot
work_view=local_work_view()
hermes_kanban.task_snapshot=original_snapshot
projected=next(item for item in work_view['recent'] if item['initiative_id']==first.id)
assert projected['forecast']['decision']=='terminal',projected
assert snapshot_reads==[first.id],snapshot_reads
request=request_work_context({'items':[],'local_work':work_view})
assert 'shadow observation only' in request['text'] and '"suggestion_enabled": false' in request['text']
assert 'source_versions' not in request['text'] and 'inspect_recorded_state' not in request['text']
second=proposal('Inspect another local fixture')
second_value=worker.work(second.id)
second_prediction=history(second_value['native_work'])['forecasts'][0]
estimate=second_prediction['detail']['conditions']['estimate']
assert estimate['sample_n']==1 and estimate['seconds']<480,estimate
assert second_prediction['horizon']-second_prediction['detail']['origin_at']==estimate['seconds']
assert second_prediction['detail']['model_provenance']['served_model'] is None
# Confirm the canonical record is runtime-origin source-only, not owner facts.
with sources._connect() as db:
 rows=db.execute('SELECT turn_id,messages_json FROM turn_sources ORDER BY turn_id').fetchall()
 assert len(rows)==5,rows
 assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0]==0
 for row in rows:
  message=json.loads(row['messages_json'])[0]
  assert message['_native_runtime_observation']=='native-task-forecast-v1'
  assert message['role']=='assistant'
# Erasing the exact completed outcome stops it influencing the next forecast.
receipt=first_history['outcomes'][0]['receipt_ref'].removeprefix('receipt:')
sources.erase_sources(turn_ids=[receipt],contact_id='owner')
assert runtime_forecasts.project(started,native,snapshot,'owner')['decision']=='source_unavailable'
third=proposal('Inspect later fixture after erased outcome')
third_value=worker.work(third.id)
third_prediction=history(third_value['native_work'])['forecasts'][0]
assert third_prediction['detail']['conditions']['estimate']['sample_n']==0
assert third_prediction['detail']['conditions']['estimate']['seconds']==480
# Installing/replaying observation never creates retrospective forecasts.
assert runtime_forecasts.observe({'native_work':{},'review':{'action':'operational_review'}},started['native_work'],{},'owner')['status']=='disabled_or_unselected'
print(json.dumps({'native_forecast_issued':True,'independent_outcome':True,'next_horizon_changed':True,
                  'erasure_removes_learning':True,'processor_unknown_honest':True,'models':0,'network':0}))
'''


def test_actual_native_forecast_learning(tmp_path):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        COLONY_HERMES_HOME=str(tmp_path/'hermes'),COLONY_HERMES_WORK_BOARDS='["default"]',
        COLONY_STATE_DIR=str(tmp_path/'state'),COLONY_OWNER_CONTACT_ID='owner',COLONY_EXPECTATIONS='on',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('COLONY_TEST_DEPENDENCY_PATH','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"next_horizon_changed": true' in result.stdout

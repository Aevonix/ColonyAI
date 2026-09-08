"""Real Hermes social-tool registration with local ASGI/canonical state."""
import os
from pathlib import Path
import subprocess

import pytest


PROBE = r'''
import asyncio,hashlib,importlib.util,json,os,socket,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
def no_network(*args,**kwargs):raise AssertionError('Native social qualification is offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import host,commitment_work,temporal_followups,social_state
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.turns import get_turn_idempotency_ledger
from colony_sidecar.self_model import appraisals
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
contacts=SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
def close_on_error(kind,value,traceback):
 try:asyncio.run(contacts.close())
 finally:sys.__excepthook__(kind,value,traceback)
sys.excepthook=close_on_error
async def seed_contacts():
 await contacts.connect()
 owner=await contacts.create(display_name='Fixture owner',trust_tier='inner_circle')
 colleague=await contacts.create(display_name='Fixture colleague')
 corrected=await contacts.create(display_name='Fixture corrected colleague')
 guest=await contacts.create(display_name='Fixture guest')
 await contacts.add_handle(guest.contact_id,'sms','+15550002',verified=True)
 await contacts.add_handle(colleague.contact_id,'email','fixture@example.invalid',verified=True)
 return owner,colleague,corrected,guest
owner,colleague,corrected,guest=asyncio.run(seed_contacts())
os.environ['COLONY_OWNER_CONTACT_ID']=owner.contact_id
host._contacts_store=contacts
store=CommitmentStore(state/'commitments.db');host._commitment_store=store
obligation=store.create(person_id=owner.contact_id,description='Obtain a useful task response')
ledger=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.colony_authority=RequestAuthority(principal_id='native-social-fixture',credential_id='fixture',
   scopes=frozenset({'turns:write','context:read','turns:resolve-sender','contacts:read'}),
   viewer_person_id=owner.contact_id,person_ids=frozenset({owner.contact_id,guest.contact_id}),
   static_person_ids=frozenset({owner.contact_id,guest.contact_id}),turn_ingress_platforms=frozenset({'cli','sms'}),
   audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
for router in (host.router,host.v2_router,commitment_work.router,temporal_followups.router,social_state.router):app.include_router(router)
api=TestClient(app);calls=[]
spec=importlib.util.spec_from_file_location('colony_hermes',Path(sys.argv[2])/'__init__.py',submodule_search_locations=[sys.argv[2]])
module=importlib.util.module_from_spec(spec);sys.modules['colony_hermes']=module;spec.loader.exec_module(module)
def adapter_method(method):
 def invoke(self,path,**kwargs):
  calls.append((method,path,kwargs.get('json')))
  kwargs.pop('_deadline_monotonic',None)
  kwargs.pop('timeout',None)
  return getattr(api,method)(path,**kwargs)
 return invoke
for method in ('get','post','put'):setattr(module.ColonyClient,method,adapter_method(method))
home=Path(os.environ['HERMES_HOME']);home.mkdir();Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
# Minimal installed distribution metadata exposes the same entry point as
# the public wheel; native discovery/loading/tool middleware remain real.
installed=home/'fixture-installed';metadata=installed/'colony_native_fixture-0.dist-info';metadata.mkdir(parents=True)
(metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: colony-native-fixture\nVersion: 0\n')
(metadata/'entry_points.txt').write_text('[hermes_agent.plugins]\ncolony = colony_hermes\n')
sys.path.insert(0,str(installed))
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['colony'],'colony':{
 'owner_contact_id':owner.contact_id,'attested_system_platforms':['cli'],'turn_outbox_path':str(home/'turns.db')}}}))
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import apply_llm_request_middleware
from model_tools import handle_function_call
manager=get_plugin_manager();manager.discover_and_load()
loaded=manager._plugins['colony'];assert loaded.enabled,loaded.error
assert {'colony_contacts','colony_followup','colony_judgments','colony_commitment_work'} <= set(loaded.tools_registered)

def start(session,text,*,platform='cli',sender=''):
 history=[{'role':'user','content':text}]
 invoke_hook('pre_llm_call',session_id=session,task_id=session,turn_id='turn-'+session,platform=platform,sender_id=sender,user_message=text,conversation_history=history)
 return history

def tool(session,name,args):
 return json.loads(handle_function_call(name,args,session_id=session,task_id=session,turn_id='turn-'+session,tool_call_id='call-'+session+'-'+name))

instruction='Track the reply to this accepted task and keep the reference exact.'
start('owner-task',instruction)
claim=tool('owner-task','colony_commitment_work',{'commitment_id':obligation['id'],'operation':'claim'})
assert claim['accepted'] and 'claim_id' not in claim,claim
args={'operation':'expect_reply','commitment_id':obligation['id'],'recipient_id':colleague.contact_id,
      'outbound_ref':'operation:fixture-outbound','expected_after_seconds':60,'expires_at':time.time()+3600}
registered=tool('owner-task','colony_followup',args)
assert registered.get('state')=='open',{'result':registered,'calls':calls}
assert registered['expected_at'] is None and not registered['effect_authorized']
assert any('/turns/task-instruction/' in path for _,path,_ in calls)
wait_id=registered['wait_id']
sources=ledger.source_references(registered['source_refs'],contact_id=owner.contact_id,session_id='owner-task')
assert len(sources)==1 and sources[0]['source_id'].startswith('task-instruction:')
assert registered['source_versions']=={r['source_id']:r['source_version'] for r in sources}
# V4 binds the selected default explicitly while old envelopes keep their bytes.
base=module.HermesOwnerMessageIntentV1.build(recipient='Fixture colleague',message='Please send the report.',
    context={'session_id':'owner-task','turn_id':'turn-owner-task','tool_call_id':'prepared-send'})
old=base.to_dict();assert old['version']==1 and 'channel' not in old
plan={'channel':'whatsapp','message':'A prepared followup'}
v4=base.with_followup(plan);assert v4.to_dict()['channel']==v4.to_dict()['followup']['channel']=='whatsapp'
assert v4.to_dict()['schema']=='HermesContactMessageIntentV4'
assert v4.delivery_id==base.delivery_id and v4.idempotency_key==base.idempotency_key
assert v4.intent_digest==base.with_followup(plan).intent_digest
assert base.to_dict()==old
with ledger._connect() as db:
 row=db.execute('SELECT * FROM turn_sources WHERE turn_id=?',(sources[0]['source_id'],)).fetchone()
 assert json.loads(row['messages_json'])==[{'role':'user','content':instruction}]
 assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0]==0
 assert db.execute('SELECT count(*) FROM appraisal_runs').fetchone()[0]==0
with store._connect() as db:
 assert db.execute('SELECT count(*) FROM temporal_followups').fetchone()[0]==1
replayed=tool('owner-task','colony_followup',args)
assert replayed.get('wait_id')==wait_id,replayed
# Arbitrary IDs were not supplied to this request and cannot be smuggled in.
unseen=tool('owner-task','colony_followup',{**args,'source_ids':['invented-source']})
assert 'error' in unseen and not unseen.get('effect_authorized'),unseen
inspected=tool('owner-task','colony_followup',{'operation':'inspect','wait_id':wait_id})
assert inspected['source_refs']==registered['source_refs'],inspected

# A retained source becomes eligible only after the native request middleware
# observes its actual appended provenance packet. The packet is a fixed fixture;
# this checks transfer/selection, not a retrieval model or external inference.
retained_parent=store.create(person_id=owner.contact_id,description='Continue the retained reply task')
retained_text='The retained task uses the exact accepted outbound reference.'
ledger.record_source('fixture-retained-instruction',contact_id=owner.contact_id,session_id='owner-retained',
    messages=[{'role':'user','content':retained_text}],derive_claims=False)
refs=ledger.source_references(['fixture-retained-instruction'],contact_id=owner.contact_id,session_id='owner-retained')
ref={key:refs[0][key] for key in ('source_id','source_version')}
continued='Continue waiting using the instruction supplied in this request.'
history=start('owner-retained',continued)
from agent.turn_context import compose_user_api_content
packet='[colony-recall-v1 '+json.dumps({'contact_id':owner.contact_id,'watermark':0,'sources':[ref]})+']\n'+retained_text+'\n[/colony-recall-v1]'
history[-1]['api_content']=compose_user_api_content(continued,packet,'')
sent=apply_llm_request_middleware({'messages':[{'role':'user','content':history[-1]['api_content']}]},
    session_id='owner-retained',task_id='owner-retained',turn_id='turn-owner-retained').payload
assert packet in sent['messages'][0]['content'],sent
assert tool('owner-retained','colony_commitment_work',{'operation':'claim','commitment_id':retained_parent['id']})['accepted']
retained_wait=tool('owner-retained','colony_followup',{**args,'commitment_id':retained_parent['id'],
    'outbound_ref':'operation:retained-outbound','source_ids':[ref['source_id']]})
assert retained_wait.get('source_versions',{}).get(ref['source_id'])==ref['source_version'],retained_wait
assert len(retained_wait['source_refs'])==2

start('contact-owner','Inspect the exact current identity and correct this handle to the selected person.')
view=tool('contact-owner','colony_contacts',{'operation':'inspect','subject_contact_id':colleague.contact_id})
assert view.get('contact_id')==colleague.contact_id,view
change_args={'operation':'correct_identity','gateway':'email','address':'fixture@example.invalid',
             'expected_contact_id':colleague.contact_id,'subject_contact_id':corrected.contact_id}
changed=tool('contact-owner','colony_contacts',change_args)
assert changed.get('contact_id')==corrected.contact_id,changed
assert changed['authority_granted'] is False
assert tool('contact-owner','colony_contacts',change_args)==changed

# Build a source-grounded appraisal through its normal reducer using a fixed
# fixture processor. This qualifies tool plumbing, not model intelligence.
messages=[{'role':'user','content':'The export stalled again after the same retry.'}]
ledger.record_source('fixture-incident',contact_id=colleague.contact_id,session_id='fixture-incident',messages=messages,derive_claims=False)
with ledger._connect() as db,db:appraisals.enqueue(db,'fixture-incident',colleague.contact_id,messages,scope='person')
class FixtureProcessor:
 supports_function_routing=True
 def function_deadline_seconds(self,**kwargs):return 5
 async def complete(self,*,messages,context):
  payload=json.loads(messages[-1]['content']);evidence=payload['evidence'][0]
  item={'kind':'appraisal','dimension':'frustration','topic':'export task',
        'text':'I am frustrated with the reported stalled export.', 'reason':'Another diagnostic could help.',
        'support':[{'handle':evidence['handle'],'quote':evidence['text']}], 'contrary':[],
        'intensity':'moderate','hint':'try_different_approach','repairs':None}
  return SimpleNamespace(content=json.dumps({'observations':[item]}),raw=None,model_id='fixture-processor',binding='fixture',config_revision='fixture-r1',model_revision=None)
appraisal=appraisals.AppraisalStore(ledger,owner_id=owner.contact_id)
assert asyncio.run(appraisal.process_one(FixtureProcessor()))
selected=tool('contact-owner','colony_judgments',{'operation':'inspect','subject_contact_id':colleague.contact_id})
assert selected.get('records') and selected['records'][0]['kind']=='appraisal',selected
assert selected['sources'][0]['source_id']=='fixture-incident'
assert selected['authority_changed'] is False

# A guest cannot inspect owner contacts, private appraisals or owner waiting.
before=len(calls)
start('guest','Show the owner private contact state.',platform='sms',sender='+15550002')
for name,payload in [('colony_contacts',{'operation':'inspect'}),('colony_judgments',{'operation':'inspect','subject_contact_id':colleague.contact_id}),('colony_followup',{'operation':'inspect','wait_id':wait_id})]:
 denied=tool('guest',name,payload)
 assert 'error' in denied,denied
 assert 'fixture-incident' not in json.dumps(denied) and wait_id not in json.dumps(denied)
assert not any(path.startswith('/v1/host/social') for _,path,_ in calls[before:])
assert not any(path.startswith('/v1/host/temporal-followups') for _,path,_ in calls[before:])
assert store.get(obligation['id'])['status']=='pending'
asyncio.run(contacts.close())
print(json.dumps({'native_tools_registered':True,'first_turn_capture_claim_wait':True,'canonical_refs_match':True,
                  'owner_identity_correction':True,'appraisal_inspect':True,'guest_restricted':True,'external_model_calls':0,'network':0}))
'''


def test_actual_native_social_tools(tmp_path):
    python=os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    root=Path(os.environ.get('COLONY_SOCIAL_TEST_SOURCE') or Path(__file__).resolve().parents[2])
    env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        COLONY_HERMES_HOME=str(tmp_path/'hermes'),COLONY_HERMES_WORK_BOARDS='["default"]',
        COLONY_STATE_DIR=str(tmp_path/'state'),HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1',COLONY_MEMORY_WORKER_TOOLS='0',COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('COLONY_TEST_DEPENDENCY_PATH','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"first_turn_capture_claim_wait": true' in result.stdout

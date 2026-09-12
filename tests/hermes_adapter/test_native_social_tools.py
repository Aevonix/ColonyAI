"""Real Hermes social-tool registration with local ASGI/canonical state."""
import os
from pathlib import Path
import subprocess

import pytest


PROBE = r'''
import asyncio,hashlib,importlib.util,json,os,socket,sys,time
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
if len(sys.argv)>4 and sys.argv[4]:sys.path.insert(0,sys.argv[4])
def no_network(*args,**kwargs):raise AssertionError('Native social qualification is offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pacomind.api.middleware import ApiKeyMiddleware
from pacomind.api.routers import host,commitment_work,temporal_followups,social_state,executions,transport,followup_plans
from pacomind.commitments.store import CommitmentStore
from pacomind.contacts.store import SQLiteContactStore
from pacomind.contacts.config import ContactsConfig
from pacomind.contacts.comms import CommsLog
from pacomind.turns import get_turn_idempotency_ledger
from pacomind.self_model import appraisals
state=Path(os.environ['PACOMIND_STATE_DIR']);state.mkdir()
contacts=SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
def close_on_error(kind,value,traceback):
 try:
  asyncio.run(contacts.close())
  if 'api' in globals():api.__exit__(None,None,None)
 finally:sys.__excepthook__(kind,value,traceback)
sys.excepthook=close_on_error
async def seed_contacts():
 await contacts.connect()
 owner=await contacts.create(display_name='Fixture owner',trust_tier='inner_circle')
 colleague=await contacts.create(display_name='Fixture colleague')
 corrected=await contacts.create(display_name='Fixture corrected colleague')
 guest=await contacts.create(display_name='Fixture guest')
 await contacts.add_handle(guest.contact_id,'sms','+15550002',verified=True)
 await contacts.add_handle(owner.contact_id,'sms','+15550001',verified=True)
 await contacts.add_handle(owner.contact_id,'whatsapp','+15550003',verified=True)
 await contacts.add_handle(owner.contact_id,'whatsapp','15550001@s.whatsapp.net',verified=True)
 await contacts.add_handle(colleague.contact_id,'email','fixture@example.invalid',verified=True)
 return owner,colleague,corrected,guest
owner,colleague,corrected,guest=asyncio.run(seed_contacts())
os.environ['PACOMIND_OWNER_CONTACT_ID']=owner.contact_id
host._contacts_store=contacts
store=CommitmentStore(state/'commitments.db');host._commitment_store=store
obligation=store.create(person_id=owner.contact_id,description='Obtain a useful task response')
ledger=get_turn_idempotency_ledger(state)
app=FastAPI()
resolver_access={'allowed':True,'available':True}
keyring=state/'keys.json'
def write_keys(active=True):
 keyring.write_text(json.dumps({'version':1,'principals':[{'principal':'native-social-fixture',
  'status':'active' if active else 'revoked','viewer_person_id':owner.contact_id,
  'person_ids':[owner.contact_id,guest.contact_id], 'audiences':['viewer'],
  'turn_ingress_platforms':['cli','sms','whatsapp'],
  'scopes':['api:access','turns:write','context:read','turns:resolve-sender','memory:read','transport:write'],
  'credentials':[{'id':'fixture','secret':'fixture','status':'active'}]},
  {'principal':'fixture-provider','status':'active','viewer_person_id':owner.contact_id,
   'audiences':['viewer'],'scopes':['transport:write'],
   'credentials':[{'id':'provider','secret':'provider-fixture','status':'active'}]}]}))
 keyring.chmod(0o600)
write_keys()
@app.middleware('http')
async def authority(request,next_call):
 if request.url.path=='/v1/host/contacts/resolve' and not resolver_access['available']:
  from starlette.responses import JSONResponse
  return JSONResponse({'detail':'fixture resolver unavailable'},status_code=503)
 return await next_call(request)
for router in (host.router,host.v2_router,commitment_work.router,temporal_followups.router,social_state.router,executions.router,transport.router,followup_plans.router):app.include_router(router)
app.add_middleware(ApiKeyMiddleware,api_key=None,keyring_path=str(keyring))
api=TestClient(app,headers={'Authorization':'Bearer fixture'});calls=[];resolver_cost_ms=[]
# CommsLog belongs to the persistent ASGI event loop, as in production.
# A bare TestClient starts a different loop for every request.
api.__enter__()
comms=api.portal.call(lambda:CommsLog(str(state/'communications.db')));host._comms_log=comms
spec=importlib.util.spec_from_file_location('pacomind_hermes',Path(sys.argv[2])/'__init__.py',submodule_search_locations=[sys.argv[2]])
module=importlib.util.module_from_spec(spec);sys.modules['pacomind_hermes']=module;spec.loader.exec_module(module)
def adapter_method(method):
 def invoke(self,path,**kwargs):
  calls.append((method,path,kwargs.get('json')))
  kwargs.pop('_deadline_monotonic',None)
  kwargs.pop('timeout',None)
  began=time.monotonic()
  response=getattr(api,method)(path,**kwargs)
  if path=='/v1/host/contacts/resolve':resolver_cost_ms.append((time.monotonic()-began)*1000)
  return response
 return invoke
for method in ('get','post','put'):setattr(module.PacoMindClient,method,adapter_method(method))
home=Path(os.environ['HERMES_HOME']);home.mkdir();Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
# Minimal installed distribution metadata exposes the same entry point as
# the public wheel; native discovery/loading/tool middleware remain real.
installed=home/'fixture-installed';metadata=installed/'pacomind_native_fixture-0.dist-info';metadata.mkdir(parents=True)
(metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: pacomind-native-fixture\nVersion: 0\n')
(metadata/'entry_points.txt').write_text('[hermes_agent.plugins]\npacomind = pacomind_hermes\n')
sys.path.insert(0,str(installed))
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['pacomind'],'pacomind':{
 'owner_contact_id':owner.contact_id,'attested_system_platforms':['cli','cron'],
 'execution_registry_enabled':True,'turn_outbox_path':str(home/'turns.db')}}}))
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import apply_llm_request_middleware,run_tool_execution_middleware
from model_tools import handle_function_call
manager=get_plugin_manager();manager.discover_and_load()
loaded=manager._plugins['pacomind'];assert loaded.enabled,loaded.error
assert {'pacomind_contacts','pacomind_followup','pacomind_judgments','pacomind_commitment_work'} <= set(loaded.tools_registered)

def start(session,text,*,platform='cli',sender=''):
 history=[{'role':'user','content':text}]
 invoke_hook('pre_llm_call',session_id=session,task_id=session,turn_id='turn-'+session,platform=platform,sender_id=sender,user_message=text,conversation_history=history)
 return history

def tool(session,name,args):
 return json.loads(handle_function_call(name,args,session_id=session,task_id=session,turn_id='turn-'+session,tool_call_id='call-'+session+'-'+name))

instruction='Track the reply to this accepted task and keep the reference exact.'
start('owner-task',instruction)
claim=tool('owner-task','pacomind_commitment_work',{'commitment_id':obligation['id'],'operation':'claim'})
assert claim['accepted'] and 'claim_id' not in claim,claim
args={'operation':'expect_reply','commitment_id':obligation['id'],'recipient_id':colleague.contact_id,
      'outbound_ref':'operation:fixture-outbound','expected_after_seconds':60,'expires_at':time.time()+3600}
registered=tool('owner-task','pacomind_followup',args)
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
replayed=tool('owner-task','pacomind_followup',args)
assert replayed.get('wait_id')==wait_id,replayed
# Arbitrary IDs were not supplied to this request and cannot be smuggled in.
unseen=tool('owner-task','pacomind_followup',{**args,'source_ids':['invented-source']})
assert 'error' in unseen and not unseen.get('effect_authorized'),unseen
inspected=tool('owner-task','pacomind_followup',{'operation':'inspect','wait_id':wait_id})
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
packet='[pacomind-recall-v1 '+json.dumps({'contact_id':owner.contact_id,'watermark':0,'sources':[ref]})+']\n'+retained_text+'\n[/pacomind-recall-v1]'
history[-1]['api_content']=compose_user_api_content(continued,packet,'')
sent=apply_llm_request_middleware({'messages':[{'role':'user','content':history[-1]['api_content']}]},
    session_id='owner-retained',task_id='owner-retained',turn_id='turn-owner-retained').payload
sent_user=[row for row in sent['messages'] if row['role']=='user']
assert len(sent_user)==1 and sent_user[0]['content'].split('\n\n<memory-context>',1)[0]==continued,sent
assert packet in sent_user[0]['content'],sent
assert tool('owner-retained','pacomind_commitment_work',{'operation':'claim','commitment_id':retained_parent['id']})['accepted']
retained_wait=tool('owner-retained','pacomind_followup',{**args,'commitment_id':retained_parent['id'],
    'outbound_ref':'operation:retained-outbound','source_ids':[ref['source_id']]})
assert retained_wait.get('source_versions',{}).get(ref['source_id'])==ref['source_version'],retained_wait
assert len(retained_wait['source_refs'])==2

start('contact-owner','Inspect the exact current identity and correct this handle to the selected person.')
view=tool('contact-owner','pacomind_contacts',{'operation':'inspect','subject_contact_id':colleague.contact_id})
assert view.get('contact_id')==colleague.contact_id,view
change_args={'operation':'correct_identity','gateway':'email','address':'fixture@example.invalid',
             'expected_contact_id':colleague.contact_id,'subject_contact_id':corrected.contact_id}
changed=tool('contact-owner','pacomind_contacts',change_args)
assert changed.get('contact_id')==corrected.contact_id,changed
assert changed['authority_granted'] is False
assert tool('contact-owner','pacomind_contacts',change_args)==changed

# Correct one exact transport and its selected source while another native
# request still holds that source. Neither the erasure watermark nor source
# bytes change, so only current canonical attribution can fence the packet.
source_change={'operation':'correct_identity','gateway':'whatsapp','address':'15550001@s.whatsapp.net',
               'expected_contact_id':owner.contact_id,'subject_contact_id':guest.contact_id,
               'source_ids':[ref['source_id']]}
source_receipt=tool('contact-owner','pacomind_contacts',source_change)
assert source_receipt.get('source_reconciliation_required') is False,source_receipt
assert source_receipt['contact_id']==guest.contact_id and not source_receipt['authority_granted']
assert ledger.erasure_watermark(owner.contact_id)==0
assert api.get('/v1/host/contacts/resolve',params={'gateway':'sms','address':'+15550001'}).json()['contact_id']==owner.contact_id
assert api.get('/v1/host/contacts/resolve',params={'gateway':'whatsapp','address':'15550001@s.whatsapp.net'}).json()['contact_id']==guest.contact_id
retained_request={'messages':[{'role':'user','content':history[-1]['api_content']}]}
after_correction=apply_llm_request_middleware(retained_request,session_id='owner-retained',
    task_id='owner-retained',turn_id='turn-owner-retained').payload
assert retained_text not in json.dumps(after_correction) and continued in json.dumps(after_correction),after_correction
assert ledger.source_references([ref['source_id']],contact_id=guest.contact_id,session_id='guest-voice')==[ref]
reversed_source=tool('contact-owner','pacomind_contacts',{**source_change,
    'expected_contact_id':guest.contact_id,'subject_contact_id':owner.contact_id})
assert reversed_source.get('source_reconciliation_required') is False,reversed_source
after_reversal=apply_llm_request_middleware(retained_request,session_id='owner-retained',
    task_id='owner-retained',turn_id='turn-owner-retained').payload
assert retained_text in json.dumps(after_reversal),after_reversal
assert any(method=='post' and path=='/v1/host/memory/sources/erasures' for method,path,_ in calls)

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
        'intensity':'moderate','hint':'try_different_approach'}
  return SimpleNamespace(content=json.dumps({'observations':[item],'incident_decisions':[]}),raw=None,model_id='fixture-processor',binding='fixture',config_revision='fixture-r1',model_revision=None)
appraisal=appraisals.AppraisalStore(ledger,owner_id=owner.contact_id)
assert asyncio.run(appraisal.process_one(FixtureProcessor()))
selected=tool('contact-owner','pacomind_judgments',{'operation':'inspect','subject_contact_id':colleague.contact_id})
assert selected.get('records') and selected['records'][0]['kind']=='appraisal',selected
assert selected['sources'][0]['source_id']=='fixture-incident'
assert selected['authority_changed'] is False

# A guest cannot inspect owner contacts, private appraisals or owner waiting.
before=len(calls)
start('guest','Show the owner private contact state.',platform='sms',sender='+15550002')
for name,payload in [('pacomind_contacts',{'operation':'inspect'}),('pacomind_judgments',{'operation':'inspect','subject_contact_id':colleague.contact_id}),('pacomind_followup',{'operation':'inspect','wait_id':wait_id})]:
 denied=tool('guest',name,payload)
 assert 'error' in denied,denied
 assert 'fixture-incident' not in json.dumps(denied) and wait_id not in json.dumps(denied)
assert not any(path.startswith('/v1/host/social') for _,path,_ in calls[before:])
assert not any(path.startswith('/v1/host/temporal-followups') for _,path,_ in calls[before:])
assert store.get(obligation['id'])['status']=='pending'

# Two admitted owner channels share one accepted task; a native child inherits
# its exact parent rather than the most recent owner session.
shared=store.create(person_id=owner.contact_id,description='Inspect the shared fixture once')
start('shared-sms','Inspect the shared fixture.',platform='sms',sender='+15550001')
start('shared-wa','Inspect the shared fixture.',platform='whatsapp',sender='+15550003')
with ThreadPoolExecutor(2) as pool:
 raced=list(pool.map(lambda s:tool(s,'pacomind_commitment_work',{'commitment_id':shared['id'],'operation':'claim'}),
                     ['shared-sms','shared-wa']))
assert all('accepted' in r for r in raced),raced
assert sorted(r['accepted'] for r in raced)==[False,True],raced
winner=next(r['session_id'] for r in raced if r['accepted'])
loser=next(s for s in ('shared-sms','shared-wa') if s!=winner)
assert tool(loser,'pacomind_commitment_work',{'commitment_id':shared['id'],'operation':'status'})['session_id']==winner
invoke_hook('subagent_start',parent_session_id=winner,parent_turn_id='turn-'+winner,child_session_id='shared-child')
invoke_hook('pre_llm_call',session_id='shared-child',task_id='child-task',turn_id='child-turn',
            parent_session_id=winner,platform='subagent',user_message='Inspect one part')
start('registered-cron','Inspect the local schedule.',platform='cron')
view=api.get('/v1/host/executions',params={'contact_id':owner.contact_id}).json()
items={i['session_id']:i for i in view['items']}
assert {'shared-sms','shared-wa','shared-child','registered-cron'}<=items.keys(),view
assert items['shared-child']['parent_execution_id']==items[winner]['execution_id']
assert items['registered-cron']['platform']=='cron' and view['complete'] is False

# A verified owner handle is corrected while its old native turn and child are
# still alive. The next privileged operation must not use cached old identity.
start('revoked-sms','Inspect a local fixture.',platform='sms',sender='+15550001')
invoke_hook('subagent_start',parent_session_id='revoked-sms',parent_turn_id='turn-revoked-sms',child_session_id='revoked-child')
invoke_hook('pre_llm_call',session_id='revoked-child',task_id='revoked-child-task',turn_id='revoked-child-turn',
            parent_session_id='revoked-sms',platform='subagent',user_message='Inspect one part')
before_correction=[]
assert run_tool_execution_middleware('read_file',{},lambda a:before_correction.append(a) or 'read',
    session_id='revoked-sms',task_id='revoked-sms',turn_id='turn-revoked-sms')=='read'
cost_start=len(resolver_cost_ms)
dispatch_total_ms=[]
for _ in range(5):
 began=time.monotonic()
 assert run_tool_execution_middleware('read_file',{},lambda a:'read',
    session_id='revoked-sms',task_id='revoked-sms',turn_id='turn-revoked-sms')=='read'
 dispatch_total_ms.append((time.monotonic()-began)*1000)
dispatch_resolve_cost_ms=resolver_cost_ms[cost_start:]
changed_owner=tool('contact-owner','pacomind_contacts',{'operation':'correct_identity','gateway':'sms','address':'+15550001',
    'expected_contact_id':owner.contact_id,'subject_contact_id':guest.contact_id})
assert changed_owner.get('contact_id')==guest.contact_id,changed_owner
for session,task_id,turn_id in [('revoked-sms','revoked-sms','turn-revoked-sms'),
                               ('revoked-child','revoked-child-task','revoked-child-turn')]:
 called=[]
 result=run_tool_execution_middleware('read_file',{},lambda a:called.append(a) or 'must not run',
    session_id=session,task_id=task_id,turn_id=turn_id)
 assert called==[] and json.loads(result)['effect_performed'] is False,(session,result)
assert len(dispatch_resolve_cost_ms)==5,dispatch_resolve_cost_ms
assert run_tool_execution_middleware('read_file',{},lambda a:'owner cli still works',
    session_id='contact-owner',task_id='contact-owner',turn_id='turn-contact-owner')=='owner cli still works'

# Server-side credential scope revocation and outage are distinguished. The
# unchanged WhatsApp owner turn must not execute either a native or PacoMind tool.
for allowed,available,reason in [(False,True,'participant_authority_revoked'),
                                (True,False,'participant_revalidation_unavailable')]:
 resolver_access.update(allowed=allowed,available=available)
 write_keys(allowed)
 called=[]
 result=run_tool_execution_middleware('read_file',{},lambda a:called.append(a),
    session_id='shared-wa',task_id='shared-wa',turn_id='turn-shared-wa')
 assert not called and json.loads(result)['reason']==reason,result
 denial=tool('shared-wa','pacomind_contacts',{'operation':'inspect'})
 assert denial['effect_performed'] is False and denial['reason']==reason,denial
resolver_access.update(allowed=True,available=True)
write_keys()

# A real provider receipt starts a short fixture expectation; loss of intake
# coverage never means the recipient ignored it. A new unqualified worker is
# held; a historical native review remains reconcilable without any send.
from pacomind_hermes.initiative_work import NativeFollowups
from pacomind.initiatives.temporal_followup import TemporalFollowups
from hermes_cli import kanban_db as kb
follow_parent=store.create(person_id=owner.contact_id,description='Obtain the fixture response')
start('follow-owner','Obtain the response and prepare one followup if needed.')
assert tool('follow-owner','pacomind_commitment_work',{'operation':'claim','commitment_id':follow_parent['id']})['accepted']
follow=tool('follow-owner','pacomind_followup',{'operation':'expect_reply','commitment_id':follow_parent['id'],
    'recipient_id':colleague.contact_id,'outbound_ref':'fixture:accepted-message',
    'expected_after_seconds':.001,'expires_at':time.time()+3600})
assert follow['state']=='open' and follow['expected_at'] is None,follow
registration=next(body for method,path,body in reversed(calls) if path=='/v1/host/temporal-followups' and method=='post')
wait=follow['wait_id'];sid=follow['source_refs'][0];message='Please send the fixture response when convenient.'
plan=dict(wait_id=wait,commitment_id=follow_parent['id'],work_id=registration['work_id'],
    source_id=sid,source_version=follow['source_versions'][sid],recipient_id=colleague.contact_id,
    channel='whatsapp',purpose='Obtain fixture response',expires_at=follow['expires_at'],max_followups=1,
    message=message,message_sha256=hashlib.sha256(message.encode()).hexdigest())
url='/v1/host/temporal-followups/'+wait
bound=api.post(url+'/bind-plan',json={key:registration[key] for key in ('contact_id','session_id','turn_id','claim_id')}|{'plan':plan})
assert bound.status_code==200 and not bound.json()['effect_authorized'],bound.text
provider={'Authorization':'Bearer provider-fixture'}
check={'plan':plan,'outbound_ref':'fixture:accepted-message','target':{'channel':'whatsapp','recipient_id':'fixture-handle'}}
def event(event_id,direction,**extra):
 return dict(event_id=event_id,contact_id=colleague.contact_id,channel='whatsapp',direction=direction,
    external_ref=event_id,receipt_ref='receipt:'+event_id,status='accepted' if direction=='out' else 'received',
    occurred_at=datetime.now(timezone.utc).isoformat(),**extra)
receipt=api.post('/v1/host/transport/observe',headers=provider,
    json=event('provider-original','out',outbound_ref='fixture:accepted-message'))
assert receipt.status_code==200,receipt.text
waiting=TemporalFollowups(store)
assert waiting.get(wait)['dispatch_receipt_ref']=='receipt:provider-original'
unknown=api.post(url+'/check-plan',headers=provider,json=check)
assert unknown.status_code==200 and not unknown.json()['review_allowed'],unknown.text
assert unknown.json()['reason']=='intake_coverage_unknown'
created=waiting.get(wait)['created_at']
for connected in (False,True):
 coverage=api.post('/v1/host/transport/ingress/coverage',headers=provider,json={
   'account_id':'fixture-account','epoch':'fixture-epoch','connected_since':created-1,
   'observed_at':time.time(),'watermark':0,'connected':connected,'unavailable':0})
 assert coverage.status_code==200,coverage.text
 checked=api.post(url+'/check-plan',headers=provider,json=check)
 assert checked.status_code==200 and checked.json()['review_allowed'] is connected,checked.text
 assert checked.json()['transport_coverage']['observed'] is connected
assert not waiting.preflight(wait)['dispatch_allowed']
reviews=[NativeFollowups(api,owner.contact_id),NativeFollowups(api,owner.contact_id)]
def refuse_unqualified(index):
 try:reviews[index].work(wait)
 except ValueError as error:assert str(error)=='readonly_followup_worker_unqualified'
 else:raise AssertionError('New unrestricted followup worker was admitted')
with ThreadPoolExecutor(2) as pool:
 list(pool.map(refuse_unqualified,range(2)))
with kb.connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks WHERE idempotency_key=?',('pacomind-followup:'+wait,)).fetchone()[0]==0
# Seed the pre-upgrade association with real native/HTTP calls; this is not
# an admission through the newly disabled dispatch path.
review=api.get(url,params={'contact_id':owner.contact_id}).json()['review']
with kb.connect(board='default') as db:
 native_id=kb.create_task(db,title=review['title'],body=review['body'],assignee='default',
     created_by='pacomind-followup',tenant=owner.contact_id,idempotency_key='pacomind-followup:'+wait,
     workspace_kind='scratch',initial_status='blocked')
association=api.post(url+'/native-task',json={'contact_id':owner.contact_id,'native_board':'default',
    'native_task_id':native_id,'contract_sha256':review['sha256']})
assert association.status_code==200,association.text
with kb.connect(board='default') as db:
 assert kb.promote_task(db,native_id,actor='historical-fixture',reason='Pre-upgrade queued task')[0]
reviews[0].work(wait)
with kb.connect(board='default') as db:
 assert kb.get_task(db,native_id).status=='blocked' and kb.get_task(db,native_id).block_kind=='needs_input'
 assert kb.latest_run(db,native_id).claim_lock is None # Native blocked receipt, no worker claim.
reply=event('provider-reply','in',reply_to_ref='provider-original',reply_to_channel='whatsapp')
reply['channel']='email'
received=api.post('/v1/host/transport/observe',headers=provider,json=reply)
assert received.status_code==200,received.text
assert waiting.get(wait)['state']=='resolved'
assert waiting.get(wait)['reply']['matches'][0]['provider_reply_to_ref']=='whatsapp:provider-original'
reviews[0].reconcile(board='default')
with kb.connect(board='default') as db:
 assert kb.get_task(db,native_id).status=='archived' and kb.latest_run(db,native_id).claim_lock is None
assert store.get(follow_parent['id'])['status']=='pending'
assert not waiting.preflight(wait)['dispatch_allowed']

# Exercise a real cron run and a real native child lifecycle. Only the model
# transport is controlled; scheduler, agents, plugins and the shared API run.
from run_agent import AIAgent
from openai.types.chat import ChatCompletion
from cron import scheduler
from tools import delegate_tool
observed_runtime=[]
def controlled_model(agent,kwargs,**ignored):
 snapshot=api.get('/v1/host/executions',params={'contact_id':owner.contact_id,'limit':100}).json()
 actual=next(i for i in snapshot['items'] if i['session_id']==agent.session_id)
 observed_runtime.append(actual)
 return ChatCompletion(id='controlled-native',created=int(time.time()),object='chat.completion',model='fixture',
     choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'Fixture review completed.'}}])
runtime={'provider':'custom','requested_provider':'custom','api_key':'fixture','base_url':'http://fixture.invalid/v1','api_mode':'chat_completions'}
setup=scheduler._CronAgentSetup(model='fixture',runtime=runtime,max_iterations=2)
with patch.object(AIAgent,'_interruptible_api_call',controlled_model),patch.object(AIAgent,'_interruptible_streaming_api_call',controlled_model),patch.object(scheduler,'_resolve_cron_agent_setup',lambda *a:setup):
 cron=scheduler.run_job({'id':'shared-state-fixture','name':'Shared state fixture','model':'fixture','prompt':'Inspect the fixture schedule.',
                         'deliver':'none','toolsets':[]})
 assert cron[0] and cron[2]=='Fixture review completed.',cron
 parent=AIAgent(model='fixture',provider='custom',api_key='fixture',base_url='http://fixture.invalid/v1',
    api_mode='chat_completions',session_id='contact-owner',enabled_toolsets=[],max_iterations=2,
    skip_context_files=True,load_soul_identity=False,skip_memory=True,skip_background_review=True)
 parent._current_turn_id='turn-contact-owner'
 try:
  child=delegate_tool._build_child_agent(0,'Inspect one fixture component.',None,[],None,2,1,parent)
  completed=delegate_tool._run_single_child(0,'Inspect one fixture component.',child,parent)
  assert completed['status']=='completed',completed
 finally:parent.close()
assert {i['platform'] for i in observed_runtime}=={'cron','subagent'},observed_runtime
actual_child=next(i for i in observed_runtime if i['platform']=='subagent')
assert actual_child['parent_execution_id']==items['contact-owner']['execution_id']
api.portal.call(comms._conn.close)
api.__exit__(None,None,None)
asyncio.run(contacts.close())
print(json.dumps({'native_tools_registered':True,'first_turn_capture_claim_wait':True,'canonical_refs_match':True,
                  'owner_identity_correction':True,'appraisal_inspect':True,'guest_restricted':True,
                  'concurrent_owner_commitment':True,'next_operation_identity_correction':True,'inherited_child_correction':True,
                  'cached_recall_identity_correction_and_reversal':True,'exact_phone_handle_split':True,
                  'actual_credential_revocation':True,'resolver_outage_distinct':True,'attested_cli_preserved':True,
                  'native_cron_and_child':True,'dispatch_resolver_cost_ms':dispatch_resolve_cost_ms,'dispatch_total_ms':dispatch_total_ms,
                  'receipt_starts_wait':True,'outage_not_silence':True,'new_unrestricted_followup_refused':True,
                  'historical_followup_held':True,'matched_reply_cancels_review':True,
                  'external_model_calls':0,'network':0}))
'''


def test_actual_native_social_tools(tmp_path):
    python=os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    root=Path(os.environ.get('PACOMIND_SOCIAL_TEST_SOURCE') or Path(__file__).resolve().parents[2])
    env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        PACOMIND_HERMES_HOME=str(tmp_path/'hermes'),PACOMIND_HERMES_WORK_BOARDS='["default"]',
        PACOMIND_STATE_DIR=str(tmp_path/'state'),HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        PACOMIND_GENERAL_PLUGIN_ACTIVE='1',PACOMIND_MEMORY_WORKER_TOOLS='0',PACOMIND_MEMORY_TURN_WRITER='disabled',
        PACOMIND_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH',''),
        os.environ.get('AEVA_HERMES_TEST_SOURCE','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"first_turn_capture_claim_wait": true' in result.stdout
    print(result.stdout.splitlines()[-1])

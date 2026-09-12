"""Built native task source adapter, real contact/source APIs and SQLite erasure.

The HTTP transport is an in-process ASGI fixture; no model, native executor,
device, or external service participates in these source-boundary checks.
"""
import os

import pytest

from conftest import ROOT, run_python


PROBE = r'''
import asyncio, copy, json, os, socket, sqlite3, sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import host
from pacomind.contacts.config import ContactsConfig
from pacomind.contacts.store import SQLiteContactStore
from pacomind.turns import get_turn_idempotency_ledger
from pacomind_hermes import _TransportScope, _TransportScopeRegistry
from pacomind_hermes.client import PacoMindClient, TurnOutbox, source_message_hash
from pacomind_hermes.input_provenance import supplied_input
from pacomind_hermes.task_handoffs import TaskHandoffs, TaskHandoffError, erase_task_handoffs
from pacomind_hermes.task_sources import NativeTaskSources

def no_network(*args,**kwargs):raise AssertionError('Native task source qualification has no network access')
socket.socket.connect=no_network;socket.create_connection=no_network
state=Path(os.environ['PACOMIND_STATE_DIR']);state.mkdir(mode=0o700)
contacts=SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
async def seed():
 await contacts.connect()
 owner=await contacts.create(display_name='Fixture task owner',trust_tier='inner_circle')
 guest=await contacts.create(display_name='Fixture participant')
 await contacts.add_handle(owner.contact_id,'sms','+15550001',verified=True)
 await contacts.add_handle(owner.contact_id,'whatsapp','+15550003',verified=True)
 await contacts.add_handle(guest.contact_id,'sms','+15550002',verified=True)
 return owner.contact_id,guest.contact_id
owner,guest=asyncio.run(seed());host._contacts_store=contacts
os.environ['PACOMIND_OWNER_CONTACT_ID']=owner
ledger=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.pacomind_authority=RequestAuthority(principal_id='native-task-fixture',credential_id='fixture',
  scopes=frozenset({'turns:write','context:read','turns:resolve-sender'}),
  viewer_person_id=owner,person_ids=frozenset({owner}),audiences=frozenset({'viewer'}),
  turn_ingress_platforms=frozenset({'sms','whatsapp','cli'}),authenticated=True)
 return await next_call(request)
app.include_router(host.router);app.include_router(host.v2_router)
api=TestClient(app);api.__enter__()
wire=[];responses=[];failure=None;lose_capture_ack=False;incomplete=False
original_client=httpx.Client
def respond(request):
 global lose_capture_ack
 wire.append((request.method,request.url.path,dict(request.url.params),
              json.loads(request.content) if request.content else None))
 if failure and failure in request.url.path:
  return httpx.Response(503,json={'detail':'Controlled API interruption'})
 response=api.request(request.method,request.url.path,params=request.url.params,
                      headers=dict(request.headers),content=request.content)
 responses.append((request.url.path,response.status_code,response.text))
 if lose_capture_ack and '/turns/task-instruction/' in request.url.path:
  lose_capture_ack=False
  assert response.status_code in {200,201},response.text
  raise httpx.ReadTimeout('Lost source receipt after canonical commit',request=request)
 if incomplete and request.url.path.endswith('/sources/erasures') and response.status_code==200:
  return httpx.Response(200,json={**response.json(),'complete':False})
 return httpx.Response(response.status_code,content=response.content,headers=response.headers)
httpx.Client=lambda **kwargs:original_client(**{**kwargs,'transport':httpx.MockTransport(respond)})
client=PacoMindClient('http://fixture')
outbox=TurnOutbox(state/'turn-outbox.db')
@contextmanager
def database():
 db=sqlite3.connect(state/'native-task-associations.db');db.row_factory=sqlite3.Row
 try:
  with db:yield db
 finally:db.close()
purges=[]
def erase(contact,rules):
 purges.append((contact,copy.deepcopy(rules)))
 erase_task_handoffs(database,contact,rules)
sources=NativeTaskSources(client,outbox,owner,erase=erase)
store=TaskHandoffs(database,sources.resolve_source,sources.resolve_owner)
def scope(platform='sms',sender='+15550001',session='native-text-one',turn='turn-one',contact=None):
 return _TransportScope(session,'foreground-'+session,turn,platform,sender,contact or owner,
  'system' if platform=='cli' else 'owner','attested_system' if platform=='cli' else 'resolved',
  'Compare the violet calibration notes and retain a concise finding.',
  authority_gateway='' if platform=='cli' else platform)
def fails(call,match):
 try:call()
 except TaskHandoffError as error:assert match in str(error),str(error)
 else:raise AssertionError('Expected source boundary rejection: '+match)
def admit(label='one',sc=None):
 src=sources.capture(sc or scope(turn='turn-'+label))
 return store.admit(request_id=label,request='Compare the retained calibration notes.',source_input=src)

def ordinary():
 global lose_capture_ack
 lose_capture_ack=True
 fails(lambda:sources.capture(scope()),'capture is unconfirmed')
 src=sources.capture(scope())
 assert src==sources.capture(scope())
 assert set(src)=={'version','principal','source_session_id','input_refs','source_refs','watermark','contact_id','origin'}
 assert src['principal']=='hermes:sms'
 assert src['origin']=={'platform':'sms','authority_gateway':'sms','sender_id':'+15550001',
                       'session_id':'native-text-one','turn_id':'turn-one'}
 assert scope().user_message not in json.dumps(src)
 assert src['input_refs'][0]['input_message_hash']==source_message_hash(scope().session_id,
     {'role':'user','content':scope().user_message})
 assert ledger.source_references([src['input_refs'][0]['source_id']],contact_id=owner,
     session_id='another-real-channel')==src['source_refs']
 with ledger._connect() as db:
  rows=db.execute('SELECT messages_json FROM turn_sources').fetchall()
 assert len(rows)==1 and json.loads(rows[0][0])==[{'role':'user','content':scope().user_message}]
 assert not outbox.snapshot()
 assert all('/turns/task-instruction/' in path for method,path,_,_ in wire if method=='PUT')
 assert all(params.get('create')=='false' for _,path,params,_ in wire if path.endswith('/contacts/resolve'))
 row=store.admit(request_id='ordinary',request='Compare notes.',source_input=src)
 assert store.admit(request_id='ordinary',request='Compare notes.',source_input=src)['id']==row['id']
 other=scope('whatsapp','+15550003',session='native-text-two',turn='turn-two')
 assert sources.authorize_control(src,other)==owner
 update=sources.capture(other)
 changed=store.admit_update(row['id'],instruction='Use a checklist.',source_input=update,principal=update['principal'])
 assert changed['source']['origin']['platform']=='whatsapp'
 assert store.get(row['id'])['source']==src

def authority_changes():
 src=sources.capture(scope())
 for platform in ('cron','subagent','background_review','pacomind_task'):
  fails(lambda:sources.capture(replace(scope(),platform=platform)),'ordinary authenticated')
 with supplied_input(contact_id=owner,session_id=scope().session_id,input_refs=src['input_refs'],source_refs=src['source_refs']):
  fails(lambda:sources.capture(scope()),'ordinary authenticated')
 fails(lambda:sources.capture(scope(sender='+15550002')),'binding is unavailable')
 fails(lambda:sources.capture(replace(scope(),authority_lane='guest')),'ordinary authenticated')
 local=sources.capture(scope('cli','',session='local',turn='local-turn'))
 assert local['principal']=='hermes:cli'
 unconfigured=NativeTaskSources(client,outbox,owner,attested_system_platforms=())
 fails(lambda:unconfigured.resolve_owner(local,require_task_grant=False),'binding is unavailable')
 fails(lambda:sources.resolve_owner({**src,'principal':'hermes:cli'},require_task_grant=False),'origin is invalid')
 asyncio.run(contacts.correct_handle_identity(operation_id='identity-change',performed_by='fixture-owner',
  gateway='sms',address='+15550001',expected_contact_id=owner,contact_id=guest,evidence_refs=['fixture:correction']))
 # The scoped resolver withholds the other person's identity after reassignment.
 fails(lambda:sources.resolve_owner(src,require_task_grant=False),'binding is unavailable')
 fails(lambda:sources.capture(scope()),'binding is unavailable')
 assert sources.resolve_owner(local,require_task_grant=False)==owner

def joined_child_is_not_direct_input():
 src=sources.capture(scope())
 for parent in (scope(), scope('cli','',session='local-parent',turn='local-turn')):
  registry=_TransportScopeRegistry()
  registry.put(parent)
  registry.bind_child(parent_session_id=parent.session_id,parent_turn_id=parent.turn_id,
                      child_session_id='child-of-'+parent.session_id)
  child=registry.child_scope(session_id='child-of-'+parent.session_id,
    parent_session_id=parent.session_id,task_id='native-child-task',turn_id='native-child-turn',
    user_message='An agent-created instruction is not another direct owner request.')
  assert child.valid_participant and child.contact_id==parent.contact_id
  assert child.platform==parent.platform
  before=len(wire)
  fails(lambda:sources.capture(child),'ordinary authenticated')
  fails(lambda:sources.authorize_control(src,child),'ordinary authenticated')
  assert len(wire)==before

def erasure():
 global failure
 row=admit();src=row['source']
 store.bind(row['id'],{'session_id':'native-task','task_id':'work','turn_id':'work-turn'})
 ledger.erase_sources(contact_id=owner,turn_ids=[src['input_refs'][0]['source_id']])
 fails(lambda:store.resolve(row['id']),'sources are unavailable')
 assert purges[-1][1] and outbox.erasure_watermark(owner)>0
 assert store.get(row['id'])['request']==''
 # The owner can stop after content erasure and during a recall API outage.
 failure='/memory/'
 before=len(wire)
 assert sources.authorize_control(src,scope('whatsapp','+15550003',session='control',turn='stop'))==owner
 stopped=store.request_stop(row['id'])
 assert stopped['stop']
 assert all('/contacts/resolve' in item[1] for item in wire[before:])
 assert sources.resolve_owner(src,require_task_grant=False)==owner
 failure=None
 reopened=NativeTaskSources(client,TurnOutbox(state/'turn-outbox.db'),owner,erase=erase)
 fails(lambda:reopened.resolve_source(src),'sources are unavailable')

def annotations():
 src=sources.capture(scope())
 ref=src['source_refs'][0]
 ledger.append_source_annotation(contact_id=owner,session_id=scope().session_id,
  annotation_id='correct-instruction',**ref,excerpt='violet calibration',
  correction='The label was corrected to orange; the earlier requested label is obsolete.',author_principal='fixture-owner')
 # An annotation leaves source bytes/version intact. This narrow resolver must
 # not treat the original unannotated intent as current after that correction.
 assert ledger.source_references([ref['source_id']],contact_id=owner,session_id='later')==[ref]
 assert outbox.erasure_watermark(owner)==0
 fails(lambda:sources.resolve_source(src),'sources are unavailable')
 fresh=sources.capture(replace(scope(),turn_id='corrected-turn',
     user_message='Compare the orange calibration notes, incorporating the label correction.'))
 assert fresh['source_refs']!=src['source_refs']
 assert sources.resolve_source(fresh)==fresh
 assert sources.resolve_owner(src,require_task_grant=False)==owner

def dependencies_and_outages():
 global failure,incomplete
 src=sources.capture(scope())
 parent=sources.capture(scope('whatsapp','+15550003',session='other',turn='other'))
 deps={'input_refs':parent['input_refs'],'source_refs':parent['source_refs']}
 assert sources.resolve_source(src,deps)==src
 wrong=copy.deepcopy(src);wrong['input_refs'][0]['input_message_hash']='0'*64
 fails(lambda:sources.resolve_source(wrong),'sources are unavailable')
 wrong=copy.deepcopy(src);wrong['source_refs'][0]['source_version']='0'*64
 fails(lambda:sources.resolve_source(wrong),'sources are unavailable')
 incomplete=True
 fails(lambda:sources.resolve_source(src),'sources are unavailable')
 incomplete=False;failure='/memory/'
 fails(lambda:sources.resolve_source(src),'sources are unavailable')
 failure='/contacts/resolve'
 fails(lambda:sources.resolve_owner(src,require_task_grant=False),'binding is unavailable')
 failure=None
 assert sources.resolve_source(src)==src
 ledger.erase_sources(contact_id=owner,turn_ids=[parent['source_refs'][0]['source_id']])
 fails(lambda:sources.resolve_source(src,deps),'sources are unavailable')
 # Unrelated source erasure advances the one existing cursor without erasing
 # this original instruction or blocking its independent reuse.
 assert sources.resolve_source(src)['watermark']>src['watermark']

try:
 globals()[sys.argv[4]]()
 print(json.dumps({'case':sys.argv[4],'passed':True,'canonical_api':True,'external_network':False}))
except Exception:
 print(json.dumps({'requests':wire,'responses':responses,'purges':purges}))
 raise
finally:
 api.__exit__(None,None,None)
 asyncio.run(contacts.close())
'''


@pytest.mark.parametrize('case', [
    'ordinary', 'authority_changes', 'joined_child_is_not_direct_input',
    'erasure', 'annotations', 'dependencies_and_outages',
])
def test_native_task_sources(artifacts, tmp_path, case):
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(PACOMIND_STATE_DIR=str(tmp_path/'state'), PACOMIND_SKIP_DOTENV='1',
        PACOMIND_INTROSPECTION_ENABLED='false', PACOMIND_GUARD_CHAT_MODE='off')
    run_python('-I', '-c', PROBE, artifacts[3], ROOT/'sidecar',
        os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH', ''), case, cwd=tmp_path, env=env)

"""Actual native cron, agent tools, HTTP acceptance and durable reconciliation."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


WORKER = r'''
import json,os,sys,uuid
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
from urllib.request import Request,urlopen
sys.path.insert(0,ADAPTER)
from hermes_cli.config import load_config
from run_agent import AIAgent
from colony_hermes.local_work_runner import main
home=Path(os.environ['HERMES_HOME'])
job=next(j for j in json.loads((home/'cron/jobs.json').read_text())['jobs'] if j['script']=='accepted-work.py')
counter=home/'requests.jsonl'
opened=[]
def audit(event,args):
    if event=='open' and str(args[0])==str(home/'source.txt'):opened.append(True)
sys.addaudithook(audit)
def call(name,args):
    return NS(id=uuid.uuid4().hex,type='function',function=NS(name=name,arguments=json.dumps(args)))
def response(**kwargs):
    rows=[r for r in kwargs['messages'] if r.get('role')=='tool']
    with counter.open('a') as f:f.write(json.dumps({'tools':len(rows)})+'\n')
    if not rows:
        if MODE=='cancel':
            urlopen(Request(BASE+'/fixture/cancel',data=b'{}',headers={'Content-Type':'application/json'})).close()
            calls=[call('tool_call',{'name':'colony_read_work_source','arguments':{'source':0}})]
        else:
            observed=json.load(urlopen(BASE+'/v1/host/executions?contact_id=owner'))
            assert observed['local_work']['items'][0]['status']=='assigned',observed
            assert observed['local_work']['items'][0]['native_execution_id'],observed
            calls=[call('tool_search',{'queries':['read selected local work source']})]
    elif len(rows)==1 and MODE!='cancel':
        assert 'colony_read_work_source' in json.loads(rows[-1]['content'])['tools'],rows
        calls=[call('tool_call',{'name':'colony_read_work_source','arguments':{'source':0}})]
    else:
        if MODE!='cancel':
            assert json.loads(rows[-1]['content'])['native_read']['content'].find('neutral fixture source')>=0,rows
        calls=None
    draft='The selected note says neutral fixture source [source:0].'
    if MODE=='fenced_missing_reference':draft=draft.replace('[source:0]','')
    final=json.dumps({'draft':draft,'sources':[0]})
    if MODE.startswith('fenced'):final='```json\n'+final+'\n```'
    return NS(choices=[NS(message=NS(content='' if calls else final,tool_calls=calls),finish_reason='tool_calls' if calls else 'stop')],model='fixture/local',usage=None)
client=MagicMock();client.chat.completions.create.side_effect=response
original=AIAgent.__init__
def initialize(self,*args,**kwargs):
    original(self,*args,**kwargs)
    self._build_system_prompt=lambda *a,**k:'Neutral controlled local source task.'
    self._use_prompt_caching=False;self.compression_enabled=False
with patch('run_agent.OpenAI',return_value=client),patch.object(AIAgent,'__init__',initialize):
    try:
        code=main(['--job-id',job['id'],'--provider','fixture','--model','fixture/local','--destination',str(home/'drafts')])
    finally:
        if MODE=='cancel':assert not opened,opened
    raise SystemExit(code)
'''


PROBE = r'''
import json,os,sys,sqlite3,threading,time,socket
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
mode=sys.argv[4];worker=sys.argv[5]
from fastapi import FastAPI,Response
from fastapi.testclient import TestClient
import uvicorn
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import commitment_work,host,executions
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.turns.local_work import local_work_view
home=Path(os.environ['HERMES_HOME']);home.mkdir(mode=0o700)
(home/'scripts').mkdir();Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir()
commitments=CommitmentStore(state/'commitments.db');initiatives=InitiativeStore(state)
host._commitment_store=commitments;host._initiative_store=initiatives
obligation=commitments.create('owner','Summarize the selected neutral fixture')
source=home/'source.txt';source.write_text('neutral fixture source\n')
app=FastAPI();failed=[False]
@app.middleware('http')
async def authority(request,next_call):
    request.state.colony_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),audiences=frozenset({'viewer'}),authenticated=True)
    if mode=='reconcile' and request.url.path.endswith('/finish') and not failed[0]:
        payload=await request.json()
        if payload.get('result',{}).get('status')=='draft_created':
            failed[0]=True
            return Response('controlled result delivery interruption',status_code=503)
    return await next_call(request)
@app.post('/fixture/cancel')
def cancel():
    commitments.update(obligation['id'],status='cancelled');return {'cancelled':True}
app.include_router(commitment_work.router);app.include_router(executions.router)
listener=socket.socket();listener.bind(('127.0.0.1',0));base='http://127.0.0.1:'+str(listener.getsockname()[1])
server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
while not server.started:time.sleep(.01)
config={'plugins':{'enabled':['colony'],'colony':{'owner_contact_id':'owner','url':base,'attested_system_platforms':['cli'],'turn_writer_platforms':[]}},
        'providers':{'fixture':{'base_url':'http://127.0.0.1:1/v1','api_key':'fixture','default_model':'fixture/local'}},
        'model':{'default':'fixture/local'},'tools':{'tool_search':{'enabled':'on'}}}
(home/'config.yaml').write_text(json.dumps(config))
script="ADAPTER="+repr(sys.argv[1])+"\nBASE="+repr(base)+"\nMODE="+repr(mode)+"\n"+worker
(home/'scripts/accepted-work.py').write_text(script)
from cron.jobs import create_job,trigger_job
from cron.scheduler import tick
job=create_job(prompt=None,schedule='every 1h',name='Accepted neutral local work',deliver='local',script='accepted-work.py',no_agent=True,attach_to_session=False)
os.environ['COLONY_LOCAL_WORK_JOB_ID']=job['id']
# Native caller derives owner/turn identity; task payload contains no authority.
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from model_tools import handle_function_call
get_plugin_manager().discover_and_load()
invoke_hook('pre_llm_call',session_id='owner-chat',task_id='owner-task',turn_id='owner-turn',platform='cli',sender_id='',user_message='Please summarize this selected neutral file for my obligation.')
accept_args={'question':'Summarize the selected note','sources':[str(source)]}
if mode!='standalone':accept_args['commitment_id']=obligation['id']
accepted=json.loads(handle_function_call('colony_accept_local_draft',accept_args,session_id='owner-chat',task_id='owner-task',turn_id='owner-turn',tool_call_id='accept'))
assert accepted.get('status')=='pending',accepted
assert accepted['context']['commitment_id']==(None if mode=='standalone' else obligation['id'])
assert local_work_view()['items'][0]['liveness']=='not_started'
for index in range(2):
    trigger_job(job['id']);tick(verbose=False,sync=True)
    if index==0:
        first=local_work_view()
        count=len((home/'requests.jsonl').read_text().splitlines())
        if mode=='reconcile':assert first['items'][0]['status']=='assigned',first
    else:assert len((home/'requests.jsonl').read_text().splitlines())==count
with sqlite3.connect(home/'cron/executions.db') as db:
    native=db.execute('SELECT id,status FROM executions WHERE job_id=? ORDER BY claimed_at',(job['id'],)).fetchall()
assert len(native)==2,native
view=local_work_view();recent=view['recent'][0]
if mode=='cancel':
    assert recent['status']=='cancelled',recent
    assert not list((home/'drafts').rglob('report.md'))
elif mode=='fenced_missing_reference':
    assert recent['status']=='failed',recent
    assert recent['result']['error_type']=='ValueError',recent
    assert not list((home/'drafts').rglob('report.md'))
    assert next((home/'drafts').rglob('model-final.txt')).read_text().startswith('```json\n')
else:
    assert recent['status']=='completed',recent
    assert recent['native_execution_id']==native[0][0]
    assert len(list((home/'drafts').rglob('report.md')))==1
    assert commitments.get(obligation['id'])['status']=='pending'
    receipt=json.loads(next((home/'drafts').rglob('draft-receipt.json')).read_text())
    with sqlite3.connect(home/'state.db') as db:
        assert db.execute('SELECT count(*) FROM messages WHERE session_id=?',(receipt['native_session_id'],)).fetchone()[0]>=4
    assert recent['result']['report_sha256']==receipt['result']['report_sha256']
    if mode=='reconcile':assert native[0][1]=='failed' and native[1][1]=='completed',native
with sqlite3.connect(initiatives._db_path) as db:
    assert db.execute('SELECT attempt_count FROM initiatives WHERE id=?',(accepted['id'],)).fetchone()[0]==1
server.should_exit=True;thread.join(5);initiatives.close()
print(json.dumps({'mode':mode,'native_execution_and_tools':True,'restart_no_duplicate':True}))
'''


@pytest.mark.parametrize('mode', ['complete', 'cancel', 'reconcile', 'standalone', 'fenced', 'fenced_missing_reference'])
def test_native_accepted_draft_lifecycle(artifacts, tmp_path, mode):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes to exercise its actual scheduler and tools')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'),COLONY_STATE_DIR=str(tmp_path/'state'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1',COLONY_MEMORY_WORKER_TOOLS='0',COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_SKIP_DOTENV='1',COLONY_LOCAL_WORK_ENABLED='true',COLONY_OWNER_CONTACT_ID='owner',
        LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=run_python('-I','-c',PROBE,artifacts[3],ROOT/'sidecar',os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),mode,WORKER,cwd=tmp_path,env=env)
    assert '"restart_no_duplicate": true' in result.stdout


def test_packaged_draft_json_format_boundary(artifacts, tmp_path):
    script = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from colony_hermes.model_response import decode_json_response
value={'draft':'Neutral source [source:0].','sources':[0]}
raw=json.dumps(value)
for body in [raw, '\n '+raw+' \n', '```json\n'+raw+'\n```', '```\r\n'+raw+'\r\n```']:
    assert decode_json_response(body)==value
for body in ['Here is the draft:\n'+raw, '```json\n'+raw,
             '```json\n'+raw+'\n```\nAdditional prose',
             '```json\n'+raw+'\n```\n```json\n'+raw+'\n```',
             '```json\n'+raw[:-1]+'\n```']:
    try:decode_json_response(body)
    except json.JSONDecodeError:pass
    else:raise AssertionError('Malformed or mixed-content result was accepted')
def reject_duplicates(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate key')
        result[key]=value
    return result
assert decode_json_response('```json\n'+raw+'\n```',object_pairs_hook=reject_duplicates)==value
try:decode_json_response('```json\n{"draft":"first","draft":"second"}\n```',object_pairs_hook=reject_duplicates)
except ValueError as error:assert str(error)=='duplicate key'
else:raise AssertionError('The caller-provided JSON decoder hook was discarded')
'''
    run_python('-I', '-c', script, artifacts[3], cwd=tmp_path)

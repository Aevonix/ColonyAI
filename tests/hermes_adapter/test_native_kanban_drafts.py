"""Installed native task/run APIs, actual agent tools, controlled local sources."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json,os,sys
os.umask(0o077)
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,MagicMock
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
mode=sys.argv[4]
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import commitment_work,host,executions
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_hermes.native_drafts import NativeDrafts
from colony_hermes import _TransportScope
from hermes_cli import kanban_db as kb
from tools import kanban_tools
root=Path(os.environ['HERMES_HOME']);root.mkdir(exist_ok=True)
state=Path(os.environ['COLONY_STATE_DIR']);state.mkdir(exist_ok=True)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir(exist_ok=True)
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
commitments=CommitmentStore(state/'commitments.db');initiatives=InitiativeStore(state)
host._commitment_store=commitments;host._initiative_store=initiatives
obligation=commitments.create('owner','Read two selected neutral notes')
sources=[root/'first.txt',root/'second.txt']
for number,path in enumerate(sources):path.write_text('Neutral note '+str(number)+' says the fixture is local.\n')
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
    request.state.colony_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
      scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),
      audiences=frozenset({'viewer'}),authenticated=True)
    return await next_call(request)
app.include_router(commitment_work.router);app.include_router(executions.router)
client=TestClient(app)
# Production sidecar and named worker have separate environment variables.
# Keep the sidecar's selected root fixed in this in-process API fixture.
selected=patch('colony_sidecar.turns.hermes_work.selected_home',return_value=root);selected.start()
selected_board=patch('colony_sidecar.turns.hermes_kanban.selected_home',return_value=root);selected_board.start()
config={'board':'colony-drafts','worker_profile':'colony-drafts','destination':str(state/'drafts'),'worker':False}
gateway=NativeDrafts(config,client,'owner')
body={'contact_id':'owner','session_id':'origin-session','turn_id':'origin-turn','question':'Compare both selected notes',
      'sources':[str(p) for p in sources],
      'origin':{'platform':'whatsapp','chat_id':'synthetic-origin','user_id':'synthetic-owner','notifier_profile':'default'}}
response=client.post('/v1/host/commitments/'+obligation['id']+'/local-draft',json=body)
assert response.status_code==200,response.text
accepted=response.json()
with ThreadPoolExecutor(max_workers=2) as pool:
    values=list(pool.map(lambda _:gateway.ensure_task(accepted),range(2)))
assert values[0]['context']['native_task_id']==values[1]['context']['native_task_id']
accepted=values[0];tid=accepted['context']['native_task_id']
db=kb.connect(board=config['board'])
assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
assert db.execute('SELECT count(*) FROM kanban_notify_subs').fetchone()[0]==1
assert kb.get_task(db,tid).status=='ready'
claimed=kb.claim_task(db,tid);assert claimed
workspace=kb.resolve_workspace(claimed,board=config['board']);kb.set_workspace_path(db,tid,str(workspace))
worker_home=root/'profiles/colony-drafts';worker_home.mkdir(parents=True)
os.environ.update(HERMES_PROFILE='colony-drafts',HERMES_HOME=str(worker_home),HERMES_KANBAN_DB=str(gateway.db_path),
 HERMES_KANBAN_BOARD=config['board'],HERMES_KANBAN_TASK=tid,HERMES_KANBAN_RUN_ID=str(claimed.current_run_id),
 HERMES_KANBAN_CLAIM_LOCK=claimed.claim_lock,HERMES_KANBAN_WORKSPACE=str(workspace),HERMES_SESSION_SOURCE='kanban')
config['worker']=True
work_config={'plugins':{'enabled':['colony'],'entries':{'colony':{'allow_tool_override':True}},'colony':{
 'owner_contact_id':'owner','attested_system_platforms':['cli'],'turn_writer_platforms':[],
 'native_local_work':config}},'model':{'default':'fixture/local'},'tools':{'tool_search':{'enabled':'off'}}}
(worker_home/'config.yaml').write_text(json.dumps(work_config))
if mode=='native_agent':
    import socket,threading,time,uvicorn
    calls=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    work_config['plugins']['colony']['url']='http://127.0.0.1:'+str(listener.getsockname()[1])
    (worker_home/'config.yaml').write_text(json.dumps(work_config))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    while not server.started:time.sleep(.01)
    from hermes_cli.plugins import get_plugin_manager
    from run_agent import AIAgent
    from hermes_state import SessionDB
    def completion(**kwargs):
        rows=[r for r in kwargs['messages'] if r.get('role')=='tool'];calls.append(len(rows))
        if len(rows)<2:name,args='colony_read_work_source',{'source':len(rows)}
        elif len(rows)==2:
            assert all('native_read' in json.loads(row['content']) for row in rows),rows
            name,args='kanban_complete',{'summary':'Retain local source draft',
                'metadata':{'draft':'Both notes are local [source:0] [source:1].','sources':[0,1]}}
        else:
            assert json.loads(rows[-1]['content']).get('ok') is True,rows[-1]
            name,args=None,None
        tools=[NS(id='call-'+str(len(rows)),type='function',function=NS(name=name,arguments=json.dumps(args)))] if name else None
        return NS(choices=[NS(message=NS(content='' if tools else 'Unverified local draft retained.',tool_calls=tools),
            finish_reason='tool_calls' if tools else 'stop')],model='fixture/local',usage=None)
    model=MagicMock();model.chat.completions.create.side_effect=completion
    with patch('run_agent.OpenAI',return_value=model):
        get_plugin_manager().discover_and_load()
        from tools.registry import registry
        assert registry.get_entry('colony_read_work_source') is not None, 'Native plugin did not register'
        import inspect
        adapter=inspect.getclosurevars(registry.get_entry('kanban_complete').handler).nonlocals['native_drafts']
        session_db=SessionDB()
        agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',model='fixture/local',
            enabled_toolsets=['colony_local_work'],platform='cli',session_db=session_db,max_iterations=6,
            skip_context_files=True,skip_memory=True,skip_background_review=True,quiet_mode=True)
        agent._build_system_prompt=lambda *a,**k:'Controlled native accepted source worker.'
        agent._use_prompt_caching=False;agent.compression_enabled=False
        result=agent.run_conversation('work kanban task '+tid,task_id='actual-native-agent-task')
        assert result['completed'] is True,(adapter.error,result)
        agent.close();session_db.close()
    assert calls==[0,1,2,3],calls
    server.should_exit=True;thread.join(5)
else:
    from colony_hermes.commitment_work import CommitmentCoordinator
    coordinator=CommitmentCoordinator(client)
    scope=_TransportScope('worker-session','worker-task','worker-turn','cli','','owner','system','attested_system')
    context={'session_id':scope.session_id,'task_id':scope.task_id,'turn_id':scope.turn_id,'model':'fixture/local'}
    worker=NativeDrafts(config,client,'owner');injected=worker.bind(scope,coordinator,context)
    assert worker.work and worker.work.bound,(injected,worker.error)
    for number in range(2):
        value=json.loads(worker.work.read_source({'source':number},{**context,'tool_name':'colony_read_work_source'}))
        assert 'native_read' in value,value
    args={'summary':'Retain accepted draft','metadata':{'draft':'Both notes are local [source:0] [source:1].','sources':[0,1]}}
    if mode in {'missing_reference','source_changed'}:
        if mode=='missing_reference':args['metadata']['draft']='Only the first note [source:0].'
        else:sources[1].write_text('Changed after reading.\n')
        result=json.loads(worker.complete(args,context,kanban_tools._handle_complete))
        expected='local_draft_reference_contract_failed' if mode=='missing_reference' else 'source_changed_during_draft'
        assert result['error']==expected,result
        assert not (worker.directory/'report.md').exists()
        assert kb.get_task(db,tid).status=='running'
        kb.block_task(db,tid,reason='Controlled invalid draft',expected_run_id=claimed.current_run_id)
        current=client.get(gateway.path(accepted['id'])+'?contact_id=owner').json()
        gateway.ensure_task(current)
        assert kb.get_task(db,tid).status=='blocked', 'Reconciliation must not retry native terminal runs'
    else:
        # Sidecar commits, then worker is interrupted before native completion.
        def interrupt(args):raise ConnectionError('Controlled interruption before native commit')
        result=json.loads(worker.complete(args,context,interrupt));assert result['saved_receipt'] is True,result
        report=worker.directory/'report.md';receipt=worker.directory/'draft-receipt.json'
        original=(report.read_bytes(),receipt.read_bytes());first_run=claimed.current_run_id
        kb.reclaim_task(db,tid,reason='Controlled interrupted worker')
        claimed=kb.claim_task(db,tid);assert claimed.current_run_id!=first_run
        assert json.loads(worker.complete(args,context,kanban_tools._handle_complete))['error']=='current_native_run_required'
        os.environ.update(HERMES_KANBAN_RUN_ID=str(claimed.current_run_id),HERMES_KANBAN_CLAIM_LOCK=claimed.claim_lock)
        recovery=NativeDrafts(config,client,'owner');injected=recovery.bind(scope,coordinator,context)
        assert 'Do not read sources or draft again' in injected['context'],injected
        assert recovery.work.read==set()
        result=json.loads(recovery.complete({'summary':'Recover retained draft'},context,kanban_tools._handle_complete))
        assert result.get('ok') is True,result
        assert (report.read_bytes(),receipt.read_bytes())==original
        assert json.loads(recovery.complete({'summary':'Replay'},context,kanban_tools._handle_complete))['replayed'] is True
        assert len(kb.list_runs(db,tid))==2
if mode in {'native_agent','recovery'}:
    task=kb.get_task(db,tid);assert task.status=='done',task
    saved=client.get(gateway.path(accepted['id'])+'?contact_id=owner').json()
    assert saved['status']=='completed',saved
    assert commitments.get(obligation['id'])['status']=='pending'
    assert len(list((state/'drafts').rglob('report.md')))==1
    assert db.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completed'",(tid,)).fetchone()[0]==1
    kb.archive_task(db,tid)
    assert gateway.ensure_task(saved)['context']['native_task_id']==tid
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
print(json.dumps({'case':mode,'native_task_unique':True,'native_run_contract':True}))
db.close();initiatives.close();selected.stop();selected_board.stop()
'''


@pytest.mark.parametrize('mode', ['native_agent', 'recovery', 'source_changed', 'missing_reference'])
def test_packaged_native_kanban_draft(artifacts, tmp_path, mode):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native task/run integration')
    env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'),HERMES_KANBAN_HOME=str(tmp_path/'profile'),
        COLONY_STATE_DIR=str(tmp_path/'state'),HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_GENERAL_PLUGIN_ACTIVE='1',COLONY_MEMORY_WORKER_TOOLS='0',COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_SKIP_DOTENV='1',COLONY_LOCAL_WORK_ENABLED='true',COLONY_LOCAL_WORK_EXECUTOR='kanban',
        COLONY_LOCAL_WORK_BOARD='colony-drafts',COLONY_LOCAL_WORK_PROFILE='colony-drafts',
        COLONY_OWNER_CONTACT_ID='owner',COLONY_GUARD_CHAT_MODE='off',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=run_python('-I','-c',PROBE,artifacts[3],ROOT/'sidecar',os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),mode,
                      cwd=tmp_path,env=env)
    assert '"native_run_contract": true' in result.stdout

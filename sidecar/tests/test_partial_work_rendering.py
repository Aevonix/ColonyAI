"""An unavailable or bounded reader must preserve independently observed work."""
import json
import sqlite3
import asyncio
import threading
from types import SimpleNamespace

import pytest

from apsimo.api.routers import executions, host
from apsimo.turns import hermes_kanban, hermes_work, local_work, reported_workers
from apsimo.turns.executions import format_view, request_work_context


def base_view():
    return {'items':[{'execution_id':'real-turn','session_id':'voice-session',
        'parent_execution_id':'','platform':'voice','phase':'model','tool_name':'',
        'liveness':'unknown','observation_age_seconds':1}], 'total':1,'truncated':False,'complete':False}


def isolate(monkeypatch):
    empty = {'items':[], 'recent':[], 'available':False,'reason':'not_selected'}
    for module, name in ((hermes_work,'cron_view'), (local_work,'local_work_view'),
                         (reported_workers,'reported_worker_view')):
        monkeypatch.setattr(module, name, lambda **_: dict(empty))
    monkeypatch.setattr(host, '_task_queue', None)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', ['native_kanban','local_work'])
async def test_failed_reader_does_not_erase_successful_turn_start_context(monkeypatch, failed):
    isolate(monkeypatch)
    def broken(**_):
        raise RuntimeError('controlled unavailable reader')
    monkeypatch.setattr(hermes_kanban,'kanban_view', lambda **_: {'items':[],'recent':[], 'available':False,'reason':'not_selected'})
    module, name = (hermes_kanban,'kanban_view') if failed=='native_kanban' else (local_work,'local_work_view')
    monkeypatch.setattr(module,name,broken)
    async def pending(**_):
        return {'items':[{'job_id':'independent-worker-job','state':'running'}], 'available':True}
    monkeypatch.setattr(host,'_task_queue', SimpleNamespace(current_work=pending))
    view = await executions.with_queue_work(base_view(), owner=True)
    rendered = format_view(view)
    assert 'voice-session' in rendered and 'independent-worker-job' in rendered
    assert 'unavailable' in rendered and view['work_sources'][failed]['status']=='unavailable'
    projected = request_work_context(view)
    assert 'real-turn' in projected['text'] and 'independent-worker-job' in projected['text']


def board(path, identifier):
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks(id TEXT,title TEXT,status TEXT,assignee TEXT,goal_mode INTEGER,'
                   'goal_max_turns INTEGER,current_run_id INTEGER,created_at REAL,started_at REAL,'
                   'completed_at REAL,last_heartbeat_at REAL)')
        db.execute('CREATE TABLE task_runs(id INTEGER,task_id TEXT,status TEXT)')
        db.execute('CREATE TABLE task_events(task_id TEXT,kind TEXT,created_at REAL)')
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (identifier,'Neutral board task','ready','default',0,1,None,1,None,None,None))


@pytest.mark.asyncio
async def test_actual_locked_later_boards_preserve_the_first_board_at_the_api_boundary(tmp_path, monkeypatch):
    isolate(monkeypatch)
    actual = hermes_kanban.kanban_view
    started = threading.Event()
    release = threading.Event()
    def scheduled(**kwargs):
        started.set()
        assert release.wait(1), 'controlled worker startup was not released'
        return actual(**kwargs)
    monkeypatch.setattr(hermes_kanban,'kanban_view',scheduled)
    monkeypatch.setattr(hermes_kanban,'selected_home',lambda:tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_HOME',raising=False)
    monkeypatch.delenv('HERMES_KANBAN_DB',raising=False)
    monkeypatch.setenv('COLONY_HERMES_WORK_BOARDS',json.dumps(['default', 'slow1','slow2','slow3','slow4','slow5']))
    board(tmp_path/'kanban.db','first-board-task')
    locks=[]
    try:
        for name in ('slow1','slow2','slow3','slow4','slow5'):
            path=tmp_path/'kanban/boards'/name/'kanban.db'
            board(path,name)
            db=sqlite3.connect(path)
            db.execute('BEGIN EXCLUSIVE')
            locks.append(db)
        # Native work starts after the outer wait budget. A small controlled
        # executor delay exposes equal inner/outer deadlines deterministically.
        task=asyncio.create_task(executions.with_queue_work(base_view(),owner=True))
        deadline=asyncio.get_running_loop().time()+1
        while not started.is_set() and asyncio.get_running_loop().time()<deadline:
            await asyncio.sleep(.001)
        assert started.is_set(), 'controlled board reader did not start'
        await asyncio.sleep(.03)
        release.set()
        view=await task
        selected=view['native_kanban']
        assert selected['available'] and selected['partial'], selected
        assert selected['items'][0]['native_task_id']=='first-board-task'
        assert len(selected['boards'])==6 and selected['boards'][0]['available']
        assert all(not item['available'] for item in selected['boards'][1:])
        assert 'first-board-task' in format_view(view)
        projected=request_work_context(view)
        assert 'first-board-task' in projected['text'] and projected['work_sources']['native_kanban']['status']=='partial'
    finally:
        release.set()
        for db in locks:
            db.rollback()
            db.close()


@pytest.mark.parametrize('source', ['native_cron','native_kanban','local_work'])
@pytest.mark.parametrize('known_total', [True,False])
def test_recent_terminal_rows_are_not_described_as_zero_observed_records(source, known_total):
    recent={'id':'just-finished','status':'completed'}
    group={'items':[], 'recent':[recent], 'available':True,'total':0}
    if known_total:
        group.update(recent_total=5,recent_truncated=True)
    result=request_work_context({'items':[],source:group})
    count='5' if known_total else '1+'
    assert source+'=0 active, '+count+' recent records' in result['text']
    assert 'just-finished' in result['text']

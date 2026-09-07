"""Observe genuine native task transitions from independent request sessions."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import asyncio, hashlib, importlib.util, json, os, socket, sys, time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, sys.argv[1])
if sys.argv[2]: sys.path.append(sys.argv[2])
def no_network(*args, **kwargs): raise AssertionError('No network in native board fixture')
socket.socket.connect = no_network
from hermes_cli import kanban_db as kb
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions, host
from colony_sidecar.turns.hermes_kanban import kanban_view
from colony_sidecar.turns.executions import format_view
root = Path(os.environ['HERMES_HOME']); root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
state = Path(os.environ['COLONY_STATE_DIR']); state.mkdir()
kb.create_board('operations')
kb.create_board('unselected')
with kb.connect(board='unselected') as db:
    kb.create_task(db, title='UNSELECTED_SECRET', assignee='default', board='unselected')
with kb.connect(board='operations') as db:
    task = kb.create_task(db, title='Compare current release manifests', body='PRIVATE_TASK_BODY',
        assignee='default', board='operations', goal_mode=True, goal_max_turns=4,
        workspace_kind='dir', workspace_path=str(root), idempotency_key='one-undertaking')
    assert kb.create_task(db, title='Compare current release manifests', assignee='default',
        board='operations', idempotency_key='one-undertaking') == task
    claimed = kb.claim_task(db, task)
    assert claimed is not None
    run_id = claimed.current_run_id

# Absent observer configuration follows only the persisted current namespace.
kb.set_current_board('operations')
view = kanban_view()
assert view['selection'] == 'current_board_only' and view['available']
assert [b['board'] for b in view['boards']] == ['operations']
assert view['items'][0]['native_task_id'] == task
assert view['items'][0]['goal_mode'] is True and view['items'][0]['goal_max_turns'] == 4
assert view['items'][0]['native_run_id'] == run_id
assert view['items'][0]['liveness'] == 'unknown'
assert 'PRIVATE_TASK_BODY' not in json.dumps(view) and 'UNSELECTED_SECRET' not in json.dumps(view)
kb.set_current_board('default')
os.environ['HERMES_KANBAN_BOARD'] = ' Operations '
assert kb.get_current_board() == 'operations'
assert kanban_view()['boards'][0]['board'] == 'operations'
os.environ['HERMES_KANBAN_BOARD'] = 'default'
default = kanban_view()
assert default['boards'][0]['board'] == 'default' and not default['available']
assert not (root/'kanban.db').exists(), 'Reader must not initialize the default board'
del os.environ['HERMES_KANBAN_BOARD']
kb.set_current_board('operations')

# Explicit missing boards stay visible and are never created by a read.
os.environ['COLONY_HERMES_WORK_BOARDS'] = json.dumps(['operations','missing'])
view = kanban_view()
assert view['available'] and view['partial'] and view['boards'][1]['reason'] == 'native_board_absent'
assert not (root/'kanban/boards/missing').exists()
os.environ['COLONY_HERMES_WORK_BOARDS'] = '["../unselected"]'
assert kanban_view()['reason'] == 'invalid_native_board_binding'
os.environ['COLONY_HERMES_WORK_BOARDS'] = '["operations"]'

host._task_queue = None
app = FastAPI()
@app.middleware('http')
async def identity(request, next_call):
    person = request.headers.get('fixture-person', 'owner')
    request.state.colony_authority = RequestAuthority(principal_id='fixture',credential_id='fixture',
        scopes=frozenset({'context:read'}),viewer_person_id=person,person_ids=frozenset({person}),
        audiences=frozenset({'viewer'}),authenticated=True)
    return await next_call(request)
app.include_router(executions.router)
async def observe():
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
        before = (await client.get('/v1/host/executions',params={'contact_id':'owner',
            'session_id':'independent-session-a','projection':'request'})).json()
        assert task in before['text'] and 'Compare current release manifests' in before['text']
        assert '"goal_mode": true' in before['text'] and '"goal_max_turns": 4' in before['text']
        assert '"status": "running"' in before['text']
        assert 'current_board_only' not in before['text'] and 'configured_boards' in before['text']
        guest = await client.get('/v1/host/executions',headers={'fixture-person':'guest'},
            params={'contact_id':'guest','session_id':'other'})
        assert guest.status_code == 200 and 'native_kanban' not in guest.json()
        with kb.connect(board='operations') as db:
            assert kb.complete_task(db, task, summary='PRIVATE_RESULT_PROSE', expected_run_id=run_id,
                                    fire_lifecycle_hook=False)
        after = (await client.get('/v1/host/executions',params={'contact_id':'owner',
            'session_id':'independent-session-b','projection':'request'})).json()
        assert task in after['text'] and '"status": "done"' in after['text']
        assert '"status": "running"' not in after['text']
        assert 'PRIVATE_RESULT_PROSE' not in after['text'] and 'PRIVATE_TASK_BODY' not in after['text']
        full = (await client.get('/v1/host/executions',params={'contact_id':'owner'})).json()
        assert 'Compare current release manifests' in format_view(full)
        assert full['native_kanban']['complete'] is False
asyncio.run(observe())

# Native archive closes a running attempt without inventing a completion time.
with kb.connect(board='operations') as db:
    archived = kb.create_task(db, title='Superseded inspection', assignee='default', board='operations')
    archive_run = kb.claim_task(db, archived).current_run_id
    assert kb.archive_task(db, archived)
    archive_at = db.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='archived'",
                            (archived,)).fetchone()[0]
    assert db.execute('SELECT status FROM task_runs WHERE id=?', (archive_run,)).fetchone()[0] == 'reclaimed'
view = kanban_view()
record = next(row for row in view['recent'] if row['native_task_id'] == archived)
assert record['status'] == 'archived' and record['native_run_id'] is None
assert record['native_run_status'] is None
assert record['completed_at'] is None and record['terminal_record_at'] == archive_at
assert record['liveness'] == 'native_terminal_record' and view['recent_total'] == 2

# Read-only snapshots preserve the actual native file, and expose omissions.
with kb.connect(board='operations') as db:
    for n in range(3): kb.create_task(db,title='Pending '+str(n),assignee='default',board='operations')
path = kb.kanban_db_path(board='operations')
before = path.read_bytes()
view = kanban_view(limit=1)
assert view['total'] == 3 and view['truncated'] and len(view['items']) == 1
assert path.read_bytes() == before
print(json.dumps({'native_task_transitions':True,'independent_owner_sessions':True,'guest_excluded':True,
                  'selected_boards_only':True,'model_calls':0,'network_calls':0}))
'''


def test_actual_native_general_task_observed_across_sessions(tmp_path):
    python = os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use the existing qualified Hermes interpreter for native integration')
        python = sys.executable
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        COLONY_STATE_DIR=str(tmp_path/'state'),COLONY_OWNER_CONTACT_ID='owner',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
                             os.environ.get('COLONY_TEST_DEPENDENCY_PATH','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"independent_owner_sessions": true' in result.stdout

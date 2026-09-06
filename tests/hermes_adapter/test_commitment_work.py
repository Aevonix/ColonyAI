"""Two native Hermes sessions race a real durable commitment over HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json, os, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
sys.path.insert(0, sys.argv[1])
home = Path(os.environ['HERMES_HOME']); home.mkdir(mode=0o700)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['colony'], 'colony': {
    'url': sys.argv[2], 'owner_contact_id': 'owner', 'attested_system_platforms': ['cli'],
    'turn_outbox_path': str(home / 'turns.sqlite3')}}}))
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import run_tool_execution_middleware
from model_tools import handle_function_call
manager = get_plugin_manager(); manager.discover_and_load()
assert manager._plugins['colony'].enabled, manager._plugins['colony'].error
assert 'colony_commitment_work' in manager._plugins['colony'].tools_registered
def start(session):
    invoke_hook('pre_llm_call', session_id=session, task_id=session, turn_id=session,
                platform='cli', sender_id='', user_message='Inspect the same obligation')
def work(session, operation):
    return json.loads(handle_function_call('colony_commitment_work', {'commitment_id': sys.argv[3], 'operation': operation},
        session_id=session, task_id=session, turn_id=session, tool_call_id='call-' + session))
for session in ('chat', 'voice'): start(session)
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda session: work(session, 'claim'), ['chat', 'voice']))
assert sorted(row['accepted'] for row in results) == [False, True], results
assert all('claim_id' not in row for row in results)
winner = next(row['session_id'] for row in results if row['accepted'])
loser = next(session for session in ('chat', 'voice') if session != winner)
assert work(loser, 'status')['session_id'] == winner
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id=winner, task_id=winner, turn_id=winner) == 'executed'
urlopen(sys.argv[2] + '/advance-clock').close()
start('recovered'); assert work('recovered', 'claim')['accepted']
called = []
stale = run_tool_execution_middleware('read_file', {}, lambda args: called.append(args),
    session_id=winner, task_id=winner, turn_id=winner)
assert json.loads(stale)['effect_performed'] is False and called == []
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='recovered', task_id='recovered', turn_id='recovered') == 'executed'
invoke_hook('subagent_start', parent_session_id='recovered', parent_turn_id='recovered', child_session_id='child')
invoke_hook('pre_llm_call', session_id='child', task_id='child-task', turn_id='child-turn',
            parent_session_id='recovered', platform='subagent', user_message='Inspect one part')
invoke_hook('pre_api_request', session_id='child-rotated', task_id='child-task', turn_id='child-turn')
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn') == 'executed'
assert work('recovered', 'release')['accepted']
assert work('chat', 'status')['work_state'] == 'released'
child_result = run_tool_execution_middleware('read_file', {}, lambda args: 'must-not-run',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn')
assert json.loads(child_result)['effect_performed'] is False
# Explicit stop is local after an authoritative stale response, including child rotation.
stopped = json.loads(handle_function_call('colony_commitment_work', {'commitment_id': sys.argv[3], 'operation': 'release'},
    session_id='child-rotated', task_id='child-task', turn_id='child-turn'))
assert stopped['detached']
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn') == 'executed'
print(json.dumps({'native_race': True, 'recovery_fenced': True}))
'''


def test_native_sessions_share_one_undertaking(artifacts, tmp_path, monkeypatch):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes to exercise native coordination')
    # Only the test HTTP server imports the source store. The isolated native
    # process receives the wheel alone and has no sidecar on its import path.
    monkeypatch.syspath_prepend(str(ROOT / 'sidecar'))
    from colony_sidecar.commitments.store import CommitmentStore
    from colony_sidecar.commitments.work import CommitmentWork
    store = CommitmentStore(tmp_path / 'commitments.db')
    obligation = store.create('owner', 'Inspect one failing fixture')
    now = [1000.0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            assert self.path == '/advance-clock'
            now[0] += 121
            self.send_response(200); self.end_headers()
        def do_POST(self):
            assert self.path == '/v1/host/commitments/' + obligation['id'] + '/work'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['contact_id'] == 'owner'
            result = CommitmentWork(store, clock=lambda: now[0]).operate(obligation['id'], principal_id='host', **body)
            encoded = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / 'profile'), HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'),
        COLONY_GENERAL_PLUGIN_ACTIVE='1', COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled')
    try:
        result = run_python('-I', '-c', PROBE, installed, 'http://127.0.0.1:' + str(server.server_port), obligation['id'], cwd=tmp_path, env=env)
        assert json.loads(result.stdout.splitlines()[-1])['native_race'] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)

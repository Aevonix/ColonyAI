"""A packaged wizard, real sidecar and real Hermes preserve a fact across sessions."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import ROOT, run_python


INSTALL = r'''
import sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2]: sys.path.append(sys.argv[2])
from colony_sidecar.cli import main
sys.argv = ['colony', 'init', '--non-interactive', '--hermes-python', sys.executable,
    '--hermes-home', sys.argv[3], '--adapter-wheel', sys.argv[4], '--agent-name', 'Orion',
    '--contact-name', 'Fixture Owner', '--model-url', sys.argv[5], '--model', 'fixture', '--port', sys.argv[6]]
main()
'''
SERVER = r'''
import sys, importlib.abc
sys.path.insert(0, sys.argv[1])
if sys.argv[2]: sys.path.append(sys.argv[2])
class NoOptionalPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'neo4j','torch','sentence_transformers','lancedb','pyarrow','pandas'}:
            raise ImportError('Optional package absent in clean local qualification')
sys.meta_path.insert(0, NoOptionalPackages())
from colony_sidecar.cli import main
sys.argv = ['colony', 'start']  # Discover the same instance from HERMES_HOME.
main()
'''
TURN = r'''
import sys, json
from pathlib import Path
from hermes_cli.plugins import get_plugin_manager
from dotenv import load_dotenv
from hermes_constants import get_hermes_home
load_dotenv(get_hermes_home()/'.env', override=True)
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['colony'].enabled, manager._plugins['colony'].error
import colony_hermes
assert Path(colony_hermes.__file__).is_relative_to(get_hermes_home()/'colony'/'adapter')
from run_agent import AIAgent
agent = AIAgent(api_key='local-no-key', base_url=sys.argv[1], provider='openai',
    model='fixture', max_iterations=2, quiet_mode=True, platform='cli', enabled_toolsets=[],
    skip_context_files=False)
assert any(type(p).__name__ == 'ColonyMemoryProvider' for p in agent._memory_manager._providers), 'Native memory provider absent'
result = agent.run_conversation(sys.argv[2])
print(json.dumps({'answer': result['final_response'], 'session_id': agent.session_id}))
agent.close()
'''


def test_packaged_guided_setup_captures_and_recalls_with_real_native_sessions(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Requires qualified native Hermes')
    output, wheel, _, installed = artifacts
    # Wheel-install Colony separately from the source tree; dependencies must be
    # installed by the qualification job. Optional local reuse is never shipped.
    sidecar_output = output/'sidecar'
    run_python('-m', 'build', '--no-isolation', '--outdir', sidecar_output, cwd=ROOT/'sidecar')
    run_python('-m', 'pip', 'install', '--no-index', '--no-deps', '--target', installed,
               next(sidecar_output.glob('*.whl')), cwd=tmp_path)
    dependency_path = os.environ.get('COLONY_TEST_DEPENDENCY_PATH', '')
    requests = []
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            last = next((m.get('content', '') for m in reversed(body.get('messages', [])) if m.get('role') == 'user'), '')
            full = json.dumps(body.get('messages', []))
            answer = ('cobalt-716' if 'Which orchard badge' in str(last) and 'cobalt-716' in full
                      else 'No retained badge' if 'Which orchard badge' in str(last)
                      else 'Recorded.' if 'Please remember' in str(last)
                      else '{"claims": []}')
            response = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 1, 'model': 'fixture',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 20, 'completion_tokens': 4, 'total_tokens': 24}}).encode()
            content_type = 'application/json'
            if body.get('stream'):
                chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                    'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': answer}, 'finish_reason': None}]}
                ending = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
                response = ('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(ending)+'\n\ndata: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            self.send_response(200); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(response))); self.end_headers(); self.wfile.write(response)
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True); thread.start()
    endpoint = f'http://127.0.0.1:{model.server_port}/v1'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    home = tmp_path/'profile'; bundled = tmp_path/'bundled'; bundled.mkdir()
    env = {key: os.environ[key] for key in ('PATH', 'LANG') if key in os.environ}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), HERMES_BUNDLED_PLUGINS=str(bundled),
               HERMES_DISABLE_TELEMETRY='1', COLONY_GUARD_CHAT_MODE='off', LITELLM_LOCAL_MODEL_COST_MAP='True')
    server = None
    try:
        run_python('-I', '-c', INSTALL, installed, dependency_path, home, wheel, endpoint, port, cwd=tmp_path, env=env)
        log_path = tmp_path/'sidecar.log'
        with log_path.open('w') as log:
            server = subprocess.Popen([sys.executable, '-I', '-c', SERVER, str(installed), dependency_path],
                                      cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
        import httpx
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if server.poll() is not None: pytest.fail(log_path.read_text()[-12000:])
            try:
                if httpx.get(f'http://127.0.0.1:{port}/v1/host/health', timeout=1, trust_env=False).status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        else: pytest.fail('Sidecar did not start: '+log_path.read_text()[-12000:])
        first = run_python('-I', '-c', TURN, endpoint, 'Please remember that my orchard badge is cobalt-716.', cwd=tmp_path, env=env)
        instance = json.loads((home/'colony'/'instance.json').read_text())
        keyring = json.loads((home/'colony'/'api-keyring.json').read_text())
        response = httpx.post(f'http://127.0.0.1:{port}/v1/host/context/assemble',
            headers={'Authorization': 'Bearer '+keyring['principals'][0]['credentials'][0]['secret']},
            json={'identity': {'host_id': 'qualification'}, 'context': {'contact_id': instance['owner_id'], 'session_id': 'verification'},
                  'incoming_message': {'role':'user', 'content':'Which orchard badge did I ask you to remember?'}}, trust_env=False, timeout=15)
        assert response.status_code == 200, response.text
        assert 'cobalt-716' in response.text, response.text
        second = run_python('-I', '-c', TURN, endpoint, 'Which orchard badge did I ask you to remember?', cwd=tmp_path, env=env)
        a, b = [json.loads(next(line for line in reversed(r.stdout.splitlines()) if line.startswith('{"answer"'))) for r in (first, second)]
        assert a['session_id'] != b['session_id']
        assert b['answer'] == 'cobalt-716', (second.stdout, second.stderr, log_path.read_text()[-12000:])
        # The fixture only answers from the actual second-session API context.
        prompts = [body for body in requests if any('Which orchard badge' in str(m.get('content')) for m in body.get('messages', []))]
        assert any('cobalt-716' in json.dumps(body) for body in prompts)
        assert any('Orion' in str(message.get('content')) for body in requests
                   if body.get('stream') for message in body.get('messages', []) if message.get('role') == 'system')
        assert not (home/'colony'/'lancedb').exists()
        assert (home/'colony'/'contacts.db').exists()
    finally:
        if server is not None:
            server.terminate()
            try: server.wait(10)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
        model.shutdown(); model.server_close(); thread.join(2)

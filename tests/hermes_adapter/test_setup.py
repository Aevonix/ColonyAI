"""A packaged wizard, real sidecar and real Hermes preserve a fact across sessions."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import venv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import ROOT, run_python


INSTALL = r'''
import sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2]: sys.path.append(sys.argv[2])
import runpy
sys.argv = ['colony', 'init', '--non-interactive', '--hermes-python', sys.argv[7],
    '--hermes-home', sys.argv[3], '--adapter-wheel', sys.argv[4], '--agent-name', 'Orion',
    '--contact-name', 'Fixture Owner', '--model-url', sys.argv[5], '--model', 'fixture', '--port', sys.argv[6]]
runpy.run_module('colony_sidecar', run_name='__main__')
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
import runpy
sys.argv = ['colony', 'start']  # Discover the same instance from HERMES_HOME.
runpy.run_module('colony_sidecar', run_name='__main__')
'''
TURN = r'''
import sys, json, inspect
from pathlib import Path
from hermes_cli.plugins import get_plugin_manager
from dotenv import load_dotenv
from hermes_constants import get_hermes_home
load_dotenv(get_hermes_home()/'.env', override=True)
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['colony'].enabled, manager._plugins['colony'].error
import colony_hermes
manifest = json.loads((get_hermes_home()/'colony'/'instance.json').read_text())
binding = manifest['adapter_binding']
roots = (binding['sources'] if binding['mode'] == 'native-installed' else
         {name: str(get_hermes_home()/'colony'/'adapter'/name) for name in ('colony_hermes', 'colony_memory')})
assert Path(colony_hermes.__file__).resolve().parent == Path(roots['colony_hermes'])
from run_agent import AIAgent
agent = AIAgent(api_key='local-no-key', base_url=sys.argv[1], provider='openai',
    model='fixture', max_iterations=2, quiet_mode=True, platform='cli', enabled_toolsets=[],
    skip_context_files=False)
providers = [p for p in agent._memory_manager._providers if type(p).__name__ == 'ColonyMemoryProvider']
assert len(providers) == 1, 'Native memory provider absent or duplicated'
assert Path(inspect.getfile(type(providers[0]))).resolve().parent == Path(roots['colony_memory'])
result = agent.run_conversation(sys.argv[2])
print(json.dumps({'answer': result['final_response'], 'session_id': agent.session_id}))
agent.close()
'''
DOCTOR = r'''
import sys, runpy
sys.path.insert(0, sys.argv[1])
if sys.argv[2]: sys.path.append(sys.argv[2])
sys.argv = ['colony', 'doctor', '--json']
runpy.run_module('colony_sidecar', run_name='__main__')
'''


def _native_interpreter(tmp_path, wheel, adapter_installation):
    """Reuse qualified dependencies while controlling actual adapter metadata.

    The native subprocess gets a real fresh venv. Symlink dependencies instead
    of downloading them twice; never hide or mock Hermes discovery functions.
    """
    target = tmp_path/'native'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(target)
    site = target/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    for source in Path(sysconfig.get_path('purelib')).iterdir():
        if 'colony_hermes' in source.name or source.name in {'colony_memory', '__pycache__'}:
            continue
        (site/source.name).symlink_to(source, target_is_directory=source.is_dir())
    if adapter_installation == 'wheel':
        run_python('-m', 'pip', 'install', '--no-index', '--no-deps', '--target', site, wheel, cwd=tmp_path)
    elif adapter_installation == 'editable':
        run_python('-m', 'pip', 'install', '--no-index', '--no-deps', '--no-build-isolation',
            '--target', site, '-e', ROOT, cwd=tmp_path)
    return target/'bin'/'python'


@pytest.mark.parametrize('adapter_installation', ['absent', 'wheel', 'editable'])
def test_packaged_guided_setup_captures_and_recalls_with_real_native_sessions(artifacts, tmp_path, adapter_installation):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Requires qualified native Hermes')
    output, wheel, _, installed = artifacts
    installed_adapter = adapter_installation != 'absent'
    native_python = _native_interpreter(tmp_path, wheel, adapter_installation)
    def run_native(*args, **kwargs):
        result = subprocess.run([str(native_python), *map(str, args)], text=True, capture_output=True, timeout=120, **kwargs)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
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
        run_python('-I', '-c', INSTALL, installed, dependency_path, home, wheel, endpoint, port, native_python, cwd=tmp_path, env=env)
        manifest = json.loads((home/'colony'/'instance.json').read_text())
        assert manifest['adapter_binding']['mode'] == ('native-installed' if installed_adapter else 'private-directory')
        assert (home/'plugins'/'colony').exists() is not installed_adapter
        assert (home/'plugins'/'colony-memory').exists() is not installed_adapter
        if adapter_installation == 'editable':
            assert set(manifest['adapter_binding']['external_modules']) == {
                'colony_hermes.colony_hostworker.catalog', 'colony_hermes.colony_hostworker.contract'}
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
        first = run_native('-I', '-c', TURN, endpoint, 'Please remember that my orchard badge is cobalt-716.', cwd=tmp_path, env=env)
        instance = json.loads((home/'colony'/'instance.json').read_text())
        keyring = json.loads((home/'colony'/'api-keyring.json').read_text())
        response = httpx.post(f'http://127.0.0.1:{port}/v1/host/context/assemble',
            headers={'Authorization': 'Bearer '+keyring['principals'][0]['credentials'][0]['secret']},
            json={'identity': {'host_id': 'qualification'}, 'context': {'contact_id': instance['owner_id'], 'session_id': 'verification'},
                  'incoming_message': {'role':'user', 'content':'Which orchard badge did I ask you to remember?'}}, trust_env=False, timeout=15)
        assert response.status_code == 200, response.text
        assert 'cobalt-716' in response.text, response.text
        second = run_native('-I', '-c', TURN, endpoint, 'Which orchard badge did I ask you to remember?', cwd=tmp_path, env=env)
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
        diagnostic = run_python('-I', '-c', DOCTOR, installed, dependency_path, cwd=tmp_path, env=env)
        checks = json.loads(diagnostic.stdout)
        assert checks['ok'], diagnostic.stdout
        names = {row['name'] for row in checks['results']}
        assert 'server-source-memory' in names and 'server-auth' not in names
        assert keyring['principals'][0]['credentials'][0]['secret'] not in diagnostic.stdout
        if adapter_installation == 'wheel':
            # Stop extraction from the completed conversations before counting
            # requests from a separate attachment. The model server stays live.
            server.terminate()
            server.wait(10)
            server = None
            # A same-version installed package with different code must not be
            # silently selected over the requested artifact on another attach.
            client = Path(manifest['adapter_binding']['sources']['colony_hermes'])/'client.py'
            client.write_bytes(client.read_bytes()+b'\n# Different installed payload\n')
            other = tmp_path/'mismatch-home'; other.mkdir()
            (other/'config.yaml').write_text('plugins: {enabled: []}\n')
            (other/'SOUL.md').write_text('Retained identity')
            before = {path.name: path.read_bytes() for path in other.iterdir()}
            request_count = len(requests)
            rejected = subprocess.run([sys.executable, '-I', '-c', INSTALL, str(installed), dependency_path,
                str(other), str(wheel), endpoint, str(port), str(native_python)],
                cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
            assert rejected.returncode != 0 and 'different installed Colony adapter' in rejected.stdout
            assert before == {path.name: path.read_bytes() for path in other.iterdir()}
            assert len(requests) == request_count, 'Adapter conflict must fail before endpoint probing'
    finally:
        if server is not None:
            server.terminate()
            try: server.wait(10)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
        model.shutdown(); model.server_close(); thread.join(2)

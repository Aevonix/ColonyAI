"""Explicit opt-in only: two disposable native user services, never existing units."""
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest


@pytest.mark.skipif(os.environ.get('COLONY_TEST_USER_SERVICE') != '1',
                    reason='Opt-in native user-manager qualification')
def test_installed_cli_two_instances_crash_recovery_and_retained_memory(tmp_path):
    python = os.environ['COLONY_TEST_SERVICE_PYTHON']
    hermes_python = os.environ['COLONY_TEST_HERMES_PYTHON']
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COLONY_', 'HERMES_', 'OPENAI_', 'ANTHROPIC_', 'PYTHONPATH'))}
    env.update(HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
               LITELLM_LOCAL_MODEL_COST_MAP='True')
    # The unit suite deliberately replaces HOME/XDG with fake locations. This
    # opt-in test needs the real user manager's discovery directory, while all
    # agent config/data remains in explicitly selected disposable instances.
    env['HOME'] = pwd.getpwuid(os.getuid()).pw_dir
    env.pop('XDG_CONFIG_HOME', None)
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            data = json.dumps({'model': 'service-fixture', 'choices': [
                {'message': {'role': 'assistant', 'content': '[]'}, 'finish_reason': 'stop'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True); thread.start()
    instances = []

    def cli(*args, check=True):
        result = subprocess.run([python, '-m', 'colony_sidecar', *map(str, args)],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def service(state, action):
        return json.loads(cli('--instance', state, 'service', action).stdout)

    def context(instance):
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(instance['url']+'/v1/host/context/assemble', headers=instance['headers'], json={
                'identity': {'host_id': 'service-fixture'},
                'context': {'contact_id': instance['owner'], 'session_id': 'second-session'},
                'incoming_message': {'role': 'user', 'content': 'violet observatory shelf'}})
            assert response.status_code == 200, response.text
            return response.text

    try:
        for name in ('first', 'second'):
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0)); port = free.getsockname()[1]
            home = tmp_path/name
            cli('init', '--non-interactive', '--hermes-home', home, '--hermes-python', hermes_python,
                '--agent-name', 'Service Fixture', '--contact-name', 'Fixture Owner',
                '--model-url', f'http://127.0.0.1:{model.server_port}/v1', '--model', 'service-fixture', '--port', port)
            state = home/'colony'
            values = dict(line.split('=', 1) for line in (state/'.env').read_text().splitlines() if '=' in line)
            item = {'state': state, 'owner': values['COLONY_OWNER_CONTACT_ID'],
                'url': f'http://127.0.0.1:{port}',
                'headers': {'Authorization': 'Bearer '+values['COLONY_CLIENT_API_KEY']}}
            instances.append(item)
            installed = service(state, 'install')
            assert installed['installed'] and not installed['running']
            assert installed['autostart_scope'] == 'user-session'
            started = service(state, 'start')
            assert started['ready']
            item['pid'] = started['pid']
        first, second = instances
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.put(first['url']+'/v2/host/turns/service-fixture-turn', headers=first['headers'], json={
                'identity': {'host_id': 'service-fixture'},
                'context': {'contact_id': first['owner'], 'session_id': 'first-session', 'turn_id': 'service-fixture-turn'},
                'user_message': {'role': 'user', 'content': 'The violet observatory shelf holds the service-fixture brass key.'}})
            assert response.status_code == 201, response.text
        assert 'service-fixture brass key' in context(first)
        assert 'service-fixture brass key' not in context(second)
        # Kill only the PID just returned by this newly created manager unit.
        os.kill(first['pid'], signal.SIGKILL)
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            current = service(first['state'], 'status')
            if current['ready'] and current['pid'] != first['pid']:
                break
            time.sleep(.25)
        else:
            pytest.fail('Native user manager did not recover the killed fixture process')
        assert 'service-fixture brass key' in context(first)
        assert service(second['state'], 'status')['pid'] == second['pid']
        assert not service(first['state'], 'stop')['running']
        assert service(second['state'], 'status')['ready']
        assert not service(first['state'], 'uninstall')['installed']
        assert (first['state']/'turn-idempotency.db').is_file()
        assert service(second['state'], 'status')['ready']
    finally:
        failures = []
        for item in instances:
            result = cli('--instance', item['state'], 'service', 'uninstall', check=False)
            if result.returncode:
                failures.append(str(item['state']))
        model.shutdown(); model.server_close(); thread.join(timeout=5)
        assert not failures, 'Fixture service cleanup failed: '+', '.join(failures)

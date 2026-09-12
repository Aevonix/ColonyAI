"""Suite CLI through a real isolated Hermes process and controlled HTTP endpoint."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from pacomind.qualification.cli import add_parser, run
from pacomind.qualification.native import configuration, native_cli
from pacomind.qualification.records import read


@contextmanager
def endpoint(*, blocked=False, unavailable=False):
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.reply({'data': [{'id': 'native-fixture', 'context_length': 65536}]})

        def reply(self, data):
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(data)
            entered.set()
            if unavailable:
                self.send_error(503, 'Controlled unavailable endpoint')
                return
            if blocked:
                release.wait(20)
            try:
                if data.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    chunk = {'id': 'native-controlled', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'native-fixture', 'choices': [{'index': 0,
                        'delta': {'role': 'assistant', 'content': '{"blue":"drawer 4","silver":null}'},
                        'finish_reason': None}]}
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                    chunk['choices'] = [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
                    self.wfile.flush()
                    return
                self.reply({'id': 'native-controlled', 'object': 'chat.completion', 'created': 1,
                    'model': 'native-fixture', 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': '{"blue":"drawer 4","silver":null}'},
                    'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, entered
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(2)


def configured(tmp_path, url):
    config = tmp_path/'hermes.yaml'
    config.write_text(json.dumps({'model': {'provider': 'fixture', 'default': 'native-fixture',
        'context_length': 65536}, 'providers': {'fixture': {'base_url': url,
        'default_model': 'native-fixture', 'api_key': 'controlled-fixture-key',
        'request_timeout_seconds': 60, 'stale_timeout_seconds': 60,
        'extra_body': {'temperature': 0.1}}}, 'agent': {'reasoning_effort': 'low'},
        'memory': {'provider': 'must-not-load'}, 'platforms': {'whatsapp': {'enabled': True}}}))
    return config


def arguments(config, output, *, deadline=8, cleanup=5):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    return parser.parse_args(['models', 'evaluate', 'fixture', '--suite', 'native',
        '--config', str(config), '--output', str(output), '--evidence-mode', 'controlled',
        '--deadline-seconds', str(deadline), '--cleanup-seconds', str(cleanup)])


def test_cli_native_case_runs_actual_loop_in_fresh_home_and_keeps_scope_honest(tmp_path, monkeypatch):
    live = tmp_path/'existing-home'
    live.mkdir()
    (live/'SOUL.md').write_text('Existing profile sentinel must not enter the request.')
    (live/'secret.key').write_text('unrelated sentinel')
    monkeypatch.setenv('HERMES_HOME', str(live))
    before = {p.name: p.read_bytes() for p in live.iterdir()}
    with endpoint() as (url, requests, entered):
        config = configured(tmp_path, url)
        config_bytes = config.read_bytes()
        output = tmp_path/'run'
        assert run(arguments(config, output)) == 0
        row = read(output/'attempts/native.chat.grounded-note/result.json')
        manifest = read(output/'run.json')
        assert entered.is_set() and len(requests) == 1
        assert requests[0]['model'] == 'native-fixture'
        assert requests[0].get('reasoning_effort') == 'low'
        assert requests[0]['temperature'] == .1
        assert requests[0].get('tools') in (None, [])
        assert 'Existing profile sentinel' not in json.dumps(requests)
        assert row['outcome'] == 'pass' and row['primary_outcome'] == 'unverified'
        assert row['effects']['consumer'] == 'isolated_native_cli_loop'
        assert row['qualification_routing']['scope'] == 'isolated_hermes_profile'
        observed = row['observations'][0]
        assert observed['process_exited'] and observed['owned_worker_stopped']
        assert observed['agent_close_returned'] and not observed['hard_interrupt_requested']
        assert observed['returned_model'] is None
        assert manifest['cases'][0]['boundary'] == 'native_hermes'
        assert manifest['recipe']['request_timeout_seconds'] == 60
        assert 'controlled-fixture-key' not in json.dumps(manifest)
        assert not list((output/'attempts/native.chat.grounded-note').glob('state-*'))
        assert config.read_bytes() == config_bytes
    assert {p.name: p.read_bytes() for p in live.iterdir()} == before


def test_cli_elapsed_deadline_interrupts_silent_native_request_before_socket_timeout(tmp_path):
    with endpoint(blocked=True) as (url, requests, entered):
        output = tmp_path/'run'
        start = time.monotonic()
        assert run(arguments(configured(tmp_path, url), output, deadline=4)) == 1
        elapsed = time.monotonic()-start
        row = read(output/'attempts/native.chat.grounded-note/result.json')
        assert entered.is_set() and len(requests) == 1
        assert row['outcome'] == 'timeout' and row['failure_category'] == 'deadline'
        observed = row['observations'][0]
        assert observed['hard_interrupt_requested'] and observed['owned_worker_stopped']
        assert observed['agent_close_returned'] and observed['process_exited']
        assert not observed['forced_termination']
        assert observed['native_stage'] == 'interrupted'
        assert row['cleanup'] == 'state_directory_removed'
        assert 4 <= elapsed < 9


def test_native_transport_failure_is_not_graded_as_a_model_answer(tmp_path):
    with endpoint(unavailable=True) as (url, requests, _entered):
        output = tmp_path/'run'
        assert run(arguments(configured(tmp_path, url), output)) == 1
        row = read(output/'attempts/native.chat.grounded-note/result.json')
        assert requests
        assert row['outcome'] == 'error' and row['checks'] == {}
        assert row['output'] is None
        observed = row['observations'][0]
        assert observed['native_stage'] == 'incomplete'
        assert observed['native_turn']['completed'] is False
        assert row['cleanup'] == 'state_directory_removed'


def test_deadline_during_process_creation_keeps_ownership(tmp_path, monkeypatch):
    import asyncio
    from pacomind.qualification import native
    original = asyncio.create_subprocess_exec
    spawned = []

    async def slow_creation(*args, **kwargs):
        proc = await original(*args, **kwargs)
        spawned.append(proc)
        await asyncio.sleep(.3)
        return proc

    monkeypatch.setattr(native.asyncio, 'create_subprocess_exec', slow_creation)
    with endpoint(blocked=True) as (url, _requests, _entered):
        output = tmp_path/'run'
        assert run(arguments(configured(tmp_path, url), output, deadline=.1)) == 1
        row = read(output/'attempts/native.chat.grounded-note/result.json')
        assert len(spawned) == 1 and spawned[0].returncode is not None
        assert row['outcome'] == 'timeout'
        assert row['observations'][0]['process_exited']
        assert row['observations'][0]['owned_worker_stopped']
        assert row['cleanup'] == 'state_directory_removed'


@pytest.mark.asyncio
async def test_native_cancellation_records_real_interruption_and_exit(tmp_path):
    import asyncio
    from pacomind.qualification.native import cases
    from pacomind.qualification.cases import EVALUATORS
    from pacomind.qualification.runner import evaluate
    with endpoint(blocked=True) as (url, requests, entered):
        config, recipe = configuration(configured(tmp_path, url), 'fixture')
        output = tmp_path/'run'
        task = asyncio.create_task(evaluate(output, recipe, cases(['chat']),
            {'native_cli': native_cli}, EVALUATORS,
            lambda _: SimpleNamespace(binding='fixture', native_config=config)))
        assert await asyncio.to_thread(entered.wait, 8)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = read(output/'attempts/native.chat.grounded-note/result.json')
        assert row['outcome'] == 'interrupted' and len(requests) == 1
        assert row['observations'][0]['hard_interrupt_requested']
        assert row['observations'][0]['owned_worker_stopped']
        assert row['cleanup'] == 'state_directory_removed'


@pytest.mark.asyncio
async def test_incomplete_native_stop_retains_state_and_does_not_start_later_case(tmp_path, monkeypatch):
    """A real owned child ignores TERM; only it is killed, and uncertainty is retained."""
    import asyncio
    import sys
    from pacomind.qualification import native
    from pacomind.qualification.cases import EVALUATORS
    from pacomind.qualification.runner import evaluate
    config, recipe = configuration(configured(tmp_path, 'http://127.0.0.1:9/v1'), 'fixture')
    original = asyncio.create_subprocess_exec
    spawned = []

    async def uncooperative(*args, **kwargs):
        child = await original(sys.executable, '-I', '-c',
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)', **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(native.asyncio, 'create_subprocess_exec', uncooperative)
    case = native.cases(['chat'], deadline_seconds=.3, cleanup_seconds=.05)[0]
    output = tmp_path/'run'
    await evaluate(output, recipe, [case, replace(case, id='later')], {'native_cli': native_cli},
        EVALUATORS, lambda _: SimpleNamespace(binding='fixture', native_config=config))
    assert len(spawned) == 1 and spawned[0].returncode is not None
    row = read(output/'attempts/native.chat.grounded-note/result.json')
    assert row['outcome'] == 'timeout' and row['cleanup'] == 'state_directory_retained'
    assert row['observations'][0]['forced_termination']
    assert not row['observations'][0]['owned_worker_stopped']
    assert Path(row['retained_state_dir']).is_dir()
    assert not (output/'attempts/later/started.json').exists()
    first = (output/'attempts/native.chat.grounded-note/result.json').read_bytes()
    await evaluate(output, recipe, [case, replace(case, id='later')], {'native_cli': native_cli},
        EVALUATORS, lambda _: SimpleNamespace(binding='fixture', native_config=config), resume=True)
    assert len(spawned) == 1
    assert (output/'attempts/native.chat.grounded-note/result.json').read_bytes() == first
    assert not (output/'attempts/later/started.json').exists()

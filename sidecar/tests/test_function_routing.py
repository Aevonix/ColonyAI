"""Real HTTP failover/reload with neutral fixtures, not model quality scores."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from colony_sidecar.router.router import LLMRouter


@contextmanager
def endpoint(*, status=200, delay=0, started=None, release=None, content=None, location=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'payload': payload, 'authorization': self.headers.get('Authorization')})
            if started: started.set()
            if release: release.wait(3)
            if delay: time.sleep(delay)
            if status != 200:
                self.send_response(status)
                if location: self.send_header('Location', location)
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                try: self.wfile.write(b'{"error":{"message":"neutral unavailable","type":"server_error"}}')
                except (BrokenPipeError, ConnectionResetError): pass
                return
            answer = content(payload) if callable(content) else (content or payload['model'])
            result = {'id': 'neutral', 'object': 'chat.completion', 'created': 1, 'model': payload['model'],
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            try: self.wfile.write(json.dumps(result).encode())
            except (BrokenPipeError, ConnectionResetError): pass
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally: server.shutdown(); server.server_close(); thread.join(2)


def config(first, second, **role):
    return {'provider': 'vllm', 'apiKey': 'neutral-key', 'models': {},
        'modelPool': {
            'interactive': {'model': 'fast-neutral', 'baseUrl': first, 'supportsTools': True,
                'contextTokens': 32000, 'latencyMs': 200, 'tokensPerSecond': 80, 'concurrency': 2},
            'deliberate': {'model': 'strong-neutral', 'baseUrl': second, 'supportsTools': True,
                'supportsVision': True, 'contextTokens': 128000, 'latencyMs': 2000, 'weightRevision': 'fixture-revision-b'},
        },
        'functionRoles': {'extraction': {'candidates': ['interactive', 'deliberate'], 'timeoutSeconds': 1, 'deadlineSeconds': 3, **role},
            'chat': {'candidates': ['interactive', 'deliberate'], 'maxLatencyMs': 500},
            'reasoning': ['deliberate', 'interactive'], 'planning': ['deliberate', 'interactive'],
            'vision': ['interactive', 'deliberate']}}


def router(cfg, path=None):
    r = LLMRouter(tiers={}, self_learner=SimpleNamespace())
    r.configure(cfg, config_path=path)
    return r


async def complete(r, role='extraction', **context):
    return await r.complete([{'role': 'user', 'content': 'Neutral routing fixture.'}], context={'function_role': role, **context})


@pytest.mark.asyncio
async def test_actual_http_same_function_failover_provenance_and_capability_filters(monkeypatch):
    with endpoint(status=503) as (first, a), endpoint() as (second, b):
        r = router(config(first, second))
        # Environment credentials/proxies cannot change explicit local binding.
        monkeypatch.setenv('OPENAI_API_BASE', 'https://untrusted.invalid/v1')
        monkeypatch.setenv('OPENAI_API_KEY', 'wrong-environment-key')
        monkeypatch.setenv('HTTP_PROXY', 'http://untrusted.invalid:80')
        response = await complete(r)
        assert response.content == 'strong-neutral'
        assert response.function_role == 'extraction' and response.binding == 'deliberate'
        assert response.model_revision == 'fixture-revision-b' and response.config_revision
        assert len(a) == len(b) == 1 and b[0]['authorization'] == 'Bearer neutral-key'
        await complete(r, 'vision')
        assert len(a) == 1 and len(b) == 2  # no modality-blind tier escalation
        with pytest.raises(RuntimeError): await complete(r, 'chat')
        assert len(a) == 2 and len(b) == 2  # slow candidate violates chat bound
        await complete(r, required_context_tokens=64000)
        assert len(a) == 2 and len(b) == 3
        status = r.routing_status()
        assert status['recent_calls'][-1]['config_revision'] == response.config_revision
        assert 'neutral-key' not in json.dumps(status) and 'baseUrl' not in json.dumps(status)


@pytest.mark.asyncio
async def test_inflight_fallback_keeps_old_snapshot_next_call_reloads(tmp_path):
    started, release = threading.Event(), threading.Event()
    with endpoint(status=503, started=started, release=release) as (first, a), endpoint() as (second, b):
        path = tmp_path / 'config.json'; original = config(first, second)
        path.write_text(json.dumps(original)); r = router(original, path)
        old = r.routing_status()['config_revision']
        pending = asyncio.create_task(complete(r))
        assert await asyncio.to_thread(started.wait, 2)
        changed = deepcopy(original)
        changed['modelPool']['deliberate']['model'] = 'replacement-neutral'
        changed['functionRoles']['extraction']['candidates'] = ['deliberate']
        path.write_text(json.dumps(changed))
        new = r.routing_status()['config_revision']
        assert new != old
        release.set()
        first_response = await pending
        assert first_response.content == 'strong-neutral' and first_response.config_revision == old
        next_response = await complete(r)
        assert next_response.content == 'replacement-neutral' and next_response.config_revision == new
        path.write_text('{partial')
        assert (await complete(r)).config_revision == new
        assert r.routing_status()['reload_error'] == 'JSONDecodeError'
        assert len(a) == 1 and len(b) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'connection'])
async def test_timeout_and_connection_failover_are_bounded(failure):
    with endpoint(delay=.2) as (first, a), endpoint() as (second, b):
        if failure == 'connection':
            import socket
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); unused = sock.getsockname()[1]
            first = f'http://127.0.0.1:{unused}/v1'
        r = router(config(first, second, timeoutSeconds=.05 if failure == 'timeout' else .5))
        response = await complete(r)
        assert response.content == 'strong-neutral' and len(b) == 1
        assert len(a) <= 1


@pytest.mark.asyncio
async def test_private_candidates_never_use_cloud_or_follow_redirects():
    with endpoint(status=307, location='https://untrusted.invalid/leak') as (first, a), endpoint() as (second, b):
        cfg = config(first, second)
        cfg['modelPool']['remote'] = {'model': 'remote', 'baseUrl': 'https://8.8.8.8/v1', 'supportsVision': True}
        cfg['functionRoles']['extraction']['candidates'] = ['remote', 'interactive', 'deliberate']
        r = router(cfg)
        with pytest.raises(RuntimeError): await complete(r)
        assert len(a) == 1 and b == []
        cfg['functionRoles']['extraction']['candidates'] = ['remote']
        r.configure(cfg)
        with pytest.raises(RuntimeError): await complete(r, allow_cloud=True)
        assert len(a) == 1 and b == []  # prompt hints do not authorize remote routing


@pytest.mark.asyncio
async def test_deployment_hostname_is_declared_resolved_checked_and_pinned(monkeypatch):
    import socket
    with endpoint() as (first, calls):
        cfg = config(first.replace('127.0.0.1', 'model.example'), first)
        cfg['functionRoles']['extraction']['candidates'] = ['interactive']
        r = router(cfg)
        with pytest.raises(RuntimeError): await complete(r)
        assert calls == []  # a hostname does not declare itself local
        cfg['localHosts'] = ['model.example']
        cfg['localNetworks'] = ['127.0.0.0/8']
        r.configure(cfg)
        dns_calls = []
        async def resolve(host, port, **kwargs):
            dns_calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 0))]
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        assert (await complete(r)).content == 'fast-neutral'
        assert dns_calls == ['model.example'] and len(calls) == 1
        async def mixed(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 0)) for ip in ('127.0.0.1', '8.8.8.8')]
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', mixed)
        with pytest.raises(RuntimeError): await complete(r)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_source_claim_flow_retains_role_generation_and_scoped_evidence(tmp_path):
    from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import claim
    text = 'My office is in Alder.'
    answer = json.dumps([claim(text, 'Alder')])
    with endpoint(status=503) as (first, a), endpoint(content=answer) as (second, b):
        r = router(config(first, second))
        ledger = TurnIdempotencyLedger(tmp_path / 'turns.db')
        ledger.record_source('neutral-source', contact_id='person', session_id='sms', messages=[{'role': 'user', 'content': text}])
        projection = SourceClaimProjection(ledger)
        assert await projection.process_one(r)
        with ledger._connect() as conn:
            rows = projection._rows(conn, 'person', 'voice')
            assert len(rows) == 1 and rows[0]['value'] == 'Alder'
            assert rows[0]['model_provenance']['function_role'] == 'extraction'
            assert rows[0]['model_provenance']['config_revision'] == r.routing_status()['config_revision']
            assert rows[0]['model_provenance']['weight_revision'] == 'fixture-revision-b'
            assert projection._rows(conn, 'stranger', 'voice') == []
        assert len(a) == len(b) == 1


@pytest.mark.asyncio
async def test_real_retained_image_job_selects_vision_binding_and_records_generation(tmp_path):
    from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
    from colony_sidecar.turns.media import SourceMedia
    from test_source_media import message
    with endpoint() as (first, a), endpoint(content='A red rectangle and blue circle.') as (second, b):
        r = router(config(first, second))
        ledger = TurnIdempotencyLedger(tmp_path / 'turns.db')
        ledger.record_source('neutral-image', contact_id='person', session_id='sms', messages=[message()])
        media = SourceMedia(ledger)
        assert await media.process_one(r)
        status = media.status('person')[0]
        provenance = json.loads(status['model_provenance_json'])
        assert status['status'] == 'complete' and provenance['function_role'] == 'vision'
        assert provenance['weight_revision'] == 'fixture-revision-b'
        assert not a and len(b) == 1
        assert media.search('blue circle', contact_id='person', session_id='voice')


@pytest.mark.asyncio
async def test_declared_tool_throughput_and_context_requirements_and_unknown_weights():
    with endpoint() as (first, a), endpoint() as (second, b):
        cfg = config(first, second)
        cfg['modelPool']['interactive'].pop('supportsTools')
        cfg['functionRoles']['extraction']['candidates'] = ['interactive', 'deliberate']
        r = router(cfg)
        tool = {'type': 'function', 'function': {'name': 'neutral_read', 'parameters': {'type': 'object', 'properties': {}}}}
        result = await r.complete([{'role': 'user', 'content': 'Neutral tool routing.'}], tools=[tool], context={'function_role': 'extraction'})
        assert result.binding == 'deliberate' and not a and len(b) == 1
        cfg['functionRoles']['extraction'].update(minTokensPerSecond=40, minConcurrency=2)
        r.configure(cfg)
        result = await complete(r)
        assert result.binding == 'interactive' and result.model_revision == 'unknown'
        cfg['functionRoles']['extraction']['minContextTokens'] = 500000
        r.configure(cfg)
        with pytest.raises(RuntimeError): await complete(r)
        assert len(a) == len(b) == 1


@pytest.mark.asyncio
async def test_unconfigured_or_invalid_initial_file_never_uses_default_provider(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'; path.write_text('{partial')
    r = LLMRouter(tiers={}, self_learner=SimpleNamespace())
    r.watch_config(path)
    async def never(**kwargs): raise AssertionError('must not call a default model')
    monkeypatch.setattr(r, '_litellm_call', never)
    with pytest.raises(ValueError): await complete(r)
    assert r.routing_status()['reload_error'] == 'JSONDecodeError'


@pytest.mark.asyncio
async def test_legacy_binding_tool_compatibility_is_reported_and_preserved():
    with endpoint() as (address, calls):
        r = router({'provider': 'vllm', 'baseUrl': address, 'models': {'small': 'legacy-neutral'}})
        assert r.routing_status()['models']['small']['legacy_unknown_tools_allowed'] is True
        tool = {'type': 'function', 'function': {'name': 'neutral_read', 'parameters': {'type': 'object', 'properties': {}}}}
        response = await r.complete([{'role': 'user', 'content': 'Neutral request.'}], tools=[tool])
        assert response.model_id == 'openai/legacy-neutral' and len(calls) == 1


@pytest.mark.asyncio
async def test_obviously_undersized_fallback_is_not_sent_the_large_prompt():
    with endpoint(status=503) as (first, a), endpoint() as (second, b):
        cfg = config(first, second)
        cfg['modelPool']['deliberate']['contextTokens'] = 100
        r = router(cfg)
        with pytest.raises(RuntimeError):
            await r.complete([{'role': 'user', 'content': 'neutral text ' * 200}], context={'function_role': 'extraction', 'max_output_tokens': 20})
        assert len(a) == 1 and b == []


@pytest.mark.asyncio
async def test_ollama_compatibility_requires_explicit_protocol_and_api_root():
    with endpoint() as (address, calls):
        cfg = {'provider': 'ollama', 'baseUrl': address, 'models': {'small': 'neutral-ollama'}}
        r = LLMRouter(tiers={}, self_learner=SimpleNamespace())
        with pytest.raises(ValueError, match='OpenAI-compatible'): r.configure(cfg)
        cfg['protocol'] = 'openai-chat'
        r.configure(cfg)
        assert (await complete(r)).model_id == 'openai/neutral-ollama'
        assert calls[0]['payload']['model'] == 'neutral-ollama'
        with pytest.raises(ValueError, match='/v1'):
            r.configure({**cfg, 'baseUrl': address.removesuffix('/v1')})
        assert (await complete(r)).model_id == 'openai/neutral-ollama'


@pytest.mark.asyncio
async def test_unsupported_streaming_fails_before_any_model_call():
    with endpoint() as (address, calls):
        r = router(config(address, address))
        with pytest.raises(ValueError, match='complete responses'):
            await r.complete([{'role': 'user', 'content': 'Neutral request.'}], stream=True)
        assert calls == []


@pytest.mark.asyncio
async def test_tool_result_round_trip_accepts_null_assistant_content():
    with endpoint() as (address, calls):
        r = router(config(address, address))
        tool = {'type': 'function', 'function': {'name': 'neutral_read', 'parameters': {'type': 'object', 'properties': {}}}}
        messages = [{'role': 'user', 'content': 'Read the neutral fixture.'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read-1', 'type': 'function', 'function': {'name': 'neutral_read', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'Fixture read.'}]
        response = await r.complete(messages, tools=[tool])
        assert response.function_role == 'reasoning'
        assert calls[0]['payload']['messages'][1].get('content') is None
        assert calls[0]['payload']['messages'][1]['tool_calls'][0]['id'] == 'read-1'

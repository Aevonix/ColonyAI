"""Exact source replay filtering, with real durable erasure rules."""
import copy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from test_hermes_turn_outbox import _load_plugin
from test_turn_source_evidence import source_app
from colony_sidecar.turns import TurnIdempotencyLedger


def packet(contact, watermark, text):
    stamp = json.dumps({'contact_id': contact, 'watermark': watermark})
    return '<memory-context>\n[colony-recall-v1 ' + stamp + ']\n' + text + '\n[/colony-recall-v1]\n</memory-context>'


@pytest.fixture
def runtime(tmp_path):
    plugin = _load_plugin()
    module = importlib.import_module(plugin.__name__ + '.request_memory')
    outbox = plugin.TurnOutbox(tmp_path / 'outbox.db')
    outbox.prepare()
    ledger = TurnIdempotencyLedger(tmp_path / 'canonical.db')
    fact = 'My neutral orchard badge is cobalt-716.'
    ledger.record_source('fixture-source', contact_id='owner', session_id='original',
        messages=[{'role': 'user', 'content': fact}], derive_claims=False)
    return SimpleNamespace(module=module, outbox=outbox, ledger=ledger, fact=fact)


def test_retained_packets_and_exact_copies_reconcile_after_restart(runtime):
    rt = runtime
    request = {'messages': [
        {'role': 'system', 'content': 'Stable identity'},
        {'role': 'user', 'content': rt.fact + '\n\n' + packet('owner', 0, rt.fact)},
        {'role': 'assistant', 'content': rt.fact},
        {'role': 'user', 'content': 'Another topic' + '\n\n' + packet('owner', 0, rt.fact)},
    ]}
    original = copy.deepcopy(request)
    assert rt.module.filter_request(request, contact_id='owner', watermark=0, rules=[], fresh=True) == original
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['fixture-source'])
    rt.outbox.apply_erasure_page('owner', rt.ledger.erasure_feed('owner'))
    watermark, rules = type(rt.outbox)(rt.outbox.path).erasure_state('owner')
    filtered = rt.module.filter_request(request, contact_id='owner', watermark=watermark, rules=rules, fresh=True)
    assert rt.fact not in json.dumps(filtered)
    assert 'Another topic' in json.dumps(filtered)
    assert request == original
    fresh = {'messages': [{'role': 'user', 'content': 'Now' + '\n\n' + packet('owner', watermark, 'New retained memory')}]}
    assert rt.module.filter_request(fresh, contact_id='owner', watermark=watermark, rules=rules, fresh=True) == fresh
    foreign = {'messages': [{'role': 'user', 'content': packet('other', watermark, 'Foreign context')}]}
    assert 'Foreign context' not in json.dumps(rt.module.filter_request(foreign, contact_id='owner', watermark=watermark, rules=rules, fresh=True))


def test_unavailable_feed_returns_current_turn_and_tool_results_without_old_context(runtime):
    rt = runtime
    def unavailable(*args, **kwargs):
        raise OSError('offline')
    middleware = rt.module.RequestMemory(SimpleNamespace(get=unavailable), rt.outbox)
    request = {'messages': [
        {'role': 'user', 'content': 'Old source'},
        {'role': 'assistant', 'content': 'Old reply'},
        {'role': 'user', 'content': 'Continue build' + '\n\n' + packet('owner', 0, 'Old recall')},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'step', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'content': 'Current tool result', 'tool_call_id': 'step'},
    ]}
    result = middleware(request, SimpleNamespace(contact_id='owner', valid_participant=True, task_id='task', turn_id='turn'))
    assert result['reason'] == 'source_erasure_unavailable'
    wire = json.dumps(result['request'])
    assert 'Old' not in wire and 'Continue build' in wire and 'Current tool result' in wire
    assert result['request']['messages'][-1]['tool_call_id'] == 'step'


def test_partial_feed_never_certifies_freshness_and_makes_bounded_progress(runtime):
    rt = runtime
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['fixture-source'])
    calls = []
    def get(path, **kwargs):
        calls.append(kwargs['params']['after'])
        page = rt.ledger.erasure_feed('owner', kwargs['params']['after'])
        page['complete'] = False
        return httpx.Response(200, json=page, request=httpx.Request('GET', 'http://fixture' + path))
    result = rt.module.RequestMemory(SimpleNamespace(get=get), rt.outbox)(
        {'messages': [{'role': 'user', 'content': rt.fact}]}, SimpleNamespace(contact_id='owner', valid_participant=True, task_id='task', turn_id='turn'))
    assert result['reason'] == 'source_erasure_unavailable'
    assert len(calls) <= 4 and calls[0] == 0
    assert rt.fact not in json.dumps(result)
    assert rt.outbox.erasure_watermark('owner') == 1


def test_responses_and_detached_tagged_packet_are_filtered(runtime):
    rt = runtime
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['fixture-source'])
    page = rt.ledger.erasure_feed('owner')
    detached = packet('owner', 0, rt.fact).replace('<memory-context>\n', '').replace('\n</memory-context>', '')
    request = {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': detached}]},
                         {'type': 'function_call_output', 'call_id': 'x', 'output': rt.fact}],
               'instructions': 'Stable identity'}
    filtered = rt.module.filter_request(request, contact_id='owner', watermark=1, rules=page['events'], fresh=True)
    assert rt.fact not in json.dumps(filtered)
    assert filtered['input'][1]['call_id'] == 'x'


def test_missing_observation_cannot_replay_enriched_history(runtime):
    rt = runtime
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['fixture-source'])
    def get(path, **kwargs):
        return httpx.Response(200, json=rt.ledger.erasure_feed('owner', kwargs['params']['after']),
                              request=httpx.Request('GET', 'http://fixture' + path))
    middleware = rt.module.RequestMemory(SimpleNamespace(get=get), rt.outbox)
    scope = SimpleNamespace(contact_id='owner', valid_participant=True, task_id='task', turn_id='turn')
    enriched = rt.fact + '\n\nNeutral clock note'
    history = [{'role': 'user', 'content': rt.fact, 'api_content': enriched}]
    request = {'messages': [{'role': 'user', 'content': enriched}, {'role': 'user', 'content': 'Current request'}]}
    middleware.observe(scope, history)
    checked = middleware(request, scope)
    assert checked['reason'] == 'source_erasure_checked' and rt.fact not in json.dumps(checked)
    middleware.finish(task_id='task', turn_id='turn')
    without_observation = middleware(request, scope)
    assert without_observation['reason'] == 'source_erasure_unavailable'
    assert rt.fact not in json.dumps(without_observation) and 'Current request' in json.dumps(without_observation)


def test_observed_retelling_keeps_new_input_but_not_its_stale_packet_or_history(runtime):
    rt = runtime
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['fixture-source'])
    def get(path, **kwargs):
        return httpx.Response(200, json=rt.ledger.erasure_feed('owner', kwargs['params']['after']),
                              request=httpx.Request('GET', 'http://fixture' + path))
    middleware = rt.module.RequestMemory(SimpleNamespace(get=get), rt.outbox)
    scope = SimpleNamespace(contact_id='owner', valid_participant=True, task_id='task', turn_id='turn')
    earlier = {'role': 'user', 'content': rt.fact}
    current = {'role': 'user', 'content': rt.fact}
    middleware.observe(scope, [earlier, current], user_message=rt.fact)
    # Native composition stamps this same descriptor after the pre-turn hook.
    current['api_content'] = rt.fact + '\n\n' + packet('owner', 0, 'stale-evidence')
    request = {'messages': [earlier, {'role': 'user', 'content': current['api_content']}]}
    result = middleware(request, scope)['request']['messages']
    assert rt.fact not in result[0]['content']
    assert result[1]['content'] == rt.fact and 'stale-evidence' not in json.dumps(result)
    # A literal fence written by the person belongs to the direct input, not
    # to the native appended packet, even if it resembles old recall markup.
    literal = 'Explain this literal format: <memory-context>user example</memory-context>'
    current = {'role': 'user', 'content': literal}
    middleware.observe(scope, [current], user_message=literal)
    current['api_content'] = literal + '\n\n' + packet('owner', 0, 'stale-evidence')
    result = middleware({'messages': [{'role': 'user', 'content': current['api_content']}]}, scope)
    assert result['request']['messages'][0]['content'] == literal
    # A last historical user row is insufficient without the actual matching
    # direct input carried by this authenticated native turn observation.
    middleware.observe(scope, [earlier], user_message='A different current input')
    result = middleware({'messages': [earlier]}, scope)
    assert rt.fact not in json.dumps(result)


@pytest.mark.asyncio
async def test_context_stamps_before_a_concurrent_forget(source_app, tmp_path, monkeypatch):
    from colony_sidecar.api.routers import host
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('stamp-source', contact_id='contact-a', session_id='original',
                         messages=[{'role': 'user', 'content': 'Neutral source'}], derive_claims=False)
    original = host._build_temporal_section
    async def racing(*args, **kwargs):
        ledger.erase_sources(contact_id='contact-a', turn_ids=['stamp-source'])
        return await original(*args, **kwargs)
    monkeypatch.setattr(host, '_build_temporal_section', racing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'resume'},
            'incoming_message': {'role': 'user', 'content': 'Neutral'}})
    assert response.status_code == 200
    assert response.json()['source_erasure_watermark'] == 0
    assert ledger.erasure_watermark('contact-a') == 1

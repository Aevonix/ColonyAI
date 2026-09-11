"""Authentic image tool parts retain scope and revision at actual dispatch."""
import base64
import copy
import hashlib
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from apsimo.turns.source_read import read
from test_native_request_erasure import runtime, freshness_response
from test_source_media import image_bytes, message


@pytest.fixture
def image_runtime(runtime):
    rt = runtime
    rt.ledger.record_source('image', contact_id='owner', session_id='original',
                            messages=[message()], derive_claims=False)
    rt.ref = rt.ledger.source_references(['image'], contact_id='owner', session_id='later')[0]
    rt.asset = hashlib.sha256(image_bytes()).hexdigest()
    rt.calls = []
    rt.freshness = []
    def get(path, **kw):
        return httpx.Response(200, json=rt.ledger.erasure_feed('owner', kw['params']['after']),
                              request=httpx.Request('GET', 'http://fixture'+path))
    def post(path, **kw):
        if path.endswith('/sources/erasures'):
            rt.freshness.append(kw['json'])
            return freshness_response(rt.ledger, path, kw['json'])
        body = kw['json']; rt.calls.append(body)
        try:
            result = read(rt.ledger, contact_id=body['person_id'], session_id=body['session_id'],
                source_id=body['source_id'], source_version=body['source_version'],
                view=body['source_view'], asset_hash=body.get('asset_hash'),
                read_revision=body.get('read_revision'), offset=body.get('offset', 0), claim_id=body.get('claim_id'))
            return httpx.Response(200, json={'source': result}, request=httpx.Request('POST', 'http://fixture'+path))
        except ValueError:
            return httpx.Response(409, json={'error': 'changed'}, request=httpx.Request('POST', 'http://fixture'+path))
    rt.client = SimpleNamespace(get=get, post=post)
    rt.middleware = rt.module.RequestMemory(rt.client, rt.outbox)
    rt.scope = SimpleNamespace(contact_id='owner', session_id='later', task_id='task', turn_id='turn',
                               valid_participant=True, authority_lane='guest')
    current = {'role': 'user', 'content': 'Inspect the original reference image.'}
    rt.current = current
    rt.middleware.observe(rt.scope, [current], user_message=current['content'])
    stamp = json.dumps({'contact_id': 'owner', 'watermark': 0, 'sources': [rt.ref]})
    current['api_content'] = current['content'] + '\n\n<memory-context>\n[colony-recall-v1 ' + stamp + ']\n' + rt.asset + '\n[/colony-recall-v1]\n</memory-context>'
    rt.wire = {'role': 'user', 'content': current['api_content']}
    rt.middleware({'messages': [rt.wire]}, rt.scope)
    helper = importlib.import_module(rt.module.__package__ + '.source_read')
    rt.result = helper.handle({**rt.ref, 'view': 'image', 'asset_hash': rt.asset}, rt.scope,
                             rt.client, rt.middleware, {'tool_call_id': 'actual-image'})
    assert rt.result['_multimodal'] is True, rt.result
    return rt


def request(rt, shape):
    text, image = copy.deepcopy(rt.result['content'])
    if shape == 'responses':
        return {'input': [rt.wire, {'type': 'function_call_output', 'call_id': 'actual-image',
            'output': [{'type': 'input_text', 'text': text['text']},
                       {'type': 'input_image', 'image_url': image['image_url']['url']}]}]}
    if shape == 'anthropic':
        return {'messages': [rt.wire, {'role': 'assistant', 'content': [{'type': 'tool_use',
            'id': 'actual-image', 'name': 'colony_memory_read_source', 'input': {
                **rt.ref, 'view': 'image', 'asset_hash': rt.asset}}]},
            {'role': 'user', 'content': [{'type': 'tool_result',
            'tool_use_id': 'actual-image', 'content': [text, {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/png',
                'data': image['image_url']['url'].split(',', 1)[1]}}]}]}]}
    return {'messages': [rt.wire, {'role': 'tool', 'tool_call_id': 'actual-image', 'content': [text, image]}]}


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('change', ['erase', 'correction', 'attribution', 'outage'])
def test_original_parts_verified_on_each_request_then_withheld_when_stale(image_runtime, shape, change):
    rt = image_runtime
    wire = request(rt, shape); before = copy.deepcopy(wire)
    encoded = base64.b64encode(image_bytes()).decode()
    assert encoded not in rt.result['content'][0]['text']
    assert encoded in json.dumps(rt.middleware(wire, rt.scope)['request'])
    assert len(rt.calls) == 2 and rt.calls[-1]['read_revision']
    if change == 'erase':
        rt.ledger.erase_sources(contact_id='owner', turn_ids=['image'])
    elif change == 'correction':
        rt.ledger.append_source_annotation(contact_id='owner', session_id='later', annotation_id='correction',
            **rt.ref, excerpt='Please retain this reference image.', correction='This is a diagram, not a photograph.',
            author_principal='operator')
    elif change == 'attribution':
        from apsimo.turns.source_attribution import correct
        correct(rt.ledger, operation_id='identity-correction', performed_by='operator', old_contact_id='owner',
                contact_id='actual-person', source_ids=['image'], evidence_refs=['owner-confirmation'])
    else:
        rt.client.post = lambda *a, **kw: (_ for _ in ()).throw(OSError('offline'))
    checked = rt.middleware(wire, rt.scope)['request']
    assert encoded not in json.dumps(checked) and 'withheld' in json.dumps(checked)
    if shape == 'anthropic':
        assert checked['messages'][-2] == before['messages'][-2]  # Keep the native tool-use pair.
    assert wire == before


@pytest.mark.parametrize('tamper', ['call', 'pixels', 'extra_part', 'historical'])
def test_no_authentic_receipt_means_no_image_replay(image_runtime, tamper):
    rt = image_runtime; wire = request(rt, 'chat'); row = wire['messages'][-1]
    if tamper == 'call': row['tool_call_id'] = 'different-call'
    elif tamper == 'pixels': row['content'][1]['image_url']['url'] += 'AA=='
    elif tamper == 'extra_part': row['content'].append(copy.deepcopy(row['content'][1]))
    else: rt.middleware.finish(task_id='task', turn_id='turn', contact_id='owner')
    checked = rt.middleware(wire, rt.scope)['request']
    assert 'withheld' in json.dumps(checked) and 'data:image/' not in json.dumps(checked)


def test_nonvision_summary_is_explicit_failure_without_image_or_source_prose(image_runtime):
    result = image_runtime.result
    summary = json.loads(result['text_summary'])
    assert summary['complete'] is False and summary['image_bytes_included'] is False
    assert 'No visual inspection occurred' in summary['error']
    assert 'data:image/' not in result['text_summary'] and image_runtime.asset not in result['text_summary']


def test_nested_anthropic_receipt_nominates_current_source_without_recall_packet(image_runtime):
    rt = image_runtime
    # Native request composition can drop a recall block while an opened tool
    # result remains. Its authentic nested receipt must still nominate parents.
    rt.current['api_content'] = rt.current['content']
    rt.wire = {'role': 'user', 'content': rt.current['content']}
    checked = rt.middleware(request(rt, 'anthropic'), rt.scope)['request']
    assert 'data:image/' not in json.dumps(checked)  # Anthropic uses typed base64, not a URL.
    assert base64.b64encode(image_bytes()).decode() in json.dumps(checked)
    assert rt.freshness[-1]['source_refs'] == [rt.ref]

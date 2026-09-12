"""Controlled backend contract: native frames stay bound to their original clip.

Decoder behavior is qualified by the backend tests, not these response fixtures.
"""
import base64
import copy
import hashlib
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from test_native_request_erasure import runtime, freshness_response
from test_source_media import image_bytes


@pytest.fixture
def video_runtime(runtime):
    rt = runtime
    rt.ledger.record_source('clip', contact_id='owner', session_id='original',
        messages=[{'role': 'user', 'content': 'Neutral selected clip fixture.'}], derive_claims=False)
    rt.ref = rt.ledger.source_references(['clip'], contact_id='owner', session_id='later')[0]
    rt.asset = hashlib.sha256(b'controlled original MP4 identity, no decoder in this fixture').hexdigest()
    pixels = image_bytes(); rt.encoded = base64.b64encode(pixels).decode()
    rt.frame = hashlib.sha256(pixels).hexdigest()
    rt.selector = {'asset_hash': rt.asset, 'mime_type': 'video/mp4', 'requested_ms': 1000}
    rt.opened = {**rt.ref, 'view': 'video', 'read_revision': 'a' * 64,
        'content': 'Attributed selected clip at requested 1000 ms. One frame only.',
        'source_refs': [rt.ref], 'watermark': 0, 'complete': True,
        'video': {**rt.selector, 'actual_ms': 1020.0, 'frame_pts': 151, 'time_base': '1/50',
            'origin_pts': 100, 'origin_time_base': '1/50',
            'stream_index': 0, 'decoder': 'PyAV', 'decoder_version': '18.1.0',
            'selection': 'first_frame_at_or_after', 'timestamp_origin': 'first_decoded_frame',
            'source_width': 20, 'source_height': 10, 'width': 20, 'height': 10,
            'transform': 'rgb24_png_no_resize', 'audio_processed': False},
        'image': {'asset_hash': rt.frame, 'mime_type': 'image/png',
                  'data_url': 'data:image/png;base64,' + rt.encoded}, 'image_bytes_included': True}
    rt.calls = []; rt.changed = None; rt.decode_calls = 0
    def get(path, **kw):
        return httpx.Response(200, json=rt.ledger.erasure_feed('owner', kw['params']['after']),
                              request=httpx.Request('GET', 'http://fixture'+path))
    def post(path, **kw):
        if path.endswith('/sources/erasures'):
            return freshness_response(rt.ledger, path, kw['json'])
        body = kw['json']; rt.calls.append(copy.deepcopy(kw))
        assert body['source_view'] == 'video' and body['asset_hash'] == rt.asset
        assert body['requested_ms'] == 1000
        result = copy.deepcopy(rt.opened)
        if body.get('read_revision'):
            result['video'] = dict(rt.selector)
            result['image_bytes_included'] = False
            result.pop('image')
            if rt.changed == 'correction': result['read_revision'] = 'b' * 64
            elif rt.changed == 'selector': result['video']['requested_ms'] = 1001
            elif rt.changed == 'content': result['content'] = 'Different correction content'
        else:
            rt.decode_calls += 1
        return httpx.Response(200, json={'source': result}, request=httpx.Request('POST', 'http://fixture'+path))
    rt.client = SimpleNamespace(get=get, post=post)
    rt.middleware = rt.module.RequestMemory(rt.client, rt.outbox)
    rt.scope = SimpleNamespace(contact_id='owner', session_id='later', task_id='task', turn_id='turn',
                               valid_participant=True, authority_lane='guest')
    current = {'role': 'user', 'content': 'Inspect the selected clip at one second.'}
    rt.middleware.observe(rt.scope, [current], user_message=current['content'])
    stamp = json.dumps({'contact_id': 'owner', 'watermark': 0, 'sources': [rt.ref]})
    current['api_content'] = current['content'] + '\n\n<memory-context>\n[pacomind-recall-v1 ' + stamp + ']\n' + rt.asset + '\n[/pacomind-recall-v1]\n</memory-context>'
    rt.wire = {'role': 'user', 'content': current['api_content']}
    rt.middleware({'messages': [rt.wire]}, rt.scope)
    rt.helper = importlib.import_module(rt.module.__package__ + '.source_read')
    rt.args = {**rt.ref, 'view': 'video', 'asset_hash': rt.asset, 'requested_ms': 1000}
    rt.result = rt.helper.handle(rt.args, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'actual-frame'})
    assert rt.result['_multimodal'] is True, rt.result
    return rt


def request(rt, shape):
    text, image = copy.deepcopy(rt.result['content'])
    if shape == 'responses':
        return {'input': [rt.wire, {'type': 'function_call_output', 'call_id': 'actual-frame',
            'output': [{'type': 'input_text', 'text': text['text']},
                       {'type': 'input_image', 'image_url': image['image_url']['url']}]}]}
    if shape == 'anthropic':
        return {'messages': [rt.wire, {'role': 'assistant', 'content': [{'type': 'tool_use',
            'id': 'actual-frame', 'name': 'pacomind_memory_read_source', 'input': rt.args}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'actual-frame',
                'content': [text, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                    'data': rt.encoded}}]}]}]}
    return {'messages': [rt.wire, {'role': 'tool', 'tool_call_id': 'actual-frame', 'content': [text, image]}]}


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('change', ['erasure', 'correction', 'selector'])
def test_exact_frame_and_clip_are_distinct_and_metadata_recheck_never_decodes(video_runtime, shape, change):
    rt = video_runtime; wire = request(rt, shape); before = copy.deepcopy(wire)
    metadata = json.loads(rt.result['content'][0]['text'])
    assert metadata['video']['asset_hash'] == rt.asset != metadata['image']['asset_hash'] == rt.frame
    assert metadata['video']['actual_ms'] == 1020 and metadata['video']['origin_pts'] == 100
    assert metadata['video']['frame_pts'] == 151 and rt.encoded not in rt.result['content'][0]['text']
    assert rt.calls[0]['timeout'] == 20
    assert rt.encoded in json.dumps(rt.middleware(wire, rt.scope)['request'])
    assert rt.decode_calls == 1 and len(rt.calls) == 2
    assert rt.calls[-1]['json']['read_revision'] == rt.opened['read_revision']
    assert rt.calls[-1]['json']['asset_hash'] == rt.asset and rt.calls[-1]['timeout'] <= .25
    if change == 'erasure': rt.ledger.erase_sources(contact_id='owner', turn_ids=['clip'])
    else: rt.changed = change
    checked = rt.middleware(wire, rt.scope)['request']
    assert rt.encoded not in json.dumps(checked) and 'withheld' in json.dumps(checked)
    if shape == 'anthropic': assert checked['messages'][-2] == before['messages'][-2]
    assert rt.decode_calls == 1 and wire == before


@pytest.mark.parametrize('invalid', [{'requested_ms': True}, {'requested_ms': -1}, {'requested_ms': 30001},
    {'offset': 0}, {'page': 1}, {'read_revision': 'a' * 64}])
def test_video_selector_rejects_non_temporal_and_history_inputs(video_runtime, invalid):
    rt = video_runtime; count = len(rt.calls)
    result = rt.helper.handle({**rt.args, **invalid}, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'bad'})
    assert 'error' in json.loads(result) and len(rt.calls) == count


@pytest.mark.parametrize('tamper', ['frame_hash', 'frame_bytes', 'earlier_timestamp'])
def test_decoded_frame_requires_its_own_hash_and_actual_selected_time(video_runtime, tamper):
    rt = video_runtime
    if tamper == 'frame_hash': rt.opened['image']['asset_hash'] = rt.asset
    elif tamper == 'frame_bytes': rt.opened['image']['data_url'] = 'data:image/png;base64,' + base64.b64encode(b'other').decode()
    else: rt.opened['video']['actual_ms'] = 999
    result = rt.helper.handle(rt.args, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'bad'})
    assert 'error' in json.loads(result)


def test_video_metadata_content_and_authentic_pixel_pair_both_remain_required(video_runtime):
    rt = video_runtime; wire = request(rt, 'chat')
    wire['messages'][-1]['content'][1]['image_url']['url'] += 'AA=='
    assert rt.encoded not in json.dumps(rt.middleware(wire, rt.scope)['request'])
    rt.changed = 'content'
    assert rt.encoded not in json.dumps(rt.middleware(request(rt, 'chat'), rt.scope)['request'])


def test_video_open_uses_absolute_deadline_and_late_response_never_registers(video_runtime, monkeypatch):
    rt = video_runtime
    assert rt.calls[0]['_deadline_monotonic'] > 0
    ticks = iter([0, 21])
    monkeypatch.setattr(rt.helper, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    result = rt.helper.handle(rt.args, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'late-frame'})
    assert 'error' in json.loads(result)
    assert rt.calls[-1]['_deadline_monotonic'] == 20
    assert 'late-frame' not in rt.middleware._read_receipts[('owner', 'task', 'turn')]


@pytest.mark.parametrize('source_kind', ['user', 'checkpoint', 'mixed'])
def test_video_source_protocol_never_retries_generic_predecessor_route(video_runtime, source_kind):
    rt = video_runtime
    module = importlib.import_module(rt.module.__package__ + '.client')
    client = module.PacoMindClient('http://fixture'); calls = []
    def reject(path, **kwargs):
        calls.append(path)
        return httpx.Response(409, request=httpx.Request('PUT', 'http://fixture' + path))
    client.put = reject
    content = [{'type': 'input_video', 'input_video': {'mime_type': 'video/mp4', 'data': 'bmV1dHJhbA=='}}]
    if source_kind == 'mixed':
        content += [{'type': 'input_audio', 'input_audio': {'format': 'wav', 'data': 'bmV1dHJhbA=='}},
                    {'type': 'input_document', 'input_document': {'mime_type': 'application/pdf', 'data': 'bmV1dHJhbA=='}}]
    extra = {'user_message': content} if source_kind != 'checkpoint' else {
        'checkpoint_messages': [{'role': 'user', 'content': content}]}
    try:
        client.sync_turn(session_id='later', contact_id='owner', turn_id='clip:1', **extra)
    except Exception:
        pass
    assert calls == ['/v2/host/turns/source-media/video/clip%3A1']


@pytest.mark.parametrize('checkpoint', [False, True])
def test_video_without_stable_turn_id_is_not_sent_to_legacy_sync(video_runtime, checkpoint):
    rt = video_runtime
    module = importlib.import_module(rt.module.__package__ + '.client')
    client = module.PacoMindClient('http://fixture'); calls = []
    def legacy_acceptance(path, **kwargs):
        calls.append(path)
        return httpx.Response(200, json={'accepted': True}, request=httpx.Request('POST', 'http://fixture'+path))
    client.post = client.put = client.get = legacy_acceptance
    content = [{'type': 'input_video', 'input_video': {'mime_type': 'video/mp4', 'data': 'bmV1dHJhbA=='}}]
    extra = {'checkpoint_messages': [{'role': 'user', 'content': content}]} if checkpoint else {'user_message': content}
    assert client.sync_turn(session_id='later', contact_id='owner', **extra) is False
    assert calls == []
    assert client.sync_turn(session_id='later', contact_id='owner', user_message='Legacy text still works') is True
    assert calls == ['/v1/host/turns/sync']

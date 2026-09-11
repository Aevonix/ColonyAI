"""Actual bounded MP4 decoding, source ownership and original frame reopening."""
import asyncio
import base64
from contextlib import closing
from fractions import Fraction
import hashlib
import io
import json
from types import SimpleNamespace

import av  # Required by the dev extra: do not skip the decoder in CI.
import httpx
from PIL import Image
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia
from colony_sidecar.turns.source_read import read, read_video
from colony_sidecar.turns.video import decode_video, MAX_VIDEO_BYTES
from test_turn_source_evidence import source_app


def clip_bytes(times=(0, 250, 1000, 1500), *, size=(160, 120)):
    output = io.BytesIO()
    with av.open(output, 'w', format='mp4') as container:
        stream = container.add_stream('mpeg4', rate=4)
        stream.width, stream.height = size; stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, 1000)
        for index, time in enumerate(times):
            frame = av.VideoFrame.from_image(Image.new('RGB', size, ('red', 'green', 'blue', 'yellow')[index % 4]))
            frame.pts = time; frame.time_base = Fraction(1, 1000)
            for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    return output.getvalue()


def message(data=None):
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Retain this selected neutral clip. Capture wall time is unknown.'},
        {'type': 'input_video', 'input_video': {'mime_type': 'video/mp4',
            'data': base64.b64encode(clip_bytes() if data is None else data).decode()}}]}


def retained(tmp_path, data=None):
    data = clip_bytes() if data is None else data
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('clip', contact_id='owner', session_id='earlier', messages=[message(data)], derive_claims=False)
    ref = ledger.source_references(['clip'], contact_id='owner', session_id='later')[0]
    selector = dict(contact_id='owner', session_id='later', **ref,
                    asset_hash=hashlib.sha256(data).hexdigest(), requested_ms=800)
    return ledger, selector, data


def row(ledger):
    with closing(ledger._connect()) as db:
        result = dict(db.execute('SELECT * FROM source_media').fetchone())
        result['metadata'] = json.loads(result['media_metadata_json'])
        return result


@pytest.mark.asyncio
async def test_actual_variable_time_frame_and_decode_free_metadata(tmp_path, monkeypatch):
    ledger, selector, data = retained(tmp_path)
    opened = await read_video(ledger, **selector)
    assert opened['video']['asset_hash'] == hashlib.sha256(data).hexdigest()
    assert opened['video']['requested_ms'] == 800 and opened['video']['actual_ms'] == 1000
    assert opened['video']['frame_pts'] * Fraction(opened['video']['time_base']) == 1
    assert opened['video']['decoder_version'] == '18.1.0' and opened['video']['audio_processed'] is False
    pixels = base64.b64decode(opened['image']['data_url'].split(',', 1)[1])
    assert hashlib.sha256(pixels).hexdigest() == opened['image']['asset_hash'] != selector['asset_hash']
    r, g, b = Image.open(io.BytesIO(pixels)).getpixel((80, 60)); assert b > 240 and r < 15 and g < 15
    import colony_sidecar.turns.video as video
    async def forbidden(*a, **kw): raise AssertionError('Metadata verification must not decode')
    monkeypatch.setattr(video, 'decode_video', forbidden)
    checked = await read_video(ledger, **selector, read_revision=opened['read_revision'])
    assert checked['content'] == opened['content'] and checked['source_refs'] == opened['source_refs']
    assert checked['image_bytes_included'] is False and 'image' not in checked
    assert checked['video'] == {'asset_hash': selector['asset_hash'], 'mime_type': 'video/mp4', 'requested_ms': 800}
    assert row(ledger)['status'] == 'video_pending'  # Explicit pixels do not need a caption.


@pytest.mark.asyncio
async def test_original_http_admission_retry_and_frame_read(source_app, tmp_path):
    from colony_sidecar.turns import get_turn_idempotency_ledger
    from colony_sidecar import get_state_dir
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    from test_scoped_api_authority import _principal, _write_keyring
    keyring = tmp_path/'video-keyring.json'
    _write_keyring(keyring, [_principal(principal='video-reader', secret='fixture-video', viewer='owner',
                                      scopes=['memory:read', 'turns:write'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'owner', 'session_id': 'earlier', 'turn_id': 'clip'},
            'user_message': message(), 'source_only': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture',
                                 headers={'Authorization': 'Bearer fixture-video'}) as client:
        for expected in (201, 200):
            response = await client.put('/v2/host/turns/source-media/video/clip', json=body)
            assert response.status_code == expected, response.text
        ledger = get_turn_idempotency_ledger(get_state_dir())
        ref = ledger.source_references(['clip'], contact_id='owner', session_id='later')[0]
        request = {'identity': {'host_id': 'fixture'}, 'person_id': 'owner', 'session_id': 'later',
                   **ref, 'source_view': 'video', 'asset_hash': hashlib.sha256(clip_bytes()).hexdigest(), 'requested_ms': 800}
        response = await client.post('/v1/host/memory/read', json=request)
        assert response.status_code == 200, response.text
        opened = response.json()['source']; assert opened['image_bytes_included']
        checked = await client.post('/v1/host/memory/read', json=request | {'read_revision': opened['read_revision']})
        assert checked.status_code == 200 and not checked.json()['source']['image_bytes_included']
        for change in ({'requested_ms': True}, {'requested_ms': -1}, {'requested_ms': 30001}, {'page': 1}, {'offset': 1}):
            assert (await client.post('/v1/host/memory/read', json=request | change)).status_code == 422


@pytest.mark.asyncio
async def test_natural_worker_samples_are_searchable_attributed_and_bounded(tmp_path):
    ledger, selector, _ = retained(tmp_path); calls = []
    class Router:
        supports_function_routing = True
        async def complete(self, **kwargs):
            calls.append(kwargs)
            assert kwargs['context']['function_role'] == 'vision'
            assert len([p for p in kwargs['messages'][1]['content'] if p['type'] == 'image_url']) == 3
            return SimpleNamespace(content='The samples show red, blue and yellow.', model_id='neutral/vision',
                                   function_role='vision', config_revision='test-role', model_revision='test-weight')
    media = SourceMedia(ledger); assert await media.process_one(Router())
    result = row(ledger); assert result['status'] == 'complete', result
    assert result['model'] == 'neutral/vision' and len(calls) == 1
    assert [f['actual_ms'] for f in result['metadata']['video']['frames']] == [0, 1000, 1500]
    assert 'data_url' not in result['media_metadata_json']
    hits = media.search('blue yellow', contact_id='owner', session_id='later')
    assert len(hits) == 1 and hits[0]['epistemic_state'] == 'derived_unverified'
    assert 'intervening activity and audio unobserved' in hits[0]['content']
    assert not media.search('blue yellow', contact_id='other', session_id='later')


@pytest.mark.asyncio
async def test_correction_changes_read_revision_and_erasure_mid_decode_withholds_pixels(tmp_path, monkeypatch):
    ledger, selector, _ = retained(tmp_path); opened = await read_video(ledger, **selector)
    ledger.append_source_annotation(contact_id='owner', session_id='later', annotation_id='correction',
        source_id=selector['source_id'], source_version=selector['source_version'],
        excerpt='Retain this selected neutral clip.', correction='This is synthetic evidence, not a camera event.', author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        await read_video(ledger, **selector, read_revision=opened['read_revision'])
    revised = await read_video(ledger, **selector); assert 'synthetic evidence' in revised['content']
    import colony_sidecar.turns.video as video
    original = video.decode_video
    async def erase_during(data, requested_ms=None):
        result = await original(data, requested_ms)
        ledger.erase_sources(contact_id='owner', turn_ids=['clip'])
        return result
    monkeypatch.setattr(video, 'decode_video', erase_during)
    with pytest.raises(ValueError, match='unavailable'):
        await read_video(ledger, **selector)
    assert not SourceMedia(ledger).store._original_path(selector['asset_hash'], 'video/mp4').exists()


@pytest.mark.asyncio
async def test_shared_original_backup_restore_and_late_worker_result(tmp_path):
    from colony_sidecar import backup
    ledger, selector, data = retained(tmp_path/'state')
    ledger.record_source('other', contact_id='other', session_id='s', messages=[message(data)], derive_claims=False)
    archive = backup.create_full_backup(tmp_path/'state', tmp_path/'archives', include_graph=False, include_vectors=False)
    destination = tmp_path/'restore'; assert backup.restore_full_backup(archive, destination)['source_images'] == 1
    restored = TurnIdempotencyLedger(destination/'turn-idempotency.db'); media = SourceMedia(restored)
    assert media.read(selector['asset_hash'], contact_id='owner', session_id='later')[0] == data
    job = media.claim_job(include_videos=True)
    restored.erase_sources(contact_id='owner', turn_ids=['clip'])
    with pytest.raises(ValueError): await read_video(restored, **selector)
    assert media.read(selector['asset_hash'], contact_id='other', session_id='later')[0] == data
    restored.erase_sources(contact_id='other', turn_ids=['other'])
    assert not media.finish_video(job, await decode_video(data))
    assert not media.store._original_path(selector['asset_hash'], 'video/mp4').exists()


@pytest.mark.parametrize('variant', ['url', 'path', 'encoding', 'mime', 'oversized'])
def test_no_remote_references_or_invalid_envelopes(tmp_path, variant):
    msg = message(); item = msg['content'][1]['input_video']
    if variant in {'url', 'path'}: item.clear(); item[variant] = 'https://private.invalid/?secret'
    if variant == 'encoding': item['data'] = 'private-secret'
    if variant == 'mime': item['mime_type'] = 'audio/wav'
    if variant == 'oversized': item['data'] = base64.b64encode(b'\0' * (MAX_VIDEO_BYTES + 1)).decode()
    ledger = TurnIdempotencyLedger(tmp_path/'s.db')
    ledger.record_source('invalid', contact_id='owner', session_id='s', messages=[msg], derive_claims=False)
    with closing(ledger._connect()) as db:
        assert not db.execute('SELECT * FROM source_media').fetchall()
        content = db.execute('SELECT messages_json FROM turn_sources').fetchone()[0]
        assert 'video_unretained' in content and 'private-secret' not in content and 'private.invalid' not in content


@pytest.mark.asyncio
async def test_malformed_duration_and_missing_target_are_explicit(tmp_path):
    invalid = b'\0\0\0\x20ftypisom' + b'\0' * 40
    ledger, _, _ = retained(tmp_path, invalid); media = SourceMedia(ledger)
    assert await media.process_one(None)
    assert row(ledger)['status'] == 'video_failed'
    assert not await media.process_one(None)  # Immutable decoder failure is not an endless caption retry.
    assert (await decode_video(clip_bytes((0, 31000))))['status'] == 'unsupported'
    assert (await decode_video(clip_bytes(), 2000))['reason'] == 'video_frame_at_time_unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_exact_decoder_child_reaped_on_timeout_or_cancellation(monkeypatch, cancel):
    import colony_sidecar.turns.video as video
    actual_spawn = asyncio.create_subprocess_exec; children = []
    async def held(*args, **kwargs):
        process = await actual_spawn(args[0], '-c', 'import time; time.sleep(20)', **kwargs)
        children.append(process); return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', held)
    monkeypatch.setattr(video, 'MAX_DECODE_SECONDS', .05)
    operation = asyncio.create_task(video.decode_video(clip_bytes()))
    if cancel:
        while not children: await asyncio.sleep(.001)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError): await operation
    else:
        assert (await operation)['reason'] == 'video_decoder_time_limit'
    assert len(children) == 1 and children[0].returncode is not None


@pytest.mark.asyncio
async def test_missing_optional_decoder_stops_worker_without_model_or_retry(tmp_path, monkeypatch):
    spawn = asyncio.create_subprocess_exec
    async def without_site(*args, **kwargs):
        return await spawn(args[0], '-S', *args[1:], **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', without_site)
    ledger, _, _ = retained(tmp_path); media = SourceMedia(ledger)
    assert await media.process_one(None)
    result = row(ledger)
    assert result['status'] == 'video_unsupported' and result['error'] == 'video_decoder_unavailable'
    assert not await media.process_one(None)


@pytest.mark.asyncio
async def test_erasure_during_worker_decode_does_not_start_caption(tmp_path, monkeypatch):
    ledger, _, _ = retained(tmp_path)
    import colony_sidecar.turns.video as video
    actual = video.decode_video
    async def erase(data, requested_ms=None):
        result = await actual(data, requested_ms)
        ledger.erase_sources(contact_id='owner', turn_ids=['clip'])
        return result
    monkeypatch.setattr(video, 'decode_video', erase)
    class NoModel:
        supports_function_routing = True
        async def complete(self, **kwargs): pytest.fail('Erased decoded frames reached inference')
    assert await SourceMedia(ledger).process_one(NoModel())
    with closing(ledger._connect()) as db:
        assert not db.execute('SELECT * FROM source_media').fetchall()


@pytest.mark.asyncio
async def test_video_worker_rejection_retains_specific_reason_and_known_model(tmp_path):
    ledger, _, _ = retained(tmp_path)
    class Router:
        supports_function_routing = True
        async def complete(self, **kwargs):
            return SimpleNamespace(content='repeated ' * 161, model_id='neutral/returned',
                                   function_role='vision', config_revision='r', model_revision='w')
    media = SourceMedia(ledger); assert await media.process_one(Router())
    result = row(ledger)
    assert result['status'] == 'video_pending' and result['error'] == 'description_word_limit'
    assert result['model'] == 'neutral/returned' and result['description'] is None
    assert 'repeated' not in result['media_metadata_json']
    assert not await media.process_one(Router())  # Existing backoff respected.


@pytest.mark.asyncio
async def test_video_jobs_share_fifo_and_are_invisible_to_old_image_document_workers(tmp_path):
    from test_source_documents import message as pdf_message
    ledger, _, _ = retained(tmp_path)
    ledger.record_source('pdf', contact_id='owner', session_id='s', messages=[pdf_message()], derive_claims=False)
    media = SourceMedia(ledger)
    assert media.claim_job() is None
    assert media.claim_job(include_documents=True)['mime_type'] == 'application/pdf'
    assert media.claim_job(include_documents=True, include_videos=True)['mime_type'] == 'video/mp4'


@pytest.mark.asyncio
async def test_current_video_cannot_be_borrowed_from_another_person_or_source(tmp_path):
    ledger, selector, _ = retained(tmp_path)
    ledger.record_source('unrelated', contact_id='owner', session_id='s',
                         messages=[{'role': 'user', 'content': 'An unrelated note.'}], derive_claims=False)
    ref = ledger.source_references(['unrelated'], contact_id='owner', session_id='later')[0]
    for change in ({'contact_id': 'stranger'}, {'asset_hash': '0' * 64}, ref):
        with pytest.raises((ValueError, KeyError)):
            await read_video(ledger, **(selector | change))
    from colony_sidecar.turns.source_attribution import correct
    opened = await read_video(ledger, **selector)
    correct(ledger, operation_id='fix', performed_by='operator', old_contact_id='owner', contact_id='other',
            source_ids=['clip'], evidence_refs=['verified-correction'])
    with pytest.raises(ValueError, match='unavailable'):
        await read_video(ledger, **selector, read_revision=opened['read_revision'])

"""Exact audio originals, fallible timed transcripts and canonical erasure."""
import base64
import copy
import hashlib
import io
import json
import sqlite3
import wave

import httpx
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.audio import decode_audio, source_text
from colony_sidecar.turns.idempotency import SourceErased, source_message_hash
from colony_sidecar.turns.media import SourceMedia
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from test_turn_source_evidence import source_app, recalled, envelope
from test_hermes_turn_outbox import _load_client


def wav_bytes(rate=16000, frames=1600):
    data = io.BytesIO()
    with wave.open(data, 'wb') as clip:
        clip.setnchannels(1); clip.setsampwidth(2); clip.setframerate(rate); clip.writeframes(b'\x01\x00' * frames)
    return data.getvalue()


def message(data=None):
    data = data or wav_bytes()
    return {'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'format': 'wav', 'data': base64.b64encode(data).decode()}},
        {'type': 'audio_transcript', 'audio_sha256': hashlib.sha256(data).hexdigest(),
         'segments': [{'start_ms': 0, 'end_ms': 100, 'text': 'The fixture lamp is violet.'}],
         'recognizer': {'model_id': 'fixture-asr', 'model_revision': 'unknown'},
         'received_at': '2026-09-09T12:00:00+00:00', 'captured_at': None}]}


def retained(ledger, turn='audio'):
    with sqlite3.connect(ledger.db_path) as db:
        return json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (turn,)).fetchone()[0])[0]


@pytest.mark.asyncio
async def test_audio_http_recall_and_full_source_preserve_derived_clock_lineage(source_app, tmp_path, monkeypatch):
    from colony_sidecar.turns.source_read import read
    from colony_sidecar.turns.source_vectors import chunks, hydrate
    from contextlib import closing
    data = wav_bytes(); original = message(data); asset = hashlib.sha256(data).hexdigest()
    body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'call', 'turn_id': 'audio'},
            'user_message': original, 'source_only': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = await client.put('/v2/host/turns/source-media/audio/audio', json=body)
        assert result.status_code == 201 and result.json()['source_recorded'], result.text
        ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
        stored = retained(ledger)
        assert source_message_hash('call', stored) == source_message_hash('call', original)
        assert original['content'][0]['input_audio']['data'] not in json.dumps(stored)
        assert stored['content'][0]['duration_ms'] == 100 and stored['content'][0]['sample_rate'] == 16000
        transcript = stored['content'][1]
        assert transcript['captured_at'] is None and transcript['received_at'] == original['content'][1]['received_at']
        assert transcript['confidence'] is None and transcript['epistemic_state'] == 'derived_unverified'
        assert SourceMedia(ledger).claim_job() is None  # Audio is never sent to the image model.
        class NoModel:
            async def complete(self, **kwargs): pytest.fail('Audio transcript became direct text assertion learning')
        assert await SourceClaimProjection(ledger).process_one(NoModel())
        asset_url = '/v1/host/memory/sources/assets/' + asset
        response = await client.get(asset_url, params={'contact_id': 'contact-a', 'session_id': 'later'})
        assert response.content == data and response.headers['content-type'] == 'audio/wav'
        assert response.headers['cache-control'] == 'no-store'
        assert (await client.get(asset_url, params={'contact_id': 'other', 'session_id': 'call'})).status_code == 404
        monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
        packet = await recalled(client, session='later', query='violet lamp')
        assert 'Unverified machine transcript' in packet and 'derived_unverified' in packet and asset in packet
        assert 'input_audio' not in packet and original['content'][0]['input_audio']['data'] not in packet
        refs = ledger.source_references(['audio'], contact_id='contact-a', session_id='later')
        opened = read(ledger, contact_id='contact-a', session_id='later', source_id='audio', source_version=refs[0]['source_version'])
        assert 'fixture-asr' in opened['content'] and 'audio_transcript' in opened['content']
        with closing(ledger._connect()) as db:
            source = dict(db.execute("SELECT * FROM turn_sources WHERE turn_id='audio'").fetchone())
            vectors = list(chunks(db, source))
        assert len(vectors) == 1
        assert hydrate(ledger, vectors[0][1], contact_id='contact-a', session_id='later')['epistemic_state'] == 'derived_unverified'
        ledger.record_source('answer', contact_id='contact-a', session_id='later', messages=[{'role': 'assistant',
            'content': 'Your extra lamp has a purple finish.', '_supplied_inputs': [{'source_id': 'audio',
            'input_message_hash': source_message_hash('call', original)}]}], derive_claims=False)
        ledger.erase_sources(contact_id='contact-a', turn_ids=['audio'])
        assert ledger.source_references(['answer'], contact_id='contact-a', session_id='later') == []
        assert not ledger.search_sources('violet', contact_id='contact-a', session_id='later')
        assert hydrate(ledger, vectors[0][1], contact_id='contact-a', session_id='later') is None
        assert (await client.get(asset_url, params={'contact_id': 'contact-a', 'session_id': 'later'})).status_code == 404
        assert not SourceMedia(ledger).store._original_path(asset, 'audio/wav').exists()
        with pytest.raises(ValueError): read(ledger, contact_id='contact-a', session_id='later', source_id='audio', source_version=refs[0]['source_version'])
        with pytest.raises(SourceErased): ledger.record_source('late', contact_id='contact-a', session_id='call', messages=[original])
        outbox = _load_client('audio_erasure').TurnOutbox(tmp_path/'outbox.db')
        outbox.enqueue('late', {'turn_id': 'late', 'contact_id': 'contact-a', 'session_id': 'call', 'user_message': original['content']})
        outbox.apply_erasure_page('contact-a', ledger.erasure_feed('contact-a'))
        assert outbox.snapshot() == []


@pytest.mark.parametrize('variant', ['mp3', 'truncated', 'too-long', 'remote', 'video', 'transcript-hash', 'segment-range'])
def test_unsupported_audio_and_invalid_transcripts_are_not_silently_claimed_retained(tmp_path, variant):
    original = message()
    if variant == 'mp3': original['content'][0]['input_audio']['format'] = 'mp3'
    if variant == 'truncated': original['content'][0]['input_audio']['data'] = base64.b64encode(wav_bytes()[:-2]).decode()
    if variant == 'too-long': original['content'][0]['input_audio']['data'] = base64.b64encode(wav_bytes(frames=16000 * 61)).decode()
    if variant == 'remote': original['content'][0] = {'type': 'audio_url', 'url': 'https://private.invalid/audio?token=secret'}
    if variant == 'video': original['content'][0] = {'type': 'input_video', 'data': 'untrusted-video-bytes'}
    if variant == 'transcript-hash': original['content'][1]['audio_sha256'] = '0'*64
    if variant == 'segment-range': original['content'][1]['segments'][0]['end_ms'] = 101
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    ledger.record_source('audio', contact_id='person', session_id='call', messages=[original])
    stored = retained(ledger)
    assert 'unretained' in json.dumps(stored)
    assert not source_text(stored['content'])
    assert 'secret' not in json.dumps(stored) and 'untrusted-video-bytes' not in json.dumps(stored)


def test_audio_original_backup_and_shared_owner_erasure(tmp_path):
    from colony_sidecar import backup
    state = tmp_path/'state'; ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')
    for person in ('a', 'b'):
        ledger.record_source(person, contact_id=person, session_id='call', messages=[message()], derive_claims=False)
    archive = backup.create_full_backup(state, tmp_path/'archives', include_graph=False, include_vectors=False)
    destination = tmp_path/'restore'
    assert backup.restore_full_backup(archive, destination)['source_images'] == 1  # Legacy counter name.
    media = SourceMedia(TurnIdempotencyLedger(destination/'turn-idempotency.db'))
    asset = hashlib.sha256(wav_bytes()).hexdigest()
    assert media.read(asset, contact_id='a', session_id='later')[0] == wav_bytes()
    media.ledger.erase_sources(contact_id='a', turn_ids=['a'])
    with pytest.raises(KeyError): media.read(asset, contact_id='a', session_id='later')
    assert media.read(asset, contact_id='b', session_id='later')[0] == wav_bytes()
    media.ledger.erase_sources(contact_id='b', turn_ids=['b'])
    assert not media.store._original_path(asset, 'audio/wav').exists()


def test_audio_memory_salvage_recovers_only_current_owned_original(tmp_path):
    from colony_sidecar import backup
    state = tmp_path/'state'; ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')
    ledger.record_source('audio', contact_id='person', session_id='call', messages=[message()], derive_claims=False)
    (state/'colony-id').write_text('fixture-audio-colony')
    archive = backup.create_full_backup(state, tmp_path/'archives', include_graph=False, include_vectors=False)
    asset = hashlib.sha256(wav_bytes()).hexdigest()
    SourceMedia(ledger).store._original_path(asset, 'audio/wav').unlink()
    destination = tmp_path/'salvage'
    result = backup.restore_source_memory(archive, destination, current_state=state)
    assert result['images_recovered_from_archive'] == 1 and result['runtime_authority_restored'] is False
    recovered = SourceMedia(TurnIdempotencyLedger(destination/'turn-idempotency.db'))
    assert recovered.read(asset, contact_id='person', session_id='later')[0] == wav_bytes()
    recovered.ledger.erase_sources(contact_id='person', turn_ids=['audio'])
    assert not recovered.store._original_path(asset, 'audio/wav').exists()


@pytest.mark.asyncio
async def test_audio_protocol_rejects_predecessor_before_raw_byte_storage_and_keeps_literal_ids(source_app):
    from fastapi import HTTPException, Response
    from colony_sidecar.api.routers.host import turns_sync_v2
    from colony_sidecar.api.schemas.host import TurnSyncRequest
    body = TurnSyncRequest.model_validate({'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'person', 'session_id': 'call', 'turn_id': 'clip'}, 'user_message': message()})
    with pytest.raises(HTTPException) as error:
        await turns_sync_v2('source-media/audio/clip', body, Response())
    assert error.value.status_code == 409 and error.value.detail['code'] == 'turn_id_mismatch'
    identifier = 'source-media/audio/old-literal-id'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        assert (await client.put('/v2/host/turns/' + identifier, json=envelope(identifier, checkpoint=True))).status_code == 201

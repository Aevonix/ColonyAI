"""Actual PDF bytes through canonical retention, bounded parsing and cleanup."""
import base64
import asyncio
from contextlib import closing
import hashlib
import io
import json
import sys
import time

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.documents import MAX_DOCUMENT_BYTES, MAX_PAGE_STREAM_BYTES, extract_document
from apsimo.turns.idempotency import SourceErased, source_message_hash
from apsimo.turns.media import SourceMedia
from test_turn_source_evidence import source_app, envelope
from test_hermes_turn_outbox import _load_client


def pdf_bytes(texts=('The first tray holds seven tiles.', 'The second tray holds nine tiles.'), *, encrypted=False, stream_bytes=None):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=200)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(stream_bytes or b'BT /F1 12 Tf 20 100 Td (' + text.encode('ascii') + b') Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream.flate_encode())
    if encrypted:
        writer.encrypt('fixture-only-password')
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


def message(data=None):
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Retain the supplied tray reference PDF.'},
        {'type': 'input_document', 'input_document': {'mime_type': 'application/pdf',
            'data': base64.b64encode(data if data is not None else pdf_bytes()).decode()}}]}


def media_row(ledger, asset):
    with closing(ledger._connect()) as db:
        row = dict(db.execute('SELECT * FROM source_media WHERE asset_hash=?', (asset,)).fetchone())
    row['document'] = json.loads(row['media_metadata_json'])['document']
    return row


@pytest.mark.asyncio
async def test_actual_pdf_http_retention_and_page_extraction_without_model(source_app, tmp_path):
    data = pdf_bytes(); original = message(data); asset = hashlib.sha256(data).hexdigest()
    body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 's', 'turn_id': 'pdf'},
            'user_message': original, 'source_only': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.put('/v2/host/turns/source-media/document/pdf', json=body)
        assert response.status_code == 201 and response.json()['source_recorded'], response.text
        assert (await client.put('/v2/host/turns/source-media/document/pdf', json=body)).status_code == 200
        ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db'); media = SourceMedia(ledger)
        with closing(ledger._connect()) as db:
            stored = json.loads(db.execute("SELECT messages_json FROM turn_sources WHERE turn_id='pdf'").fetchone()[0])[0]
            schema_before = list(db.execute('SELECT sql FROM sqlite_master ORDER BY name'))
        assert source_message_hash('s', stored) == source_message_hash('s', original)
        assert original['content'][1]['input_document']['data'] not in json.dumps(stored)
        assert stored['content'][1] == {'type': 'document', 'asset_id': 'sha256:' + asset, 'mime_type': 'application/pdf'}
        assert media.claim_job() is None  # An image worker must never claim it.
        assert media_row(ledger, asset)['status'] == 'document_pending'
        class NoModel:
            async def complete(self, **kwargs): pytest.fail('PDF extraction invoked a model')
        assert await media.process_one(NoModel())
        row = media_row(ledger, asset)
        assert row['status'] == 'complete' and row['attempts'] == 1
        assert row['document']['status'] == 'complete' and row['document']['page_count'] == 2
        assert row['document']['pages'] == [
            {'page': 1, 'text': 'The first tray holds seven tiles.', 'status': 'text'},
            {'page': 2, 'text': 'The second tray holds nine tiles.', 'status': 'text'}]
        import pypdf
        assert row['document']['parser_version'] == pypdf.__version__ and row['document']['ocr_performed'] is False
        assert row['model'] is None and row['description'] is None
        assert not await media.process_one(NoModel())
        with closing(ledger._connect()) as db:
            assert list(db.execute('SELECT sql FROM sqlite_master ORDER BY name')) == schema_before
            assert not db.execute('SELECT * FROM source_media_search').fetchall()
            assert not db.execute('SELECT * FROM source_claims').fetchall()
        response = await client.get('/v1/host/memory/sources/assets/' + asset,
                                    params={'contact_id': 'contact-a', 'session_id': 'later'})
        assert response.content == data and response.headers['content-type'] == 'application/pdf'
        assert response.headers['cache-control'] == 'no-store'
        assert media.store._original_path(asset, 'application/pdf').stat().st_mode & 0o777 == 0o600
        assert (await client.get('/v1/host/memory/sources/assets/' + asset,
                                params={'contact_id': 'other', 'session_id': 's'})).status_code == 404
        ref = ledger.source_references(['pdf'], contact_id='contact-a', session_id='later')[0]
        response = await client.post('/v1/host/memory/read', json={'identity': {'host_id': 'fixture'},
            'person_id': 'contact-a', 'session_id': 'later', **ref, 'source_view': 'document', 'asset_hash': asset, 'page': 2})
        assert response.status_code == 200, response.text
        assert 'nine tiles' in response.json()['source']['content']
        ledger.erase_sources(contact_id='contact-a', turn_ids=['pdf'])
        assert not media.store._original_path(asset, 'application/pdf').exists()
        with pytest.raises(SourceErased): ledger.record_source('late', contact_id='contact-a', session_id='s', messages=[original])


@pytest.mark.asyncio
@pytest.mark.parametrize(('variant', 'status', 'reason'), [
    ('encrypted', 'unsupported', 'encrypted_pdf'),
    ('blank', 'unsupported', 'no_extractable_text'),
    ('scan', 'unsupported', 'no_extractable_text'),
    ('mixed', 'partial', 'no_extractable_text_on_some_pages'),
    ('pages', 'unsupported', 'page_count_exceeds_limit'),
    ('stream', 'unsupported', 'page_stream_exceeds_limit'),
    ('text', 'unsupported', 'extracted_text_exceeds_limit'),
    ('malformed', 'failed', 'invalid_or_unreadable_pdf'),
])
async def test_pdf_unsupported_disposition_retains_original_without_retry(tmp_path, variant, status, reason):
    data = pdf_bytes()
    if variant == 'encrypted': data = pdf_bytes(encrypted=True)
    if variant == 'blank': data = pdf_bytes(('',))
    if variant == 'mixed': data = pdf_bytes(('One visible text layer.', ''))
    if variant == 'pages': data = pdf_bytes(('',) * 65)
    if variant == 'stream': data = pdf_bytes(('ignored',), stream_bytes=b' ' * (MAX_PAGE_STREAM_BYTES + 1))
    if variant == 'text': data = pdf_bytes(('a' * 32001,))
    if variant == 'malformed': data = b'%PDF-1.7\nmalformed fixture\n'
    if variant == 'scan':
        from PIL import Image, ImageDraw
        pixels = Image.new('RGB', (200, 100), 'white'); ImageDraw.Draw(pixels).text((10, 10), 'Raster words', fill='black')
        output = io.BytesIO(); pixels.save(output, format='PDF'); data = output.getvalue()
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); media = SourceMedia(ledger)
    ledger.record_source('pdf', contact_id='a', session_id='s', messages=[message(data)], derive_claims=False)
    assert await media.process_one(None)
    asset = hashlib.sha256(data).hexdigest(); row = media_row(ledger, asset)
    assert row['document']['status'] == status and row['document']['reason'] == reason
    assert row['document']['ocr_performed'] is False
    assert media.read(asset, contact_id='a', session_id='s') == (data, 'application/pdf')
    assert not await media.process_one(None)
    assert media_row(ledger, asset)['attempts'] == 1


@pytest.mark.parametrize('variant', ['url', 'path', 'filename', 'encoding', 'mime', 'oversized', 'file-block'])
def test_no_document_paths_urls_or_oversized_bytes_are_retained(tmp_path, variant):
    original = message(); block = original['content'][1]
    if variant in {'url', 'path', 'filename'}:
        block['input_document'] = {'mime_type': 'application/pdf', variant: '/private/credential-secret.pdf'}
    if variant == 'encoding': block['input_document']['data'] = 'not-base64-secret'
    if variant == 'mime': block['input_document']['mime_type'] = 'text/plain'
    if variant == 'oversized': block['input_document']['data'] = base64.b64encode(b'%PDF-' + b'x' * MAX_DOCUMENT_BYTES).decode()
    if variant == 'file-block': block = original['content'][1] = {'type': 'input_file', 'file_url': 'https://private.invalid/?secret'}
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    ledger.record_source('pdf', contact_id='a', session_id='s', messages=[original], derive_claims=False)
    with closing(ledger._connect()) as db:
        stored = json.loads(db.execute('SELECT messages_json FROM turn_sources').fetchone()[0])
        assert not db.execute('SELECT * FROM source_media').fetchall()
    assert stored[0]['content'][1]['type'].endswith('_unretained')
    assert 'secret' not in json.dumps(stored)


@pytest.mark.asyncio
async def test_pdf_backup_restore_shared_ownership_and_parse_erasure_race(tmp_path):
    from apsimo import backup
    data = pdf_bytes(); asset = hashlib.sha256(data).hexdigest()
    state = tmp_path/'state'; ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')
    for person in ('a', 'b'):
        ledger.record_source(person, contact_id=person, session_id='s', messages=[message(data)], derive_claims=False)
    media = SourceMedia(ledger); assert await media.process_one(None)
    archive = backup.create_full_backup(state, tmp_path/'archives', include_graph=False, include_vectors=False)
    destination = tmp_path/'restore'
    assert backup.restore_full_backup(archive, destination)['source_images'] == 1
    restored = SourceMedia(TurnIdempotencyLedger(destination/'turn-idempotency.db'))
    assert restored.read(asset, contact_id='a', session_id='later')[0] == data
    assert media_row(restored.ledger, asset)['document'] == media_row(ledger, asset)['document']
    restored.ledger.erase_sources(contact_id='a', turn_ids=['a'])
    with pytest.raises(KeyError): restored.read(asset, contact_id='a', session_id='later')
    assert restored.read(asset, contact_id='b', session_id='later')[0] == data
    restored.ledger.erase_sources(contact_id='b', turn_ids=['b'])
    assert not restored.store._original_path(asset, 'application/pdf').exists()
    race = TurnIdempotencyLedger(tmp_path/'race.db'); race_media = SourceMedia(race)
    race.record_source('r', contact_id='r', session_id='s', messages=[message(data)], derive_claims=False)
    job = race_media.claim_document_job()
    race.erase_sources(contact_id='r', turn_ids=['r'])
    assert not race_media.finish_document(job, await extract_document(data))
    with closing(race._connect()) as db:
        assert not db.execute('SELECT * FROM source_media').fetchall()


@pytest.mark.asyncio
async def test_document_protocol_and_client_never_fall_back_to_predecessor(source_app, tmp_path):
    from fastapi import HTTPException, Response
    from apsimo.api.routers.host import turns_sync_v2
    from apsimo.api.schemas.host import TurnSyncRequest
    body = TurnSyncRequest.model_validate({'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'a', 'session_id': 's', 'turn_id': 'pdf'}, 'user_message': message()})
    with pytest.raises(HTTPException) as error:
        await turns_sync_v2('source-media/document/pdf', body, Response())
    assert error.value.status_code == 409 and error.value.detail['code'] == 'turn_id_mismatch'
    identifier = 'source-media/document/old-literal-id'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        assert (await client.put('/v2/host/turns/' + identifier, json=envelope(identifier, checkpoint=True))).status_code == 201
    module = _load_client('document_route'); client = module.ColonyClient('http://fixture')
    requests = []
    def reject(path, **kwargs):
        requests.append(path)
        return httpx.Response(409, request=httpx.Request('PUT', 'http://fixture' + path))
    client.put = reject
    # Delivery failure stays queued at the existing outbox layer; no generic
    # source route should receive the raw document block after this rejection.
    try:
        client.sync_turn(session_id='s', contact_id='a', turn_id='pdf', user_message=message()['content'])
    except Exception:
        pass
    assert requests == ['/v2/host/turns/source-media/document/pdf']


@pytest.mark.asyncio
async def test_pdf_source_memory_recovery_preserves_actual_pages_without_runtime_authority(tmp_path):
    from apsimo import backup
    data = pdf_bytes(); asset = hashlib.sha256(data).hexdigest()
    state = tmp_path/'state'; ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')
    ledger.record_source('pdf', contact_id='a', session_id='s', messages=[message(data)], derive_claims=False)
    media = SourceMedia(ledger); assert await media.process_one(None)
    (state/'colony-id').write_text('fixture-pdf-colony')
    archive = backup.create_full_backup(state, tmp_path/'archives', include_graph=False, include_vectors=False)
    media.store._original_path(asset, 'application/pdf').unlink()
    destination = tmp_path/'salvage'
    result = backup.restore_source_memory(archive, destination, current_state=state)
    assert result['images_recovered_from_archive'] == 1 and result['runtime_authority_restored'] is False
    restored = SourceMedia(TurnIdempotencyLedger(destination/'turn-idempotency.db'))
    assert restored.read(asset, contact_id='a', session_id='later')[0] == data
    assert media_row(restored.ledger, asset)['document'] == media_row(ledger, asset)['document']
    restored.ledger.erase_sources(contact_id='a', turn_ids=['pdf'])
    assert not restored.store._original_path(asset, 'application/pdf').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_parser_child_is_reaped_after_timeout_or_worker_cancellation(monkeypatch, cancel):
    from apsimo.turns import documents
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event(); children = []
    async def delayed_child(*args, **kwargs):
        process = await spawn(sys.executable, '-I', '-c', 'import time; time.sleep(30)', **kwargs)
        children.append(process); started.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_child)
    monkeypatch.setattr(documents, 'MAX_PARSE_SECONDS', 0.02)
    job = asyncio.create_task(extract_document(pdf_bytes()))
    await started.wait()
    if cancel:
        job.cancel()
        with pytest.raises(asyncio.CancelledError): await job
    else:
        assert (await job)['reason'] == 'parser_time_limit'
    assert len(children) == 1 and children[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('document_first', [False, True])
async def test_shared_worker_fifo_does_not_starve_either_media_kind(tmp_path, document_first):
    from test_source_media import message as image_message, Vision
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); media = SourceMedia(ledger)
    first = message() if document_first else image_message()
    second = image_message() if document_first else message()
    for identifier, original in [('first', first), ('second', second)]:
        ledger.record_source(identifier, contact_id='a', session_id='s', messages=[original], derive_claims=False)
    vision = Vision()
    assert await media.process_one(vision)
    assert vision.calls == (0 if document_first else 1)
    # Later PDF arrivals must not overtake either earlier original.
    for number in range(3):
        data = pdf_bytes((f'Later PDF {number}.',))
        ledger.record_source('later-' + str(number), contact_id='a', session_id='s',
                             messages=[message(data)], derive_claims=False)
    assert await media.process_one(vision)
    assert vision.calls == 1
    with closing(ledger._connect()) as db:
        rows = db.execute('SELECT mime_type,status,attempts FROM source_media ORDER BY rowid').fetchall()
        assert all(row['status'] == 'complete' and row['attempts'] == 1 for row in rows[:2])
        assert all(row['status'] == 'document_pending' and row['attempts'] == 0 for row in rows[2:])


@pytest.mark.asyncio
async def test_noneligible_document_rows_and_image_retry_deadlines_do_not_block_worker(tmp_path):
    from test_source_media import message as image_message, Vision
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); media = SourceMedia(ledger)
    for number, status in enumerate(('document_running', 'document_failed', 'document_unsupported', 'orphan')):
        data = pdf_bytes((f'Ineligible PDF {number}.',)); asset = hashlib.sha256(data).hexdigest()
        ledger.record_source('pdf-' + str(number), contact_id='a', session_id='s', messages=[message(data)], derive_claims=False)
        with closing(ledger._connect()) as db, db:
            db.execute('UPDATE source_media SET status=?,lease_until=? WHERE asset_hash=?', (status, time.time() + 600, asset))
            if status == 'orphan': db.execute('DELETE FROM source_media_links WHERE asset_hash=?', (asset,))
    ledger.record_source('image', contact_id='a', session_id='s', messages=[image_message()], derive_claims=False)
    with closing(ledger._connect()) as db, db:
        db.execute("UPDATE source_media SET next_attempt=? WHERE mime_type='image/png'", (time.time() + 600,))
    assert media.claim_job(include_documents=True) is None
    with closing(ledger._connect()) as db, db:
        db.execute("UPDATE source_media SET next_attempt=0 WHERE mime_type='image/png'")
    vision = Vision(); assert await media.process_one(vision)
    assert vision.calls == 1
    with closing(ledger._connect()) as db:
        assert db.execute("SELECT attempts FROM source_media WHERE mime_type='image/png'").fetchone()[0] == 1
        assert all(row[0] == 0 for row in db.execute("SELECT attempts FROM source_media WHERE mime_type='application/pdf'"))

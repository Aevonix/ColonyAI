"""Non-text sources retain media evidence without spinning the text extractor."""
import hashlib
import json
import sqlite3

import httpx
import pytest

from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia
from test_source_media import message, image_bytes
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


@pytest.mark.asyncio
@pytest.mark.parametrize('source_only', [False, True])
async def test_image_claim_job_completes_without_inference_and_keeps_stable_metadata(source_app, tmp_path, source_only):
    data = image_bytes()
    original = message(data)
    metadata = {'type': 'camera_capture', 'image_sha256': hashlib.sha256(data).hexdigest(),
                'acquired_at': '2026-09-07T10:00:00Z', 'received_at': '2026-09-07T10:00:01Z'}
    original['content'].append(metadata)
    body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'capture',
            'channel_id': 'fixture', 'turn_id': 'image'}, 'user_message': original}
    route = '/v2/host/turns/image'
    if source_only:
        body['source_only'] = True
        route = '/v2/host/turns/source-survivors/image'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.put(route, json=body)
        assert response.status_code == 201 and response.json()['source_recorded'], response.text
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    refs = ledger.source_references(['image'], contact_id='contact-a', session_id='later')
    class NoGeneration:
        async def complete(self, **kwargs):
            pytest.fail('Image-only source reached the text extraction model')
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(NoGeneration())
    status = projection.status('contact-a')[0]
    assert status['status'] == 'complete' and not status['error'], status
    assert not await projection.process_one(NoGeneration())
    assert ledger.source_references(['image'], contact_id='contact-a', session_id='later') == refs
    with sqlite3.connect(ledger.db_path) as conn:
        retained = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='image'").fetchone()[0])
        assert metadata in retained[0]['content']
        assert conn.execute("SELECT count(*) FROM source_claims WHERE turn_id='image'").fetchone()[0] == 0
    assert SourceMedia(ledger).read(metadata['image_sha256'], contact_id='contact-a', session_id='later')[0] == data

    # The next ordinary text source still receives normal claim extraction.
    text = 'My office is in River.'
    ledger.record_source('text', contact_id='contact-a', session_id='later', messages=[{'role': 'user', 'content': text}])
    model = Model({text: claim(text, 'River')})
    assert await projection.process_one(model) and len(model.calls) == 2
    assert next(row for row in projection.status('contact-a') if row['turn_id'] == 'text')['claim_count'] == 1

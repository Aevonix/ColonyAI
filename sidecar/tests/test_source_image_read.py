"""Original pixels reopen only through their current canonical source revision."""
import base64
import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia, MAX_IMAGE_BYTES
from colony_sidecar.turns.source_read import read
from test_scoped_api_authority import _principal, _write_keyring
from test_source_media import image_bytes, message
from test_turn_source_evidence import source_app


@pytest.fixture
def original(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('image-source', contact_id='person', session_id='original',
                         messages=[message()], derive_claims=False)
    ref = ledger.source_references(['image-source'], contact_id='person', session_id='later')[0]
    return ledger, ref, hashlib.sha256(image_bytes()).hexdigest()


def opened(original, **kwargs):
    ledger, ref, asset = original
    return read(ledger, **({'contact_id': 'person', 'session_id': 'later', **ref,
                           'view': 'image', 'asset_hash': asset} | kwargs))


def test_original_bytes_and_metadata_only_revision_are_independent_of_caption(original):
    first = opened(original)
    assert base64.b64decode(first['image']['data_url'].split(',', 1)[1]) == image_bytes()
    assert first['image_bytes_included'] is True and first['complete']
    assert first['source_refs'] == [original[1]]
    assert base64.b64encode(image_bytes()).decode() not in first['content']
    checked = opened(original, read_revision=first['read_revision'])
    assert checked['image_bytes_included'] is False and 'data_url' not in checked['image']
    assert checked['source_refs'] == first['source_refs'] and checked['content'] == first['content']


def test_image_correction_changes_revision_without_erasure_and_erase_revokes_pixels(original):
    ledger, ref, asset = original
    first = opened(original)
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='correction',
        **ref, excerpt='Please retain this reference image.', correction='The picture is a sketch, not a camera observation.',
        author_principal='operator')
    assert ledger.erasure_watermark('person') == first['watermark']
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, read_revision=first['read_revision'])
    revised = opened(original)
    assert 'a sketch, not a camera observation' in revised['content']
    assert note['source_id'] in {r['source_id'] for r in revised['source_refs']}
    assert revised['image']['data_url'] == first['image']['data_url']
    ledger.erase_sources(contact_id='person', turn_ids=['image-source'])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original, read_revision=revised['read_revision'])
    assert not SourceMedia(ledger).store._original_path(asset, 'image/png').exists()


def test_exact_asset_and_source_revision_cannot_be_borrowed_from_another_owned_source(original):
    ledger, ref, asset = original
    ledger.record_source('text-source', contact_id='person', session_id='original',
                         messages=[{'role': 'user', 'content': 'An unrelated document.'}], derive_claims=False)
    text_ref = ledger.source_references(['text-source'], contact_id='person', session_id='later')[0]
    for selectors in ({'contact_id': 'stranger'}, {'source_version': '0'*64},
                      {'asset_hash': '0'*64}, text_ref):
        with pytest.raises(ValueError, match='unavailable'):
            opened(original, **selectors)
    ledger.record_source('session-image', contact_id='person', session_id='private',
                         messages=[message()], scope='session', derive_claims=False)
    private_ref = ledger.source_references(['session-image'], contact_id='person', session_id='private')[0]
    with pytest.raises(ValueError, match='unavailable'):
        opened(original, **private_ref)
    assert opened(original, **private_ref, session_id='private')['image_bytes_included']


@pytest.mark.parametrize('damage', ['changed', 'oversized', 'missing'])
def test_corrupt_or_oversized_original_never_becomes_image_output(original, damage):
    ledger, _, asset = original
    path = SourceMedia(ledger).store._original_path(asset, 'image/png')
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'x' * (MAX_IMAGE_BYTES + 1) if damage == 'oversized' else b'wrong bytes')
    with pytest.raises((ValueError, KeyError), match='unavailable|integrity|size|image'):
        opened(original)


def test_attribution_invalidation_revokes_existing_image_revision(original):
    ledger, ref, _ = original
    first = opened(original)
    from colony_sidecar.turns.source_attribution import correct
    correct(ledger, operation_id='identity-correction', performed_by='operator', old_contact_id='person',
            contact_id='actual-person', source_ids=[ref['source_id']], evidence_refs=['owner-confirmation'])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original, read_revision=first['read_revision'])


def test_oversized_corrections_do_not_silently_disappear_from_image_evidence(original):
    ledger, ref, _ = original
    for index in range(5):
        ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id=f'note-{index}',
            **ref, excerpt='Please retain this reference image.', correction=f'Qualification {index}. ' + 'Evidence. '*400,
            author_principal='operator')
    with pytest.raises(ValueError, match='corrections_exceed_read_limit'):
        opened(original)


def test_erase_racing_file_read_never_publishes_captured_pixels(original, monkeypatch):
    ledger = original[0]
    original_read = SourceMedia.read
    def racing(media, *args, **kwargs):
        result = original_read(media, *args, **kwargs)
        ledger.erase_sources(contact_id='person', turn_ids=['image-source'])
        return result
    monkeypatch.setattr(SourceMedia, 'read', racing)
    with pytest.raises(ValueError, match='changed_during_read'):
        opened(original)


def test_audio_asset_is_not_an_image_read_even_with_valid_source_scope(original):
    from test_source_audio import message as audio_message
    ledger = original[0]
    ledger.record_source('audio-source', contact_id='person', session_id='original',
                         messages=[audio_message()], derive_claims=False)
    with ledger._connect() as conn:
        asset = conn.execute("SELECT asset_hash FROM source_media_links WHERE turn_id='audio-source'").fetchone()[0]
    ref = ledger.source_references(['audio-source'], contact_id='person', session_id='later')[0]
    with pytest.raises(ValueError, match='image_unavailable'):
        opened(original, **ref, asset_hash=asset)


@pytest.mark.asyncio
async def test_image_read_api_enforces_scope_hash_and_selector_contract(source_app, original, tmp_path):
    ledger, ref, asset = original
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='reader', secret='read', viewer='person', scopes=['memory:read']),
                            _principal(principal='other', secret='other', viewer='other', scopes=['memory:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    body = {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later',
            **ref, 'source_view': 'image', 'asset_hash': asset}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer read'})
        assert result.status_code == 200, result.text
        assert base64.b64decode(result.json()['source']['image']['data_url'].split(',')[1]) == image_bytes()
        assert (await client.post('/v1/host/memory/read', json=body,
                                  headers={'Authorization': 'Bearer other'})).status_code == 403
        for changes in ({'asset_hash': '/tmp/image.png'}, {'asset_hash': 'https://invalid/image.png'},
                        {'asset_hash': None}, {'offset': 1}, {'source_view': 'source'}):
            invalid = await client.post('/v1/host/memory/read', json=body | changes,
                                         headers={'Authorization': 'Bearer read'})
            assert invalid.status_code == 422, invalid.text

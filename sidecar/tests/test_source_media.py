"""Real source/image storage, scope, erasure and one shared recall packet."""
import base64
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace

from PIL import Image, ImageDraw
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.idempotency import SourceErased, source_message_hash
from colony_sidecar.turns.media import SourceMedia
from colony_sidecar.vector.multimodal_types import ImageInput
from test_turn_source_evidence import source_app, recalled
from test_hermes_turn_outbox import _load_client


def image_bytes():
    image = Image.new('RGB', (320, 160), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 35, 100, 120), fill='red')
    draw.ellipse((205, 40, 280, 115), fill='blue')
    output = io.BytesIO(); image.save(output, format='PNG')
    return output.getvalue()


def message(data=None):
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Please retain this reference image.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data or image_bytes()).decode()}},
    ]}


class Vision:
    def __init__(self, before=None): self.calls = 0; self.before = before
    def tier_config(self, tier): return SimpleNamespace(base_url='http://127.0.0.1:8080/v1', supports_vision=True)
    async def complete(self, **kwargs):
        self.calls += 1
        assert kwargs['context']['allow_fallback'] is False
        assert kwargs['messages'][-1]['content'][0]['image_url']['url'].startswith('data:image/png;base64,')
        if self.before: self.before()
        return SimpleNamespace(content='A red rectangle is on the left and a blue circle on the right, on white.', model_id='fixture-vision-a')


@pytest.mark.asyncio
async def test_media_http_reads_require_memory_scope_and_bound_person(source_app, tmp_path):
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    from test_scoped_api_authority import _principal, _write_keyring
    principals = [
        _principal(principal='reader', secret='reader-key', viewer='contact-a', scopes=['memory:read']),
        _principal(principal='context', secret='context-key', viewer='contact-a', scopes=['context:read']),
        _principal(principal='other', secret='other-key', viewer='contact-b', scopes=['memory:read']),
    ]
    for principal in principals:
        principal['allow_unscoped_api'] = False
    keyring = tmp_path / 'keys.json'; _write_keyring(keyring, principals)
    source_app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keyring))
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('image', contact_id='contact-a', session_id='s', messages=[message()])
    asset = hashlib.sha256(image_bytes()).hexdigest()
    routes = [('/v1/host/memory/sources/assets/' + asset, {'session_id': 'later'}),
              ('/v1/host/memory/sources/claims/status', {})]
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        for path, params in routes:
            params = {**params, 'contact_id': 'contact-a'}
            assert (await client.get(path, params=params)).status_code == 401
            denied = await client.get(path, params=params, headers={'Authorization': 'Bearer context-key'})
            assert denied.status_code == 403
            allowed = await client.get(path, params=params, headers={'Authorization': 'Bearer reader-key'})
            assert allowed.status_code == 200, allowed.text
            if '/assets/' in path:
                assert allowed.content == image_bytes()
            impersonated = await client.get(path, params=params, headers={'Authorization': 'Bearer other-key'})
            assert impersonated.status_code == 403
        absent = await client.get(routes[0][0], params={'contact_id': 'contact-b', 'session_id': 's'},
                                  headers={'Authorization': 'Bearer other-key'})
        assert absent.status_code == 404


@pytest.mark.asyncio
async def test_inline_image_retained_exactly_and_recalled_across_sessions(source_app, tmp_path, monkeypatch):
    data = image_bytes(); original = message(data); asset = hashlib.sha256(data).hexdigest()
    body = {'identity': {'host_id': 'test'}, 'context': {'session_id': 's1', 'contact_id': 'contact-a', 'turn_id': 'image-a', 'channel_id': 'test:a'}, 'user_message': original}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        response = await client.put('/v2/host/turns/image-a', json=body)
        assert response.status_code == 201, response.text
        assert (await client.put('/v2/host/turns/image-a', json=body)).status_code == 200
        ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
        media = SourceMedia(ledger)
        with sqlite3.connect(ledger.db_path) as conn:
            encoded = conn.execute('SELECT messages_json FROM turn_sources').fetchone()[0]
        stored = json.loads(encoded)[0]
        assert source_message_hash('s1', stored) == source_message_hash('s1', original)
        assert base64.b64encode(data).decode() not in encoded
        assert stored['content'][1]['asset_id'] == 'sha256:' + asset
        assert media.read(asset, contact_id='contact-a', session_id='another')[0] == data
        blob = await client.get('/v1/host/memory/sources/assets/' + asset, params={'contact_id': 'contact-a', 'session_id': 'another'})
        assert blob.content == data and blob.headers['cache-control'] == 'no-store'
        assert (await client.get('/v1/host/memory/sources/assets/' + asset, params={'contact_id': 'contact-b', 'session_id': 's1'})).status_code == 404
        vision = Vision(); assert await media.process_one(vision)
        monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
        context = await recalled(client, session='another', query='blue circle')
        assert 'blue circle' in context and asset in context and 'derived_unverified' in context
        assert 'fixture-vision-a' in context
        assert await recalled(client, contact='contact-b', query='blue circle') == ''
        reopened = SourceMedia(TurnIdempotencyLedger(ledger.db_path))
        assert reopened.search('blue circle', contact_id='contact-a', session_id='new')[0]['asset_id'] == 'sha256:' + asset
        assert not await reopened.process_one(Vision())
        assert vision.calls == 1
        assert media.store._original_path(asset, 'image/png').stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_erasure_removes_checkpoint_copies_and_outbox_replay(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db'); media = SourceMedia(ledger)
    original = message(); asset = hashlib.sha256(image_bytes()).hexdigest()
    for turn, scope in [('ordinary', 'person'), ('checkpoint', 'session')]:
        ledger.record_source(turn, contact_id='c', session_id='s', messages=[original], scope=scope)
    await media.process_one(Vision())
    result = ledger.erase_sources(contact_id='c', turn_ids=['ordinary'])
    assert result['media_cleanup'] == 'complete'
    assert not list(media.store._originals_dir.iterdir()) and not list(media.store._thumbs_dir.iterdir())
    assert media.search('blue circle', contact_id='c', session_id='s') == []
    with pytest.raises(KeyError): media.read(asset, contact_id='c', session_id='s')
    rules = ledger.erasure_feed(contact_id='c', after=0)['events']
    module = _load_client('media_erasure_hash')
    assert module.redact_source_payload({'turn_id': 'copy', 'contact_id': 'c', 'session_id': 's', 'user_message': original['content']}, rules) is None
    with pytest.raises(SourceErased): ledger.record_source('late-copy', contact_id='c', session_id='s', messages=[original])


@pytest.mark.asyncio
async def test_shared_pixels_keep_other_contacts_owned_source(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db'); media = SourceMedia(ledger)
    original = message(); asset = hashlib.sha256(image_bytes()).hexdigest()
    for person in ('a', 'b'):
        ledger.record_source(person, contact_id=person, session_id='s', messages=[original])
    await media.process_one(Vision())
    ledger.erase_sources(contact_id='a', turn_ids=['a'])
    with pytest.raises(KeyError): media.read(asset, contact_id='a', session_id='s')
    assert media.read(asset, contact_id='b', session_id='other')[0] == image_bytes()
    assert media.search('blue circle', contact_id='a', session_id='s') == []
    assert media.search('blue circle', contact_id='b', session_id='s')
    ledger.erase_sources(contact_id='b', turn_ids=['b'])
    assert not media.store._original_path(asset, 'image/png').exists()


@pytest.mark.asyncio
async def test_delete_during_description_blocks_late_derived_write(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db'); media = SourceMedia(ledger)
    ledger.record_source('turn', contact_id='c', session_id='s', messages=[message()])
    await media.process_one(Vision(before=lambda: ledger.erase_sources(contact_id='c', turn_ids=['turn'])))
    assert media.search('blue circle', contact_id='c', session_id='s') == []
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM source_media').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM source_media_search').fetchone()[0] == 0


def test_remote_reference_never_fetches_or_retains_url_credentials(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db')
    original = {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://user:password@example.invalid/image?token=private'}}]}
    ledger.record_source('turn', contact_id='c', session_id='s', messages=[original])
    with sqlite3.connect(ledger.db_path) as conn:
        encoded = conn.execute('SELECT messages_json FROM turn_sources').fetchone()[0]
    assert 'password' not in encoded and 'private' not in encoded and 'example.invalid' not in encoded
    assert source_message_hash('s', json.loads(encoded)[0]) == source_message_hash('s', original)


def test_startup_recovers_only_unowned_source_namespace(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db'); media = SourceMedia(ledger)
    image = ImageInput(data=image_bytes(), mime_type='image/png', width=320, height=160)
    source = media.store.store_original(image)
    from colony_sidecar.vector.image_store import LocalImageStore
    legacy = LocalImageStore(str(tmp_path)).store_original(image)
    media.recover_unowned_files()
    from pathlib import Path
    assert not Path(source.path).exists() and Path(legacy.path).exists()


@pytest.mark.asyncio
async def test_invalid_image_stays_explicitly_unretained_without_losing_text(source_app, tmp_path):
    body = {'identity': {'host_id': 'test'}, 'context': {'session_id': 's', 'contact_id': 'c', 'turn_id': 'invalid'}, 'user_message': message(b'not image bytes')}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        assert (await client.put('/v2/host/turns/invalid', json=body)).status_code == 201
    with sqlite3.connect(tmp_path / 'turn-idempotency.db') as conn:
        encoded = conn.execute('SELECT messages_json FROM turn_sources').fetchone()[0]
    assert 'Please retain' in encoded and 'unsupported_inline_image' in encoded


@pytest.mark.asyncio
async def test_media_requires_explicit_local_vision_capability(tmp_path):
    from colony_sidecar.router.tiers import ModelTier, build_tiers_from_host
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.router.fallback import FallbackHandler
    from unittest.mock import AsyncMock
    ledger = TurnIdempotencyLedger(tmp_path / 'ledger.db'); media = SourceMedia(ledger)
    ledger.record_source('turn', contact_id='c', session_id='s', messages=[message()])
    config = {'provider': 'local', 'baseUrl': 'http://127.0.0.1:8080/v1',
              'models': {'small': {'model': 'text-model', 'supportsVision': True}}}
    router = LLMRouter(tiers=build_tiers_from_host(config), self_learner=SimpleNamespace())
    router.complete = AsyncMock()
    assert router.tier_config(ModelTier.VISION) is None
    await media.process_one(router)
    router.complete.assert_not_awaited()
    assert media.status('c')[0]['error'] == 'local_vision_role_unavailable'
    config['models']['vision'] = {'model': 'image-model', 'supportsVision': True}
    router = LLMRouter(tiers=build_tiers_from_host(config), self_learner=SimpleNamespace())
    assert router.tier_config(ModelTier.VISION).supports_vision is True
    assert router.tier_config(ModelTier.VISION).model_id == 'openai/image-model'
    for score in (0.1, 0.5, 0.9):
        router._scorer = SimpleNamespace(score=lambda *args: score)
        router._learner = None
        assert router.route('text query')[0] in {ModelTier.SMALL, ModelTier.MEDIUM, ModelTier.LARGE}
    assert not FallbackHandler().should_escalate(RuntimeError('rate limit'), ModelTier.VISION)
    assert FallbackHandler().next_tier(ModelTier.VISION) is None

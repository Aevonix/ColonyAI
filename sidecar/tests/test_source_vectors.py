"""Actual source API, worker, SQLite and Lance paths with controlled embeddings.

These prove scope/lineage/rebuild behavior. Semantic quality is measured by the
separate neutral evaluation using the actual deployment embedding and reranker.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.source_vectors import SourceVectors, merge_source_hits
from colony_sidecar.vector import Collection
from colony_sidecar.vector.indexes import EmbeddingIdentity, IndexCatalog, IncompatibleIndex
from colony_sidecar.vector.migrate import migrate_tier
from colony_sidecar.vector.store import VectorStore
from test_turn_source_evidence import source_app, recalled, envelope
from test_recall_unified_context import Reranker, calibrate


class Pipeline:
    def __init__(self, name='a', rotate=False):
        self.index_identity = EmbeddingIdentity(name, name, 'unknown', 3)
        self.rotate = rotate
        self.started, self.release, self.before = asyncio.Event(), None, None

    def vector(self, text):
        text = text.casefold()
        index = 0 if any(word in text for word in ('hydrofoil', 'vessel', 'office', 'workplace')) else (
            1 if any(word in text for word in ('circle', 'round', 'circular')) else 2)
        if self.rotate: index = (index + 1) % 3
        return [float(i == index) for i in range(3)]

    async def embed_batch(self, texts):
        self.started.set()
        if self.release: await self.release.wait()
        if self.before: self.before()
        return [self.vector(text) for text in texts]

    async def embed_query(self, text):
        return self.vector(text)


async def setup(tmp_path, pipeline=None):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    pipeline = pipeline or Pipeline()
    store = VectorStore(str(tmp_path / 'lancedb'), identity=pipeline.index_identity, catalog=IndexCatalog(ledger))
    await store.connect(3); await store.ensure_collections(3)
    return ledger, store, pipeline, SourceVectors(ledger, store, pipeline)


async def drain(projection):
    for _ in range(200):
        if not await projection.process_one(): return
    raise AssertionError('neutral jobs did not finish')


@pytest.mark.asyncio
async def test_ordinary_ingest_worker_semantic_context_and_one_abstention(source_app, tmp_path, monkeypatch):
    import colony_sidecar.vector as vectors
    from colony_sidecar.api.routers import host
    from colony_sidecar.beliefs.source_projection import run_source_claim_worker
    ledger, store, pipeline, projection = await setup(tmp_path)
    monkeypatch.setattr(vectors, '_store', store)
    monkeypatch.setattr(vectors, '_pipeline', pipeline)
    reranker = Reranker(); calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, '_reranker', reranker)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        body = envelope('ordinary')
        body['user_message']['content'] = 'The hydrofoil departure code is cedar-42.'
        assert (await client.put('/v2/host/turns/ordinary', json=body)).status_code == 201
        assert not ledger.search_sources('vessel identifier', contact_id='contact-a', session_id='other')
        task = asyncio.create_task(run_source_claim_worker(ledger, lambda: None, claims_enabled=False))
        try:
            for _ in range(100):
                with ledger._connect() as conn:
                    status = conn.execute('SELECT status FROM source_vector_jobs WHERE turn_id=?', ('ordinary',)).fetchone()[0]
                if status == 'complete': break
                await asyncio.sleep(.01)
            assert status == 'complete'
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError): await task
        text = await recalled(client, query='vessel identifier', session='other')
        assert 'cedar-42' in text and 'source_message_hash' in text and 'source_quote' in text
        assert len(reranker.calls) == 1
        assert reranker.calls[0].count(body['user_message']['content']) == 1
        reranker.score = .01
        assert await recalled(client, query='unrelated question', session='other') == ''
        assert len(reranker.calls) == 2
        assert await recalled(client, contact='someone-else', query='vessel identifier') == ''
        status = await client.get('/v1/host/memory/sources/claims/status', params={'contact_id':'contact-a'})
        assert status.json()['semantic']['projected_turns'] == 1
        assert status.json()['semantic']['pending_turns'] == 0


@pytest.mark.asyncio
async def test_scope_is_prefiltered_and_vector_text_is_never_evidence(tmp_path):
    ledger, store, pipeline, projection = await setup(tmp_path)
    # More closer foreign hits than the legacy global overfetch window.
    foreign = ('hydrofoil ' + 'x' * 1790) * 220
    ledger.record_source('foreign', contact_id='other', session_id='s', messages=[{'role':'user','content':foreign}])
    ledger.record_source('mine', contact_id='c', session_id='s', messages=[{'role':'user','content':'The hydrofoil code is ash-7.'}])
    ledger.record_source('checkpoint', contact_id='c', session_id='private', scope='session',
                         messages=[{'role':'user','content':'The hydrofoil private code is willow.'}])
    await drain(projection)
    hits, _ = await projection.search('vessel identifier', contact_id='c', session_id='new', limit=1)
    assert [hit['turn_id'] for hit in hits] == ['mine']
    table = await store._table(Collection.CONVERSATIONS, semantic=True)
    await table.update(updates={'text': 'corrupted cached vector text'})
    hits, _ = await projection.search('vessel identifier', contact_id='c', session_id='private')
    assert {hit['turn_id'] for hit in hits} == {'mine', 'checkpoint'}
    assert all('corrupted' not in hit['content'] for hit in hits)


@pytest.mark.asyncio
async def test_partial_erasure_preserves_unrelated_chunks_and_fences_late_embedding(tmp_path):
    ledger, store, pipeline, projection = await setup(tmp_path)
    erased = {'role':'user','content':'The hydrofoil code is cedar.'}
    retained = {'role':'user','content':'The office is beside the orchard.'}
    ledger.record_source('ordinary', contact_id='c', session_id='s', messages=[erased])
    ledger.record_source('checkpoint', contact_id='c', session_id='s', scope='session', messages=[erased, retained])
    await drain(projection)
    generation = store.catalog.begin(store.identity)
    pipeline.release = asyncio.Event()
    pipeline.started.clear()
    task = asyncio.create_task(projection.process_one())
    await asyncio.wait_for(pipeline.started.wait(), 2)
    ledger.erase_sources(contact_id='c', turn_ids=['ordinary'])
    pipeline.release.set(); await task
    await store.erase_source_projections(['ordinary', 'checkpoint'])
    await drain(projection)
    result = await migrate_tier(store, pipeline)
    assert not result.errors and result.generation_id == generation['id']
    hits, _ = await projection.search('workplace vessel', contact_id='c', session_id='s')
    assert [hit['content'] for hit in hits] == [retained['content']]
    for gen in store.catalog.generations():
        table = await store._table(Collection.CONVERSATIONS, generation=gen)
        assert all('cedar' not in row['text'] for row in await table.query().select(['text']).to_list())


@pytest.mark.asyncio
async def test_model_swap_reprojects_canonical_sources_with_new_equal_dimension_space(tmp_path):
    ledger, store, pipeline, projection = await setup(tmp_path)
    ledger.record_source('mine', contact_id='c', session_id='s', messages=[{'role':'user','content':'The hydrofoil code is ash-7.'}])
    await drain(projection)
    _, replacement, new_pipeline, new_projection = await setup(tmp_path, Pipeline('b', rotate=True))
    with pytest.raises(IncompatibleIndex):
        await new_projection.search('vessel identifier', contact_id='c', session_id='other')
    result = await migrate_tier(replacement, new_pipeline)
    assert not result.errors
    await drain(new_projection)
    hits, _ = await new_projection.search('vessel identifier', contact_id='c', session_id='other')
    assert hits[0]['content'].endswith('ash-7.')
    with ledger._connect() as conn:
        assert conn.execute('SELECT generation_id FROM source_vector_jobs').fetchone()[0] == result.generation_id


@pytest.mark.asyncio
async def test_bounded_projection_resumes_cursor_and_reports_retryable_failure(tmp_path):
    ledger, store, pipeline, projection = await setup(tmp_path)
    ledger.record_source('long', contact_id='c', session_id='s', messages=[
        {'role':'user', 'content':'The hydrofoil code is ' + str(index)} for index in range(3)])
    assert await projection.process_one(batch_size=1)
    with ledger._connect() as conn:
        assert conn.execute('SELECT cursor FROM source_vector_jobs').fetchone()[0] == 1
    reopened = SourceVectors(TurnIdempotencyLedger(ledger.db_path), store, pipeline)
    reopened.backfill()
    def unavailable(): raise OSError('neutral endpoint unavailable')
    pipeline.before = unavailable
    assert await reopened.process_one(batch_size=1)
    assert reopened.status('c')['failed_turns'] == 1
    assert not await reopened.process_one()  # Backoff allows other work to run.
    pipeline.before = None
    with ledger._connect() as conn:
        conn.execute('UPDATE source_vector_jobs SET next_attempt=0')
    await drain(reopened)
    assert reopened.status('c')['projected_turns'] == 1
    assert reopened.status('c')['failed_turns'] == 0
    assert await store.count(Collection.CONVERSATIONS) == 3


@pytest.mark.asyncio
async def test_caption_semantics_keeps_exact_asset_and_shared_source_erasure(tmp_path):
    from test_source_media import message, Vision, image_bytes
    from colony_sidecar.turns.media import SourceMedia
    ledger, store, pipeline, projection = await setup(tmp_path)
    for person in ('a', 'b'):
        ledger.record_source(person, contact_id=person, session_id='s', messages=[message()])
    await drain(projection)  # No caption yet, the source text alone is indexed.
    media = SourceMedia(ledger); await media.process_one(Vision())
    assert not media.search('round shape', contact_id='a', session_id='later')
    await drain(projection)  # Caption completion reschedules both owners.
    _, hits = await projection.search('round shape', contact_id='a', session_id='later')
    assert len(hits) == 1 and hits[0]['epistemic_state'] == 'derived_unverified'
    assert hits[0]['source_message_hash'] and hits[0]['source_turn_id'] == 'a'
    asset = hits[0]['asset_id'][7:]
    assert media.read(asset, contact_id='a', session_id='later')[0] == image_bytes()
    ledger.erase_sources(contact_id='a', turn_ids=['a'])
    await store.erase_source_projections(['a'])
    assert await projection.search('round shape', contact_id='a', session_id='later') == ([], [])
    _, kept = await projection.search('round shape', contact_id='b', session_id='later')
    assert len(kept) == 1 and kept[0]['source_turn_id'] == 'b'


@pytest.mark.asyncio
async def test_semantic_candidates_still_use_temporal_conflict_and_correction_bundles(source_app, tmp_path):
    from test_source_claim_projection import Model, claim, ingest
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    from colony_sidecar.beliefs.source_time import interpret_time_query
    ledger, store, pipeline, projection = await setup(tmp_path)
    claims = SourceClaimProjection(ledger)
    a, b = 'My office is in River.', 'My office is in Lake.'
    model = Model({a:claim(a, 'River'), b:claim(b, 'Lake', match_prior=True)})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        for turn, text in [('a', a), ('b', b)]:
            await ingest(client, turn, text)
            await claims.process_one(model)
        await drain(projection)
        assert not ledger.search_sources('workplace', contact_id='contact-a', session_id='later')
        hits, _ = await projection.search('workplace', contact_id='contact-a', session_id='later')
        query = interpret_time_query('workplace', now=datetime.now(timezone.utc))
        _, rows = claims.prepare_context([], hits, contact_id='contact-a', session_id='later', time_query=query)
        bundles = [json.loads(row['content']) for row in rows if row.get('atomic_evidence')]
        assert bundles[0]['status'] == 'unresolved_conflict'
        assert {row['value'] for row in bundles[0]['assertions']} == {'River', 'Lake'}
        corrected = 'Correction: My office is in Orchard, not River.'
        await ingest(client, 'corrected', corrected)
        await claims.process_one(Model({corrected:claim(corrected, 'Orchard', operation='correct', match_prior=True)}))
        await drain(projection)
        hits, _ = await projection.search('workplace', contact_id='contact-a', session_id='later')
        _, rows = claims.prepare_context([], hits, contact_id='contact-a', session_id='later', time_query=query)
        assert all('River' not in str(row.get('content')) or row.get('atomic_evidence') for row in rows)

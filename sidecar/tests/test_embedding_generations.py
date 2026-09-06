"""Real LanceDB tests for compatible generations and deletion during rebuilding."""
import asyncio

import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.vector.collections import Collection
from colony_sidecar.vector.indexes import EmbeddingIdentity, IndexCatalog, IncompatibleIndex
from colony_sidecar.vector.migrate import migrate_tier
from colony_sidecar.vector.store import VectorStore


class Pipeline:
    def __init__(self, identity):
        self.index_identity = identity
        self.fail = False
        self.started = asyncio.Event()
        self.release = None

    async def embed_batch(self, texts):
        self.started.set()
        if self.release:
            await self.release.wait()
        if self.fail:
            raise OSError('neutral fixture endpoint unavailable')
        return [[1.] + [0.] * (self.index_identity.dimensions - 1) for _ in texts]


async def legacy(tmp_path, *, metadata=None):
    store = VectorStore(str(tmp_path / 'lancedb'))
    await store.connect(2)
    await store.ensure_collections(2)
    await store.add(Collection.MEMORIES, 'neutral-a', 'The hydrofoil departs Friday.', [1., 0.], metadata)
    return store


async def managed(tmp_path, identity, ledger=None):
    ledger = ledger or TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    store = VectorStore(str(tmp_path / 'lancedb'), identity=identity, catalog=IndexCatalog(ledger))
    await store.connect(identity.dimensions)
    await store.ensure_collections(identity.dimensions)
    return store


@pytest.mark.asyncio
async def test_failed_dimension_update_retains_original_row(tmp_path):
    store = await legacy(tmp_path)
    with pytest.raises(ValueError, match='dimension'):
        await store.update(Collection.MEMORIES, 'neutral-a', 'Changed', [1., 0., 0.])
    assert (await store.get(Collection.MEMORIES, 'neutral-a')).text == 'The hydrofoil departs Friday.'
    await store.update(Collection.MEMORIES, 'neutral-a', 'The boat departs Saturday.', [0., 1.])
    assert await store.count(Collection.MEMORIES) == 1
    assert (await store.get(Collection.MEMORIES, 'neutral-a')).text.endswith('Saturday.')


@pytest.mark.asyncio
async def test_unknown_legacy_and_equal_dimension_model_changes_are_not_compatible(tmp_path):
    await legacy(tmp_path)
    identity = EmbeddingIdentity('model-a', 'served-a', 'unknown', 2)
    store = await managed(tmp_path, identity)
    with pytest.raises(IncompatibleIndex):
        await store.search(Collection.MEMORIES, [1., 0.])
    result = await migrate_tier(store, Pipeline(identity), batch_size=1)
    assert result.errors == [] and result.vectors_migrated == 1
    assert (await store.search(Collection.MEMORIES, [1., 0.]))[0].id == 'neutral-a'
    replacement = await managed(tmp_path, EmbeddingIdentity('model-b', 'served-b', 'unknown', 2))
    with pytest.raises(IncompatibleIndex):
        await replacement.search(Collection.MEMORIES, [1., 0.])
    assert (await replacement.get(Collection.MEMORIES, 'neutral-a')).text.endswith('Friday.')


@pytest.mark.asyncio
async def test_failed_rebuild_resumes_without_replacing_the_active_index(tmp_path):
    old = await legacy(tmp_path)
    identity = EmbeddingIdentity('model-new', 'served-new', 'declared-r2', 3)
    store = await managed(tmp_path, identity)
    pipeline = Pipeline(identity)
    pipeline.fail = True
    result = await migrate_tier(store, pipeline, batch_size=1)
    assert result.errors and store.catalog.active()['id'] == 'legacy'
    assert await old.count(Collection.MEMORIES) == 1
    pipeline.fail = False
    resumed = await migrate_tier(store, pipeline, batch_size=1)
    assert resumed.generation_id == result.generation_id
    assert resumed.errors == [] and store.catalog.active()['fingerprint'] == identity.fingerprint
    assert len((await store.search(Collection.MEMORIES, [1., 0., 0.]))) == 1
    assert await old.count(Collection.MEMORIES) == 1  # retained legacy files were not overwritten


@pytest.mark.asyncio
async def test_delete_and_new_arrival_during_rebuild_survive_promotion(tmp_path):
    await legacy(tmp_path)
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 3)
    store = await managed(tmp_path, identity)
    pipeline = Pipeline(identity)
    pipeline.release = asyncio.Event()
    task = asyncio.create_task(migrate_tier(store, pipeline, batch_size=1))
    await asyncio.wait_for(pipeline.started.wait(), 3)
    await store.delete(Collection.MEMORIES, 'neutral-a')
    await store.add(Collection.MEMORIES, 'new-arrival', 'The new neutral message.', [1., 0., 0.])
    pipeline.release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.errors == []
    assert [row.id for row in await store.search(Collection.MEMORIES, [1., 0., 0.])] == ['new-arrival']
    for generation in store.catalog.generations():
        table = await store._table(Collection.MEMORIES, generation=generation)
        assert await table.query().where("id = 'neutral-a'").to_list() == []
    # Neither a late updater nor selecting an older generation can undo deletion.
    await store.add(Collection.MEMORIES, 'neutral-a', 'Late evidence', [1., 0., 0.])
    assert await store.get(Collection.MEMORIES, 'neutral-a') is None


@pytest.mark.asyncio
async def test_canonical_source_tombstone_blocks_old_rows_without_cleanup(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('turn-a', contact_id='person-a', session_id='s', messages=[{'role':'user', 'content':'Neutral fact'}])
    await legacy(tmp_path, metadata={'source_uri':'turn:turn-a', 'person_id':'person-a'})
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 2)
    store = await managed(tmp_path, identity, ledger)
    ledger.erase_sources(contact_id='person-a', turn_ids=['turn-a'])
    result = await migrate_tier(store, Pipeline(identity))
    assert result.errors == [] and result.vectors_migrated == 0
    assert await store.search(Collection.MEMORIES, [1., 0.]) == []


@pytest.mark.asyncio
async def test_provider_order_identity_and_query_format_are_bound(monkeypatch):
    import httpx
    from colony_sidecar.vector.config import EmbeddingConfig
    from colony_sidecar.vector.embedder import EmbeddingPipeline
    from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider
    import json

    requests = []
    served = ['actual-model']
    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        count = len(payload['input'])
        return httpx.Response(200, json={'model': served[0], 'data': [
            {'index': i, 'embedding': [1., float(i)]} for i in reversed(range(count))]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    monkeypatch.setenv('COLONY_EMBED_QUERY_INSTRUCTION', 'Retrieve: ')
    provider = OpenAIAPIEmbeddingProvider(EmbeddingConfig(provider='openai_api', model_id='requested-alias', dimensions=2, revision='operator-r1'))
    provider.configure('http://fixture/v1', '')
    pipeline = EmbeddingPipeline(provider)
    await pipeline.warmup()
    identity = pipeline.index_identity
    assert identity.requested_model == 'requested-alias' and identity.served_model == 'actual-model'
    assert identity.declared_revision == 'operator-r1'
    assert await pipeline.embed_batch(['first', 'second']) == [[1., 0.], [1., 1.]]
    monkeypatch.setenv('COLONY_EMBED_QUERY_INSTRUCTION', 'Different: ')
    await pipeline.embed_query('neutral question')
    assert requests[-1]['input'] == ['Retrieve: neutral question']
    assert pipeline.index_identity == identity
    served[0] = 'different-actual-model'
    with pytest.raises(ValueError, match='reported model'):
        await pipeline.embed('new uncached text')


@pytest.mark.asyncio
async def test_migration_api_and_forget_cover_retained_files_without_graph(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    import colony_sidecar.vector as vector_module
    from colony_sidecar.api.routers import host

    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('turn-a', contact_id='person-a', session_id='s', messages=[{'role':'user', 'content':'Neutral fact'}])
    await legacy(tmp_path, metadata={'source_uri':'turn:turn-a', 'person_id':'person-a'})
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 2)
    store = await managed(tmp_path, identity, ledger)
    monkeypatch.setattr(vector_module, '_store', store)
    monkeypatch.setattr(host, '_graph', None)
    monkeypatch.setattr(host, '_embedder', Pipeline(identity))
    monkeypatch.setattr(host.router, '_migrate_running', False, raising=False)
    monkeypatch.setattr(host.router, '_migrate_results', {}, raising=False)
    tasks = []
    def spawn(coro):
        tasks.append(asyncio.create_task(coro))
        return tasks[-1]
    monkeypatch.setattr(host, '_spawn_task', spawn)
    app = FastAPI(); app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/host/memory/migrate', json={'identity':{'host_id':'fixture'}, 'batch_size':1})
        assert response.status_code == 200, response.text
        task_id = response.json()['task_id']
        await asyncio.gather(*tasks)
        status = await client.get('/v1/host/memory/migrate/' + task_id)
        assert status.json()['status'] == 'completed' and status.json()['vectors_migrated'] == 1
        # Durable generation identity still answers after process-local status is lost.
        host.router._migrate_results.clear()
        status = await client.get('/v1/host/memory/migrate/' + task_id)
        assert status.json()['status'] == 'completed' and status.json()['fingerprint'] == identity.fingerprint
        erased = await client.post('/v1/host/memory/sources/forget', json={'contact_id':'person-a', 'source_ids':['turn-a']})
        assert erased.status_code == 200 and erased.json()['vector_cleanup'] == 'complete'
        for generation in store.catalog.generations():
            table = await store._table(Collection.MEMORIES, generation=generation)
            assert await table.count_rows() == 0

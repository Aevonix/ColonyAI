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
async def test_interrupted_completion_keeps_new_arrivals_in_resumable_generation(tmp_path):
    await legacy(tmp_path)
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 2)
    store = await managed(tmp_path, identity)
    generation = store.catalog.begin(identity)
    await store.add(Collection.CONVERSATIONS, 'arrival', 'A new ordinary turn.', [1., 0.])
    # Fail after the completion status update but before pointer commit. SQLite
    # must roll both back, including on a fresh catalog/process view.
    with store.catalog.ledger._connect() as conn:
        conn.execute("""CREATE TRIGGER interrupt_promotion BEFORE UPDATE ON vector_active
                        BEGIN SELECT RAISE(ABORT, 'interrupted commit'); END""")
    result = await migrate_tier(store, Pipeline(identity))
    assert result.errors and store.catalog.active()['id'] == 'legacy'
    fresh = await managed(tmp_path, identity)
    assert fresh.catalog.begin(identity)['id'] == generation['id']
    assert fresh.catalog.write_generation(identity)['id'] == generation['id']
    await fresh.add(Collection.CONVERSATIONS, 'after-restart', 'Another ordinary turn.', [1., 0.])
    with fresh.catalog.ledger._connect() as conn:
        conn.execute('DROP TRIGGER interrupt_promotion')
    resumed = await migrate_tier(fresh, Pipeline(identity))
    assert not resumed.errors and resumed.generation_id == generation['id']
    assert {hit.id for hit in await fresh.search(Collection.CONVERSATIONS, [1., 0.])} == {'arrival', 'after-restart'}


@pytest.mark.parametrize('status_code,status,wait,expected', [
    (200, 'resumable', 10, 'interrupted, not complete'),
    (404, '', 10, 'completion is not confirmed'),
    (200, 'running', 1, 'server migration may still be running'),
])
def test_cli_migration_stops_truthfully(monkeypatch, capsys, status_code, status, wait, expected):
    import httpx
    import sys
    import time
    from colony_sidecar import cli
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['colony', 'migrate-tier', '--wait-seconds', str(wait)])
    clock = [0.]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(200, json={'task_id':'neutral-task'}))
    polls = []
    def get(*args, **kwargs):
        polls.append(args)
        return httpx.Response(status_code, json={'status':status})
    monkeypatch.setattr(httpx, 'get', get)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2 and len(polls) == 1
    assert expected in capsys.readouterr().out


@pytest.mark.asyncio
async def test_multimodal_api_has_explicit_endpoint_without_losing_text_provider(monkeypatch):
    import httpx
    from colony_sidecar.vector.config import EmbeddingConfig
    from colony_sidecar.vector.embedder import EmbeddingPipeline
    from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider
    from colony_sidecar.vector.multimodal_provider import make_multimodal_provider
    config = EmbeddingConfig(provider='openai_api', model_id='neutral', dimensions=2)
    with pytest.raises(ValueError, match='explicit endpoint'):
        make_multimodal_provider(config)
    config.base_url, config.api_key = 'http://fixture/v1/', 'private-test-key'
    assert config.api_key not in repr(config)
    text_provider = OpenAIAPIEmbeddingProvider(config)
    text_provider.configure(config.base_url, config.api_key)
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'model':'neutral', 'data':[{'index':0, 'embedding':[1., 0.]}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    mm_provider = make_multimodal_provider(config)
    pipeline = EmbeddingPipeline(provider=text_provider, multimodal_provider=mm_provider)
    await pipeline.warmup()
    assert await pipeline.embed('neutral ordinary text') == [1., 0.]
    assert await mm_provider.embed_text('neutral image description') == [1., 0.]
    assert len(requests) == 3
    assert all(str(request.url) == 'http://fixture/v1/embeddings' for request in requests)
    assert all(request.headers['Authorization'] == 'Bearer private-test-key' for request in requests)


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


@pytest.mark.parametrize('command,request_type,summary', [
    ('migrate-tier', 'MigrateRequest', 'Migration complete: 3 vectors migrated'),
    ('backfill', 'BackfillRequest', 'Backfill complete: 3 processed'),
])
def test_vector_cli_request_matches_current_host_contract(monkeypatch, capsys, command, request_type, summary):
    import httpx
    import sys
    import time
    from colony_sidecar import cli
    from colony_sidecar.api.schemas import host
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['colony', command, '--batch-size', '32'])
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    accepted, polls = [], []
    def post(url, *, json, **kwargs):
        # Validate the actual CLI body against the API's own request schema.
        # A hand-written successful HTTP response alone masked the live 422.
        request = getattr(host, request_type).model_validate(json)
        assert request.identity.host_id == 'cli' and request.batch_size == 32
        accepted.append(url)
        return httpx.Response(200, json={'task_id': 'neutral-contract-task'})
    def get(url, **kwargs):
        polls.append(url)
        return httpx.Response(200, json={'status': 'completed', 'vectors_migrated': 3,
            'collections_migrated': 1, 'processed': 3})
    monkeypatch.setattr(httpx, 'post', post)
    monkeypatch.setattr(httpx, 'get', get)
    cli.main()
    assert len(accepted) == len(polls) == 1
    assert polls[0] == accepted[0] + '/neutral-contract-task'
    assert summary in capsys.readouterr().out


class BatchMergeObserver:
    """Observe the real Lance commit boundary, including an injected race."""
    def __init__(self, table, batches, after_commit=None, before_commit=None):
        self.table, self.batches = table, batches
        self.after_commit, self.before_commit = after_commit, before_commit

    def __getattr__(self, name):
        return getattr(self.table, name)

    def merge_insert(self, key):
        observer = self
        class Builder:
            def when_not_matched_insert_all(self):
                return self

            async def execute(self, rows):
                observer.batches.append([row['id'] for row in rows])
                if observer.before_commit:
                    await observer.before_commit(observer.table, rows)
                await observer.table.merge_insert(key).when_not_matched_insert_all().execute(rows)
                if observer.after_commit:
                    observer.after_commit(rows)
        return Builder()


@pytest.mark.asyncio
async def test_batch_merge_preserves_concurrent_update_and_erasure_during_commit(tmp_path, monkeypatch):
    old = await legacy(tmp_path)
    for name in ('neutral-b', 'neutral-c'):
        await old.add(Collection.MEMORIES, name, 'Retained '+name, [1., 0.])
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 2)
    store = await managed(tmp_path, identity)
    original_table, batches = store._table, []
    async def live_write(table, rows):
        newer = dict(next(row for row in rows if row['id'] == 'neutral-a'), text='Newer concurrent evidence')
        await table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([newer])
    def erase_while_committing(rows):
        # Only the durable fence has landed. Migration must remove the row it
        # just committed without requiring an independent cleanup worker.
        store.catalog.delete(Collection.MEMORIES.value, 'neutral-b')
    async def observed_table(collection, **kwargs):
        table = await original_table(collection, **kwargs)
        if collection == Collection.MEMORIES and kwargs.get('write'):
            return BatchMergeObserver(table, batches, erase_while_committing, live_write)
        return table
    monkeypatch.setattr(store, '_table', observed_table)
    result = await migrate_tier(store, Pipeline(identity), batch_size=3)
    assert not result.errors and result.vectors_migrated == 2
    assert len(batches) == 1 and set(batches[0]) == {'neutral-a', 'neutral-b', 'neutral-c'}
    table = await original_table(Collection.MEMORIES)
    rows = {row['id']: row['text'] for row in await table.query().select(['id', 'text']).to_list()}
    assert rows == {'neutral-a': 'Newer concurrent evidence', 'neutral-c': 'Retained neutral-c'}


@pytest.mark.asyncio
async def test_interruption_after_atomic_batch_commit_resumes_only_missing_rows(tmp_path, monkeypatch):
    old = await legacy(tmp_path)
    for index in range(1, 8):
        await old.add(Collection.MEMORIES, 'neutral-'+str(index), 'Retained '+str(index), [1., 0.])
    identity = EmbeddingIdentity('new', 'served-new', 'r2', 2)
    store = await managed(tmp_path, identity)
    original_table, batches = store._table, []
    def crash_after_first_commit(rows):
        if len(batches) == 1:
            raise OSError('Interrupted immediately after atomic batch commit')
    async def observed_table(collection, **kwargs):
        table = await original_table(collection, **kwargs)
        if collection == Collection.MEMORIES and kwargs.get('write'):
            return BatchMergeObserver(table, batches, crash_after_first_commit)
        return table
    monkeypatch.setattr(store, '_table', observed_table)
    first = await migrate_tier(store, Pipeline(identity), batch_size=3)
    assert first.errors and store.catalog.active()['id'] == 'legacy'
    staged = await original_table(Collection.MEMORIES, generation=store.catalog.begin(identity))
    assert len(await staged.query().select(['id']).to_list()) == 3
    resumed = await migrate_tier(store, Pipeline(identity), batch_size=3)
    assert not resumed.errors and resumed.generation_id == first.generation_id
    assert resumed.vectors_migrated == 5 and [len(batch) for batch in batches] == [3, 3, 2]
    final = await original_table(Collection.MEMORIES)
    ids = [row['id'] for row in await final.query().select(['id']).to_list()]
    assert len(ids) == len(set(ids)) == 8

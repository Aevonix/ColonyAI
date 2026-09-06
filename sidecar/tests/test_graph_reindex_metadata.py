"""The projection rebuild must read metadata produced by earlier writers."""
from types import SimpleNamespace
import json

import pytest

from colony_sidecar.intelligence.graph.client import ColonyGraph
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.vector.collections import Collection
from colony_sidecar.vector.indexes import EmbeddingIdentity, IndexCatalog
from colony_sidecar.vector.migrate import migrate_tier
from colony_sidecar.vector.store import VectorStore


class Result:
    def __init__(self, rows=(), single=None):
        self.rows, self.value = rows, single

    async def single(self):
        return self.value

    def __aiter__(self):
        async def rows():
            for row in self.rows:
                yield row
        return rows()


class Driver:
    def __init__(self, records=()):
        self.records, self.written_metadata = records, None

    def session(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run(self, query, **params):
        if 'after' in params:
            return Result([{'memory': row, 'people': ['person-a']}
                for row in self.records if row['id'] > params['after']][:params['limit']])
        if 'CREATE (m:Memory' in query:
            self.written_metadata = params['metadata']
            return Result(single={'id': 'new-memory'})
        return Result(single=None)  # No deduplicated row.


def graph(records=()):
    value = ColonyGraph.__new__(ColonyGraph)
    value.driver = Driver(records)
    value.database = 'neutral-fixture'
    value._embed_fn = value._vector_store = None
    return value


@pytest.mark.asyncio
async def test_rebuild_reads_both_retained_formats_and_current_writer(tmp_path):
    metadata = {'enabled': True, 'unknown': None, 'tags': ['neutral', "owner's quote"]}
    rows = [
        {'id': 'a', 'content': 'The ferry departs Friday.', 'metadata': str(metadata)},
        {'id': 'b', 'content': 'The library opens Tuesday.', 'metadata': json.dumps(metadata)},
        {'id': 'c', 'content': 'The notebook is blue.', 'metadata': None},
    ]
    value = graph(rows)
    identity = EmbeddingIdentity('fixture', 'fixture', 'unknown', 2)
    store = VectorStore(str(tmp_path / 'lance'), identity=identity,
        catalog=IndexCatalog(TurnIdempotencyLedger(tmp_path / 'turns.db')))
    await store.connect(2)
    await store.ensure_collections(2)

    async def embed_batch(texts):
        return [[1., 0.] for _ in texts]

    result = await migrate_tier(store, SimpleNamespace(index_identity=identity,
        embed_batch=embed_batch), graph=value, batch_size=2)
    assert result.errors == [] and result.vectors_migrated == 3
    for memory_id in ('a', 'b'):
        retained = await store.get(Collection.MEMORIES, memory_id)
        assert all(retained.metadata[key] == expected for key, expected in metadata.items())
        assert retained.metadata['person_id'] == 'person-a'
    assert (await store.get(Collection.MEMORIES, 'c')).text == rows[2]['content']

    assert await value.store_memory('A fresh neutral note.', 'semantic', [],
        metadata=metadata) == 'new-memory'
    assert json.loads(value.driver.written_metadata) == metadata
    # New JSON output can be read by the same projection path.
    value.driver.records = [{'id': 'd', 'content': 'A fresh neutral note.',
        'metadata': value.driver.written_metadata}]
    projected = [row async for row in value.iter_indexable_memories()]
    assert projected[0]['metadata']['enabled'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', ['[1, 2]', "{'not': valid_syntax}", "__import__('os').getcwd()"])
async def test_invalid_metadata_does_not_silently_drop_provenance(metadata):
    value = graph([{'id': 'a', 'content': 'A neutral note.', 'metadata': metadata}])
    with pytest.raises((ValueError, SyntaxError)):
        _ = [row async for row in value.iter_indexable_memories()]

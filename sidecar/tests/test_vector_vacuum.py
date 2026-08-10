"""Orphan-vector vacuum (U11): projected id listing, graph/vector set diff,
fail-closed Neo4j handling, admin endpoint, and the post-prune sweep gate.

Locks: dry_run deletes nothing, Neo4j failure aborts BEFORE any deletion,
and the post-prune sweep only activates when COLONY_MEMORY_PRUNE_MODE=live
(default shadow = no sweep, byte-identical to pre-U11 behavior).
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager

import pytest

from colony_sidecar.intelligence.graph import client as client_mod
from colony_sidecar.vector.collections import Collection


# --- VectorStore.list_ids (real LanceDB) --------------------------------------

@pytest.mark.asyncio
async def test_list_ids_projected_query():
    from colony_sidecar.vector.store import VectorStore
    with tempfile.TemporaryDirectory() as d:
        vs = VectorStore(data_dir=d)
        await vs.connect(dimensions=4)
        await vs.ensure_collections(dimensions=4)
        for i in range(3):
            await vs.add(Collection.MEMORIES, id=f"v{i}", text=f"t{i}",
                         vector=[0.1, 0.2, 0.3, float(i)])
        ids = await vs.list_ids(Collection.MEMORIES)
        assert sorted(ids) == ["v0", "v1", "v2"]
        assert await vs.list_ids(Collection.DOCUMENTS) == []


# --- vacuum_orphan_vectors ------------------------------------------------------

class _FakeVectorStore:
    def __init__(self, ids):
        self.ids = list(ids)
        self.deleted = []

    async def list_ids(self, collection):
        return list(self.ids)

    async def delete(self, collection, id):
        self.deleted.append(id)


class _FakeIdResult:
    def __init__(self, ids):
        self._ids = ids

    def __aiter__(self):
        self._it = iter(self._ids)
        return self

    async def __anext__(self):
        try:
            return {"id": next(self._it)}
        except StopIteration:
            raise StopAsyncIteration


class _FakeSession:
    def __init__(self, owner):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, **params):
        if self._owner.fail:
            raise RuntimeError("neo4j unavailable")
        assert "MATCH (m:Memory) RETURN m.id AS id" in cypher
        return _FakeIdResult(self._owner.graph_ids)


class _FakeDriver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _FakeSession(self._owner)


class _Fixture:
    def __init__(self, vector_ids, graph_ids, fail=False):
        self.graph_ids = list(graph_ids)
        self.fail = fail
        self.vec = _FakeVectorStore(vector_ids)
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        g.driver = _FakeDriver(self)
        g.database = "neo4j"
        g._vector_store = self.vec
        self.graph = g


@pytest.mark.asyncio
async def test_dry_run_counts_orphans_deletes_nothing():
    fx = _Fixture(vector_ids=["a", "b", "orphan1", "orphan2"],
                  graph_ids=["a", "b"])
    out = await fx.graph.vacuum_orphan_vectors(dry_run=True)
    assert out["available"] is True
    assert out["vectors"] == 4
    assert out["orphans"] == 2
    assert out["deleted"] == 0
    assert out["dry_run"] is True
    assert out["ids"] == ["orphan1", "orphan2"]
    assert fx.vec.deleted == []


@pytest.mark.asyncio
async def test_live_deletes_only_orphans():
    fx = _Fixture(vector_ids=["a", "orphan1", "b", "orphan2"],
                  graph_ids=["a", "b", "c-graph-only"])
    out = await fx.graph.vacuum_orphan_vectors(dry_run=False,
                                               batch_sleep_secs=0)
    assert out["orphans"] == 2 and out["deleted"] == 2
    assert sorted(fx.vec.deleted) == ["orphan1", "orphan2"]


@pytest.mark.asyncio
async def test_max_delete_bounds_one_run():
    fx = _Fixture(vector_ids=[f"o{i}" for i in range(10)], graph_ids=[])
    out = await fx.graph.vacuum_orphan_vectors(dry_run=False, max_delete=3,
                                               batch_sleep_secs=0)
    assert out["orphans"] == 10   # full backlog reported
    assert out["deleted"] == 3    # but only the cap deleted
    assert len(fx.vec.deleted) == 3


@pytest.mark.asyncio
async def test_neo4j_error_aborts_before_any_deletion():
    fx = _Fixture(vector_ids=["a", "b"], graph_ids=[], fail=True)
    with pytest.raises(RuntimeError):
        await fx.graph.vacuum_orphan_vectors(dry_run=False)
    assert fx.vec.deleted == []


@pytest.mark.asyncio
async def test_no_vector_store_reports_unavailable():
    fx = _Fixture(vector_ids=[], graph_ids=[])
    fx.graph._vector_store = None
    out = await fx.graph.vacuum_orphan_vectors(dry_run=True)
    assert out == {"available": False, "vectors": 0, "orphans": 0,
                   "deleted": 0, "dry_run": True, "ids": []}


# --- endpoint -------------------------------------------------------------------

from colony_sidecar.api.routers import host as host_mod  # noqa: E402


@asynccontextmanager
async def _app(graph):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    prev = host_mod._graph
    host_mod._graph = graph
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            yield client
    finally:
        host_mod._graph = prev


@pytest.mark.asyncio
async def test_endpoint_501_when_graph_missing():
    async with _app(None) as client:
        resp = await client.post("/v1/host/memory/vector-vacuum", json={})
        assert resp.status_code == 501


@pytest.mark.asyncio
async def test_endpoint_defaults_to_dry_run():
    fx = _Fixture(vector_ids=["a", "orphan"], graph_ids=["a"])
    async with _app(fx.graph) as client:
        resp = await client.post("/v1/host/memory/vector-vacuum", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True and body["orphans"] == 1
        assert fx.vec.deleted == []


@pytest.mark.asyncio
async def test_endpoint_live_run_and_fail_closed():
    fx = _Fixture(vector_ids=["orphan"], graph_ids=[])
    async with _app(fx.graph) as client:
        resp = await client.post("/v1/host/memory/vector-vacuum",
                                 json={"dry_run": False})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
    broken = _Fixture(vector_ids=["orphan"], graph_ids=[], fail=True)
    async with _app(broken.graph) as client:
        resp = await client.post("/v1/host/memory/vector-vacuum",
                                 json={"dry_run": False})
        assert resp.status_code == 500
        assert broken.vec.deleted == []


# --- post-prune sweep gate --------------------------------------------------------

def _loop_with_graph(graph):
    from colony_sidecar.autonomy.loop import AutonomyLoop, LoopStats

    class _Reg:
        pass

    reg = _Reg()
    reg.graph = graph
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop._registry = reg
    loop._periodic_last = {}
    loop.stats = LoopStats()
    return loop


class _SweepRecordingGraph:
    def __init__(self):
        self.prune_calls = []
        self.vacuum_calls = []

    async def prune_weak_memories(self, threshold=0.05, *, dry_run=False,
                                  max_delete=500):
        self.prune_calls.append({"dry_run": dry_run})
        return {"matched": 0, "deleted": 0, "dry_run": dry_run, "ids": []}

    async def vacuum_orphan_vectors(self, *, dry_run=False, max_delete=None,
                                    batch_size=200, batch_sleep_secs=0.05):
        self.vacuum_calls.append({"dry_run": dry_run,
                                  "max_delete": max_delete})
        return {"available": True, "vectors": 0, "orphans": 0, "deleted": 0,
                "dry_run": dry_run, "ids": []}


@pytest.mark.asyncio
async def test_sweep_not_run_in_default_shadow_mode(monkeypatch):
    """Regression lock: default prune mode never triggers the sweep."""
    monkeypatch.delenv("COLONY_MEMORY_PRUNE_MODE", raising=False)
    graph = _SweepRecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.prune_calls == [{"dry_run": True}]
    assert graph.vacuum_calls == []


@pytest.mark.asyncio
async def test_sweep_runs_bounded_in_live_mode(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY_PRUNE_MODE", "live")
    graph = _SweepRecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.prune_calls == [{"dry_run": False}]
    assert graph.vacuum_calls == [{"dry_run": False, "max_delete": 2000}]


@pytest.mark.asyncio
async def test_sweep_failure_does_not_fail_prune_phase(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY_PRUNE_MODE", "live")
    graph = _SweepRecordingGraph()

    async def _boom(**kwargs):
        raise RuntimeError("lance down")

    graph.vacuum_orphan_vectors = _boom
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()  # must not raise
    assert graph.prune_calls == [{"dry_run": False}]

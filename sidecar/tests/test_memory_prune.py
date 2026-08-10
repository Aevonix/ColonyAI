"""Real memory pruning (U1): dry_run, delete cap, vector coupling,
fail-closed Neo4j handling, and the COLONY_MEMORY_PRUNE_MODE phase gate.

The flag defaults to ``shadow``, so the regression lock here is that the
default path never deletes anything — graph or vector.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from colony_sidecar.intelligence.graph import client as client_mod
from colony_sidecar.vector.collections import Collection


# --- fakes -------------------------------------------------------------------

class _FakeResult:
    def __init__(self, record):
        self._record = record

    async def single(self):
        return self._record


class _FakeSession:
    def __init__(self, owner):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, **params):
        self._owner.queries.append((cypher, params))
        if self._owner.fail:
            raise RuntimeError("neo4j unavailable")
        if "DETACH DELETE" in cypher:
            self._owner.deleted_ids.append(params["memory_id"])
            return _FakeResult(None)
        # candidate-id query
        ids = self._owner.weak_ids[: params["max_delete"]]
        return _FakeResult({"ids": ids, "matched": len(self._owner.weak_ids)})


class _FakeDriver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _FakeSession(self._owner)


class _Fixture:
    def __init__(self, weak_ids, fail=False, vector_store=None):
        self.weak_ids = weak_ids
        self.fail = fail
        self.queries = []
        self.deleted_ids = []
        self.graph = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        self.graph.driver = _FakeDriver(self)
        self.graph.database = "neo4j"
        self.graph._vector_store = vector_store


# --- prune_weak_memories -------------------------------------------------------

async def test_dry_run_counts_but_deletes_nothing():
    vec = AsyncMock()
    fx = _Fixture(["m1", "m2", "m3"], vector_store=vec)
    out = await fx.graph.prune_weak_memories(dry_run=True)
    assert out == {"matched": 3, "deleted": 0, "dry_run": True,
                   "ids": ["m1", "m2", "m3"]}
    assert fx.deleted_ids == []
    vec.delete.assert_not_awaited()


async def test_live_deletes_graph_and_vector():
    vec = AsyncMock()
    fx = _Fixture(["m1", "m2"], vector_store=vec)
    out = await fx.graph.prune_weak_memories(dry_run=False)
    assert out["matched"] == 2 and out["deleted"] == 2
    assert fx.deleted_ids == ["m1", "m2"]
    assert vec.delete.await_count == 2
    vec.delete.assert_awaited_with(collection=Collection.MEMORIES, id="m2")


async def test_max_delete_cap():
    fx = _Fixture([f"m{i}" for i in range(10)])
    out = await fx.graph.prune_weak_memories(dry_run=False, max_delete=4)
    assert out["matched"] == 10          # full backlog reported
    assert out["deleted"] == 4           # but only the cap deleted
    assert len(fx.deleted_ids) == 4


async def test_neo4j_error_fails_closed():
    vec = AsyncMock()
    fx = _Fixture(["m1"], fail=True, vector_store=vec)
    with pytest.raises(RuntimeError):
        await fx.graph.prune_weak_memories(dry_run=False)
    assert fx.deleted_ids == []
    vec.delete.assert_not_awaited()


async def test_vector_failure_does_not_abort_pass():
    vec = AsyncMock()
    vec.delete.side_effect = RuntimeError("lance down")
    fx = _Fixture(["m1", "m2"], vector_store=vec)
    out = await fx.graph.prune_weak_memories(dry_run=False)
    assert out["deleted"] == 2  # graph deletes proceed; orphans swept later


# --- phase gate (COLONY_MEMORY_PRUNE_MODE) -----------------------------------

def _loop_with_graph(graph):
    from colony_sidecar.autonomy.loop import AutonomyLoop

    class _Reg:
        pass

    reg = _Reg()
    reg.graph = graph
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop._registry = reg
    loop._periodic_last = {}
    from colony_sidecar.autonomy.loop import LoopStats
    loop.stats = LoopStats()
    return loop


class _RecordingGraph:
    def __init__(self):
        self.calls = []

    async def prune_weak_memories(self, threshold=0.05, *, dry_run=False,
                                  max_delete=500):
        self.calls.append({"dry_run": dry_run, "max_delete": max_delete})
        return {"matched": 0, "deleted": 0, "dry_run": dry_run, "ids": []}


async def test_phase_default_is_shadow(monkeypatch):
    """Regression lock: with no flag set, the phase never live-deletes."""
    monkeypatch.delenv("COLONY_MEMORY_PRUNE_MODE", raising=False)
    graph = _RecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.calls == [{"dry_run": True, "max_delete": 500}]


async def test_phase_off_never_touches_graph(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY_PRUNE_MODE", "off")
    graph = _RecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.calls == []


async def test_phase_live_deletes(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY_PRUNE_MODE", "live")
    graph = _RecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.calls == [{"dry_run": False, "max_delete": 500}]


async def test_phase_unknown_mode_fails_safe_to_shadow(monkeypatch):
    monkeypatch.setenv("COLONY_MEMORY_PRUNE_MODE", "banana")
    graph = _RecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    assert graph.calls == [{"dry_run": True, "max_delete": 500}]


async def test_phase_runs_once_per_week_key(monkeypatch):
    monkeypatch.delenv("COLONY_MEMORY_PRUNE_MODE", raising=False)
    graph = _RecordingGraph()
    loop = _loop_with_graph(graph)
    await loop._phase_memory_pruning()
    await loop._phase_memory_pruning()
    assert len(graph.calls) == 1

"""Recall ranking pipeline (U8+): ANN oversample -> filter -> trim.

The regression lock here is that with COLONY_RECALL_OVERSAMPLE unset (or 1)
the vector path is byte-identical to the legacy behavior: the ANN fetch uses
exactly the requested limit, relevance == vector_score * effective_confidence,
and ordering is relevance-descending.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from colony_sidecar.intelligence.graph import client as client_mod


# --- fakes -------------------------------------------------------------------

@dataclass
class _Hit:
    id: str
    score: float


class _FakeVectorStore:
    """Records search kwargs; returns canned ANN hits (respecting limit)."""

    def __init__(self, hits):
        self.hits = hits
        self.search_calls = []

    async def search(self, collection, query_vector, limit, filter=None):
        self.search_calls.append({"limit": limit, "filter": filter})
        return self.hits[:limit]

    async def delete(self, collection, id):
        pass


class _FakeHydrationResult:
    def __init__(self, records):
        self._records = records

    def __aiter__(self):
        self._it = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return {"memory": dict(next(self._it))}
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
        self._owner.queries.append((cypher, params))
        if "WHERE m.id IN $ids" in cypher:
            ids = set(params["ids"])
            return _FakeHydrationResult(
                [m for m in self._owner.node_props if m["id"] in ids])
        return _FakeHydrationResult([])


class _FakeDriver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _FakeSession(self._owner)


class RecallFixture:
    """ColonyGraph wired to a fake ANN store + fake Neo4j hydration."""

    def __init__(self, hits, node_props):
        self.queries = []
        self.node_props = node_props
        self.vector = _FakeVectorStore(hits)
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        g.driver = _FakeDriver(self)
        g.database = "neo4j"
        g._vector_store = self.vector

        async def _embed(text):
            return [0.1, 0.2, 0.3]

        g._embed_fn = _embed
        self.graph = g

    async def recall(self, *args, **kwargs):
        out = await self.graph.recall(*args, **kwargs)
        # Drain fire-and-forget touch tasks so nothing leaks across tests
        for t in list(getattr(self.graph, "_bg_tasks", [])):
            try:
                await t
            except Exception:
                pass
        return out


def _node(mid, strength=1.0, confidence=None, state="inferred", **extra):
    props = {"id": mid, "content": f"content {mid}", "strength": strength,
             "epistemic_state": state}
    if confidence is not None:
        props["effective_confidence"] = confidence
    props.update(extra)
    return props


# --- regression lock: default path byte-identical -----------------------------

@pytest.mark.asyncio
async def test_default_fetch_uses_exact_limit(monkeypatch):
    monkeypatch.delenv("COLONY_RECALL_OVERSAMPLE", raising=False)
    fx = RecallFixture(
        hits=[_Hit("a", 0.9), _Hit("b", 0.8)],
        node_props=[_node("a", confidence=0.7), _node("b", confidence=0.6)],
    )
    out = await fx.recall("q", limit=7)
    assert fx.vector.search_calls == [{"limit": 7, "filter": None}]
    assert [m["id"] for m in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_default_relevance_formula_and_ordering(monkeypatch):
    """Legacy formula lock: relevance = vector_score * effective_confidence,
    sorted descending — strength does NOT participate in ranking."""
    monkeypatch.delenv("COLONY_RECALL_OVERSAMPLE", raising=False)
    monkeypatch.delenv("COLONY_RECALL_STRENGTH_RANKING", raising=False)
    fx = RecallFixture(
        hits=[_Hit("a", 0.9), _Hit("b", 0.8), _Hit("c", 0.5)],
        node_props=[
            _node("a", strength=0.2, confidence=0.5),   # 0.9*0.5 = 0.45
            _node("b", strength=1.0, confidence=0.9),   # 0.8*0.9 = 0.72
            _node("c", strength=1.0, confidence=0.95),  # 0.5*0.95 = 0.475
        ],
    )
    out = await fx.recall("q", limit=10)
    by_id = {m["id"]: m for m in out}
    assert by_id["a"]["relevance"] == pytest.approx(0.9 * 0.5)
    assert by_id["b"]["relevance"] == pytest.approx(0.8 * 0.9)
    assert by_id["c"]["relevance"] == pytest.approx(0.5 * 0.95)
    assert [m["id"] for m in out] == ["b", "c", "a"]


@pytest.mark.asyncio
async def test_default_filters_unchanged(monkeypatch):
    """Strength floor, terminal states, and min_confidence still drop hits."""
    monkeypatch.delenv("COLONY_RECALL_OVERSAMPLE", raising=False)
    fx = RecallFixture(
        hits=[_Hit("weak", 0.9), _Hit("stale", 0.9),
              _Hit("lowconf", 0.9), _Hit("ok", 0.9)],
        node_props=[
            _node("weak", strength=0.05, confidence=0.9),
            _node("stale", strength=1.0, confidence=0.9, state="stale"),
            _node("lowconf", strength=1.0, confidence=0.05),
            _node("ok", strength=1.0, confidence=0.9),
        ],
    )
    out = await fx.recall("q", limit=10)
    assert [m["id"] for m in out] == ["ok"]


# --- oversample behavior -------------------------------------------------------

@pytest.mark.asyncio
async def test_oversample_widens_fetch_and_trims_to_limit(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    hits = [_Hit(f"m{i}", 0.9 - i * 0.01) for i in range(6)]
    nodes = []
    for i in range(6):
        # first two hits are junk that the filters drop
        if i < 2:
            nodes.append(_node(f"m{i}", strength=0.01, confidence=0.9))
        else:
            nodes.append(_node(f"m{i}", strength=1.0, confidence=0.9))
    fx = RecallFixture(hits=hits, node_props=nodes)
    out = await fx.recall("q", limit=2)
    assert fx.vector.search_calls == [{"limit": 6, "filter": None}]
    # junk filtered, survivors trimmed back to the requested limit
    assert [m["id"] for m in out] == ["m2", "m3"]


@pytest.mark.asyncio
async def test_oversample_fetch_capped_at_100(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "50")
    fx = RecallFixture(hits=[], node_props=[])
    await fx.recall("q", limit=10)
    assert fx.vector.search_calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_oversample_never_shrinks_below_limit(monkeypatch):
    """A caller limit above the cap must not be reduced by the cap."""
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "2")
    fx = RecallFixture(hits=[], node_props=[])
    await fx.recall("q", limit=150)
    assert fx.vector.search_calls[0]["limit"] == 150


@pytest.mark.asyncio
async def test_oversample_invalid_value_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "banana")
    fx = RecallFixture(hits=[], node_props=[])
    await fx.recall("q", limit=9)
    assert fx.vector.search_calls[0]["limit"] == 9

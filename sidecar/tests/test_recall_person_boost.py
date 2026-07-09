"""Person-scoped recall boost (U17): COLONY_RECALL_PERSON_BOOST.

person_id is a BOOST multiplier on memories ABOUT that person, never a
filter — cross-person memories must remain reachable. The regression lock is
that with the boost unset (default 0.0), passing a person_id changes nothing:
the hydration cypher is the exact legacy text and relevance is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from colony_sidecar.intelligence.graph import client as client_mod


# --- fakes (mirrors test_recall_ranking.py, plus ABOUT support) --------------

@dataclass
class _Hit:
    id: str
    score: float


class _FakeVectorStore:
    def __init__(self, hits):
        self.hits = hits

    async def search(self, collection, query_vector, limit, filter=None):
        return self.hits[:limit]


class _FakeHydrationResult:
    def __init__(self, records, about_ids=None, with_about=False):
        self._records = records
        self._about = about_ids or set()
        self._with_about = with_about

    def __aiter__(self):
        self._it = iter(self._records)
        return self

    async def __anext__(self):
        try:
            props = dict(next(self._it))
        except StopIteration:
            raise StopAsyncIteration
        rec = {"memory": props}
        if self._with_about:
            rec["about_person"] = props["id"] in self._about
        return rec


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
                [m for m in self._owner.node_props if m["id"] in ids],
                about_ids=self._owner.about_ids,
                with_about="about_person" in cypher)
        return _FakeHydrationResult([])


class _FakeDriver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _FakeSession(self._owner)


class Fixture:
    def __init__(self, hits, node_props, about_ids=()):
        self.queries = []
        self.node_props = node_props
        self.about_ids = set(about_ids)
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        g.driver = _FakeDriver(self)
        g.database = "neo4j"
        g._vector_store = _FakeVectorStore(hits)

        async def _embed(text):
            return [0.1, 0.2, 0.3]

        g._embed_fn = _embed
        self.graph = g

    async def recall(self, *args, **kwargs):
        out = await self.graph.recall(*args, **kwargs)
        for t in list(getattr(self.graph, "_bg_tasks", [])):
            try:
                await t
            except Exception:
                pass
        return out


def _node(mid, confidence=1.0):
    return {"id": mid, "content": f"content {mid}", "strength": 1.0,
            "epistemic_state": "inferred", "effective_confidence": confidence}


_HITS = [_Hit("m1", 0.9), _Hit("m2", 0.8)]
_NODES = [_node("m1"), _node("m2")]


@pytest.mark.asyncio
async def test_default_boost_ignores_person_id(monkeypatch):
    """Regression lock: boost unset -> legacy cypher, relevance untouched."""
    monkeypatch.delenv("COLONY_RECALL_PERSON_BOOST", raising=False)
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2, person_id="cid-1")
    assert [m["id"] for m in out] == ["m1", "m2"]
    assert out[0]["relevance"] == pytest.approx(0.9)
    assert out[1]["relevance"] == pytest.approx(0.8)
    hydration = [q for q, _ in fx.queries if "WHERE m.id IN $ids" in q]
    assert all("ABOUT" not in q for q in hydration)   # exact legacy query


@pytest.mark.asyncio
async def test_boost_promotes_about_memories(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2, person_id="cid-1")
    # m2: 0.8 * 1.5 = 1.2 outranks m1: 0.9
    assert [m["id"] for m in out] == ["m2", "m1"]
    assert out[0]["relevance"] == pytest.approx(1.2)
    hydration = [(q, p) for q, p in fx.queries if "WHERE m.id IN $ids" in q]
    assert "ABOUT" in hydration[0][0]
    assert hydration[0][1]["person_id"] == "cid-1"


@pytest.mark.asyncio
async def test_boost_never_filters_cross_person(monkeypatch):
    """A memory NOT about the person is still returned (boost, not filter)."""
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids=set())     # nothing about cid-1
    out = await fx.recall("q", limit=2, person_id="cid-1")
    assert [m["id"] for m in out] == ["m1", "m2"]    # all reachable, unboosted
    assert out[0]["relevance"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_boost_without_person_id_is_legacy(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2)               # no person_id supplied
    assert [m["id"] for m in out] == ["m1", "m2"]
    hydration = [q for q, _ in fx.queries if "WHERE m.id IN $ids" in q]
    assert all("ABOUT" not in q for q in hydration)

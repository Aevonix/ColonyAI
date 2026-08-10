"""Phase 1 hard recipient/viewer candidate boundary.

Audit reproduction: U17 deliberately made ``person_id`` a ranking boost and
kept cross-person memories reachable. That is useful personalization but not
an information boundary. The corrected contract filters graph candidates by
the exact person before any relevance/reranker step and never retries a scoped
miss as a global query.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from colony_sidecar.intelligence.graph import client as client_mod
from colony_sidecar.research.gatherer import GraphGatherer


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
            if "person_id" in params:
                ids &= self._owner.about_ids
            excluded = set(params.get("exclude_source_uris") or ())
            metadata_markers = set(
                params.get("exclude_metadata_markers") or ())
            return _FakeHydrationResult(
                [m for m in self._owner.node_props
                 if m["id"] in ids
                 and str(m.get("source_uri") or "") not in excluded
                 and not any(
                     marker in str(m.get("metadata") or "").lower()
                     for marker in metadata_markers
                 )],
                about_ids=self._owner.about_ids,
                with_about="about_person" in cypher)
        rows = self._owner.node_props
        if "person_id" in params:
            rows = [m for m in rows if m["id"] in self._owner.about_ids]
        excluded = set(params.get("exclude_source_uris") or ())
        metadata_markers = set(
            params.get("exclude_metadata_markers") or ())
        rows = [m for m in rows
                if str(m.get("source_uri") or "") not in excluded
                and not any(
                    marker in str(m.get("metadata") or "").lower()
                    for marker in metadata_markers
                )]
        return _FakeHydrationResult(rows)


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


def _node(mid, confidence=1.0, source_uri="", metadata=None):
    return {"id": mid, "content": f"content {mid}", "strength": 1.0,
            "epistemic_state": "inferred", "effective_confidence": confidence,
            "source_uri": source_uri, "metadata": metadata or "{}"}


_HITS = [_Hit("m1", 0.9), _Hit("m2", 0.8)]
_NODES = [_node("m1"), _node("m2")]


@pytest.mark.asyncio
async def test_explicit_person_is_a_hard_vector_candidate_filter(monkeypatch):
    """A cross-person ANN hit is not hydrated, ranked, or returned."""
    monkeypatch.delenv("COLONY_RECALL_PERSON_BOOST", raising=False)
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2, person_id="cid-1")
    assert [m["id"] for m in out] == ["m2"]
    assert out[0]["relevance"] == pytest.approx(0.8)
    hydration = [(q, p) for q, p in fx.queries if "WHERE m.id IN $ids" in q]
    assert "[:ABOUT]->(:Person {id: $person_id})" in hydration[0][0]
    assert "OPTIONAL MATCH (m)-[:ABOUT]" not in hydration[0][0]
    assert hydration[0][1]["person_id"] == "cid-1"


@pytest.mark.asyncio
async def test_legacy_boost_flag_cannot_reopen_candidate_scope(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2, person_id="cid-1")
    assert [m["id"] for m in out] == ["m2"]
    assert out[0]["relevance"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_scoped_miss_stays_empty_without_global_fallback(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids=set())     # nothing about cid-1
    out = await fx.recall("q", limit=2, person_id="cid-1")
    assert out == []
    assert all("person_id" in params for _, params in fx.queries)


@pytest.mark.asyncio
async def test_unscoped_internal_recall_keeps_legacy_global_behavior(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_PERSON_BOOST", "0.5")
    fx = Fixture(_HITS, _NODES, about_ids={"m2"})
    out = await fx.recall("q", limit=2)               # no person_id supplied
    assert [m["id"] for m in out] == ["m1", "m2"]
    hydration = [q for q, _ in fx.queries if "WHERE m.id IN $ids" in q]
    assert all("ABOUT" not in q for q in hydration)


@pytest.mark.asyncio
async def test_graph_only_fallback_has_the_same_hard_boundary(monkeypatch):
    monkeypatch.delenv("COLONY_RECALL_PERSON_BOOST", raising=False)
    fx = Fixture([], _NODES, about_ids={"m2"})
    fx.graph._vector_store = None
    out = await fx.recall("content", limit=2, person_id="cid-1")
    assert [m["id"] for m in out] == ["m2"]
    cypher, params = fx.queries[0]
    assert "MATCH (m:Memory)-[:ABOUT]->(:Person {id: $person_id})" in cypher
    assert params["person_id"] == "cid-1"


@pytest.mark.asyncio
async def test_source_exclusion_precedes_relevance_ranking_and_fallback():
    nodes = [
        _node("mirror", source_uri="tom:shared_fact"),
        _node("ordinary", source_uri="session:one"),
    ]
    fx = Fixture(
        [_Hit("mirror", 0.99), _Hit("ordinary", 0.5)],
        nodes,
        about_ids={"mirror", "ordinary"},
    )
    out = await fx.recall(
        "content", limit=1, person_id="cid-1",
        exclude_source_uris=["tom:shared_fact"],
    )
    assert [memory["id"] for memory in out] == ["ordinary"]
    hydration = [
        (query, params) for query, params in fx.queries
        if "WHERE m.id IN $ids" in query
    ]
    assert "$exclude_source_uris" in hydration[0][0]
    assert hydration[0][1]["exclude_source_uris"] == (
        "tom:shared_fact",)

    fx.graph._vector_store = None
    fx.queries.clear()
    fallback = await fx.recall(
        "content", limit=2, person_id="cid-1",
        exclude_source_uris=["tom:shared_fact"],
    )
    assert [memory["id"] for memory in fallback] == ["ordinary"]


@pytest.mark.asyncio
async def test_graph_wide_source_exclusion_protects_untyped_recall_callers():
    """A runtime policy must cover tool/internal callers that pass no filter."""
    nodes = [
        _node("mirror", source_uri="tom:shared_fact"),
        _node("legacy", metadata="{'shared_fact': True}"),
        _node("ordinary", source_uri="session:one"),
    ]
    fx = Fixture(
        [_Hit("mirror", 0.99), _Hit("legacy", 0.98),
         _Hit("ordinary", 0.5)],
        nodes,
        about_ids={"mirror", "legacy", "ordinary"},
    )
    fx.graph.set_recall_source_exclusions(
        ["tom:shared_fact"], legacy_metadata_markers=["shared_fact"])

    # Deliberately omit exclude_source_uris, exactly as the model-facing
    # colony_memory_search and internal synthesis consumers do.
    out = await fx.recall("content", limit=1, person_id="cid-1")
    assert [memory["id"] for memory in out] == ["ordinary"]
    hydration = [
        params for query, params in fx.queries
        if "WHERE m.id IN $ids" in query
    ]
    assert hydration[0]["exclude_source_uris"] == ("tom:shared_fact",)
    assert hydration[0]["exclude_metadata_markers"] == ("shared_fact",)

    # A caller may add a boundary but cannot erase the graph-wide one.
    fx.queries.clear()
    out = await fx.recall(
        "content", limit=2, person_id="cid-1",
        exclude_source_uris=["session:one"],
    )
    assert out == []
    hydration = [
        params for query, params in fx.queries
        if "WHERE m.id IN $ids" in query
    ]
    assert hydration[0]["exclude_source_uris"] == (
        "session:one", "tom:shared_fact")


@pytest.mark.asyncio
async def test_direct_memory_read_uses_the_same_exact_about_boundary():
    fx = Fixture([], _NODES, about_ids={"m2"})
    out = await fx.graph.read_memories(
        person_id="cid-1", memory_id="m2", limit=5,
    )
    assert [m["id"] for m in out] == ["m2"]
    cypher, params = fx.queries[0]
    assert "MATCH (m:Memory)-[:ABOUT]->(:Person {id: $person_id})" in cypher
    assert params == {"person_id": "cid-1", "memory_id": "m2", "limit": 5}
    assert "MATCH (m:Memory) WHERE" not in cypher


@pytest.mark.asyncio
async def test_graph_wide_policy_protects_direct_reads_and_vector_content():
    nodes = [
        _node("mirror", source_uri="tom:shared_fact"),
        _node("legacy", metadata="{'shared_fact': True}"),
        _node("ordinary", source_uri="session:one"),
    ]
    fx = Fixture([], nodes, about_ids={"mirror", "legacy", "ordinary"})
    fx.graph.set_recall_source_exclusions(
        ["tom:shared_fact"], legacy_metadata_markers=["shared_fact"])

    direct = await fx.graph.read_memories(person_id="cid-1", limit=10)
    assert [memory["id"] for memory in direct] == ["ordinary"]
    _, params = fx.queries[-1]
    assert params["exclude_source_uris"] == ("tom:shared_fact",)
    assert params["exclude_metadata_markers"] == ("shared_fact",)

    fx.queries.clear()
    vector_rows = [
        {"id": "mirror", "text": "new mirror", "metadata": {
            "source_uri": "tom:shared_fact"}},
        {"id": "legacy", "text": "legacy mirror", "metadata": {}},
        {"id": "ordinary", "text": "ordinary memory", "metadata": {}},
        # An orphan text row is ambiguous under P8 and fails closed.
        {"id": "orphan-text", "text": "ambiguous text", "metadata": {}},
        # A non-graph image/vector remains available.
        {"id": "orphan-image", "text": "image caption", "metadata": {
            "modality": "image"}},
    ]
    filtered = await fx.graph.filter_memory_vector_results(vector_rows)
    assert [row["id"] for row in filtered] == ["ordinary", "orphan-image"]


@pytest.mark.asyncio
async def test_research_gatherer_borrows_policy_graph_without_closing_it():
    nodes = [
        _node("mirror", source_uri="tom:shared_fact"),
        _node("ordinary", source_uri="session:one"),
    ]
    fx = Fixture(
        [_Hit("mirror", 0.99), _Hit("ordinary", 0.5)], nodes)
    fx.graph.set_recall_source_exclusions(["tom:shared_fact"])
    fx.graph.close = AsyncMock(
        side_effect=AssertionError("borrowed graph must not close"))

    gathered = await GraphGatherer(graph=fx.graph).gather("content")
    assert [item.content for item in gathered] == ["content ordinary"]
    fx.graph.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_research_graph_gather_fails_empty_without_governed_p8_graph():
    gathered = await GraphGatherer(
        graph=None, allow_fallback_graph=False,
    ).gather("content")
    assert gathered == []


@pytest.mark.asyncio
async def test_default_research_fallback_still_owns_and_closes_graph(
    monkeypatch,
):
    class OwnedGraph:
        def __init__(self):
            self.close = AsyncMock()

        async def recall(self, _query, limit):
            assert limit == 20
            return [{
                "id": "ordinary", "content": "ordinary research memory",
                "strength": 0.7,
            }]

    owned = OwnedGraph()
    monkeypatch.setattr(
        client_mod, "ColonyGraph", lambda: owned)
    gathered = await GraphGatherer().gather("ordinary")
    assert [item.content for item in gathered] == [
        "ordinary research memory"]
    owned.close.assert_awaited_once()

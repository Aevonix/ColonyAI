"""H2.3 — causal contradiction detection + causal staleness decay.

Contradictions (opposing WM_CAUSES/WM_ENABLES vs WM_BLOCKS/WM_INHIBITS over
the same ordered pair) become conflict rows + review initiatives and are
NEVER auto-resolved, at any mode or trust stage. Stale causal edges
(unsupported past COLONY_CAUSAL_TTL_DAYS, default 120) lose 0.05 confidence
per run, floored at 0.2, in live/supervised belief mode only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.beliefs.engine import BeliefEngine
from colony_sidecar.beliefs.store import BeliefStore
from colony_sidecar.world_model import causal_maintenance as cm
from colony_sidecar.world_model.relationships import WorldRelationship


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _edge(eid, src, tgt, rel, conf=0.5, support_days_ago=1.0, valid_to=None,
          props=None):
    p = {"last_support_at": _iso(support_days_ago)}
    p.update(props or {})
    return WorldRelationship(id=eid, source_id=src, target_id=tgt,
                             relationship_type=rel, confidence=conf,
                             valid_to=valid_to, properties=p)


class FakeCausalWorld:
    def __init__(self, edges):
        self.edges = list(edges)
        self.upserts = []

    async def query_relationships(self, source_id=None, target_id=None,
                                  relationship_type=None,
                                  min_confidence=0.0, limit=500):
        return [e for e in self.edges
                if (relationship_type is None
                    or e.relationship_type == relationship_type)
                and (source_id is None or e.source_id == source_id)
                and (target_id is None or e.target_id == target_id)]

    async def upsert_relationship(self, rel):
        self.upserts.append(rel)
        return rel

    async def find_entities(self, query="", min_confidence=0.0, limit=500,
                            entity_type=None):
        return []


class FakeInitStore:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="init-x")


# ---------------------------------------------------------------------------
# Pure detection
# ---------------------------------------------------------------------------

class TestOpposingPairs:
    def test_positive_vs_negative_same_pair(self):
        pos = _edge("wr-1", "a", "b", "WM_CAUSES")
        neg = _edge("wr-2", "a", "b", "WM_INHIBITS")
        assert cm.opposing_pairs([pos, neg]) == [(pos, neg)]

    def test_all_four_type_combinations(self):
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES"),
                 _edge("wr-2", "a", "b", "WM_ENABLES"),
                 _edge("wr-3", "a", "b", "WM_BLOCKS"),
                 _edge("wr-4", "a", "b", "WM_INHIBITS")]
        assert len(cm.opposing_pairs(edges)) == 4  # 2 pos x 2 neg

    def test_different_pairs_do_not_conflict(self):
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES"),
                 _edge("wr-2", "b", "a", "WM_BLOCKS"),   # reversed direction
                 _edge("wr-3", "a", "c", "WM_INHIBITS")]
        assert cm.opposing_pairs(edges) == []

    def test_inactive_edge_claims_nothing(self):
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES"),
                 _edge("wr-2", "a", "b", "WM_BLOCKS", valid_to=_iso(1))]
        assert cm.opposing_pairs(edges) == []

    def test_same_polarity_is_not_a_conflict(self):
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES"),
                 _edge("wr-2", "a", "b", "WM_ENABLES")]
        assert cm.opposing_pairs(edges) == []


class TestStaleness:
    def test_ttl_default_120(self, monkeypatch):
        monkeypatch.delenv("COLONY_CAUSAL_TTL_DAYS", raising=False)
        assert cm.causal_ttl_days() == 120.0
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES", support_days_ago=100),
                 _edge("wr-2", "a", "c", "WM_CAUSES", support_days_ago=150)]
        assert [e.id for e in cm.stale_causal_edges(edges)] == ["wr-2"]

    def test_ttl_env_override(self, monkeypatch):
        monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "10")
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES", support_days_ago=15)]
        assert len(cm.stale_causal_edges(edges)) == 1

    def test_floor_edges_and_inactive_excluded(self, monkeypatch):
        monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "10")
        edges = [_edge("wr-1", "a", "b", "WM_CAUSES", conf=0.2,
                       support_days_ago=400),
                 _edge("wr-2", "a", "c", "WM_CAUSES", conf=0.5,
                       support_days_ago=400, valid_to=_iso(1))]
        assert cm.stale_causal_edges(edges) == []

    def test_undateable_edge_never_stale(self, monkeypatch):
        monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "10")
        e = WorldRelationship(id="wr-x", source_id="a", target_id="b",
                              relationship_type="WM_CAUSES", confidence=0.5)
        assert cm.stale_causal_edges([e]) == []


# ---------------------------------------------------------------------------
# Engine pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contradiction_conflict_row_and_review_never_resolved(
        monkeypatch):
    """Live mode, opposing edges: conflict row lands in status 'review',
    a review initiative fires, and NOTHING auto-resolves — the edges are
    untouched even at full live."""
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    monkeypatch.delenv("COLONY_CAUSAL_TTL_DAYS", raising=False)
    pos = _edge("wr-p", "we-a", "we-b", "WM_CAUSES", conf=0.6,
                props={"evidence": "a caused b"})
    neg = _edge("wr-n", "we-a", "we-b", "WM_BLOCKS", conf=0.5,
                props={"evidence": "a prevented b"})
    world = FakeCausalWorld([pos, neg])
    inits = FakeInitStore()
    store = BeliefStore()
    engine = BeliefEngine(store, world_store=world, initiative_store=inits)
    report = await engine.run()
    assert report["causal_conflicts"] == 1
    assert report["pass_errors"] == 0
    rows = store.conflicts(status="review")
    assert len(rows) == 1
    assert rows[0]["scope"] == "world_causal"
    assert rows[0]["value_a"] == "WM_CAUSES"
    assert rows[0]["value_b"] == "WM_BLOCKS"
    assert len(inits.created) == 1
    assert inits.created[0]["dedup_key"].startswith("causal_conflict:")
    assert "never auto-resolved" in inits.created[0]["description"]
    # never auto-resolved: no edge writes, no resolved rows
    assert world.upserts == []
    assert store.conflicts(status="resolved") == []


@pytest.mark.asyncio
async def test_contradiction_detected_in_shadow_too(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    world = FakeCausalWorld([_edge("wr-p", "a", "b", "WM_ENABLES"),
                             _edge("wr-n", "a", "b", "WM_INHIBITS")])
    store = BeliefStore()
    engine = BeliefEngine(store, world_store=world,
                          initiative_store=FakeInitStore())
    report = await engine.run()
    assert report["causal_conflicts"] == 1
    assert store.conflicts(status="review")
    assert world.upserts == []


@pytest.mark.asyncio
async def test_stale_causal_decay_live(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "30")
    original_support = _iso(90)
    stale = WorldRelationship(
        id="wr-s", source_id="a", target_id="b",
        relationship_type="WM_CAUSES", confidence=0.5,
        properties={"last_support_at": original_support})
    fresh = _edge("wr-f", "a", "c", "WM_CAUSES", conf=0.5,
                  support_days_ago=1)
    world = FakeCausalWorld([stale, fresh])
    engine = BeliefEngine(BeliefStore(), world_store=world)
    report = await engine.run()
    assert report["causal_decayed"] == 1
    assert stale.confidence == pytest.approx(0.45)
    assert fresh.confidence == 0.5
    assert [r.id for r in world.upserts] == ["wr-s"]
    # the decay write must NOT reset the staleness clock
    assert stale.properties["last_support_at"] == original_support
    assert stale.properties["stale_decays"] == 1


@pytest.mark.asyncio
async def test_stale_causal_decay_floors_at_02(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "30")
    e = _edge("wr-s", "a", "b", "WM_CAUSES", conf=0.22,
              support_days_ago=90)
    world = FakeCausalWorld([e])
    await BeliefEngine(BeliefStore(), world_store=world).run()
    assert e.confidence == pytest.approx(0.2)
    # at the floor it never decays further (and is never deleted)
    world2 = FakeCausalWorld([e])
    report = await BeliefEngine(BeliefStore(), world_store=world2).run()
    assert report["causal_decayed"] == 0
    assert world2.upserts == []


@pytest.mark.asyncio
async def test_stale_causal_decay_shadow_does_not_mutate(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    monkeypatch.setenv("COLONY_CAUSAL_TTL_DAYS", "30")
    e = _edge("wr-s", "a", "b", "WM_CAUSES", conf=0.5, support_days_ago=90)
    world = FakeCausalWorld([e])
    report = await BeliefEngine(BeliefStore(), world_store=world).run()
    assert report["causal_decayed"] == 0
    assert e.confidence == 0.5 and world.upserts == []


@pytest.mark.asyncio
async def test_extractor_stamps_last_support_at(monkeypatch):
    """Create and corroborate both stamp last_support_at, which is what
    keeps a living edge off the decay path."""
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    world = FakeCausalWorld([])
    x = WorldLLMExtractor(world)
    x._seen_rels = set()
    report = {"causal": [], "causal_corroborated": [], "causal_skipped": 0}
    await x._upsert_causal("we-a", "WM_CAUSES", "we-b", "a caused b",
                           0.9, "live", report)
    assert len(world.upserts) == 1
    created = world.upserts[0]
    assert created.properties.get("last_support_at")
    # corroboration restamps
    created.properties["last_support_at"] = "2000-01-01T00:00:00+00:00"
    world.edges = [created]
    x._seen_rels = set()
    await x._upsert_causal("we-a", "WM_CAUSES", "we-b", "a caused b again",
                           0.9, "live", report)
    assert created.properties["last_support_at"] != \
        "2000-01-01T00:00:00+00:00"

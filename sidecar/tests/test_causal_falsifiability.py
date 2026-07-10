"""H2.6 — causal falsifiability: every LIVE causal write/boost stakes a
world-causal:<edge_id> prediction (horizon +30d) that the claim survives at
its creation confidence, unopposed. Hit = persisted unopposed; miss =
decayed/deleted/opposed; None = world store unseen. Rides
COLONY_EXPECTATIONS and registers beside the U24 world resolvers.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import BaseEntity
from colony_sidecar.world_model.expectation_resolvers import (
    CAUSAL_PREFIX, register_world_resolvers, resolve_causal_edge,
)
from colony_sidecar.world_model.relationships import WorldRelationship
from colony_sidecar.world_model.store import WorldModelStore


async def _store_with(*rels):
    s = WorldModelStore(WorldModelConfig(backend="sqlite",
                                         sqlite_path=":memory:"))
    await s.connect()
    for eid in ("we-a", "we-b"):
        await s.upsert_entity(BaseEntity(id=eid, name=eid,
                                         entity_type="concept",
                                         confidence=0.8))
    for r in rels:
        await s.upsert_relationship(r)
    return s


def _edge(eid="wr-c", rel="WM_CAUSES", conf=0.5, valid_to=None):
    return WorldRelationship(id=eid, source_id="we-a", target_id="we-b",
                             relationship_type=rel, confidence=conf,
                             valid_to=valid_to)


def _pred(conf_floor=0.5, edge_id="wr-c", rel="WM_CAUSES"):
    return SimpleNamespace(
        subject=f"world-causal:{edge_id}",
        detail={"edge_id": edge_id, "source_id": "we-a",
                "target_id": "we-b", "relationship_type": rel,
                "confidence_at_creation": conf_floor})


def _with_store(monkeypatch, store):
    from colony_sidecar.api.routers import host as host_mod
    monkeypatch.setattr(host_mod, "_world_store", store)


# ---------------------------------------------------------------------------
# Resolver verdicts
# ---------------------------------------------------------------------------

def test_hit_when_edge_persists_unopposed(monkeypatch):
    async def run():
        return await _store_with(_edge(conf=0.55))
    _with_store(monkeypatch, asyncio.run(run()))
    assert resolve_causal_edge(_pred(conf_floor=0.5)) is True


def test_miss_when_decayed_below_creation_confidence(monkeypatch):
    async def run():
        return await _store_with(_edge(conf=0.45))
    _with_store(monkeypatch, asyncio.run(run()))
    assert resolve_causal_edge(_pred(conf_floor=0.5)) is False


def test_miss_when_deleted(monkeypatch):
    async def run():
        return await _store_with()  # no edges at all
    _with_store(monkeypatch, asyncio.run(run()))
    assert resolve_causal_edge(_pred()) is False


def test_miss_when_opposed(monkeypatch):
    async def run():
        return await _store_with(
            _edge(conf=0.6),
            WorldRelationship(id="wr-opp", source_id="we-a",
                              target_id="we-b",
                              relationship_type="WM_BLOCKS",
                              confidence=0.4))
    _with_store(monkeypatch, asyncio.run(run()))
    assert resolve_causal_edge(_pred(conf_floor=0.5)) is False


def test_none_when_store_unseen(monkeypatch):
    _with_store(monkeypatch, None)
    assert resolve_causal_edge(_pred()) is None


def test_none_when_detail_malformed(monkeypatch):
    async def run():
        return await _store_with(_edge())
    _with_store(monkeypatch, asyncio.run(run()))
    p = SimpleNamespace(subject="world-causal:wr-c", detail={})
    assert resolve_causal_edge(p) is None


def test_registered_beside_u24_resolvers():
    class FakeEngine:
        def __init__(self):
            self.registered = {}

        def register_resolver(self, prefix, fn):
            self.registered[prefix] = fn

    eng = FakeEngine()
    register_world_resolvers(eng)
    assert CAUSAL_PREFIX in eng.registered
    assert "world-relationship:" in eng.registered
    assert "world-property:" in eng.registered


# ---------------------------------------------------------------------------
# Creation side (rides COLONY_EXPECTATIONS)
# ---------------------------------------------------------------------------

def _extractor_env(monkeypatch, engine):
    from colony_sidecar.api.routers import host as host_mod
    monkeypatch.setattr(host_mod, "_expectations", engine)


def _expectation_engine(tmp_path):
    from colony_sidecar.self_model.expectations import (
        ExpectationEngine, ExpectationStore,
    )
    return ExpectationEngine(
        ExpectationStore(db_path=str(tmp_path / "exp.db")))


@pytest.mark.asyncio
async def test_live_causal_write_creates_prediction(monkeypatch, tmp_path):
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    monkeypatch.setenv("COLONY_EXPECTATIONS", "on")
    eng = _expectation_engine(tmp_path)
    _extractor_env(monkeypatch, eng)
    store = await _store_with()
    x = WorldLLMExtractor(store)
    x._seen_rels = set()
    report = {"causal": [], "causal_corroborated": [], "causal_skipped": 0}
    await x._upsert_causal("we-a", "WM_CAUSES", "we-b", "a caused b",
                           0.9, "live", report)
    pend = eng.store.pending()
    assert len(pend) == 1
    p = pend[0]
    assert p.subject.startswith("world-causal:")
    assert p.domain == "world_causal"
    assert p.confidence == pytest.approx(0.5)  # create ceiling
    assert p.detail["confidence_at_creation"] == pytest.approx(0.5)
    assert p.detail["relationship_type"] == "WM_CAUSES"
    # horizon ~ +30d
    assert abs(p.horizon - (time.time() + 30 * 86400)) < 3600
    # a boost while one prediction is pending dedups (no second row)
    x._seen_rels = set()
    await x._upsert_causal("we-a", "WM_CAUSES", "we-b", "again",
                           0.9, "live", report)
    assert len(eng.store.pending()) == 1


@pytest.mark.asyncio
async def test_expectations_off_no_prediction_and_write_unaffected(
        monkeypatch, tmp_path):
    """Regression lock: with COLONY_EXPECTATIONS off the causal write path
    behaves exactly as before — edge written, nothing predicted."""
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    monkeypatch.delenv("COLONY_EXPECTATIONS", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)
    eng = _expectation_engine(tmp_path)
    _extractor_env(monkeypatch, eng)
    store = await _store_with()
    x = WorldLLMExtractor(store)
    x._seen_rels = set()
    report = {"causal": [], "causal_corroborated": [], "causal_skipped": 0}
    await x._upsert_causal("we-a", "WM_CAUSES", "we-b", "a caused b",
                           0.9, "live", report)
    edges = await store.query_relationships(
        source_id="we-a", target_id="we-b",
        relationship_type="WM_CAUSES", min_confidence=0.0, limit=10)
    assert len(edges) == 1
    assert eng.store.pending() == []


@pytest.mark.asyncio
async def test_prediction_resolves_end_to_end(monkeypatch, tmp_path):
    """Due world-causal prediction resolves through the engine: hit while
    the edge holds, miss after decay below the creation confidence."""
    monkeypatch.setenv("COLONY_EXPECTATIONS", "on")
    eng = _expectation_engine(tmp_path)
    register_world_resolvers(eng)
    store = await _store_with(_edge(conf=0.5))
    _with_store(monkeypatch, store)
    eng.store.create(
        subject="world-causal:wr-c", domain="world_causal",
        expectation="edge holds", confidence=0.5,
        horizon=time.time() - 10, source="test",
        dedup_key="world-causal:wr-c",
        detail={"edge_id": "wr-c", "source_id": "we-a",
                "target_id": "we-b", "relationship_type": "WM_CAUSES",
                "confidence_at_creation": 0.5})
    counts = eng.check()
    assert counts["hit"] == 1 and counts["miss"] == 0

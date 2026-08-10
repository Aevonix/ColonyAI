"""H1.5 — world_model as the second supervised-rung consumer.

Only when "world_model" is enrolled in COLONY_SUPERVISED_LIVE_DOMAINS does
the LLM extractor's mode graduate through the trust engine; otherwise the
env mode is returned untouched (regression lock). Supervised permits only
the reversible ops (entity_upsert / alias_merge / edge_corroborate), capped
at COLONY_WORLD_SUPERVISED_MAX_WRITES per run, never creates edges, never
touches causal edge types, and records real outcomes only when writes > 0.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine,
)
from colony_sidecar.self_model.supervised import reversible
from colony_sidecar.world_model.llm_extract import (
    WorldLLMExtractor, world_supervised_max_writes,
)
from colony_sidecar.world_model.relationships import WorldRelationship


class FakeWorld:
    def __init__(self, rels=()):
        self.rels = list(rels)
        self.entity_upserts = []
        self.aliases = []
        self.rel_upserts = []

    async def upsert_entity(self, e):
        self.entity_upserts.append(e)
        return e

    async def add_entity_alias(self, entity_id, alias):
        self.aliases.append((entity_id, alias))

    async def query_relationships(self, source_id=None, target_id=None,
                                  relationship_type=None,
                                  min_confidence=0.0, limit=50):
        return [r for r in self.rels
                if (relationship_type is None
                    or r.relationship_type == relationship_type)
                and (source_id is None or r.source_id == source_id)
                and (target_id is None or r.target_id == target_id)]

    async def upsert_relationship(self, rel):
        self.rel_upserts.append(rel)
        return rel


def _self_model(stage="ask_first"):
    store = CompetenceStore()
    trust = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=trust)
    trust.set_stage("world_model", stage, notify=False)
    return sm


def _report():
    return {"mode": "supervised", "batches": 0, "created": [], "merged": [],
            "relationships": [], "skipped": 0, "causal": [],
            "causal_corroborated": [], "causal_skipped": 0,
            "writes": 0, "supervised_capped": 0}


# ---------------------------------------------------------------------------
# Contract + mode resolution
# ---------------------------------------------------------------------------

def test_reversible_contract_pins_the_three_ops():
    assert reversible("world_model", "entity_upsert")
    assert reversible("world_model", "alias_merge")
    assert reversible("world_model", "edge_corroborate")
    assert not reversible("world_model", "edge_create")
    assert not reversible("world_model", "entity_delete")


def test_effective_mode_untouched_when_domain_not_enrolled(monkeypatch):
    """Regression lock: without world_model in the domains list the env
    mode passes through raw, whatever the trust stage says."""
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "shadow")
    monkeypatch.delenv("COLONY_SUPERVISED_LIVE_DOMAINS", raising=False)
    for stage in ("shadow", "ask_first", "act_first"):
        x = WorldLLMExtractor(FakeWorld(), self_model=_self_model(stage))
        assert x._effective_mode() == "shadow", stage


def test_effective_mode_supervised_when_enrolled_at_ask_first(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "shadow")
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "world_model")
    x = WorldLLMExtractor(FakeWorld(), self_model=_self_model("ask_first"))
    assert x._effective_mode() == "supervised"
    x2 = WorldLLMExtractor(FakeWorld(), self_model=_self_model("shadow"))
    assert x2._effective_mode() == "shadow"


def test_env_live_and_off_remain_owner_overrides(monkeypatch):
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "world_model")
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    x = WorldLLMExtractor(FakeWorld(), self_model=_self_model("shadow"))
    assert x._effective_mode() == "live"
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "off")
    assert x._effective_mode() == "off"


# ---------------------------------------------------------------------------
# Supervised writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervised_entity_upsert_and_alias_merge(monkeypatch):
    monkeypatch.delenv("COLONY_WORLD_SUPERVISED_MAX_WRITES", raising=False)
    world = FakeWorld()
    x = WorldLLMExtractor(world)
    report = _report()
    eid = await x._upsert("Widget API", "project", 0.8, "supervised", report)
    assert eid is not None and len(world.entity_upserts) == 1
    assert report["writes"] == 1
    # alias merge via a stubbed resolver verdict
    x._resolver = SimpleNamespace(resolve=None)

    async def _merge(cand, etype):
        return SimpleNamespace(action="merge", matched_entity_id="we-x")
    x._resolver.resolve = _merge
    out = await x._upsert("WidgetAPI", "project", 0.8, "supervised", report)
    assert out == "we-x"
    assert world.aliases == [("we-x", "WidgetAPI")]
    assert report["writes"] == 2


@pytest.mark.asyncio
async def test_supervised_corroborates_existing_edge_never_creates():
    existing = WorldRelationship(id="wr-1", source_id="a", target_id="b",
                                 relationship_type="WM_KNOWS",
                                 confidence=0.5)
    world = FakeWorld([existing])
    x = WorldLLMExtractor(world)
    x._seen_rels = set()
    report = _report()
    # existing edge -> corroborate (bounded bump)
    await x._upsert_rel("a", "WM_KNOWS", "b", 0.6, "supervised", report)
    assert existing.confidence == pytest.approx(0.55)
    assert existing.properties["corroborations"] == 1
    assert report["writes"] == 1
    # new edge -> NOT created under supervised
    await x._upsert_rel("a", "WM_WORKS_AT", "c", 0.6, "supervised", report)
    assert all(r.relationship_type != "WM_WORKS_AT"
               for r in world.rel_upserts)
    assert report["writes"] == 1


@pytest.mark.asyncio
async def test_supervised_never_writes_causal_edge_types():
    world = FakeWorld()
    x = WorldLLMExtractor(world)
    x._seen_rels = set()
    report = _report()
    await x._upsert_causal("a", "WM_CAUSES", "b", "a caused b", 0.9,
                           "supervised", report)
    assert world.rel_upserts == []
    assert report["writes"] == 0


@pytest.mark.asyncio
async def test_supervised_write_cap(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_SUPERVISED_MAX_WRITES", "2")
    assert world_supervised_max_writes() == 2
    world = FakeWorld()
    x = WorldLLMExtractor(world)
    report = _report()
    for i in range(4):
        await x._upsert(f"Entity {i} Name", "project", 0.8,
                        "supervised", report)
    assert report["writes"] == 2
    assert len(world.entity_upserts) == 2
    assert report["supervised_capped"] == 2


@pytest.mark.asyncio
async def test_shadow_still_writes_nothing():
    world = FakeWorld([WorldRelationship(
        id="wr-1", source_id="a", target_id="b",
        relationship_type="WM_KNOWS", confidence=0.5)])
    x = WorldLLMExtractor(world)
    x._seen_rels = set()
    report = _report()
    await x._upsert("Widget API", "project", 0.8, "shadow", report)
    await x._upsert_rel("a", "WM_KNOWS", "b", 0.6, "shadow", report)
    assert world.entity_upserts == [] and world.rel_upserts == []
    assert report["writes"] == 0


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------

def _outcomes(sm):
    return sm.store.events("world_model") if hasattr(sm, "store") else []


def test_outcome_recorded_only_when_writes_positive(monkeypatch):
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "world_model")
    recorded = []
    sm = _self_model("ask_first")
    sm.record = lambda *a, **k: recorded.append((a, k))
    x = WorldLLMExtractor(FakeWorld(), self_model=sm)
    r = _report()
    x._record_outcome("supervised", r)          # writes == 0 -> nothing
    assert recorded == []
    r["writes"] = 3
    x._record_outcome("supervised", r)
    assert recorded == [(("world_model", "success"), {"shadow": False})]
    recorded.clear()
    x._record_outcome("shadow", r)               # non-acting mode -> nothing
    assert recorded == []


def test_outcome_not_recorded_when_domain_not_enrolled(monkeypatch):
    """Regression lock: no enrollment -> extractor records no trust
    outcomes at all, exactly as before H1.5."""
    monkeypatch.delenv("COLONY_SUPERVISED_LIVE_DOMAINS", raising=False)
    recorded = []
    sm = _self_model("ask_first")
    sm.record = lambda *a, **k: recorded.append((a, k))
    x = WorldLLMExtractor(FakeWorld(), self_model=sm)
    r = _report()
    r["writes"] = 5
    x._record_outcome("live", r)
    assert recorded == []

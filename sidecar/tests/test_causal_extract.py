"""Causal extraction (H2.2): evidence-pinned, confidence-capped, min-coupled.

Fabrication controls under test:
  * evidence MUST be a case-insensitive verbatim substring of the excerpt;
  * create confidence <= 0.5; corroboration +0.05 with a 0.75 ceiling;
  * effective mode = min(COLONY_CAUSAL_EXTRACT, COLONY_WORLD_LLM_EXTRACT);
  * shadow writes nothing.
"""

from __future__ import annotations

import pytest

from colony_sidecar.world_model.llm_extract import (
    WorldLLMExtractor, causal_extract_mode,
)

_TEXT = "Jordan Reyes works at Initech now. The migration caused the outage."


class FakeWorld:
    def __init__(self):
        self.entities = []
        self.rels = []

    async def upsert_entity(self, e):
        self.entities.append(e)
        return e

    async def add_entity_alias(self, eid, alias):
        pass

    async def upsert_relationship(self, r):
        if not r.id:
            r.id = f"wr-test-{len(self.rels)}"
        for i, old in enumerate(self.rels):
            if old.id == r.id:
                self.rels[i] = r
                return r
        self.rels.append(r)
        return r

    async def query_relationships(self, source_id=None, target_id=None,
                                  relationship_type=None, **kw):
        return [r for r in self.rels
                if (source_id is None or r.source_id == source_id)
                and (target_id is None or r.target_id == target_id)
                and (relationship_type is None
                     or r.relationship_type == relationship_type)]

    async def find_entities(self, *a, **k):
        return []


def _payload(causal):
    return {
        "entities": [
            {"name": "Jordan Reyes", "type": "person", "confidence": 0.8},
            {"name": "Initech", "type": "company", "confidence": 0.7},
        ],
        "relationships": [],
        "causal": causal,
    }


class _Extractor(WorldLLMExtractor):
    def __init__(self, *a, payload=None, **k):
        super().__init__(*a, **k)
        self._payload = payload

    async def _llm_batch(self, texts):
        return self._payload


def _causal_claim(evidence, conf=0.9, rel="WM_CAUSES"):
    return {"source": "Jordan Reyes", "rel": rel, "target": "Initech",
            "evidence": evidence, "confidence": conf}


def _causal_edges(world):
    return [r for r in world.rels if r.relationship_type == "WM_CAUSES"]


# ---------------------------------------------------------------------------
# Mode coupling
# ---------------------------------------------------------------------------

def test_causal_mode_off_by_default(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.delenv("COLONY_CAUSAL_EXTRACT", raising=False)
    assert causal_extract_mode() == "off"


def test_causal_mode_never_exceeds_extractor(monkeypatch):
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "shadow")
    assert causal_extract_mode() == "shadow"
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "off")
    assert causal_extract_mode() == "off"


@pytest.mark.asyncio
async def test_flag_off_writes_and_reports_nothing(monkeypatch):
    """Flag-off regression lock: causal payloads are ignored entirely."""
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.delenv("COLONY_CAUSAL_EXTRACT", raising=False)
    world = FakeWorld()
    x = _Extractor(world, payload=_payload(
        [_causal_claim("The migration caused the outage")]))
    report = await x.run(texts=[_TEXT])
    assert report["causal"] == [] and report["causal_skipped"] == 0
    assert _causal_edges(world) == []


@pytest.mark.asyncio
async def test_shadow_reports_but_writes_no_causal_edge(monkeypatch):
    """Causal shadow on a live extractor: entities land, causal edges never."""
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "shadow")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload(
        [_causal_claim("The migration caused the outage")]))
    report = await x.run(texts=[_TEXT])
    assert report["causal_mode"] == "shadow"
    assert len(report["causal"]) == 1            # observed and reported
    assert world.entities                        # extractor itself is live
    assert _causal_edges(world) == []            # shadow writes NOTHING


# ---------------------------------------------------------------------------
# Evidence pin (anti-fabrication)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fabricated_evidence_is_discarded(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload([
        _causal_claim("the CEO confirmed the root cause in the postmortem"),
        _causal_claim(""),                      # empty evidence
    ]))
    report = await x.run(texts=[_TEXT])
    assert report["causal_skipped"] == 2
    assert report["causal"] == []
    assert _causal_edges(world) == []


@pytest.mark.asyncio
async def test_evidence_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload(
        [_causal_claim("the MIGRATION caused THE outage")]))
    report = await x.run(texts=[_TEXT])
    assert len(report["causal"]) == 1
    assert len(_causal_edges(world)) == 1


# ---------------------------------------------------------------------------
# Confidence economics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_confidence_capped_at_half(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload(
        [_causal_claim("The migration caused the outage", conf=0.95)]))
    await x.run(texts=[_TEXT])
    edge = _causal_edges(world)[0]
    assert edge.confidence == 0.5
    assert edge.properties["evidence"].lower().startswith("the migration")


@pytest.mark.asyncio
async def test_corroboration_steps_and_ceiling(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    world = FakeWorld()
    payload = _payload([_causal_claim("The migration caused the outage")])
    # first run creates at 0.5; each subsequent run corroborates +0.05
    for _ in range(7):
        x = _Extractor(world, payload=payload)
        await x.run(texts=[_TEXT])
    # NOTE: each run creates fresh entity ids, so corroboration is proven
    # against a pinned pair below instead.
    edges = _causal_edges(world)
    assert all(e.confidence <= 0.75 for e in edges)


@pytest.mark.asyncio
async def test_corroboration_bumps_existing_edge(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    monkeypatch.setenv("COLONY_CAUSAL_EXTRACT", "live")
    world = FakeWorld()

    class _Pinned(_Extractor):
        """Resolve the same names to the same ids on every run."""
        async def _upsert(self, name, etype, conf, mode, report):
            return f"we-{name.lower().replace(' ', '-')}"

    payload = _payload([_causal_claim("The migration caused the outage")])
    for _ in range(10):
        x = _Pinned(world, payload=payload)
        await x.run(texts=[_TEXT])
    edges = _causal_edges(world)
    assert len(edges) == 1                       # corroborated, not duplicated
    assert edges[0].confidence == 0.75           # 0.5 + steps, ceiling holds
    assert edges[0].properties["corroborations"] >= 5

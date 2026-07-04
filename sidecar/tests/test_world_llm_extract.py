"""LLM-assisted world-model extraction: validation, journaling, modes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.self_model import ActionJournal
from colony_sidecar.world_model.llm_extract import WorldLLMExtractor


class FakeWorld:
    def __init__(self):
        self.entities = []
        self.aliases = []
        self.rels = []

    async def upsert_entity(self, e):
        self.entities.append(e)
        return e

    async def add_entity_alias(self, eid, alias):
        self.aliases.append((eid, alias))

    async def upsert_relationship(self, r):
        self.rels.append(r)
        return r

    async def find_entities(self, *a, **k):
        return []


def _payload():
    return {
        "entities": [
            {"name": "Jordan Reyes", "type": "person", "confidence": 0.8},
            {"name": "Initech", "type": "company", "confidence": 0.7},
            {"name": "hello", "type": "person", "confidence": 0.9},      # junk
            {"name": "Monday", "type": "person", "confidence": 0.9},     # noise
            {"name": "Vague Thing", "type": "gadget", "confidence": 0.9},  # bad type
            {"name": "Weak Signal Co", "type": "company", "confidence": 0.2},  # low conf
        ],
        "relationships": [
            {"source": "Jordan Reyes", "rel": "WM_WORKS_AT",
             "target": "Initech", "confidence": 0.6},
            {"source": "Jordan Reyes", "rel": "WM_EXPLODES",
             "target": "Initech", "confidence": 0.6},                    # bad rel
        ],
    }


class _Extractor(WorldLLMExtractor):
    """Bypass the HTTP call; return a canned parsed payload."""

    def __init__(self, *a, payload=None, **k):
        super().__init__(*a, **k)
        self._payload = payload

    async def _llm_batch(self, texts):
        return self._payload


@pytest.mark.asyncio
async def test_live_writes_validated_entities_and_journals(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    world = FakeWorld()
    journal = ActionJournal()
    x = _Extractor(world, journal=journal, payload=_payload())
    report = await x.run(texts=["Jordan Reyes works at Initech now."])
    names = {e.name for e in world.entities}
    assert names == {"Jordan Reyes", "Initech"}     # junk all filtered
    assert report["skipped"] >= 4
    assert len(world.rels) == 1
    assert world.rels[0].relationship_type == "WM_WORKS_AT"
    # every write journaled with reasoning (review surface)
    entries = journal.recent(domain="world_model")
    assert len(entries) == 3                        # 2 entities + 1 relationship
    assert all(e["decision"] == "acted" for e in entries)


@pytest.mark.asyncio
async def test_shadow_reports_without_writing(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "shadow")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload())
    report = await x.run(texts=["some text"])
    assert len(report["created"]) == 2
    assert world.entities == [] and world.rels == []


@pytest.mark.asyncio
async def test_off_mode_does_nothing(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "off")
    world = FakeWorld()
    x = _Extractor(world, payload=_payload())
    report = await x.run(texts=["some text"])
    assert report["batches"] == 0 and world.entities == []


@pytest.mark.asyncio
async def test_boundary_suppresses_entity(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_LLM_EXTRACT", "live")
    from colony_sidecar.directives import DirectiveManager, DirectiveStore
    dm = DirectiveManager(DirectiveStore())
    dm.add_explicit("Initech", polarity="prohibit",
                    raw_text="don't even look at Initech",)
    world = FakeWorld()
    x = _Extractor(world, directive_manager=dm, payload=_payload())
    await x.run(texts=["text"])
    names = {e.name for e in world.entities}
    assert "Initech" not in names

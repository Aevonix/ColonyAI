"""Regression tests for the graph/world-model tool handlers.

These handlers previously called methods that don't exist on the wired
objects (ColonyGraph.search / WorldModelStore.query), so every call raised
AttributeError and returned an error to the reasoner. They must now call the
real methods (recall / find_entities) and map results correctly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from colony_sidecar.tools.handlers import (
    handle_memory_search,
    handle_query_entities,
)


@dataclass
class _Entity:
    id: str
    name: str
    entity_type: str


class _Graph:
    def __init__(self):
        self.recall_calls = []

    async def recall(self, query, limit=10, **kw):
        self.recall_calls.append((query, limit))
        return [
            {"content": "the owner prefers concise replies", "created_at": "2026-07-01T00:00:00Z", "relevance": 0.91},
            {"content": None, "relevance": 0.4},  # None content must not crash
            # A non-JSON-serialisable datetime (mimics neo4j.time.DateTime)
            {"content": "dated", "created_at": datetime(2026, 7, 4, tzinfo=timezone.utc), "relevance": 0.2},
        ]


class _World:
    def __init__(self):
        self.find_calls = []

    async def find_entities(self, query, entity_type=None, limit=20, **kw):
        self.find_calls.append((query, entity_type, limit))
        return [
            _Entity(id="we-1", name="Acme Corp", entity_type="company"),
            _Entity(id="we-2", name="Alice", entity_type="person"),
        ]


@pytest.mark.asyncio
async def test_memory_search_uses_recall_and_maps():
    graph = _Graph()
    registry = SimpleNamespace(graph=graph)
    out = json.loads(await handle_memory_search(
        {"query": "preferences", "limit": 5}, registry,
    ))
    assert "error" not in out
    assert graph.recall_calls == [("preferences", 5)]
    assert out["count"] == 3
    assert out["memories"][0]["content"] == "the owner prefers concise replies"
    assert out["memories"][0]["relevance"] == 0.91
    assert out["memories"][0]["timestamp"] == "2026-07-01T00:00:00Z"
    # None content mapped to "" without crashing
    assert out["memories"][1]["content"] == ""
    # A datetime created_at is serialised to an ISO string (was the real
    # production crash: "Object of type DateTime is not JSON serializable")
    assert out["memories"][2]["timestamp"].startswith("2026-07-04T00:00:00")


@pytest.mark.asyncio
async def test_memory_search_coerces_string_limit():
    """A string/float limit (as LLMs often emit) must not crash."""
    graph = _Graph()
    registry = SimpleNamespace(graph=graph)
    out = json.loads(await handle_memory_search(
        {"query": "q", "limit": "5"}, registry,
    ))
    assert "error" not in out
    # coerced to int before reaching recall / slicing
    assert graph.recall_calls == [("q", 5)]


@pytest.mark.asyncio
async def test_query_entities_uses_find_entities_and_maps():
    world = _World()
    registry = SimpleNamespace(world_model=world)
    out = json.loads(await handle_query_entities(
        {"query": "acme", "entity_type": "all", "limit": 7}, registry,
    ))
    assert "error" not in out
    # "all" must be translated to no type filter (None)
    assert world.find_calls == [("acme", None, 7)]
    assert out["count"] == 2
    assert out["entities"][0] == {"id": "we-1", "name": "Acme Corp", "type": "company"}
    assert out["entities"][1]["type"] == "person"


@pytest.mark.asyncio
async def test_query_entities_passes_specific_type():
    world = _World()
    registry = SimpleNamespace(world_model=world)
    await handle_query_entities(
        {"query": "x", "entity_type": "person", "limit": 3}, registry,
    )
    assert world.find_calls == [("x", "person", 3)]


@pytest.mark.asyncio
async def test_registry_exposes_world_model():
    from colony_sidecar.autonomy.registry import SubsystemRegistry
    assert hasattr(SubsystemRegistry, "world_model")

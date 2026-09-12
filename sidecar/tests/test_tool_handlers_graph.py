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

from apsimo.tools.handlers import (
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
async def test_memory_search_requires_invocation_scope_without_graph_fallback():
    graph = _Graph()
    out = json.loads(await handle_memory_search({'query': 'private'}, SimpleNamespace(graph=graph)))
    assert out['status'] == 'unavailable'
    assert 'bound participant' in out['error']
    assert graph.recall_calls == []


@pytest.mark.asyncio
async def test_memory_search_preserves_canonical_packet_without_truncation():
    packet = {'content': 'evidence ' * 200, 'count': 1,
              'source_refs': [{'source_id': 'source', 'source_version': 'a'*64}],
              'watermark': 0, 'annotation_checks': []}
    calls = []
    async def search(args):
        calls.append(args)
        return packet
    graph = _Graph()
    out = json.loads(await handle_memory_search({'query': 'private', 'limit': 5},
                    SimpleNamespace(graph=graph), search=search))
    assert out == packet
    assert calls == [{'query': 'private', 'limit': 5}]
    assert graph.recall_calls == []


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
    from apsimo.autonomy.registry import SubsystemRegistry
    assert hasattr(SubsystemRegistry, "world_model")

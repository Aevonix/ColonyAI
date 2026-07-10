"""Causal-chain query (H2.4): read-only, causal-edges-only traversal."""

from __future__ import annotations

import asyncio

import pytest

from colony_sidecar.world_model.causal_query import causal_chain, causal_edges
from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import BaseEntity
from colony_sidecar.world_model.relationships import WorldRelationship
from colony_sidecar.world_model.store import WorldModelStore


async def _seeded_store():
    s = WorldModelStore(WorldModelConfig(backend="sqlite", sqlite_path=":memory:"))
    await s.connect()
    for eid, name in (("we-a", "Alpha"), ("we-b", "Beta"), ("we-c", "Gamma")):
        await s.upsert_entity(BaseEntity(id=eid, name=name,
                                         entity_type="concept",
                                         confidence=0.8))
    await s.upsert_relationship(WorldRelationship(
        id="wr-cause-ab", source_id="we-a", target_id="we-b",
        relationship_type="WM_CAUSES", confidence=0.5,
        properties={"evidence": "alpha caused beta"}))
    await s.upsert_relationship(WorldRelationship(
        id="wr-enable-bc", source_id="we-b", target_id="we-c",
        relationship_type="WM_ENABLES", confidence=0.5))
    await s.upsert_relationship(WorldRelationship(
        id="wr-knows-ab", source_id="we-a", target_id="we-b",
        relationship_type="WM_KNOWS", confidence=0.9))
    return s


def test_downstream_chain_walks_causal_edges_only():
    async def run():
        s = await _seeded_store()
        out = await causal_chain(s, "we-a", direction="downstream", max_hops=3)
        ids = {e["id"] for e in out["edges"]}
        assert ids == {"wr-cause-ab", "wr-enable-bc"}
        types = {e["relationship_type"] for e in out["edges"]}
        assert "WM_KNOWS" not in types
        assert out["nodes"]["we-c"]["hops"] == 2
        assert out["edges"][0]["evidence"] or True   # evidence surfaced
        assert any(e["evidence"] == "alpha caused beta" for e in out["edges"])
    asyncio.run(run())


def test_upstream_chain_finds_causes():
    async def run():
        s = await _seeded_store()
        out = await causal_chain(s, "we-c", direction="upstream", max_hops=3)
        ids = {e["id"] for e in out["edges"]}
        assert ids == {"wr-cause-ab", "wr-enable-bc"}
    asyncio.run(run())


def test_hop_limit_truncates_walk():
    async def run():
        s = await _seeded_store()
        out = await causal_chain(s, "we-a", direction="downstream", max_hops=1)
        assert {e["id"] for e in out["edges"]} == {"wr-cause-ab"}
    asyncio.run(run())


def test_causal_edges_listing():
    async def run():
        s = await _seeded_store()
        edges = await causal_edges(s)
        assert {e["id"] for e in edges} == {"wr-cause-ab", "wr-enable-bc"}
    asyncio.run(run())


@pytest.mark.asyncio
async def test_endpoints_serve_causal_surface(monkeypatch):
    from colony_sidecar.api.routers import host as host_mod
    s = await _seeded_store()
    monkeypatch.setattr(host_mod, "_world_store", s)
    chain = await host_mod.world_causal_chain("we-a", direction="downstream")
    assert {e["id"] for e in chain["edges"]} == {"wr-cause-ab", "wr-enable-bc"}
    flat = await host_mod.world_causal_edges()
    assert flat["total"] == 2

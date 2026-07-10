"""H2.5 — THE query-only hard guard for causal edges (integration proof).

Seeds WM_KNOWS + WM_CAUSES over the same entities and proves the causal
edge is invisible to every generic read surface:

  * get_neighbors / get_neighborhood with no relationship_types
  * query_relationships with relationship_type=None
  * query_at_time with no relationship_types
  * find_path (untyped traversal)
  * /context/assemble output
  * expectation resolution (resolve_relationship_still_active)

...while remaining reachable through the two sanctioned surfaces only:
/world/causal/chain and explicitly typed queries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import BaseEntity
from colony_sidecar.world_model.expectation_resolvers import (
    resolve_relationship_still_active,
)
from colony_sidecar.world_model.relationships import WorldRelationship
from colony_sidecar.world_model.store import WorldModelStore

CAUSAL_EDGE_ID = "wr-causal-guard"
KNOWS_EDGE_ID = "wr-knows-guard"


async def _seeded_store():
    s = WorldModelStore(WorldModelConfig(backend="sqlite", sqlite_path=":memory:"))
    await s.connect()
    for eid, name in (("we-a", "Alpha Person"), ("we-b", "Beta Person"),
                      ("we-c", "Gamma Concept")):
        await s.upsert_entity(BaseEntity(id=eid, name=name,
                                         entity_type="person",
                                         confidence=0.9))
    await s.upsert_relationship(WorldRelationship(
        id=KNOWS_EDGE_ID, source_id="we-a", target_id="we-b",
        relationship_type="WM_KNOWS", confidence=0.9))
    await s.upsert_relationship(WorldRelationship(
        id=CAUSAL_EDGE_ID, source_id="we-a", target_id="we-b",
        relationship_type="WM_CAUSES", confidence=0.75))
    # we-c is reachable from we-a ONLY through a causal edge
    await s.upsert_relationship(WorldRelationship(
        id="wr-causal-bc", source_id="we-b", target_id="we-c",
        relationship_type="WM_ENABLES", confidence=0.75))
    return s


# ---------------------------------------------------------------------------
# Generic read paths: causal edges must be invisible
# ---------------------------------------------------------------------------

def test_get_neighborhood_default_excludes_causal():
    async def run():
        s = await _seeded_store()
        hood = await s.get_neighborhood("we-a", max_hops=3)
        edge_ids = {e.id for e in hood.edges}
        types = {e.relationship_type for e in hood.edges}
        assert CAUSAL_EDGE_ID not in edge_ids
        assert not types & {"WM_CAUSES", "WM_ENABLES", "WM_BLOCKS",
                            "WM_INHIBITS"}
        assert KNOWS_EDGE_ID in edge_ids                # generic edge intact
        # we-c hangs off a causal-only edge: unreachable in a generic walk
        assert "we-c" not in {e.id for e in hood.reachable}
    asyncio.run(run())


def test_backend_get_neighbors_untyped_excludes_causal():
    async def run():
        s = await _seeded_store()
        pairs = await s._backend.get_neighbors("we-a", min_confidence=0.0)
        assert {r.id for _, r in pairs} == {KNOWS_EDGE_ID}
    asyncio.run(run())


def test_query_relationships_untyped_excludes_causal():
    async def run():
        s = await _seeded_store()
        rels = await s.query_relationships(source_id="we-a",
                                           min_confidence=0.0)
        assert {r.id for r in rels} == {KNOWS_EDGE_ID}
        # no filters at all: still no causal edge anywhere in the answer
        rels_all = await s.query_relationships(min_confidence=0.0)
        assert CAUSAL_EDGE_ID not in {r.id for r in rels_all}
    asyncio.run(run())


def test_query_at_time_untyped_excludes_causal():
    async def run():
        s = await _seeded_store()
        now = datetime.now(timezone.utc).isoformat()
        rels = await s.query_at_time("we-a", as_of=now)
        assert CAUSAL_EDGE_ID not in {r.id for r in rels}
    asyncio.run(run())


def test_find_path_never_walks_causal_edges():
    async def run():
        s = await _seeded_store()
        # we-a -> we-c exists only through causal edges: no generic path
        assert await s.find_path("we-a", "we-c") is None
        # the generic edge still routes we-a -> we-b
        path = await s.find_path("we-a", "we-b")
        assert path is not None and path[0].id == KNOWS_EDGE_ID
    asyncio.run(run())


@pytest.mark.asyncio
async def test_context_assemble_output_carries_no_causal_edge(monkeypatch):
    from colony_sidecar.api.routers import host as host_mod
    from colony_sidecar.api.schemas.host import (
        ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext,
    )
    s = await _seeded_store()
    monkeypatch.setattr(host_mod, "_world_store", s)
    resp = await host_mod.context_assemble(ContextAssembleRequest(
        identity=HostIdentity(host_id="test"),
        context=HostTurnContext(contact_id="contact:x", session_id="s1"),
        incoming_message=HostMessage(role="user", content="Alpha Person"),
    ))
    serialized = resp.model_dump_json()
    assert "WM_CAUSES" not in serialized
    assert CAUSAL_EDGE_ID not in serialized
    # the entity itself is fair game — only the causal EDGE is guarded
    assert "Alpha Person" in serialized


@pytest.mark.asyncio
async def test_expectation_resolution_ignores_causal(monkeypatch):
    from colony_sidecar.api.routers import host as host_mod
    s = await _seeded_store()
    monkeypatch.setattr(host_mod, "_world_store", s)

    def _pred(**detail):
        return SimpleNamespace(detail=detail)

    # causal-typed prediction: never resolved through the generic machinery
    assert resolve_relationship_still_active(_pred(
        source_id="we-a", target_id="we-b",
        relationship_type="WM_CAUSES")) is None
    # untyped prediction over a causal-only pair: the generic read cannot
    # see the edge, so the prediction stays unresolved (no fabricated hit)
    assert resolve_relationship_still_active(_pred(
        source_id="we-b", target_id="we-c")) is None
    # regression lock: a generic relationship still resolves normally
    assert resolve_relationship_still_active(_pred(
        source_id="we-a", target_id="we-b")) is True


# ---------------------------------------------------------------------------
# Sanctioned surfaces: causal edges must be present
# ---------------------------------------------------------------------------

def test_typed_query_returns_causal_edge():
    async def run():
        s = await _seeded_store()
        rels = await s.query_relationships(
            source_id="we-a", relationship_type="WM_CAUSES",
            min_confidence=0.0)
        assert {r.id for r in rels} == {CAUSAL_EDGE_ID}
        # typed neighborhood traversal is explicit intent: allowed
        hood = await s.get_neighborhood(
            "we-a", max_hops=1, relationship_types=["WM_CAUSES"])
        assert {e.id for e in hood.edges} == {CAUSAL_EDGE_ID}
    asyncio.run(run())


@pytest.mark.asyncio
async def test_world_causal_chain_serves_the_edge(monkeypatch):
    from colony_sidecar.api.routers import host as host_mod
    s = await _seeded_store()
    monkeypatch.setattr(host_mod, "_world_store", s)
    chain = await host_mod.world_causal_chain("we-a", direction="downstream",
                                              max_hops=3)
    edge_ids = {e["id"] for e in chain["edges"]}
    assert CAUSAL_EDGE_ID in edge_ids
    assert "wr-causal-bc" in edge_ids       # walks the causal-only extension

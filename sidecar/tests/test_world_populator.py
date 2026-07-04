"""Tests for the world-model populator (shadow-first, boundary-checked)."""

from __future__ import annotations

import asyncio

from colony_sidecar.world_model.store import WorldModelStore
from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.populator import WorldModelPopulator
from colony_sidecar.directives import DirectiveManager, DirectiveStore


_TEXT = "I met Alice Chen who works at Acme Corp about the launch. Bob Smith joined too."


async def _store():
    s = WorldModelStore(WorldModelConfig(backend="sqlite", sqlite_path=":memory:"))
    await s.connect()
    return s


def test_shadow_extracts_but_writes_nothing():
    async def run():
        s = await _store()
        pop = WorldModelPopulator(s, mode="shadow")
        rep = await pop.populate_from_text(_TEXT, "msg-1")
        # it surfaced entities it WOULD create
        names = {c["name"] for c in rep.created}
        assert "Alice Chen" in names
        assert any(c["type"] == "company" for c in rep.created)
        # but wrote nothing to the store
        stats = await s.get_stats()
        assert stats.total_entities == 0
        # and inferred a work relationship
        assert any(r["rel"] == "WM_WORKS_AT" for r in rep.relationships)
    asyncio.run(run())


def test_live_writes_entities():
    async def run():
        s = await _store()
        pop = WorldModelPopulator(s, mode="live")
        rep = await pop.populate_from_text(_TEXT, "msg-2")
        assert rep.total() > 0
        stats = await s.get_stats()
        assert stats.total_entities >= 2  # at least Alice + Acme
    asyncio.run(run())


def test_off_mode_is_noop():
    async def run():
        s = await _store()
        pop = WorldModelPopulator(s, mode="off")
        rep = await pop.populate_from_text(_TEXT, "msg-3")
        assert rep.total() == 0
    asyncio.run(run())


def test_boundary_skips_prohibited_subject():
    async def run():
        s = await _store()
        dm = DirectiveManager(DirectiveStore(db_path=None))
        dm.capture_from_message("don't track anything about Acme Corp")
        pop = WorldModelPopulator(s, directive_manager=dm, mode="shadow")
        rep = await pop.populate_from_text(_TEXT, "msg-4")
        created_names = {c["name"] for c in rep.created}
        # Acme was boundary-skipped; Alice still surfaced
        assert not any("acme" in n.lower() for n in created_names)
        assert any("acme" in x.lower() for x in rep.skipped_boundary)
        assert "Alice Chen" in created_names
    asyncio.run(run())


def test_dedup_within_message():
    async def run():
        s = await _store()
        pop = WorldModelPopulator(s, mode="shadow")
        rep = await pop.populate_from_text(
            "Alice Chen called. Alice Chen is great. Alice Chen again.", "msg-5")
        names = [c["name"] for c in rep.created if c["type"] == "person"]
        assert names.count("Alice Chen") == 1
    asyncio.run(run())

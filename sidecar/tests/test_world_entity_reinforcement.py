"""Entity reinforcement on merge (U19).

A repeat mention of a known entity used to write nothing but (maybe) an
alias — last_seen stayed frozen, so a frequently-discussed entity aged toward
prune eligibility exactly like a one-off. reinforce_entity touches
last_seen=now, mention_count+1 (guarded additive column, migrates existing
DBs in place) and confidence +0.02 capped 0.95. Strictly anti-data-loss:
a repeat mention must never make an entity MORE prunable than a single one.
"""

from __future__ import annotations

import asyncio

from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import BaseEntity
from colony_sidecar.world_model.populator import WorldModelPopulator
from colony_sidecar.world_model.sqlite.backend import SQLiteBackend
from colony_sidecar.world_model.store import WorldModelStore


async def _store():
    s = WorldModelStore(WorldModelConfig(backend="sqlite", sqlite_path=":memory:"))
    await s.connect()
    return s


async def _row(backend, entity_id):
    async with backend._db.execute(
            "SELECT last_seen, mention_count, confidence FROM wm_entities "
            "WHERE id = ?", (entity_id,)) as cur:
        return dict(await cur.fetchone())


def _entity(eid="we-1", name="Alice Chen", confidence=0.5):
    return BaseEntity(id=eid, name=name, entity_type="person",
                      confidence=confidence)


def test_reinforce_touches_last_seen_count_and_confidence():
    async def run():
        s = await _store()
        await s.upsert_entity(_entity(confidence=0.5))
        backend = s._backend
        await backend._db.execute(
            "UPDATE wm_entities SET last_seen = '2020-01-01T00:00:00Z' WHERE id = 'we-1'")
        await backend._db.commit()
        await s.reinforce_entity("we-1")
        row = await _row(backend, "we-1")
        assert row["last_seen"] > "2020-01-01T00:00:00Z"     # moved forward
        assert row["mention_count"] == 2                     # default 1 -> +1
        assert row["confidence"] == 0.52
        await s.close()
    asyncio.run(run())


def test_reinforce_caps_at_095_and_never_lowers():
    async def run():
        s = await _store()
        await s.upsert_entity(_entity(eid="we-cap", confidence=0.94))
        await s.upsert_entity(_entity(eid="we-high", name="B Corp", confidence=0.98))
        await s.reinforce_entity("we-cap")
        await s.reinforce_entity("we-high")
        backend = s._backend
        assert (await _row(backend, "we-cap"))["confidence"] == 0.95   # capped
        # Anti-data-loss: an entity already above the cap keeps its confidence
        # (never lowered to the cap, which could make it MORE prunable).
        assert (await _row(backend, "we-high"))["confidence"] == 0.98
        await s.close()
    asyncio.run(run())


def test_mention_count_column_migrates_existing_db(tmp_path):
    """A pre-U19 database (no mention_count column) migrates on connect."""
    db = str(tmp_path / "wm.db")

    async def run():
        # Simulate an old DB: full schema, then drop the new column.
        b = SQLiteBackend(db)
        await b.connect()
        async with b._db.execute("PRAGMA table_info(wm_entities)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        assert "mention_count" in cols
        await b._db.execute("ALTER TABLE wm_entities DROP COLUMN mention_count")
        await b._db.commit()
        await b.close()
        # Reconnect: the guarded ALTER re-adds it without touching data.
        b2 = SQLiteBackend(db)
        await b2.connect()
        async with b2._db.execute("PRAGMA table_info(wm_entities)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        assert "mention_count" in cols
        await b2.close()

    asyncio.run(run())


def test_live_populate_merge_reinforces():
    """A repeat mention in live mode reinforces the existing entity."""
    async def run():
        s = await _store()
        pop = WorldModelPopulator(s, mode="live")
        await pop.populate_from_text("I met Alice Chen at the launch.", "m1")
        ents = await s.find_entities(query="Alice Chen", min_confidence=0.0)
        assert ents, "first mention should create the entity"
        eid = ents[0].id
        backend = s._backend
        await backend._db.execute(
            "UPDATE wm_entities SET last_seen = '2020-01-01T00:00:00Z' WHERE id = ?",
            (eid,))
        await backend._db.commit()
        before = await _row(backend, eid)
        await pop.populate_from_text("Alice Chen called again today.", "m2")
        after = await _row(backend, eid)
        assert after["mention_count"] == before["mention_count"] + 1
        assert after["last_seen"] > before["last_seen"]
        assert after["confidence"] >= before["confidence"]   # never more prunable
        await s.close()
    asyncio.run(run())


def test_shadow_populate_never_reinforces():
    """Reinforcement is live-populate only; shadow observes, writes nothing."""
    async def run():
        s = await _store()
        await s.upsert_entity(_entity(eid="we-sh", confidence=0.5))
        backend = s._backend
        before = await _row(backend, "we-sh")
        pop = WorldModelPopulator(s, mode="shadow")
        await pop.populate_from_text("Alice Chen called again.", "m3")
        after = await _row(backend, "we-sh")
        assert after == before
        await s.close()
    asyncio.run(run())

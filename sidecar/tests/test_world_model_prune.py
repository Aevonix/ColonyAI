"""World-model prune: stale low-confidence entities expire; fresh or
high-confidence entities survive; relationships/observations cascade and the
FTS mirror stays consistent."""

from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import PersonEntity
from colony_sidecar.world_model.relationships import WorldRelationship
from colony_sidecar.world_model.sqlite.backend import SQLiteBackend
from colony_sidecar.world_model.store import WorldModelStore


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed(backend):
    stale_low = PersonEntity(id="we-stale-low", name="Rumor Rick",
                             entity_type="person", confidence=0.15)
    stale_high = PersonEntity(id="we-stale-high", name="Old Friend",
                              entity_type="person", confidence=0.9)
    fresh_low = PersonEntity(id="we-fresh-low", name="New Lead",
                             entity_type="person", confidence=0.15)
    for e in (stale_low, stale_high, fresh_low):
        await backend.upsert_entity(e)
    # backdate last_seen on the stale pair
    for eid in ("we-stale-low", "we-stale-high"):
        await backend._db.execute(
            "UPDATE wm_entities SET last_seen = ? WHERE id = ?",
            (_iso(120), eid))
    await backend._db.commit()
    rel = WorldRelationship(id="wr-1", source_id="we-stale-low",
                            target_id="we-stale-high",
                            relationship_type="knows", confidence=0.5)
    await backend.upsert_relationship(rel)


@pytest.mark.asyncio
async def test_backend_prune_deletes_stale_low_confidence(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "wm.db"))
    await backend.connect()
    await _seed(backend)

    pruned = await backend.prune_entities(_iso(90), max_confidence=0.30)
    assert pruned == 1

    assert await backend.get_entity("we-stale-low") is None
    assert (await backend.get_entity("we-stale-high")) is not None
    assert (await backend.get_entity("we-fresh-low")) is not None

    # relationship cascaded with its entity
    assert await backend.get_relationship("wr-1") is None

    # FTS mirror no longer matches the pruned name
    found = await backend.find_entities("Rumor", min_confidence=0.0)
    assert not any(e.id == "we-stale-low" for e in found)

    # idempotent
    assert await backend.prune_entities(_iso(90), max_confidence=0.30) == 0


@pytest.mark.asyncio
async def test_store_prune_uses_config_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_MODEL_SQLITE_PATH", str(tmp_path / "wm.db"))
    cfg = WorldModelConfig(backend="sqlite")
    store = WorldModelStore(cfg)
    await store.connect()
    try:
        await _seed(store._backend)
        out = await store.prune()          # defaults: ttl 90d, conf < 0.30
        assert out["status"] == "ok"
        assert out["pruned"] == 1
        stats = await store.get_stats()
        assert stats.total_entities == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_prune_skips_backends_without_primitive(tmp_path):
    class _NoPrune:
        pass

    store = WorldModelStore.__new__(WorldModelStore)
    store._backend = _NoPrune()
    store._config = WorldModelConfig(backend="sqlite")
    out = await store.prune()
    assert out["status"] == "skipped"

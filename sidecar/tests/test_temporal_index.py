"""Phase 3: cadence/silence + telemetry persistence (v0.21.0)."""

import pytest

from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.contacts.config import ContactsConfig


async def _set_history(store, cid, first_days_ago, last_days_ago, count):
    from colony_sidecar.util import temporal as T
    from datetime import timedelta
    now = T.now_utc()
    first = (now - timedelta(days=first_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    last = (now - timedelta(days=last_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await store._db.execute(
        "UPDATE contacts SET first_seen_at=?, last_interaction_at=?, interaction_count=? "
        "WHERE contact_id=?", (first, last, count, cid),
    )
    await store._db.commit()


@pytest.mark.asyncio
async def test_cadence_overdue_is_relative_to_rhythm():
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await store.connect()
    try:
        daily = await store.create(display_name="Daily Dan", trust_tier="regular")
        monthly = await store.create(display_name="Monthly Mia", trust_tier="regular")
        recent = await store.create(display_name="Recent Rae", trust_tier="regular")
        # daily contact (≈1d cadence), silent 5d -> overdue
        await _set_history(store, daily.contact_id, first_days_ago=30, last_days_ago=5, count=30)
        # monthly contact (≈33d cadence), silent 5d -> NOT overdue
        await _set_history(store, monthly.contact_id, first_days_ago=365, last_days_ago=5, count=12)
        # daily contact seen yesterday -> not overdue
        await _set_history(store, recent.contact_id, first_days_ago=30, last_days_ago=1, count=30)

        overdue = await store.compute_cadence_overdue(overdue_only=True)
        ids = {o["contact_id"] for o in overdue}
        assert daily.contact_id in ids
        assert monthly.contact_id not in ids
        assert recent.contact_id not in ids

        # exclude_ids respected
        overdue2 = await store.compute_cadence_overdue(
            overdue_only=True, exclude_ids={daily.contact_id})
        assert daily.contact_id not in {o["contact_id"] for o in overdue2}

        # the daily contact's cadence estimate is ~1 day
        dan = next(o for o in overdue if o["contact_id"] == daily.contact_id)
        assert dan["cadence_days"] < 2.0 and dan["days_since"] >= 4.5
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_telemetry_persists_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    from colony_sidecar.telemetry import TelemetryStore

    t = TelemetryStore()
    await t.touch("last_sync_at")
    await t.touch("last_agent_outreach_at")

    t2 = TelemetryStore()
    t2.load()
    assert t2.last_sync_at is not None
    assert t2.last_agent_outreach_at is not None
    assert t2.started_at is None  # started_at is per-process, not restored

"""Per-contact timezone: store migration + set/get/clear (v0.21.0)."""

import pytest

from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.contacts.config import ContactsConfig


@pytest.mark.asyncio
async def test_contact_timezone_roundtrip():
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await store.connect()
    try:
        c = await store.create(display_name="Robin", trust_tier="trusted")
        assert c.timezone is None  # column exists, defaults NULL

        await store.set_timezone(c.contact_id, "Asia/Tokyo")
        c2 = await store.get(c.contact_id)
        assert c2 is not None and c2.timezone == "Asia/Tokyo"
        assert c2.to_dict()["timezone"] == "Asia/Tokyo"

        with pytest.raises(ValueError):
            await store.set_timezone(c.contact_id, "Not/AZone")

        await store.set_timezone(c.contact_id, None)
        c3 = await store.get(c.contact_id)
        assert c3 is not None and c3.timezone is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_migration_adds_timezone_to_legacy_db(tmp_path):
    """A DB on the pre-v0.21.0 schema (everything but timezone) gains the
    column on connect() — the real production upgrade path."""
    import re
    import aiosqlite
    from colony_sidecar.contacts.store import _SCHEMA_FILE

    # Reconstruct the previous schema = current schema minus the timezone column.
    legacy_schema = re.sub(
        r",\s*\n\s*timezone\s+TEXT[^\n]*", "", _SCHEMA_FILE.read_text()
    )
    db_path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(legacy_schema)
        await db.execute(
            "INSERT INTO contacts (contact_id, display_name) VALUES ('cid-x', 'Legacy')"
        )
        await db.commit()
        async with db.execute("PRAGMA table_info(contacts)") as cur:
            assert "timezone" not in {r[1] for r in await cur.fetchall()}

    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=db_path))
    await store.connect()
    try:
        async with store._db.execute("PRAGMA table_info(contacts)") as cur:
            assert "timezone" in {r[1] for r in await cur.fetchall()}
        await store.set_timezone("cid-x", "Europe/Paris")
        c = await store.get("cid-x")
        assert c is not None and c.timezone == "Europe/Paris"
    finally:
        await store.close()

"""L1.1 — ConversationPresenceStore: passive conversation census.

Fed from the turns/sync attribution chokepoint (after the ParticipantResolver
settles WHO), gated by COLONY_CONV_PRESENCE (default on). The system sentinel
is never recorded; reads PROPAGATE errors so a broken store can never look
like an empty (safe) room to the risk classifier.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import (
    HostIdentity, HostMessage, HostSender, HostTurnContext, TurnSyncRequest)
from colony_sidecar.channels.presence import (
    STRONG_METHODS, ConversationPresenceStore, conv_presence_enabled)


def test_gate_default_on(monkeypatch):
    monkeypatch.delenv("COLONY_CONV_PRESENCE", raising=False)
    assert conv_presence_enabled() is True
    monkeypatch.setenv("COLONY_CONV_PRESENCE", "off")
    assert conv_presence_enabled() is False


def test_record_and_census():
    s = ConversationPresenceStore()
    assert s.record("rcs:conv-1", "cid-alice", method="handle") is True
    assert s.record("rcs:conv-1", "cid-bob", method="shadow",
                    group_id="g-9") is True
    rows = s.census("rcs:conv-1")
    by_id = {r["contact_id"]: r for r in rows}
    assert set(by_id) == {"cid-alice", "cid-bob"}
    assert by_id["cid-alice"]["method"] == "handle"
    assert by_id["cid-bob"]["group_id"] == "g-9"
    # repeat sighting bumps turns, keeps one row
    s.record("rcs:conv-1", "cid-alice", method="handle")
    rows = s.census("rcs:conv-1")
    assert len(rows) == 2
    assert {r["turns"] for r in rows if r["contact_id"] == "cid-alice"} == {2}


def test_latest_method_wins_downgrade():
    """A weak latest sighting downgrades the row: a stale strong method
    cannot vouch for a sender the server no longer resolves strongly."""
    s = ConversationPresenceStore()
    s.record("c1", "cid-a", method="handle")
    s.record("c1", "cid-a", method="client")
    assert s.census("c1")[0]["method"] == "client"
    assert "client" not in STRONG_METHODS


def test_system_sentinel_and_empties_never_recorded():
    s = ConversationPresenceStore()
    assert s.record("c1", "system") is False
    assert s.record("c1", "") is False
    assert s.record("", "cid-a") is False
    assert s.census("c1") == []


def test_gate_off_is_noop(monkeypatch):
    monkeypatch.setenv("COLONY_CONV_PRESENCE", "off")
    s = ConversationPresenceStore()
    assert s.record("c1", "cid-a", method="handle") is False
    assert s.census("c1") == []


def test_window_excludes_stale_sightings():
    s = ConversationPresenceStore()
    s.record("c1", "cid-old", method="handle")
    s._conn.execute(
        "UPDATE conversation_presence SET last_seen_at='2000-01-01T00:00:00+00:00' "
        "WHERE contact_id='cid-old'")
    s._conn.commit()
    s.record("c1", "cid-new", method="handle")
    assert [r["contact_id"] for r in s.census("c1", window_hours=48)] == ["cid-new"]
    assert s.is_present("c1", "cid-new") is True
    assert s.is_present("c1", "cid-old") is False


def test_cooccurrence():
    s = ConversationPresenceStore()
    s.record("c1", "cid-a", method="handle")
    s.record("c1", "cid-b", method="handle")
    s.record("c2", "cid-c", method="handle")
    assert s.cooccurred("cid-a", "cid-b") is True
    assert s.cooccurred("cid-a", "cid-c") is False
    assert s.cooccurred("cid-a", "cid-a") is False
    assert s.cooccurred("cid-a", "") is False


def test_reads_fail_closed_by_raising():
    """A broken store must RAISE, never answer 'empty room'."""
    s = ConversationPresenceStore()
    s.record("c1", "cid-a", method="handle")
    s._conn.close()
    with pytest.raises(sqlite3.Error):
        s.census("c1")
    with pytest.raises(sqlite3.Error):
        s.is_present("c1", "cid-a")
    with pytest.raises(sqlite3.Error):
        s.cooccurred("cid-a", "cid-b")


# ---------------------------------------------------------------------------
# The turns/sync chokepoint feed
# ---------------------------------------------------------------------------

class _FakeContact:
    def __init__(self, contact_id):
        self.contact_id = contact_id


class _FakeContacts:
    """Resolves any handle to cid-alice (method 'handle')."""
    async def resolve_messaging_handle(self, platform, user_id):
        return _FakeContact("cid-alice")

    async def get(self, contact_id):
        return None

    async def get_handles(self, contact_id):
        return []


def _turn(contact_id="cid-alice", channel_id="rcs:conv-1", sender=None,
          text="hello"):
    return TurnSyncRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id=contact_id,
                                channel_id=channel_id),
        sender=sender,
        user_message=HostMessage(role="user", content=text),
    )


@pytest.mark.asyncio
async def test_turns_sync_feeds_presence_with_resolution_method(monkeypatch):
    store = ConversationPresenceStore()
    monkeypatch.setattr(host_mod, "_presence_store", store)
    monkeypatch.setattr(host_mod, "_graph", None)
    monkeypatch.setattr(host_mod, "_context_provenance", None)
    monkeypatch.setattr(host_mod, "_contacts_store", _FakeContacts())
    await host_mod.turns_sync(_turn(
        contact_id="cid-stale",
        sender=HostSender(platform="sms", user_id="+15550001111",
                          group_id="g-7")))
    rows = store.census("rcs:conv-1")
    assert len(rows) == 1
    assert rows[0]["contact_id"] == "cid-alice"     # resolved, not client-claimed
    assert rows[0]["method"] == "handle"
    assert rows[0]["group_id"] == "g-7"


@pytest.mark.asyncio
async def test_turns_sync_unresolved_records_client_method(monkeypatch):
    store = ConversationPresenceStore()
    monkeypatch.setattr(host_mod, "_presence_store", store)
    monkeypatch.setattr(host_mod, "_graph", None)
    monkeypatch.setattr(host_mod, "_context_provenance", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    await host_mod.turns_sync(_turn(contact_id="cid-claimed"))
    rows = store.census("rcs:conv-1")
    assert [(r["contact_id"], r["method"]) for r in rows] == [
        ("cid-claimed", "client")]


@pytest.mark.asyncio
async def test_turns_sync_machine_turn_not_recorded(monkeypatch):
    store = ConversationPresenceStore()
    monkeypatch.setattr(host_mod, "_presence_store", store)
    monkeypatch.setattr(host_mod, "_graph", None)
    monkeypatch.setattr(host_mod, "_context_provenance", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    await host_mod.turns_sync(_turn(contact_id="whatever",
                                    channel_id="cron:heartbeat"))
    assert store.census("cron:heartbeat") == []


@pytest.mark.asyncio
async def test_turns_sync_survives_presence_failure(monkeypatch):
    class _Broken:
        def record(self, *a, **k):
            raise RuntimeError("disk full")

    monkeypatch.setattr(host_mod, "_presence_store", _Broken())
    monkeypatch.setattr(host_mod, "_graph", None)
    monkeypatch.setattr(host_mod, "_context_provenance", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    resp = await host_mod.turns_sync(_turn())
    assert resp.accepted is True


@pytest.mark.asyncio
async def test_turns_sync_without_store_unchanged(monkeypatch):
    monkeypatch.setattr(host_mod, "_presence_store", None)
    monkeypatch.setattr(host_mod, "_graph", None)
    monkeypatch.setattr(host_mod, "_context_provenance", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    resp = await host_mod.turns_sync(_turn())
    assert resp.accepted is True

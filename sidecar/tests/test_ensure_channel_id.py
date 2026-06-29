"""Tests for _ensure_channel_id() -- Phase 0 of the Channel Framework.

Validates the 5-level fallback that auto-derives a stable channel_id
when the host does not provide one, so context provenance and
cross-context leak detection always work.
"""

import pytest
from types import SimpleNamespace

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import HostIdentity, HostTurnContext


@pytest.fixture(autouse=True)
def _isolate_stores(monkeypatch):
    monkeypatch.setattr(host_mod, "_session_store", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)


# --- Level 1: host-provided channel_id passes through ---


@pytest.mark.asyncio
async def test_host_provided_channel_id_passes_through():
    ctx = HostTurnContext(session_id="s1", contact_id="c1", channel_id="rcs:conv-42")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "rcs:conv-42"


# --- Level 2: session gateway ---


@pytest.mark.asyncio
async def test_derives_from_session_gateway(monkeypatch):
    class FakeSessionStore:
        async def get_by_contact(self, contact_id):
            return SimpleNamespace(gateway="whatsapp")

    monkeypatch.setattr(host_mod, "_session_store", FakeSessionStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    result = await host_mod._ensure_channel_id(ctx)
    assert result == "whatsapp:c1"


@pytest.mark.asyncio
async def test_session_store_returns_none(monkeypatch):
    class FakeSessionStore:
        async def get_by_contact(self, contact_id):
            return None

    monkeypatch.setattr(host_mod, "_session_store", FakeSessionStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "hermes:c1"


@pytest.mark.asyncio
async def test_session_store_raises(monkeypatch):
    class FakeSessionStore:
        async def get_by_contact(self, contact_id):
            raise RuntimeError("db locked")

    monkeypatch.setattr(host_mod, "_session_store", FakeSessionStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "hermes:c1"


# --- Level 3: contact primary handle ---


@pytest.mark.asyncio
async def test_derives_from_contact_primary_handle(monkeypatch):
    class FakeContactsStore:
        async def get_handles(self, contact_id):
            return [
                SimpleNamespace(gateway="sms", is_primary=False),
                SimpleNamespace(gateway="imessage", is_primary=True),
            ]

    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    result = await host_mod._ensure_channel_id(ctx)
    assert result == "imessage:c1"


@pytest.mark.asyncio
async def test_derives_from_first_handle_when_no_primary(monkeypatch):
    class FakeContactsStore:
        async def get_handles(self, contact_id):
            return [SimpleNamespace(gateway="telegram", is_primary=False)]

    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    result = await host_mod._ensure_channel_id(ctx)
    assert result == "telegram:c1"


@pytest.mark.asyncio
async def test_contacts_store_empty_handles(monkeypatch):
    class FakeContactsStore:
        async def get_handles(self, contact_id):
            return []

    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "hermes:c1"


@pytest.mark.asyncio
async def test_contacts_store_raises(monkeypatch):
    class FakeContactsStore:
        async def get_handles(self, contact_id):
            raise RuntimeError("db locked")

    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "hermes:c1"


# --- Level 4: host_id ---


@pytest.mark.asyncio
async def test_derives_from_host_id():
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="kiosk-office")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "kiosk-office:c1"


# --- Level 5: unknown ---


@pytest.mark.asyncio
async def test_falls_back_to_unknown():
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    result = await host_mod._ensure_channel_id(ctx)
    assert result == "unknown:c1"


@pytest.mark.asyncio
async def test_anonymous_when_no_contact_id():
    ctx = HostTurnContext(session_id="s1", contact_id="")
    result = await host_mod._ensure_channel_id(ctx)
    assert result == "unknown:anonymous"


# --- Priority ordering (session beats contact beats host_id) ---


@pytest.mark.asyncio
async def test_session_gateway_beats_contact_handle(monkeypatch):
    class FakeSessionStore:
        async def get_by_contact(self, contact_id):
            return SimpleNamespace(gateway="rcs")

    class FakeContactsStore:
        async def get_handles(self, contact_id):
            return [SimpleNamespace(gateway="imessage", is_primary=True)]

    monkeypatch.setattr(host_mod, "_session_store", FakeSessionStore())
    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "rcs:c1"


@pytest.mark.asyncio
async def test_contact_handle_beats_host_id(monkeypatch):
    class FakeContactsStore:
        async def get_handles(self, contact_id):
            return [SimpleNamespace(gateway="whatsapp", is_primary=True)]

    monkeypatch.setattr(host_mod, "_contacts_store", FakeContactsStore())
    ctx = HostTurnContext(session_id="s1", contact_id="c1")
    identity = HostIdentity(host_id="hermes")
    result = await host_mod._ensure_channel_id(ctx, identity=identity)
    assert result == "whatsapp:c1"

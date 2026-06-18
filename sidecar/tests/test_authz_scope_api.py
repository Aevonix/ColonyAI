"""/v1/host/authz/scope — context-scoped authorization over HTTP. A group scope
authorizes its members WITHIN the group (group_guest), auto-creating shadow contacts
for unknown members on first sight, and never granting them 1:1 DM rights.
"""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore

# Framework test fixtures only (555-prefix numbers are reserved/non-routable).
OWNER = "+15550000001"
GUEST = "+15550000002"
STRANGER = "+15559999999"


@asynccontextmanager
async def _client(store):
    orig = host_mod._contacts_store
    host_mod._contacts_store = store
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        host_mod._contacts_store = orig


@pytest.fixture
async def store():
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_scope_create_authorizes_members_only_in_group(store):
    owner = await store.create(display_name="Owner", trust_tier="inner_circle")
    # handles are stored under the canonical phone gateway (sms); rcs queries resolve to them
    await store.add_handle(owner.contact_id, gateway="sms", address=OWNER)

    async with _client(store) as c:
        # Create the group scope; GUEST is unknown -> auto-created as a shadow.
        r = await c.post("/v1/host/authz/scope", json={
            "platform": "rcs", "external_id": "conv-9", "label": "Owner & Guest",
            "members": [
                {"gateway": "rcs", "address": OWNER, "role": "owner"},
                {"gateway": "rcs", "address": GUEST, "name": "Guest"},
            ],
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["granted_tier"] == "group_guest"
        assert len(body["members"]) == 2

        # Guest is authorized INSIDE the group.
        a = await c.get("/v1/host/authz/scope", params={
            "platform": "rcs", "external_id": "conv-9", "gateway": "rcs", "address": GUEST})
        assert a.status_code == 200
        assert a.json()["authorized"] is True
        assert a.json()["granted_tier"] == "group_guest"

        # A stranger is not.
        s = await c.get("/v1/host/authz/scope", params={
            "platform": "rcs", "external_id": "conv-9", "gateway": "rcs", "address": STRANGER})
        assert s.json()["authorized"] is False

        # Deactivating the scope revokes everyone at once.
        d = await c.post("/v1/host/authz/scope/deactivate",
                         json={"platform": "rcs", "external_id": "conv-9"})
        assert d.status_code == 200 and d.json()["active"] is False
        a2 = await c.get("/v1/host/authz/scope", params={
            "platform": "rcs", "external_id": "conv-9", "gateway": "rcs", "address": GUEST})
        assert a2.json()["authorized"] is False

    # The auto-created guest is a shadow: in the group, but NO global 1:1 rights.
    guest = await store.resolve_messaging_handle("rcs", GUEST)
    assert guest is not None
    assert guest.trust_tier == "acquaintance"
    assert guest.interaction_allowed is False


@pytest.mark.asyncio
async def test_authz_scope_unknown_group_is_unauthorized(store):
    async with _client(store) as c:
        a = await c.get("/v1/host/authz/scope", params={
            "platform": "rcs", "external_id": "no-such-conv", "gateway": "rcs", "address": GUEST})
        assert a.status_code == 200
        assert a.json()["authorized"] is False

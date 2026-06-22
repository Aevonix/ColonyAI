"""Introduction capture — social-graph autonomy Slice 1 (#109).

Capturing an organic introduction creates (or annotates) a PROVISIONAL contact
with provenance (introduced_by + met_via) and never grants interaction standing.
"""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.world_model.constants import RELATIONSHIP_TYPES

GUEST = "+15550000042"


@pytest.fixture
async def store(tmp_path):
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / "c.db")))
    await s.connect()
    yield s
    await s.close()


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


def test_introduced_by_relationship_type_exists():
    # Groundwork for Slice 2's world-model edge.
    assert "WM_INTRODUCED_BY" in RELATIONSHIP_TYPES


@pytest.mark.asyncio
async def test_capture_creates_inert_provisional_contact(store):
    intro = {"channel": "rcs-group", "scope_id": "ts-1"}
    c = await store.create(
        display_name="Rae", trust_tier="unknown",
        interaction_allowed=False, import_source="agent_intro",
        introduced_by="cid-owner", met_via=intro,
    )
    assert c.import_source == "agent_intro"
    assert c.interaction_allowed is False        # an intro never grants standing
    assert c.introduced_by == "cid-owner"
    assert c.met_via == intro


@pytest.mark.asyncio
async def test_provenance_survives_reload(store, tmp_path):
    intro = {"channel": "voice", "scope_id": None}
    c = await store.create(display_name="Dana", trust_tier="unknown",
                           interaction_allowed=False, import_source="agent_intro",
                           introduced_by="cid-owner", met_via=intro)
    reloaded = await store.get(c.contact_id)
    assert reloaded.introduced_by == "cid-owner"
    assert reloaded.met_via == intro
    assert reloaded.to_dict()["met_via"] == intro


@pytest.mark.asyncio
async def test_record_introduction_annotates_existing_only_fills_blanks(store):
    # A pre-existing contact (e.g. resolved by handle) gets provenance recorded
    # without duplicating it and without changing its standing.
    c = await store.create(display_name="Sam", trust_tier="regular",
                           interaction_allowed=True, import_source="manual")
    updated = await store.record_introduction(
        c.contact_id, introduced_by="cid-owner",
        met_via={"channel": "rcs-group", "scope_id": "ts-9"})
    assert updated.introduced_by == "cid-owner"
    assert updated.met_via["scope_id"] == "ts-9"
    assert updated.trust_tier == "regular"          # standing untouched
    assert updated.interaction_allowed is True

    # First introduction wins — a second call does not overwrite.
    again = await store.record_introduction(
        c.contact_id, introduced_by="cid-someone-else",
        met_via={"channel": "voice"})
    assert again.introduced_by == "cid-owner"
    assert again.met_via["scope_id"] == "ts-9"


@pytest.mark.asyncio
async def test_record_introduction_missing_contact_returns_none(store):
    assert await store.record_introduction("cid-nope", introduced_by="x") is None


@pytest.mark.asyncio
async def test_intro_endpoint_creates_provisional_with_handle(store):
    owner = await store.create(display_name="Owner", trust_tier="inner_circle")
    async with _client(store) as c:
        r = await c.post("/v1/host/contacts/intro", json={
            "name": "Rae", "gateway": "rcs", "address": GUEST,
            "introduced_by": owner.contact_id,
            "met_via": {"channel": "rcs-group", "scope_id": "ts-7"},
        })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    ct = body["contact"]
    assert ct["import_source"] == "agent_intro"
    assert ct["interaction_allowed"] is False
    assert ct["introduced_by"] == owner.contact_id
    assert ct["met_via"]["scope_id"] == "ts-7"
    # The handle resolves back to this provisional contact.
    resolved = await store.resolve_messaging_handle("rcs", GUEST)
    assert resolved is not None and resolved.contact_id == ct["contact_id"]


@pytest.mark.asyncio
async def test_intro_endpoint_annotates_existing_no_duplicate(store):
    # Known person on this handle already.
    sam = await store.create(display_name="Sam", trust_tier="regular",
                             interaction_allowed=True, import_source="manual")
    await store.add_handle(sam.contact_id, gateway="sms", address=GUEST)
    async with _client(store) as c:
        r = await c.post("/v1/host/contacts/intro", json={
            "name": "Sam (via group)", "gateway": "rcs", "address": GUEST,
            "introduced_by": "cid-owner",
            "met_via": {"channel": "rcs-group", "scope_id": "ts-3"},
        })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is False                       # annotated, not duplicated
    assert body["contact"]["contact_id"] == sam.contact_id
    assert body["contact"]["introduced_by"] == "cid-owner"
    assert body["contact"]["trust_tier"] == "regular"     # standing untouched
    assert body["contact"]["interaction_allowed"] is True

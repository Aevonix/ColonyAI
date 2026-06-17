"""Cross-channel resolution: a phone number is ONE contact regardless of the gateway/transport it
arrives on, and regardless of formatting. This lets per-contact memory engage for any phone-bearing
channel (SMS/RCS/voice/...) instead of pooling unresolved senders under a single fallback contact.
"""

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore

# Framework test fixtures only — 555-prefix numbers and example.com are reserved/non-routable.
PHONE = "+15550101234"
EMAIL = "person@example.com"


@pytest.fixture
async def store():
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_phone_resolves_across_gateways_and_formats(store):
    """A number stored under ONE gateway (here imessage) must still resolve when the same number
    arrives on a different phone-bearing gateway, or in different formatting."""
    c = await store.create(display_name="Test Person", trust_tier="inner_circle")
    await store.add_handle(c.contact_id, gateway="imessage", address=PHONE, is_primary=True)

    cases = [
        ("imessage", PHONE),            # same gateway, exact
        ("sms", PHONE),                 # different gateway, same number
        ("rcs", PHONE),                 # rcs canonicalizes to the phone identity
        ("rcs", "+1 (555) 010-1234"),   # rcs + messy formatting
        ("sms", "15550101234"),         # no leading +
        ("sms", "5550101234"),          # national-form (last-10) digits
        ("whatsapp", PHONE),            # any phone-bearing gateway
    ]
    for gw, addr in cases:
        got = await store.resolve_messaging_handle(gw, addr)
        assert got is not None and got.contact_id == c.contact_id, f"{gw}:{addr} should resolve"


@pytest.mark.asyncio
async def test_unknown_number_does_not_resolve(store):
    c = await store.create(display_name="Test Person", trust_tier="inner_circle")
    await store.add_handle(c.contact_id, gateway="imessage", address=PHONE, is_primary=True)
    assert await store.resolve_messaging_handle("sms", "+15550109999") is None


@pytest.mark.asyncio
async def test_email_resolves_case_insensitively(store):
    c = await store.create(display_name="Test Person", trust_tier="inner_circle")
    await store.add_handle(c.contact_id, gateway="email", address=EMAIL)
    got = await store.resolve_messaging_handle("email", EMAIL.upper())
    assert got is not None and got.contact_id == c.contact_id


@pytest.mark.asyncio
async def test_soft_deleted_contact_not_resolved(store):
    """Resolution must return a LIVE contact, never a soft-deleted one (unlike find_by_handle)."""
    c = await store.create(display_name="Former Contact", trust_tier="regular")
    await store.add_handle(c.contact_id, gateway="imessage", address="+15550100000", is_primary=True)
    await store.soft_delete(c.contact_id)
    assert await store.resolve_messaging_handle("sms", "+15550100000") is None

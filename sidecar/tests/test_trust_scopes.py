"""Context-scoped trust: a trust_scope (e.g. a group chat) grants its members a tier
that applies ONLY inside the scope. Membership never confers global 1:1 rights — a
member's contacts row is untouched. This is the generic "trusted in this room, not in
my DMs" primitive (any Hermes agent can use it).
"""

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore


@pytest.fixture
async def store():
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_scope_membership_authorizes_in_scope_only(store):
    owner = await store.create(display_name="Owner", trust_tier="inner_circle")
    # A brand-new person: discovered/shadow-style, NO global interaction rights.
    guest = await store.create(display_name="Guest", trust_tier="acquaintance")

    scope = await store.create_scope(
        scope_type="group", platform="rcs", external_id="conv-9", label="Owner & Guest"
    )
    assert scope.granted_tier == "group_guest"
    await store.add_scope_member(scope.scope_id, owner.contact_id, role="owner")
    await store.add_scope_member(scope.scope_id, guest.contact_id)

    # Guest is authorized INSIDE the scope...
    assert await store.is_authorized_in_scope(guest.contact_id, scope.scope_id) is True
    # ...but their global standing is untouched — still no 1:1 interaction.
    g = await store.get(guest.contact_id)
    assert g.trust_tier == "acquaintance"
    assert g.interaction_allowed is False
    # And not authorized in a scope they're not a member of.
    other = await store.create_scope(platform="rcs", external_id="conv-99")
    assert await store.is_authorized_in_scope(guest.contact_id, other.scope_id) is False


@pytest.mark.asyncio
async def test_create_scope_is_idempotent_by_external_id(store):
    a = await store.create_scope(platform="rcs", external_id="conv-7")
    b = await store.create_scope(platform="rcs", external_id="conv-7")
    assert a.scope_id == b.scope_id  # same (platform, external_id) → same scope


@pytest.mark.asyncio
async def test_remove_member_and_deactivate_revoke_scope_trust(store):
    c = await store.create(display_name="Member", trust_tier="unknown")
    scope = await store.create_scope(platform="rcs", external_id="conv-5")
    await store.add_scope_member(scope.scope_id, c.contact_id)
    assert await store.is_authorized_in_scope(c.contact_id, scope.scope_id) is True

    # Leaving the group revokes in-scope authorization (history preserved).
    await store.remove_scope_member(scope.scope_id, c.contact_id)
    assert await store.is_authorized_in_scope(c.contact_id, scope.scope_id) is False
    assert await store.scope_members(scope.scope_id, current_only=True) == []

    # Re-adding re-activates; deactivating the whole scope revokes everyone at once.
    await store.add_scope_member(scope.scope_id, c.contact_id)
    assert await store.is_authorized_in_scope(c.contact_id, scope.scope_id) is True
    await store.deactivate_scope(scope.scope_id)
    assert await store.is_authorized_in_scope(c.contact_id, scope.scope_id) is False
    assert await store.scopes_for_contact(c.contact_id, active_only=True) == []


@pytest.mark.asyncio
async def test_scopes_for_contact_lists_active_memberships(store):
    c = await store.create(display_name="Multi", trust_tier="unknown")
    s1 = await store.create_scope(platform="rcs", external_id="conv-1", label="One")
    s2 = await store.create_scope(platform="rcs", external_id="conv-2", label="Two")
    await store.add_scope_member(s1.scope_id, c.contact_id)
    await store.add_scope_member(s2.scope_id, c.contact_id)
    ids = {s.scope_id for s in await store.scopes_for_contact(c.contact_id)}
    assert ids == {s1.scope_id, s2.scope_id}

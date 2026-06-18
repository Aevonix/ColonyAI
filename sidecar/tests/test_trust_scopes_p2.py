"""P2 — Feature A polish: scope lifecycle is auditable, and group_guest is a
first-class, restrictively-gated trust tier (not a silent peripheral fallback).
"""

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.gate.layers.l4_trust_tier import TrustTierChecker
from colony_sidecar.gate.models import GatePayload
from colony_sidecar.intelligence.relationships.trust_tiers import (
    TIER_CAPABILITIES,
    TrustTier,
)


@pytest.fixture
async def store():
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


# ── audit trail over the scope lifecycle ─────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_lifecycle_is_audited(store):
    guest = await store.create(display_name="Guest", trust_tier="acquaintance")
    scope = await store.create_scope(platform="rcs", external_id="conv-7", label="G")

    await store.add_scope_member(scope.scope_id, guest.contact_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_member_added" in actions

    # deactivating the scope records a revocation against each current member
    await store.deactivate_scope(scope.scope_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_deactivated" in actions

    # explicit member removal is audited too
    await store.add_scope_member(scope.scope_id, guest.contact_id)
    await store.remove_scope_member(scope.scope_id, guest.contact_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_member_removed" in actions


# ── group_guest is a real, restrictive gate tier ─────────────────────────────

def _payload(text, tier):
    return GatePayload(
        response_text=text,
        target_contact_id="c1",
        target_gateway="rcs",
        session_id="s1",
        trust_tier=tier,
        mentioned_entities=frozenset(),
        turn_id="t1",
        incoming_message_text="hi",
    )


@pytest.mark.asyncio
async def test_group_guest_gated_like_peripheral():
    checker = TrustTierChecker()
    # internal-state, relationship-assessment, and private-detail disclosures all blocked
    assert (await checker.check(_payload("I store and track everything about you", TrustTier.GROUP_GUEST))).blocked
    assert (await checker.check(_payload("based on my interactions with Marc he stays busy", TrustTier.GROUP_GUEST))).blocked
    assert (await checker.check(_payload("his home address is on file", TrustTier.GROUP_GUEST))).blocked
    # benign group chatter passes
    assert not (await checker.check(_payload("sounds good, see you at 6", TrustTier.GROUP_GUEST))).blocked


def test_group_guest_capabilities_are_restrictive():
    caps = TIER_CAPABILITIES[TrustTier.GROUP_GUEST]
    assert caps["colony_proactive_reach_out"] is False
    assert caps["colony_full_context_sharing"] is False
    assert caps["contact_can_request_reminders"] is False


def test_group_guest_is_in_the_gate_enum():
    # the inference gate path does TrustTier(contact.trust_tier); group_guest must resolve
    assert TrustTier("group_guest") is TrustTier.GROUP_GUEST

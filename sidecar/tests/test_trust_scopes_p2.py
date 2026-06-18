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


# ── P2c: promotion (group_guest -> 1:1) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_promotion_candidates_and_promote(store):
    guest = await store.create(display_name="Frequent Guest", trust_tier="acquaintance")
    scope = await store.create_scope(platform="rcs", external_id="conv-pc", label="PC")
    await store.add_scope_member(scope.scope_id, guest.contact_id)
    for _ in range(5):
        await store.record_interaction(guest.contact_id)

    cands = await store.group_promotion_candidates(min_interactions=5)
    assert any(c.contact_id == guest.contact_id for c in cands)
    # below threshold excluded
    assert not any(c.contact_id == guest.contact_id
                   for c in await store.group_promotion_candidates(min_interactions=6))

    # promote raises to regular + grants 1:1
    assert await store.promote_scope_member(guest.contact_id) is True
    g = await store.get(guest.contact_id)
    assert g.trust_tier == "regular" and g.interaction_allowed is True
    # no longer a candidate (now has 1:1 rights); idempotent re-promote is a no-op
    assert not any(c.contact_id == guest.contact_id
                   for c in await store.group_promotion_candidates(min_interactions=1))
    assert await store.promote_scope_member(guest.contact_id) is False
    assert "scope_promoted_to_1on1" in [a["action"] for a in await store.get_audit_log(guest.contact_id)]


@pytest.mark.asyncio
async def test_promote_never_lowers_standing(store):
    # an already-trusted contact in a group keeps their higher tier (promote only raises)
    vip = await store.create(display_name="VIP", trust_tier="trusted")
    scope = await store.create_scope(platform="rcs", external_id="conv-vip")
    await store.add_scope_member(scope.scope_id, vip.contact_id)
    await store.promote_scope_member(vip.contact_id)  # to_tier=regular < trusted
    assert (await store.get(vip.contact_id)).trust_tier == "trusted"


def test_config_flag_from_env(monkeypatch):
    from colony_sidecar.contacts.config import ContactsConfig
    monkeypatch.setenv("COLONY_AUTO_PROMOTE_GROUP_TO_1ON1", "true")
    monkeypatch.setenv("COLONY_GROUP_PROMOTE_MIN_INTERACTIONS", "3")
    cfg = ContactsConfig.from_env()
    assert cfg.auto_promote_group_to_1on1 is True
    assert cfg.group_promote_min_interactions == 3

"""Organic introduction initiatives — social-graph autonomy Slice 2 (#109).

The autonomy loop finds pairs of contacts who share related work and proposes an
INTRODUCTION for the owner to approve. PROPOSE-ONLY: an INTRODUCTION is not an
agent_action, so it is surfaced, never auto-executed.
"""

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.identity.resolver import reset_identity_resolver
from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeEngine,
    InitiativeType,
)


@pytest.fixture
async def store(tmp_path):
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / "c.db")))
    await s.connect()
    yield s
    await s.close()


async def _mk(store, name, org, tier="regular", deleted=False):
    c = await store.create(display_name=name, organization=org, trust_tier=tier)
    if deleted:
        await store.soft_delete(c.contact_id, reason="test")
    return c


# ── store.introduction_candidates ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_candidates_pairs_shared_org_above_floor(store):
    a = await _mk(store, "Alex", "Acme")
    b = await _mk(store, "Bo", "Acme")
    await _mk(store, "Cy", "OtherCo")  # different org -> not paired with Acme folks
    pairs = await store.introduction_candidates(trust_floor="regular")
    assert len(pairs) == 1
    p = pairs[0]
    assert {p["a_id"], p["b_id"]} == {a.contact_id, b.contact_id}
    assert p["organization"] == "Acme"


@pytest.mark.asyncio
async def test_candidates_exclude_below_floor(store):
    await _mk(store, "Alex", "Acme", tier="regular")
    await _mk(store, "Bo", "Acme", tier="acquaintance")  # below floor
    pairs = await store.introduction_candidates(trust_floor="regular")
    assert pairs == []


@pytest.mark.asyncio
async def test_candidates_exclude_owner_and_deleted(store):
    owner = await _mk(store, "Owner", "Acme", tier="inner_circle")
    await _mk(store, "Bo", "Acme")
    await _mk(store, "Gone", "Acme", deleted=True)
    # Only Owner + Bo + (deleted) share Acme; excluding the owner leaves no pair.
    pairs = await store.introduction_candidates(
        trust_floor="regular", owner_contact_id=owner.contact_id)
    assert pairs == []


@pytest.mark.asyncio
async def test_candidates_blank_org_not_paired(store):
    await _mk(store, "Alex", "")
    await _mk(store, "Bo", None)
    pairs = await store.introduction_candidates(trust_floor="regular")
    assert pairs == []


# ── engine._generate_introduction_initiatives ───────────────────────────────

@pytest.mark.asyncio
async def test_engine_proposes_owner_gated_introduction(monkeypatch):
    reset_identity_resolver()
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    try:
        engine = InitiativeEngine(graph_client=None, event_bus=None, mind_model=None)
        engine.add_context("introduction_candidates", [
            {"a_id": "cid-b", "a_name": "Bo", "b_id": "cid-a", "b_name": "Alex",
             "organization": "Acme"},
        ])
        out = await engine._generate_introduction_initiatives()
        assert len(out) == 1
        i = out[0]
        assert i.type == InitiativeType.INTRODUCTION
        assert i.action_hint == "propose_introduction"  # surfaced, not executed
        # ids are sorted into a stable, order-independent dedup key
        assert i.dedup_key == "intro:cid-a:cid-b"
        assert i.entity_id == "cid-a:cid-b"
        assert "Bo" in i.description and "Alex" in i.description
        # Must clear the default min_priority confidence gate (0.7) or it would
        # be filtered out before the cap/headroom ever apply.
        assert i.priority >= 0.7
    finally:
        reset_identity_resolver()


def _engine():
    return InitiativeEngine(graph_client=None, event_bus=None, mind_model=None)


def _init(type_, priority, n, action_hint=None):
    from colony_sidecar.intelligence.components.initiative_engine import Initiative
    return Initiative(
        id=f"{type_.value}-{n}", type=type_, description=f"{type_.value} {n}",
        priority=priority, rationale="x", dedup_key=f"{type_.value}:{n}",
        action_hint=action_hint,
    )


def test_apply_cap_reserves_headroom_for_introductions():
    """A saturated cap must still surface bounded intros (not starve them)."""
    engine = _engine()
    flood = [_init(InitiativeType.OPERATIONAL, 0.95, n) for n in range(40)]
    intros = [_init(InitiativeType.INTRODUCTION, 0.5, n) for n in range(5)]
    capped = engine._apply_cap(flood + intros, max_initiatives=20)
    surfaced = [i for i in capped if i.type == InitiativeType.INTRODUCTION]
    assert len(surfaced) == 2          # default _INTRO_HEADROOM
    assert len([i for i in capped if i.type == InitiativeType.OPERATIONAL]) == 20


def test_apply_cap_keeps_all_owed_deliverables():
    """Owed deliverables are unbounded — never dropped by the cap."""
    engine = _engine()
    flood = [_init(InitiativeType.OPERATIONAL, 0.95, n) for n in range(20)]
    owed = [_init(InitiativeType.AGENT_ACTION, 0.1, n,
                  action_hint="agent_deliver_message") for n in range(3)]
    capped = engine._apply_cap(flood + owed, max_initiatives=20)
    assert sum(1 for i in capped
               if getattr(i, "action_hint", "") == "agent_deliver_message") == 3


def test_apply_cap_no_overflow_when_under_limit():
    engine = _engine()
    items = [_init(InitiativeType.OPERATIONAL, 0.9, n) for n in range(5)]
    assert len(engine._apply_cap(items, max_initiatives=20)) == 5


@pytest.mark.asyncio
async def test_engine_skips_owner_party(monkeypatch):
    reset_identity_resolver()
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    try:
        engine = InitiativeEngine(graph_client=None, event_bus=None, mind_model=None)
        engine.add_context("introduction_candidates", [
            {"a_id": "cid-owner", "a_name": "Owner", "b_id": "cid-x", "b_name": "X",
             "organization": "Acme"},
        ])
        out = await engine._generate_introduction_initiatives()
        assert out == []   # never introduce the owner to anyone
    finally:
        reset_identity_resolver()

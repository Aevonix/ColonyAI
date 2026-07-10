"""L4.2 — context-assembly wiring for leveled cross-contact tom2.

The flip point: non-owner readers get resolve_effective_level(); level 1
injects the self-reflexive section, level 2 injects the silent prior AFTER
exposure + taint bookkeeping. Locks:

  * DEFAULT-INERT — with shipped defaults the assembled sections are
    byte-identical to a run with the new block neutralized entirely.
  * NOT-INVOKED-BELOW-LEVEL — renderers are never even called below their
    level.
  * OWNER PATH UNTOUCHED — the H3.3 owner section logic is unaffected;
    leveled sections never render to the owner.
  * AUTO-DOWNGRADE leak-proofs — unknown participant, subject joining,
    tripped breaker, stale enforce evidence: each drops the level with no
    human action, next turn.
  * FAIL-CLOSED — any error in the block yields no section.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import colony_sidecar.api.routers.host as host
from colony_sidecar.api.schemas.host import (
    ContextAssembleRequest, ContextSection, HostIdentity, HostMessage,
    HostTurnContext,
)
from colony_sidecar.channels.presence import ConversationPresenceStore
from colony_sidecar.gate.guard_audit import GuardAuditStore
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard
from colony_sidecar.gate.taint import TaintRegistry
from colony_sidecar.proposals import ProposalStore
from colony_sidecar.tom import leveled as leveled_mod
from colony_sidecar.tom import levels as levels_mod
from colony_sidecar.tom.approvals import Tom2ApprovalRegistry
from colony_sidecar.tom.exposure import Tom2ExposureStore
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.levels import clear_level_cache, set_evidence_probe
from colony_sidecar.tom.tom2 import Tom2Store

OWNER = "cid-owner-test"
READER = "cid-alice"
SUBJECT = "cid-bob"
CONV = "dm:cid-alice"
FACT_TEXT = "the launch moved to friday"


class FakeContacts:
    def __init__(self, contacts):
        self._contacts = dict(contacts)

    async def get(self, contact_id):
        row = self._contacts.get(contact_id)
        if row is None:
            return None
        return SimpleNamespace(contact_id=contact_id, **row)

    async def get_handles(self, contact_id):
        return []


def _req(cid, channel=CONV):
    return ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(contact_id=cid, session_id="s1",
                                channel_id=channel),
        incoming_message=HostMessage(role="user", content="hi"),
    )


async def _fixed_temporal(contact_id=None, tz=None):
    return ContextSection(id="temporal-context", title="Current Time",
                          body="frozen", priority=100)


@pytest.fixture(autouse=True)
def _reset_levels():
    clear_level_cache()
    set_evidence_probe(None)
    yield
    clear_level_cache()
    set_evidence_probe(None)


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """An R1 room with a full level-2 chain ready: trusted strong reader in
    a private DM, subject approved + mutually known, evidence probe live."""
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    monkeypatch.setenv("COLONY_ENV_RISK_GATEWAY_CLASS", "dm:private")

    facts = SharedFactsStore(db_path=str(tmp_path / "facts.db"))
    f = facts.create_fact(contact_id=READER, fact=FACT_TEXT, confidence=0.9)
    tom2 = Tom2Store(db_path=str(tmp_path / "tom2.db"))
    tom2.record_inference(contact_id=SUBJECT, kind="unaware_of",
                          fact_ref=f["id"], confidence=0.4)
    tom2.record_inference(contact_id=READER, kind="knows",
                          fact_ref=f["id"], confidence=0.9)

    presence = ConversationPresenceStore()
    presence.record(CONV, OWNER, method="handle")
    presence.record(CONV, READER, method="handle")
    # mutual knowledge: reader and subject share ANOTHER conversation
    presence.record("dm:shared-thread", READER, method="handle")
    presence.record("dm:shared-thread", SUBJECT, method="handle")

    contacts = FakeContacts({
        READER: {"trust_tier": "trusted", "display_name": "Alice"},
        SUBJECT: {"trust_tier": "regular", "display_name": "Bob Smith"},
        OWNER: {"trust_tier": "inner_circle", "display_name": "Owner"},
    })

    exposure = Tom2ExposureStore()
    taints = TaintRegistry()
    proposals = ProposalStore()
    Tom2ApprovalRegistry(proposals).approve_pair(READER, SUBJECT)

    monkeypatch.setattr(host, "_tom2_store", tom2)
    monkeypatch.setattr(host, "_facts_store", facts)
    monkeypatch.setattr(host, "_presence_store", presence)
    monkeypatch.setattr(host, "_contacts_store", contacts)
    monkeypatch.setattr(host, "_tom2_exposure", exposure)
    monkeypatch.setattr(host, "_taint_registry", taints)
    monkeypatch.setattr(host, "_proposal_store", proposals)
    monkeypatch.setattr(host, "_build_temporal_section", _fixed_temporal)
    return SimpleNamespace(facts=facts, tom2=tom2, presence=presence,
                           contacts=contacts, exposure=exposure,
                           taints=taints, fact=f)


def _arm_level2(monkeypatch, probe=None):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(probe or (lambda gw: True))


def _leveled(resp):
    return [s for s in resp.sections if s.id in ("colony-tom2-l1",
                                                 "colony-tom2-l2")]


def _dump(resp):
    return [(s.id, s.title, s.body, s.priority) for s in resp.sections]


# ---------------------------------------------------------------------------
# THE regression lock: default-inert, byte-identical
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_defaults_byte_identical_to_neutralized_block(world,
                                                            monkeypatch):
    """With ALL shipped defaults, assembly output is byte-equal to a run in
    which the L4.2 block is neutralized outright (its first import made to
    explode). The block therefore contributes exactly zero bytes today."""
    for var in ("COLONY_TOM2_LEVEL", "COLONY_TOM2_MAX_LEVEL",
                "COLONY_TOM2_RISK_CAPS", "COLONY_TOM2_CROSS_CONTEXT",
                "COLONY_TOM2_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    with_defaults = _dump(await host.context_assemble(_req(READER)))

    def boom():
        raise RuntimeError("block neutralized")

    monkeypatch.setattr(levels_mod, "configured_level", boom)
    neutralized = _dump(await host.context_assemble(_req(READER)))
    assert with_defaults == neutralized
    assert all(sid not in ("colony-tom2-l1", "colony-tom2-l2")
               for sid, *_ in with_defaults)


# ---------------------------------------------------------------------------
# Not-invoked-below-level
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_level1_not_invoked_at_level_zero(world, monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_LEVEL", raising=False)
    calls = []
    monkeypatch.setattr(leveled_mod, "render_level1",
                        lambda *a, **k: calls.append(1))
    monkeypatch.setattr(leveled_mod, "render_level2",
                        lambda *a, **k: calls.append(2))
    await host.context_assemble(_req(READER))
    assert calls == []


@pytest.mark.asyncio
async def test_render_level2_not_invoked_below_level_2(world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "1")
    calls = []
    monkeypatch.setattr(leveled_mod, "render_level2",
                        lambda *a, **k: calls.append(2))
    resp = await host.context_assemble(_req(READER))
    assert calls == []
    # level 1 itself DID render (the reader's own knows row)
    assert [s.id for s in _leveled(resp)] == ["colony-tom2-l1"]


# ---------------------------------------------------------------------------
# The full chain at level 2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level2_renders_with_bookkeeping(world, monkeypatch):
    _arm_level2(monkeypatch)
    resp = await host.context_assemble(_req(READER))
    ids = [s.id for s in _leveled(resp)]
    assert ids == ["colony-tom2-l1", "colony-tom2-l2"]
    l2 = next(s for s in resp.sections if s.id == "colony-tom2-l2")
    assert f"{SUBJECT} has not heard: {FACT_TEXT}" in l2.body
    assert "SILENT" in l2.body                       # framed as silent prior
    # ledger-first bookkeeping happened
    events = world.exposure.recent()
    assert len(events) == 1
    assert events[0]["reader_contact_id"] == READER
    assert events[0]["subject_contact_id"] == SUBJECT
    assert FACT_TEXT not in str(events[0])           # refs, never content
    taints = world.taints.active_for(CONV)
    assert len(taints) == 1
    assert "bob smith" in taints[0]["subject_names"]


@pytest.mark.asyncio
async def test_missing_taint_registry_renders_no_l2(world, monkeypatch):
    """No taint registry => no egress net => nothing may render at L2."""
    _arm_level2(monkeypatch)
    monkeypatch.setattr(host, "_taint_registry", None)
    resp = await host.context_assemble(_req(READER))
    assert [s.id for s in _leveled(resp)] == ["colony-tom2-l1"]


@pytest.mark.asyncio
async def test_budget_exhaustion_stops_further_renders(world, monkeypatch):
    """The pair budget (default 1/day) binds: the second assembly renders
    no L2 line for the same pair."""
    _arm_level2(monkeypatch)
    await host.context_assemble(_req(READER))
    clear_level_cache()
    resp2 = await host.context_assemble(_req(READER))
    assert all(s.id != "colony-tom2-l2" for s in resp2.sections)
    assert len(world.exposure.recent()) == 1         # no second exposure


# ---------------------------------------------------------------------------
# Owner path untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_never_gets_leveled_sections(world, monkeypatch):
    _arm_level2(monkeypatch)
    resp = await host.context_assemble(_req(OWNER, channel="dm:owner"))
    assert _leveled(resp) == []


@pytest.mark.asyncio
async def test_owner_h33_section_unaffected_by_leveled_flags(world,
                                                             monkeypatch):
    """H3.3 verbatim: the owner section keys ONLY off COLONY_TOM2_CONTEXT,
    exactly as before the leveled wiring existed."""
    _arm_level2(monkeypatch)
    resp = await host.context_assemble(_req(OWNER, channel="dm:owner"))
    assert all(s.id != "colony-tom2" for s in resp.sections)
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    resp2 = await host.context_assemble(_req(OWNER, channel="dm:owner"))
    tom2_secs = [s for s in resp2.sections if s.id == "colony-tom2"]
    assert len(tom2_secs) == 1 and FACT_TEXT in tom2_secs[0].body


# ---------------------------------------------------------------------------
# Auto-downgrade leak-proofs (no human action, next turn)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_participant_drops_everything(world, monkeypatch):
    _arm_level2(monkeypatch)
    assert len(_leveled(await host.context_assemble(_req(READER)))) == 2
    # a stranger (weak resolution) is sighted in the conversation
    world.presence.record(CONV, "cid-stranger", method="scoped_name")
    clear_level_cache()
    resp = await host.context_assemble(_req(READER))
    assert _leveled(resp) == []                      # nothing new renders


@pytest.mark.asyncio
async def test_subject_joining_vanishes_their_inferences(world, monkeypatch):
    _arm_level2(monkeypatch)
    assert len(_leveled(await host.context_assemble(_req(READER)))) == 2
    world.presence.record(CONV, SUBJECT, method="handle")
    clear_level_cache()
    resp = await host.context_assemble(_req(READER))
    joined = "\n".join(s.body for s in resp.sections)
    assert "has not heard" not in joined
    assert all(s.id != "colony-tom2-l2" for s in resp.sections)


@pytest.mark.asyncio
async def test_breaker_tripped_caps_at_level_1(world, monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    monkeypatch.setenv("COLONY_GUARD_TRIP_BLOCKS", "1")
    audit = GuardAuditStore()
    for _ in range(3):
        audit.record(conversation_key=CONV, mode="enforce",
                     decision="allow", authorized=False,
                     checks=["secret_leak"], entities=[], gateway="dm")
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, audit_store=audit)
    _arm_level2(monkeypatch, probe=guard.evidence_probe())
    assert len(_leveled(await host.context_assemble(_req(READER)))) == 2
    guard._block_times.append(time.time())           # breaker trips
    clear_level_cache()
    resp = await host.context_assemble(_req(READER))
    assert [s.id for s in _leveled(resp)] == ["colony-tom2-l1"]


@pytest.mark.asyncio
async def test_stale_enforce_evidence_caps_at_level_1(world, monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    audit = GuardAuditStore()
    audit.record(conversation_key=CONV, mode="enforce", decision="allow",
                 authorized=False, checks=["secret_leak"], entities=[],
                 gateway="dm")
    # age every audit row out of the evidence window
    audit._conn.execute("UPDATE guard_events SET ts = '2000-01-01T00:00:00'")
    audit._conn.commit()
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, audit_store=audit)
    _arm_level2(monkeypatch, probe=guard.evidence_probe())
    resp = await host.context_assemble(_req(READER))
    assert [s.id for s in _leveled(resp)] == ["colony-tom2-l1"]


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_any_error_in_block_renders_no_section(world, monkeypatch):
    _arm_level2(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("resolver on fire")

    monkeypatch.setattr(levels_mod, "resolve_effective_level", boom)
    resp = await host.context_assemble(_req(READER))
    assert _leveled(resp) == []


@pytest.mark.asyncio
async def test_exposure_write_failure_aborts_the_section(world, monkeypatch):
    _arm_level2(monkeypatch)

    class _BrokenLedger:
        def budget_ok(self, *a, **k):
            return True

        def record_exposure(self, **k):
            raise RuntimeError("ledger disk full")

    monkeypatch.setattr(host, "_tom2_exposure", _BrokenLedger())
    resp = await host.context_assemble(_req(READER))
    assert all(s.id != "colony-tom2-l2" for s in resp.sections)

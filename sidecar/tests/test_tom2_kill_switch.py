"""L4.4 — kill switch + end-to-end default-inertness.

COLONY_TOM2_LEVEL=0 is the single-variable panic path (docs/TOM2-LEVELS.md):
rendering stops next turn for every reader and conversation, while the
egress net stays armed for taints already in the wild. And with EVERY flag
at its shipped default, the whole leveled system is invisible end to end.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import colony_sidecar.api.routers.host as host
from colony_sidecar.api.schemas.host import (
    ContextAssembleRequest, ContextSection, HostIdentity, HostMessage,
    HostTurnContext,
)
from colony_sidecar.channels.presence import ConversationPresenceStore
from colony_sidecar.gate.layers.tom2_epistemic import Tom2EpistemicGuard
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard
from colony_sidecar.gate.taint import TaintRegistry
from colony_sidecar.proposals import ProposalStore
from colony_sidecar.tom.approvals import Tom2ApprovalRegistry
from colony_sidecar.tom.exposure import Tom2ExposureStore
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.levels import (
    clear_level_cache, resolve_effective_level, set_evidence_probe)
from colony_sidecar.tom.tom2 import Tom2Store

OWNER = "cid-owner-test"
READER = "cid-alice"
SUBJECT = "cid-bob"
CONV = "dm:cid-alice"
FACT_TEXT = "the launch moved to friday"

_DEFAULT_VARS = (
    "COLONY_TOM2_LEVEL", "COLONY_TOM2_MAX_LEVEL", "COLONY_TOM2_RISK_CAPS",
    "COLONY_TOM2_CROSS_CONTEXT", "COLONY_TOM2_CONTEXT",
    "COLONY_TOM2_L2_APPROVAL", "COLONY_GUARD_ENFORCE_CHECKS",
    "COLONY_GUARD_DERIVE_CONTEXT", "COLONY_ENV_RISK_GATEWAY_CLASS",
)


class FakeContacts:
    def __init__(self, contacts):
        self._contacts = dict(contacts)

    async def get(self, contact_id):
        row = self._contacts.get(contact_id)
        return None if row is None else SimpleNamespace(
            contact_id=contact_id, **row)

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
def _reset():
    clear_level_cache()
    set_evidence_probe(None)
    yield
    clear_level_cache()
    set_evidence_probe(None)


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
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
                           exposure=exposure, taints=taints)


def _leveled(resp):
    return [s for s in resp.sections if s.id in ("colony-tom2-l1",
                                                 "colony-tom2-l2")]


def _arm_level2(monkeypatch):
    monkeypatch.setenv("COLONY_ENV_RISK_GATEWAY_CLASS", "dm:private")
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(lambda gw: True)


# ---------------------------------------------------------------------------
# End to end: all flags default == today
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_defaults_whole_system_is_invisible(world, monkeypatch):
    """Every flag at its shipped default, real inference data in every
    store: assembly injects nothing leveled, the level resolver answers 0,
    and the egress check finds nothing (no taints can exist)."""
    for var in _DEFAULT_VARS:
        monkeypatch.delenv(var, raising=False)
    # 1. assembly: no leveled sections, no epistemic phrasing anywhere
    resp = await host.context_assemble(_req(READER))
    assert _leveled(resp) == []
    joined = "\n".join(s.body for s in resp.sections)
    assert "has not heard" not in joined
    assert "SILENT" not in joined
    # 2. resolver: level 0, everywhere, even in this healthy room
    res = await resolve_effective_level(
        CONV, READER, presence_store=world.presence,
        contacts_store=host._contacts_store, use_cache=False)
    assert res.level == 0
    # 3. egress: guard evaluates clean (empty taint registry => inert)
    guard = ResponseGuard(
        default_mode=GuardMode.SHADOW,
        tom2_epistemic=Tom2EpistemicGuard(world.taints,
                                          facts_store=world.facts))
    r = await guard.evaluate(response_text="Bob hasn't heard the news",
                             target_gateway="dm", conversation_key=CONV)
    assert r.decision == "allow" and r.findings == []
    # 4. and nothing was ever booked
    assert world.exposure.counts()["total"] == 0
    assert world.taints.any_active() is False


# ---------------------------------------------------------------------------
# The single-var kill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level_zero_kills_all_rendering_next_turn(world, monkeypatch):
    _arm_level2(monkeypatch)
    assert len(_leveled(await host.context_assemble(_req(READER)))) == 2
    # PANIC: one variable, nothing else touched
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "0")
    clear_level_cache()
    resp = await host.context_assemble(_req(READER))
    assert _leveled(resp) == []
    joined = "\n".join(s.body for s in resp.sections)
    assert "has not heard" not in joined


@pytest.mark.asyncio
async def test_kill_switch_leaves_the_egress_net_armed(world, monkeypatch):
    """Taints already in the wild keep protecting after the kill: rendering
    stops, but a reply voicing the previously injected prior still blocks
    until the taint's TTL runs out."""
    _arm_level2(monkeypatch)
    await host.context_assemble(_req(READER))          # registers the taint
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "0")       # kill
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    guard = ResponseGuard(
        default_mode=GuardMode.ENFORCE,
        tom2_epistemic=Tom2EpistemicGuard(world.taints,
                                          facts_store=world.facts))
    r = await guard.evaluate(
        response_text="bob smith hasn't heard about it yet",
        target_gateway="dm", conversation_key=CONV)
    assert r.decision == "revise"
    assert any(f.check == "tom2_epistemic" for f in r.findings)

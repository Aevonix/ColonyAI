"""L3.3 — server-side guard-context completion (COLONY_GUARD_DERIVE_CONTEXT).

The chat hot path's plugin posts only text + ids, so the context-dependent
checks evaluated against a null conversation_key and returned [] — dead
where they matter most. The endpoint now derives conversation_key (same
path as turns/sync), trust_tier (contact store) and mentioned_entities
(rule-based NER over the incoming message) when the host omits them.
Flag off restores the legacy null-key pass-through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import ResponseGuardCheckRequest
from colony_sidecar.gate.context_provenance import (
    ContextProvenanceStore, ProvenanceCrossContextGuard)
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard


class _SpyGuard:
    """Records the evaluate() kwargs the endpoint hands the guard."""

    def __init__(self):
        self.seen = None

    async def evaluate(self, **kw):
        self.seen = kw
        return SimpleNamespace(
            to_dict=lambda: {"decision": "allow", "mode": "shadow",
                             "findings": []})


class _Sessions:
    def __init__(self, gateway="rcs"):
        self._gateway = gateway

    async def get_by_contact(self, contact_id):
        return SimpleNamespace(gateway=self._gateway)


class _Contacts:
    def __init__(self, tiers):
        self._tiers = dict(tiers)

    async def get(self, contact_id):
        tier = self._tiers.get(contact_id)
        return None if tier is None else SimpleNamespace(
            contact_id=contact_id, trust_tier=tier)


class _Extractor:
    async def extract(self, text, source):
        ents = []
        if "falcon" in text.lower():
            ents.append(SimpleNamespace(text="Project Falcon"))
        return SimpleNamespace(entities=ents)


@pytest.fixture()
def spy(monkeypatch):
    g = _SpyGuard()
    monkeypatch.setattr(host_mod, "_response_guard", g)
    monkeypatch.setattr(host_mod, "_session_store", _Sessions())
    monkeypatch.setattr(host_mod, "_contacts_store",
                        _Contacts({"cid-42": "trusted"}))
    monkeypatch.setattr(host_mod, "_conversation_extractor", _Extractor())
    monkeypatch.delenv("COLONY_GUARD_DERIVE_CONTEXT", raising=False)
    return g


@pytest.mark.asyncio
async def test_conversation_key_derived_from_session(spy):
    await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="hi", target_contact_id="cid-42"))
    assert spy.seen["conversation_key"] == "rcs:cid-42"


@pytest.mark.asyncio
async def test_trust_tier_resolved_from_contacts(spy):
    await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="hi", target_contact_id="cid-42"))
    assert spy.seen["trust_tier"] == "trusted"


@pytest.mark.asyncio
async def test_mentioned_entities_derived_from_incoming_message(spy):
    await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="sure", target_contact_id="cid-42",
        incoming_message_text="what about Project Falcon?"))
    assert spy.seen["mentioned_entities"] == ["Project Falcon"]


@pytest.mark.asyncio
async def test_host_supplied_context_is_never_overwritten(spy):
    await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="hi", target_contact_id="cid-42",
        conversation_key="rcs:conv-77", trust_tier="regular",
        mentioned_entities=["Alpha"],
        incoming_message_text="what about Project Falcon?"))
    assert spy.seen["conversation_key"] == "rcs:conv-77"
    assert spy.seen["trust_tier"] == "regular"
    assert spy.seen["mentioned_entities"] == ["Alpha"]


@pytest.mark.asyncio
async def test_flag_off_restores_null_key_passthrough(spy, monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_DERIVE_CONTEXT", "0")
    await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="hi", target_contact_id="cid-42",
        incoming_message_text="what about Project Falcon?"))
    assert spy.seen["conversation_key"] is None
    assert spy.seen["trust_tier"] == "regular"      # endpoint's legacy default
    assert spy.seen["mentioned_entities"] is None


@pytest.mark.asyncio
async def test_derivation_failure_still_evaluates(spy, monkeypatch):
    class _Boom:
        async def get_by_contact(self, contact_id):
            raise RuntimeError("session store down")

    class _BoomContacts:
        async def get(self, contact_id):
            raise RuntimeError("contacts down")

    monkeypatch.setattr(host_mod, "_session_store", _Boom())
    monkeypatch.setattr(host_mod, "_contacts_store", _BoomContacts())
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="hi", target_contact_id="cid-42"))
    assert out["decision"] == "allow"
    assert spy.seen is not None                     # evaluation still ran


# ---------------------------------------------------------------------------
# End-to-end: the previously-dead chat-path cross_context check now fires
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_path_cross_context_fires_with_derived_key(monkeypatch):
    """A plugin-shaped request (no conversation_key, no entities) that
    surfaces an entity known only from ANOTHER private conversation is now
    flagged — before L3.3 this evaluated with a null key and passed."""
    monkeypatch.delenv("COLONY_GUARD_DERIVE_CONTEXT", raising=False)
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    store = ContextProvenanceStore(":memory:")
    store.record("rcs:conv-other", ["Project Falcon"])
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    monkeypatch.setattr(host_mod, "_session_store", _Sessions())
    monkeypatch.setattr(host_mod, "_contacts_store",
                        _Contacts({"cid-42": "regular"}))
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="re: Project Falcon", target_contact_id="cid-42",
        target_gateway="rcs",
        mentioned_entities=["Project Falcon"]))
    assert out["decision"] == "revise"
    assert any(f["check"] == "cross_context" for f in out["findings"])

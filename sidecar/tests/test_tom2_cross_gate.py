"""H3.5 — cross-contact tom2 gate: BUILT, DEFAULT OFF, DELIBERATELY UNWIRED.

render_for_contact is the double gate (COLONY_TOM2_CROSS_CONTEXT=1 AND every
ref independently visible to the reading contact). Any partial visibility
renders None — no redacted hints. No live injection path calls it: with
every tom2 flag forced on, a non-owner context assembly still carries no
tom2 section (test-locked, plus a source-level unwired lock). The doctor
WARNs whenever the flag is on while the chat guard is not enforcing.
"""

from __future__ import annotations

import inspect

import pytest

import colony_sidecar.api.routers.host as host
from colony_sidecar import doctor
from colony_sidecar.api.schemas.host import (
    ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext,
)
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.tom2 import (
    Tom2Store, render_for_contact, render_inference_for_contact,
    tom2_cross_context_enabled,
)

OWNER = "cid-owner-test"


@pytest.fixture()
def stores(tmp_path):
    facts = SharedFactsStore(db_path=str(tmp_path / "facts.db"))
    tom2 = Tom2Store(db_path=str(tmp_path / "tom2.db"))
    # Alice shared a fact; Bob is inferred unaware of it.
    f_alice = facts.create_fact(contact_id="cid-alice",
                                fact="the launch moved to friday",
                                confidence=0.9)
    tom2.record_inference(contact_id="cid-bob", kind="unaware_of",
                          fact_ref=f_alice["id"], confidence=0.4)
    return facts, tom2, f_alice


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CROSS_CONTEXT", raising=False)
    assert tom2_cross_context_enabled() is False


def test_render_none_when_flag_off_even_if_fully_visible(stores, monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CROSS_CONTEXT", raising=False)
    facts, tom2, _ = stores
    assert render_for_contact(tom2, facts, "cid-alice") is None


def test_render_for_entitled_contact_when_flag_on(stores, monkeypatch):
    """Alice owns the fact, so an inference resting only on it is fully
    visible to her."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, _ = stores
    out = render_for_contact(tom2, facts, "cid-alice")
    assert out is not None
    assert "cid-bob has not heard" in out
    assert "the launch moved to friday" in out


def test_render_none_for_unentitled_contact(stores, monkeypatch):
    """Carol never heard the fact — the inference must not render for her,
    and the None carries no redacted hint."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, _ = stores
    assert render_for_contact(tom2, facts, "cid-carol") is None


def test_partial_visibility_renders_none(stores, monkeypatch):
    """One visible ref + one invisible evidence ref = None. Never a
    partially-redacted line."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, f_alice = stores
    f_dave = facts.create_fact(contact_id="cid-dave",
                               fact="dave's private detail", confidence=0.9)
    tom2.record_inference(
        contact_id="cid-bob", kind="unaware_of", fact_ref=f_alice["id"],
        evidence_refs=[f_dave["id"]], confidence=0.4)
    rows = tom2.list_inferences(contact_id="cid-bob")
    mixed = [r for r in rows if r["evidence_refs"]][0]
    line = render_inference_for_contact(mixed, facts, "cid-alice")
    assert line is None
    # aggregate render for alice must not mention dave's fact either way
    out = render_for_contact(tom2, facts, "cid-alice") or ""
    assert "dave" not in out.lower()
    assert "private detail" not in out


def test_missing_evidence_ref_fails_closed(stores, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, f_alice = stores
    tom2.record_inference(contact_id="cid-erin", kind="unaware_of",
                          fact_ref=f_alice["id"],
                          evidence_refs=["fact-that-does-not-exist"],
                          confidence=0.4)
    row = tom2.list_inferences(contact_id="cid-erin")[0]
    assert render_inference_for_contact(row, facts, "cid-alice") is None


def test_inference_about_the_reader_never_renders(stores, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, _ = stores
    row = tom2.list_inferences(contact_id="cid-bob")[0]
    assert render_inference_for_contact(row, facts, "cid-bob") is None


# ---------------------------------------------------------------------------
# UNWIRED lock: this unit ships OFF and connected to nothing live.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_live_injection_even_with_every_flag_on(stores,
                                                         monkeypatch):
    """All tom2 flags forced on: a NON-owner context assembly still carries
    zero tom2 content — the cross-contact renderer is not wired anywhere."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    facts, tom2, _ = stores
    monkeypatch.setattr(host, "_tom2_store", tom2)
    monkeypatch.setattr(host, "_facts_store", facts)
    resp = await host.context_assemble(ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(contact_id="cid-alice", session_id="s1"),
        incoming_message=HostMessage(role="user", content="hi"),
    ))
    assert all(s.id != "colony-tom2" for s in resp.sections)
    joined = "\n".join(s.body for s in resp.sections)
    assert "has not heard" not in joined


def test_renderer_not_referenced_by_context_assembly():
    """Source-level unwired lock: context assembly never calls the
    cross-contact renderer."""
    src = inspect.getsource(host.context_assemble)
    assert "render_for_contact" not in src
    assert "render_inference_for_contact" not in src


# ---------------------------------------------------------------------------
# Doctor posture
# ---------------------------------------------------------------------------

def test_doctor_pass_when_flag_off(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CROSS_CONTEXT", raising=False)
    r = doctor.check_tom2_cross_context()
    assert r.status == doctor.PASS
    assert "ships dark" in r.detail


def test_doctor_warns_when_on_without_chat_enforce(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    for mode in ("", "off", "shadow"):
        if mode:
            monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", mode)
        else:
            monkeypatch.delenv("COLONY_GUARD_CHAT_MODE", raising=False)
        r = doctor.check_tom2_cross_context()
        assert r.status == doctor.WARN, mode
        assert "implication leak" in r.detail


def test_doctor_pass_when_on_with_chat_enforce(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    r = doctor.check_tom2_cross_context()
    assert r.status == doctor.PASS
    assert "unwired" in r.detail

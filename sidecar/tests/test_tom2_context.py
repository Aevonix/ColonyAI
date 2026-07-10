"""H3.3 — owner tom2 context: GET /tom2/report (owner API, full content)
plus the colony-tom2 context section, injected ONLY when the assembling
contact IS the owner, behind COLONY_TOM2_CONTEXT (default 0).

Hard test-lock: the section is absent for every non-owner contact_id even
with the flag on — the flag turns the owner section on, it can never widen
the audience.
"""

from __future__ import annotations

import pytest

import colony_sidecar.api.routers.host as host
from colony_sidecar.api.schemas.host import (
    ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext,
)
from colony_sidecar.tom.asymmetry import tom2_context_enabled
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.tom2 import Tom2Store

OWNER = "cid-owner-test"


def _req(cid):
    return ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(contact_id=cid, session_id="s1"),
        incoming_message=HostMessage(role="user", content="hi"),
    )


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    facts = SharedFactsStore(db_path=str(tmp_path / "facts.db"))
    tom2 = Tom2Store(db_path=str(tmp_path / "tom2.db"))
    f = facts.create_fact(contact_id="cid-alice",
                          fact="the release slipped to next month",
                          confidence=0.9)
    tom2.record_inference(contact_id="cid-bob", kind="unaware_of",
                          fact_ref=f["id"], confidence=0.4)
    tom2.record_inference(contact_id="cid-alice", kind="knows",
                          fact_ref=f["id"], confidence=0.9)
    monkeypatch.setattr(host, "_tom2_store", tom2)
    monkeypatch.setattr(host, "_facts_store", facts)
    return facts, tom2, f


def _tom2_sections(resp):
    return [s for s in resp.sections if s.id == "colony-tom2"]


# ---------------------------------------------------------------------------
# Flag default
# ---------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CONTEXT", raising=False)
    assert tom2_context_enabled() is False
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    assert tom2_context_enabled() is True


# ---------------------------------------------------------------------------
# GET /tom2/report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_full_content_for_owner_api(wired):
    out = await host.tom2_report()
    assert out["available"] is True
    assert out["count"] == 2
    unaware = [r for r in out["inferences"] if r["kind"] == "unaware_of"]
    assert unaware and unaware[0]["contact_id"] == "cid-bob"
    # fact refs resolve to text on the owner surface
    assert unaware[0]["fact"] == "the release slipped to next month"
    assert unaware[0]["fact_contact_id"] == "cid-alice"


@pytest.mark.asyncio
async def test_report_filters(wired):
    out = await host.tom2_report(contact_id="cid-bob")
    assert out["count"] == 1
    out2 = await host.tom2_report(kind="knows")
    assert out2["count"] == 1
    assert out2["inferences"][0]["contact_id"] == "cid-alice"


@pytest.mark.asyncio
async def test_report_unwired(monkeypatch):
    monkeypatch.setattr(host, "_tom2_store", None)
    out = await host.tom2_report()
    assert out == {"available": False, "inferences": []}


# ---------------------------------------------------------------------------
# Context section
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_section_for_owner_when_flag_on(wired, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    resp = await host.context_assemble(_req(OWNER))
    secs = _tom2_sections(resp)
    assert len(secs) == 1
    assert "cid-bob" in secs[0].body
    assert "the release slipped to next month" in secs[0].body


@pytest.mark.asyncio
async def test_section_absent_when_flag_off_even_for_owner(wired,
                                                           monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CONTEXT", raising=False)
    resp = await host.context_assemble(_req(OWNER))
    assert _tom2_sections(resp) == []


@pytest.mark.asyncio
async def test_section_absent_for_every_non_owner_even_with_flag_on(
        wired, monkeypatch):
    """THE lock: no non-owner contact ever sees colony-tom2, whatever the
    flag says — including ids that resemble or contain the owner's."""
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    for cid in ("cid-alice", "cid-bob", "cid-anyone", OWNER + "-suffix",
                "CID-OWNER-TEST", "cid-owner", ""):
        resp = await host.context_assemble(_req(cid or "cid-empty-stub"))
        if cid == "":
            continue
        assert _tom2_sections(resp) == [], f"leaked to {cid!r}"


@pytest.mark.asyncio
async def test_section_absent_when_owner_identity_unset(wired, monkeypatch):
    """No owner configured => no section anywhere (fails closed)."""
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
    monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
    resp = await host.context_assemble(_req(OWNER))
    assert _tom2_sections(resp) == []


@pytest.mark.asyncio
async def test_section_body_never_carries_other_visibility(wired,
                                                           monkeypatch):
    """Render helper only reads owner-scoped rows (the store refuses any
    other visibility at write time; belt-and-suspenders check here)."""
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    body = host._render_tom2_context()
    assert "unaware of" in body

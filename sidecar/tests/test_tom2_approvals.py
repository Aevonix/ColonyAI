"""L2.4 — owner pair-approvals: per (reader, subject) pair, TTL'd, over
the ProposalStore. is_approved answers False on any doubt.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.proposals import ProposalStore
from colony_sidecar.tom.approvals import (
    APPROVED, PENDING, REVOKED, Tom2ApprovalRegistry, approval_ttl_days)

READER, SUBJECT = "cid-alice", "cid-bob"


@pytest.fixture()
def reg():
    return Tom2ApprovalRegistry(ProposalStore())


def test_ttl_env(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_APPROVAL_TTL_DAYS", raising=False)
    assert approval_ttl_days() == 30.0
    monkeypatch.setenv("COLONY_TOM2_APPROVAL_TTL_DAYS", "junk")
    assert approval_ttl_days() == 30.0
    monkeypatch.setenv("COLONY_TOM2_APPROVAL_TTL_DAYS", "-5")
    assert approval_ttl_days() == 0.0


def test_request_files_one_pending_proposal(reg):
    p1 = reg.request_pair(READER, SUBJECT)
    assert p1.status == PENDING
    assert p1.initiative_type == "tom2_pair"
    p2 = reg.request_pair(READER, SUBJECT)      # idempotent
    assert p2.id == p1.id
    assert reg._store.count() == 1
    assert reg.is_approved(READER, SUBJECT) is False   # pending != approved


def test_approve_revoke_lifecycle(reg):
    assert reg.is_approved(READER, SUBJECT) is False
    reg.approve_pair(READER, SUBJECT)
    assert reg.is_approved(READER, SUBJECT) is True
    assert reg.is_approved(SUBJECT, READER) is False   # pairs are directed
    assert reg.revoke_pair(READER, SUBJECT) is True
    assert reg.is_approved(READER, SUBJECT) is False
    assert reg.revoke_pair("cid-x", "cid-y") is False  # nothing to revoke


def test_ttl_expiry_and_refresh(reg, monkeypatch):
    p = reg.approve_pair(READER, SUBJECT)
    p.created_at = time.time() - 31 * 86400            # backdate past TTL
    reg._store.add(p)
    assert reg.is_approved(READER, SUBJECT) is False   # expired on its own
    reg.approve_pair(READER, SUBJECT)                  # re-approve = fresh TTL
    assert reg.is_approved(READER, SUBJECT) is True


def test_prose_ids_refused(reg):
    with pytest.raises(ValueError):
        reg.request_pair("alice from work", SUBJECT)
    with pytest.raises(ValueError):
        reg.approve_pair(READER, "bob said hi")
    assert reg.is_approved("alice from work", SUBJECT) is False  # no raise


def test_is_approved_fails_closed_on_store_error():
    class Broken:
        def list(self, **kw):
            raise RuntimeError("db down")

    reg = Tom2ApprovalRegistry(Broken())
    assert reg.is_approved(READER, SUBJECT) is False
    reg = Tom2ApprovalRegistry(None)
    assert reg.is_approved(READER, SUBJECT) is False


def test_list_pairs(reg):
    reg.approve_pair(READER, SUBJECT)
    reg.request_pair(READER, "cid-carol")
    pairs = reg.list_pairs()
    by = {(p["reader_contact_id"], p["subject_contact_id"]): p
          for p in pairs}
    assert by[(READER, SUBJECT)]["status"] == APPROVED
    assert by[(READER, SUBJECT)]["approved"] is True
    assert by[(READER, "cid-carol")]["status"] == PENDING
    assert by[(READER, "cid-carol")]["approved"] is False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoints_lifecycle(monkeypatch):
    monkeypatch.setattr(host_mod, "_proposal_store", ProposalStore())
    body = host_mod.Tom2PairApprovalRequest(reader=READER, subject=SUBJECT,
                                            action="request")
    out = await host_mod.tom2_approvals_act(body)
    assert out["ok"] is True and out["approved"] is False
    body.action = "approve"
    out = await host_mod.tom2_approvals_act(body)
    assert out["approved"] is True
    listed = await host_mod.tom2_approvals_list()
    assert listed["available"] is True
    assert listed["pairs"][0]["approved"] is True
    body.action = "revoke"
    out = await host_mod.tom2_approvals_act(body)
    assert out["approved"] is False


@pytest.mark.asyncio
async def test_endpoint_errors(monkeypatch):
    monkeypatch.setattr(host_mod, "_proposal_store", None)
    with pytest.raises(HTTPException) as e:
        await host_mod.tom2_approvals_act(host_mod.Tom2PairApprovalRequest(
            reader=READER, subject=SUBJECT))
    assert e.value.status_code == 501
    assert (await host_mod.tom2_approvals_list())["available"] is False

    monkeypatch.setattr(host_mod, "_proposal_store", ProposalStore())
    with pytest.raises(HTTPException) as e:
        await host_mod.tom2_approvals_act(host_mod.Tom2PairApprovalRequest(
            reader=READER, subject=SUBJECT, action="bless"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await host_mod.tom2_approvals_act(host_mod.Tom2PairApprovalRequest(
            reader="alice from accounting", subject=SUBJECT,
            action="approve"))
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# The registry IS the eligibility pipeline's approval hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_feeds_eligibility_pipeline(monkeypatch):
    from tests.test_tom2_eligibility import World

    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    monkeypatch.delenv("COLONY_TOM2_L2_APPROVAL", raising=False)
    world = World()
    reg = Tom2ApprovalRegistry(ProposalStore())
    d = await world.evaluate(approval_check=reg.is_approved)
    assert d.failed_check == "approval"            # default: not approved
    reg.approve_pair(READER, SUBJECT)
    d = await world.evaluate(approval_check=reg.is_approved)
    assert d.eligible is True

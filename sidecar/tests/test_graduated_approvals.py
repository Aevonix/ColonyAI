"""Graduated approval policy v0.18.0 — COLONY_APPROVAL_POLICY=graduated.

Owner's policy: nothing waits on a manual approval unless it is
potentially destructive or an outreach to an unauthorized individual.

Covers:
- policy resolution (env, default strict; unknown values fail closed)
- strict mode preserves v0.17 classification (everything non-read-only
  gated) — test_approval_gate.py runs unchanged alongside this file
- graduated: mutating auto-queues with an audit tag + broadcast;
  destructive blocks; outbound to an authorized contact auto-queues;
  outbound to unknown/unauthorized targets blocks with a reason
- is_authorized_target failure reasons (no store / unknown / not allowed)
- standing approvals: grant via approve {"always": true}, override in
  both modes (even destructive), revoke restores blocking, persistence
  across module reload, list/delete endpoints
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from colony_sidecar.api.routers import task_queue as tq_router
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.events import broadcaster
from colony_sidecar.initiatives import standing_approvals
from colony_sidecar.initiatives.action_registry import (
    ACTION_REGISTRY,
    RiskTier,
    classify_agent_action,
    get_action,
    get_approval_policy,
)
from colony_sidecar.initiatives.approval_policy import is_authorized_target
from colony_sidecar.task_queue.models import JobStatus
from colony_sidecar.task_queue.queue_manager import TaskQueueManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Per-test state dir so standing approvals never leak between tests."""
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("COLONY_APPROVAL_POLICY", raising=False)
    monkeypatch.delenv("COLONY_AGENT_AUTO_APPROVE", raising=False)


@pytest.fixture()
def captured_events():
    """Capture broadcaster emits; restore lazy resolution afterwards."""
    events = []
    broadcaster.reset_broadcaster_for_tests(lambda e: events.append(e))
    yield events
    broadcaster.reset_broadcaster_for_tests(None)


async def _make_mgr(tmp_path) -> TaskQueueManager:
    """Fresh singleton TaskQueueManager backed by a tmp SQLite db."""
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")


def _loop_stub(mgr: TaskQueueManager, contacts=None, context=None) -> SimpleNamespace:
    """Minimal stand-in for AutonomyLoop's self in _post_agent_action_to_queue."""
    return SimpleNamespace(
        _registry=SimpleNamespace(
            task_queue=mgr, initiative_store=None, delivery=None,
            contacts=contacts,
        ),
        config=SimpleNamespace(proactive_delivery_enabled=False),
        stats=SimpleNamespace(actions_executed=0, actions_this_hour=0),
        _build_initiative_context=lambda initiative, type_value: dict(context or {}),
    )


def _initiative(description: str = "Test action", entity_id: str = "e1") -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        entity_id=entity_id,
        priority=0.5,
        rationale="because",
    )


def _contact(cid="cid-bob", name="Bob Vance", allowed=True) -> SimpleNamespace:
    return SimpleNamespace(
        contact_id=cid,
        display_name=name,
        interaction_allowed=allowed,
        trust_tier="trusted",
    )


class _FakeContactStore:
    """Async contact-store double exposing the resolution surface."""

    def __init__(self, by_handle=None, by_cid=None, by_name=None):
        self.by_handle = by_handle or {}
        self.by_cid = by_cid or {}
        self.by_name = by_name or {}

    async def resolve_handle(self, gateway, address):
        return self.by_handle.get((gateway, address))

    async def get(self, contact_id):
        return self.by_cid.get(contact_id)

    async def find_by_name(self, name, threshold=0.5):
        return self.by_name.get(name.lower(), [])


async def _submit(stub, action_hint, initiative_id="init-1", entity_id="e1"):
    await AutonomyLoop._post_agent_action_to_queue(
        stub, _initiative(entity_id=entity_id), initiative_id,
        "agent_action", action_hint,
    )


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------

def test_default_policy_is_strict(monkeypatch):
    assert get_approval_policy() == "strict"
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    assert get_approval_policy() == "graduated"
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", " GRADUATED ")
    assert get_approval_policy() == "graduated"
    # Unknown values fail closed to strict
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "yolo")
    assert get_approval_policy() == "strict"


# ---------------------------------------------------------------------------
# Classification — strict preserves v0.17, graduated differentiates
# ---------------------------------------------------------------------------

def test_strict_gates_everything_non_read_only():
    cases = {
        "commitment_list_open": False,    # read_only
        "commitment_mark_complete": True, # mutating
        "coding_comment_on_pr": True,     # mutating (was outbound in v0.17)
        "coding_merge_pr": True,          # destructive
        "calendar_send_reminder": True,   # outbound
    }
    for name, gated in cases.items():
        verdict = classify_agent_action(name, policy="strict")
        assert verdict["executable"] is True
        assert verdict["requires_approval"] is gated, name
        assert verdict["reason"], name


def test_graduated_classification():
    assert classify_agent_action(
        "commitment_list_open", policy="graduated")["requires_approval"] is False

    mutating = classify_agent_action("commitment_mark_complete", policy="graduated")
    assert mutating["requires_approval"] is False
    assert mutating["reason"] == "graduated_auto_mutating"

    destructive = classify_agent_action("coding_merge_pr", policy="graduated")
    assert destructive["requires_approval"] is True
    assert destructive["reason"] == "destructive_requires_owner"
    assert destructive["risk"] == "destructive"

    outbound = classify_agent_action("calendar_send_reminder", policy="graduated")
    assert outbound["requires_approval"] is True
    assert outbound["reason"] == "outbound_target_unverified"

    authorized = classify_agent_action(
        "calendar_send_reminder", policy="graduated", target_authorized=True)
    assert authorized["requires_approval"] is False
    assert authorized["reason"] == "outbound_authorized_contact"


def test_unregistered_still_fails_closed_in_both_modes():
    for policy in ("strict", "graduated"):
        verdict = classify_agent_action("agent_rm_rf_slash", policy=policy)
        assert verdict["executable"] is False
        assert verdict["requires_approval"] is True
        assert verdict["reason"] == "unregistered_action"


def test_registry_tier_audit():
    """v0.18.0 reclassification table — pin the destructive/outbound sets."""
    tiers = {name: spec.risk for name, spec in ACTION_REGISTRY.items()}
    destructive = {n for n, r in tiers.items() if r == RiskTier.DESTRUCTIVE}
    outbound = {n for n, r in tiers.items() if r == RiskTier.OUTBOUND}

    assert destructive == {
        "agent_cleanup_orphans",   # deletes graph nodes
        "agent_service_restart",
        "agent_file_delete",       # rm
        "agent_deploy",            # overwrites running version
        "coding_merge_pr",
        "system_restart_service",
    }
    # OUTBOUND is reserved for actions that reach a person; every
    # remaining outbound spec must name its recipient param.
    assert outbound == {"calendar_send_reminder", "agent_deliver_message"}
    for name in outbound:
        assert get_action(name).target_param, name
    # Platform writes that message no individual moved to MUTATING.
    assert tiers["coding_comment_on_pr"] == RiskTier.MUTATING
    assert tiers["system_send_alert"] == RiskTier.MUTATING


# ---------------------------------------------------------------------------
# Graduated submission flow (loop.py agent_action region)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graduated_mutating_auto_queues_with_audit(
        tmp_path, monkeypatch, captured_events):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await _submit(stub, "commitment_mark_complete")

        queued = await mgr.queue.get_jobs_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        job = queued[0]
        assert job.tags.get("auto_approved_by_policy") == "graduated"
        assert job.tags.get("risk") == "mutating"
        assert job.payload["destructive"] is False
        assert await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED) == []
        assert stub.stats.actions_executed == 1

        audits = [e for e in captured_events if e["type"] == "action_auto_approved"]
        assert len(audits) == 1
        assert audits[0]["payload"]["action_hint"] == "commitment_mark_complete"
        assert audits[0]["payload"]["policy"] == "graduated"
        assert audits[0]["payload"]["risk"] == "mutating"
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_graduated_destructive_blocks(tmp_path, monkeypatch, captured_events):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await _submit(stub, "coding_merge_pr")

        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        job = blocked[0]
        assert job.tags.get("blocked_reason") == "awaiting_owner_approval"
        assert job.payload["risk"] == "destructive"
        assert job.payload["destructive"] is True
        assert await mgr.queue.get_jobs_by_status(JobStatus.QUEUED) == []
        assert stub.stats.actions_executed == 0
        assert [e for e in captured_events if e["type"] == "action_auto_approved"] == []
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_graduated_outbound_authorized_contact_auto_queues(
        tmp_path, monkeypatch, captured_events):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        store = _FakeContactStore(
            by_handle={("email", "bob@example.com"): _contact(allowed=True)},
        )
        stub = _loop_stub(
            mgr, contacts=store,
            context={"RECIPIENT": "email:bob@example.com"},
        )
        await _submit(stub, "calendar_send_reminder")

        queued = await mgr.queue.get_jobs_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        job = queued[0]
        assert job.tags.get("auto_approved_by_policy") == "graduated"
        assert job.tags.get("risk") == "outbound"
        assert job.tags.get("outbound_target") == "contact:cid-bob"
        assert await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED) == []

        audits = [e for e in captured_events if e["type"] == "action_auto_approved"]
        assert len(audits) == 1
        assert audits[0]["payload"]["reason"] == "outbound_authorized_contact"
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_graduated_outbound_unknown_target_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        # Store resolves nothing for this address
        stub = _loop_stub(
            mgr, contacts=_FakeContactStore(),
            context={"RECIPIENT": "email:stranger@example.com"},
        )
        await _submit(stub, "calendar_send_reminder")

        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        assert blocked[0].tags.get("blocked_reason") == "awaiting_owner_approval"
        assert await mgr.queue.get_jobs_by_status(JobStatus.QUEUED) == []
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_graduated_outbound_unauthorized_contact_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        store = _FakeContactStore(
            by_handle={("email", "spam@example.com"): _contact(
                cid="cid-spam", name="Spam Caller", allowed=False)},
        )
        stub = _loop_stub(
            mgr, contacts=store,
            context={"RECIPIENT": "email:spam@example.com"},
        )
        await _submit(stub, "calendar_send_reminder")

        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        assert await mgr.queue.get_jobs_by_status(JobStatus.QUEUED) == []
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_graduated_outbound_without_contact_store_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(
            mgr, contacts=None,
            context={"RECIPIENT": "email:bob@example.com"},
        )
        await _submit(stub, "calendar_send_reminder")
        assert len(await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)) == 1
        assert await mgr.queue.get_jobs_by_status(JobStatus.QUEUED) == []
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# is_authorized_target — failure reasons and resolution forms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_authorized_target_reasons():
    spec = get_action("calendar_send_reminder")
    bob = _contact(allowed=True)

    assert await is_authorized_target(
        {"RECIPIENT": "email:bob@x.com"}, spec, None,
    ) == (False, "no_contact_store")

    store = _FakeContactStore(by_handle={("email", "bob@x.com"): bob})
    assert await is_authorized_target({}, spec, store) == (False, "unknown_target")
    assert await is_authorized_target(
        {"RECIPIENT": "email:nobody@x.com"}, spec, store,
    ) == (False, "unknown_target")

    not_allowed = _FakeContactStore(
        by_handle={("email", "bob@x.com"): _contact(allowed=False)})
    assert await is_authorized_target(
        {"RECIPIENT": "email:bob@x.com"}, spec, not_allowed,
    ) == (False, "contact_not_authorized")

    ok, reason = await is_authorized_target(
        {"RECIPIENT": "email:bob@x.com"}, spec, store)
    assert ok is True and reason == "contact:cid-bob"

    # Target found in the nested context dict, gateway:address form
    ok, _ = await is_authorized_target(
        {"context": {"RECIPIENT": "email:bob@x.com"}}, spec, store)
    assert ok is True

    # CID and unambiguous-name fallbacks
    store2 = _FakeContactStore(
        by_cid={"cid-bob": bob},
        by_name={"bob vance": [bob]},
    )
    ok, _ = await is_authorized_target({"RECIPIENT": "cid-bob"}, spec, store2)
    assert ok is True
    ok, _ = await is_authorized_target({"RECIPIENT": "Bob Vance"}, spec, store2)
    assert ok is True

    # Ambiguous names never authorize
    ambiguous = _FakeContactStore(
        by_name={"bob": [_contact("cid-1", "Bob A"), _contact("cid-2", "Bob B")]})
    assert await is_authorized_target(
        {"RECIPIENT": "Bob"}, spec, ambiguous,
    ) == (False, "unknown_target")


# ---------------------------------------------------------------------------
# Standing approvals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_always_grants_standing_and_unblocks_next_job(tmp_path):
    """grant via approve always:true → next same-action job auto-queues
    even though it is destructive; revoke restores blocking."""
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)

        # 1. Destructive job blocks (default strict policy)
        await _submit(stub, "coding_merge_pr", "init-1", entity_id="pr-1")
        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        job_id = blocked[0].job_id

        # 2. Owner approves with always=true
        resp = await tq_router.approve_job(
            job_id,
            tq_router.JobApproveRequest(approved_by="sam", always=True),
        )
        assert resp["success"] is True
        assert resp["standing_approval"]["action_name"] == "coding_merge_pr"
        assert resp["standing_approval"]["approved_by"] == "sam"
        assert standing_approvals.is_approved("coding_merge_pr") is True
        assert (await mgr.queue.get_job(job_id)).status == JobStatus.QUEUED

        # 3. Next job for the same action auto-queues, with audit tag
        await _submit(stub, "coding_merge_pr", "init-2", entity_id="pr-2")
        queued = await mgr.queue.get_jobs_by_status(JobStatus.QUEUED)
        new_jobs = [j for j in queued if j.job_id != job_id]
        assert len(new_jobs) == 1
        assert new_jobs[0].tags.get("auto_approved_by_policy") == "standing_approval"
        assert await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED) == []

        # 4. Revoke → blocking restored
        resp = await tq_router.revoke_standing_approval("coding_merge_pr")
        assert resp["success"] is True
        assert standing_approvals.is_approved("coding_merge_pr") is False

        await _submit(stub, "coding_merge_pr", "init-3", entity_id="pr-3")
        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        assert blocked[0].tags.get("blocked_reason") == "awaiting_owner_approval"
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_plain_approve_does_not_grant_standing(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await _submit(stub, "coding_merge_pr")
        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        resp = await tq_router.approve_job(
            blocked[0].job_id, tq_router.JobApproveRequest(approved_by="sam"),
        )
        assert resp["standing_approval"] is None
        assert standing_approvals.is_approved("coding_merge_pr") is False
    finally:
        await mgr.stop()


def test_standing_approval_overrides_both_modes():
    for policy in ("strict", "graduated"):
        assert classify_agent_action(
            "coding_merge_pr", policy=policy)["requires_approval"] is True
    standing_approvals.grant("coding_merge_pr", approved_by="sam")
    for policy in ("strict", "graduated"):
        verdict = classify_agent_action("coding_merge_pr", policy=policy)
        assert verdict["requires_approval"] is False, policy
        assert verdict["reason"] == "standing_approval"
    # Unregistered actions stay non-executable even with a grant
    standing_approvals.grant("agent_rm_rf_slash", approved_by="sam")
    assert classify_agent_action("agent_rm_rf_slash")["executable"] is False


def test_standing_approvals_persist_across_reload(tmp_path):
    standing_approvals.grant("coding_merge_pr", approved_by="sam")
    standing_approvals.grant("calendar_send_reminder", approved_by="sam")

    # On disk, under $COLONY_STATE_DIR
    path = Path(standing_approvals._path())
    assert path.name == "standing_approvals.json"
    on_disk = json.loads(path.read_text())
    assert set(on_disk) == {"coding_merge_pr", "calendar_send_reminder"}

    # Survives a module reload (fresh process simulation)
    fresh = importlib.reload(standing_approvals)
    assert fresh.is_approved("coding_merge_pr") is True
    names = [e["action_name"] for e in fresh.list()]
    assert names == ["calendar_send_reminder", "coding_merge_pr"]  # sorted

    assert fresh.revoke("coding_merge_pr") is True
    assert fresh.revoke("coding_merge_pr") is False
    assert fresh.is_approved("coding_merge_pr") is False
    assert fresh.is_approved("calendar_send_reminder") is True


def test_corrupt_standing_approvals_file_fails_closed():
    path = standing_approvals._path()
    path.write_text("{not json")
    assert standing_approvals.load() == {}
    assert standing_approvals.is_approved("coding_merge_pr") is False
    # classify also stays gated
    assert classify_agent_action(
        "coding_merge_pr", policy="graduated")["requires_approval"] is True


@pytest.mark.asyncio
async def test_standing_approval_endpoints():
    standing_approvals.grant("coding_merge_pr", approved_by="sam")

    items = await tq_router.list_standing_approvals()
    assert [e["action_name"] for e in items] == ["coding_merge_pr"]

    resp = await tq_router.revoke_standing_approval("coding_merge_pr")
    assert resp == {"success": True, "action_name": "coding_merge_pr"}
    assert await tq_router.list_standing_approvals() == []

    with pytest.raises(HTTPException) as exc_info:
        await tq_router.revoke_standing_approval("coding_merge_pr")
    assert exc_info.value.status_code == 404

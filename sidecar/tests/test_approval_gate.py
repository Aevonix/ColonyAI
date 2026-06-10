"""Approval gate v0.17.0 — server-side BLOCKED lifecycle for gated agent actions.

Covers:
- Gated (mutating/outbound) agent_action submissions enter BLOCKED with
  blocked_reason=awaiting_owner_approval; read_only and auto-approved
  submissions enter QUEUED
- claim_job never hands out BLOCKED jobs
- approve: BLOCKED → QUEUED, records approved_by/approved_at, claimable after
- reject: BLOCKED → CANCELLED, records reason
- approve/reject return 409 when the job is not BLOCKED
- approval timeout sweep fails stale BLOCKED jobs with owner_approval_timeout
- respond_to_initiative syncs approve/dismiss to the linked BLOCKED job
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from colony_sidecar.api.routers import task_queue as tq_router
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.task_queue.models import (
    Job,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_mgr(tmp_path) -> TaskQueueManager:
    """Fresh singleton TaskQueueManager backed by a tmp SQLite db."""
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")


def _worker_caps(node_id: str = "test-worker") -> WorkerCapabilities:
    return WorkerCapabilities(node_id=node_id)


def _loop_stub(mgr: TaskQueueManager) -> SimpleNamespace:
    """Minimal stand-in for AutonomyLoop's self in _post_agent_action_to_queue."""
    return SimpleNamespace(
        _registry=SimpleNamespace(task_queue=mgr, initiative_store=None, delivery=None),
        config=SimpleNamespace(proactive_delivery_enabled=False),
        stats=SimpleNamespace(actions_executed=0, actions_this_hour=0),
        _build_initiative_context=lambda initiative, type_value: {},
    )


def _initiative(description: str = "Test action", entity_id: str = "e1") -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        entity_id=entity_id,
        priority=0.5,
        rationale="because",
    )


async def _submit_blocked(mgr: TaskQueueManager, **payload_overrides) -> str:
    """Submit an agent_action job directly in BLOCKED (awaiting approval)."""
    params = {
        "action_hint": "commitment_mark_complete",
        "risk": "mutating",
        "description": "Mark a commitment fulfilled",
    }
    params.update(payload_overrides)
    submitted = await mgr.submit(
        task_type="agent_action",
        params=params,
        initial_status=JobStatus.BLOCKED,
        tags={"blocked_reason": "awaiting_owner_approval"},
    )
    return submitted["id"]


# ---------------------------------------------------------------------------
# Blocked-at-submission classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gated_action_submission_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv("COLONY_AGENT_AUTO_APPROVE", raising=False)
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await AutonomyLoop._post_agent_action_to_queue(
            stub, _initiative(), "init-1", "commitment", "commitment_mark_complete",
        )
        blocked = await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(blocked) == 1
        job = blocked[0]
        assert job.tags.get("blocked_reason") == "awaiting_owner_approval"
        assert job.payload["initiative_id"] == "init-1"
        assert job.payload["destructive"] is True
        # Never created as QUEUED, and not counted as executed
        assert await mgr.queue.get_jobs_by_status(JobStatus.QUEUED) == []
        assert stub.stats.actions_executed == 0
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_read_only_action_submission_is_queued(tmp_path, monkeypatch):
    monkeypatch.delenv("COLONY_AGENT_AUTO_APPROVE", raising=False)
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await AutonomyLoop._post_agent_action_to_queue(
            stub, _initiative(), "init-2", "commitment", "commitment_list_open",
        )
        queued = await mgr.queue.get_jobs_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        assert queued[0].tags.get("blocked_reason") is None
        assert await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED) == []
        assert stub.stats.actions_executed == 1
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_auto_approve_env_bypasses_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_AUTO_APPROVE", "true")
    mgr = await _make_mgr(tmp_path)
    try:
        stub = _loop_stub(mgr)
        await AutonomyLoop._post_agent_action_to_queue(
            stub, _initiative(), "init-3", "commitment", "commitment_mark_complete",
        )
        queued = await mgr.queue.get_jobs_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        assert await mgr.queue.get_jobs_by_status(JobStatus.BLOCKED) == []
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Claim protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_excludes_blocked_jobs(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        blocked_id = await _submit_blocked(mgr)

        # Only a blocked job exists — nothing claimable
        assert await mgr.queue.claim_job("w1", _worker_caps("w1")) is None

        # A queued job alongside it is claimable; the blocked one is not
        queued = await mgr.submit(task_type="agent_action", params={"x": 1})
        claimed = await mgr.queue.claim_job("w1", _worker_caps("w1"))
        assert claimed is not None
        assert claimed.job_id == queued["id"]
        assert claimed.job_id != blocked_id

        # Blocked job untouched
        job = await mgr.queue.get_job(blocked_id)
        assert job.status == JobStatus.BLOCKED
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Approve / reject endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_transitions_to_queued_and_claimable(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        job_id = await _submit_blocked(mgr)
        resp = await tq_router.approve_job(
            job_id, tq_router.JobApproveRequest(approved_by="marc"),
        )
        assert resp["success"] is True
        assert resp["status"] == "queued"

        job = await mgr.queue.get_job(job_id)
        assert job.status == JobStatus.QUEUED
        assert job.tags.get("approved_by") == "marc"
        assert job.tags.get("approved_at")

        # Approved job is claimable now
        claimed = await mgr.queue.claim_job("w1", _worker_caps("w1"))
        assert claimed is not None
        assert claimed.job_id == job_id
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_reject_transitions_to_cancelled(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        job_id = await _submit_blocked(mgr)
        resp = await tq_router.reject_job(
            job_id,
            tq_router.JobRejectRequest(rejected_by="marc", reason="too risky"),
        )
        assert resp["success"] is True
        assert resp["status"] == "cancelled"

        job = await mgr.queue.get_job(job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.tags.get("rejected_by") == "marc"
        assert job.tags.get("rejected_reason") == "too risky"

        # Rejected job is never claimable
        assert await mgr.queue.claim_job("w1", _worker_caps("w1")) is None

        # Audit trail records the reason
        entries = await mgr.queue.get_audit_log(job_id=job_id)
        assert any(e.reason == "too risky" for e in entries)
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_approve_and_reject_409_when_not_blocked(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        queued = await mgr.submit(task_type="agent_action", params={"x": 1})
        job_id = queued["id"]

        with pytest.raises(HTTPException) as exc_info:
            await tq_router.approve_job(job_id, tq_router.JobApproveRequest())
        assert exc_info.value.status_code == 409

        with pytest.raises(HTTPException) as exc_info:
            await tq_router.reject_job(job_id, tq_router.JobRejectRequest())
        assert exc_info.value.status_code == 409

        # Unknown job → 404
        with pytest.raises(HTTPException) as exc_info:
            await tq_router.approve_job("no-such-job", tq_router.JobApproveRequest())
        assert exc_info.value.status_code == 404
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_blocked_listing_only_includes_approval_gated(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        approval_id = await _submit_blocked(mgr, description="Needs owner sign-off")

        # Dependency-blocked job — must not appear in the approval list
        dep = Job(job_type=JobType.CUSTOM, payload={}, posted_by="test")
        await mgr.queue.post(dep)
        dependent = Job(
            job_type=JobType.CUSTOM,
            payload={},
            posted_by="test",
            depends_on=[dep.job_id],
        )
        await mgr.queue.post(dependent)
        assert (await mgr.queue.get_job(dependent.job_id)).status == JobStatus.BLOCKED

        # Explicit limit: calling the endpoint function directly bypasses
        # FastAPI's Query() default resolution.
        items = await tq_router.list_blocked_jobs(limit=50)
        assert len(items) == 1
        item = items[0]
        assert item["id"] == approval_id
        assert item["action_hint"] == "commitment_mark_complete"
        assert item["risk"] == "mutating"
        assert item["description"] == "Needs owner sign-off"
        assert item["created_at"]
        assert item["blocked_reason"] == "awaiting_owner_approval"
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Approval timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_timeout_fails_stale_blocked_jobs(tmp_path):
    mgr = await _make_mgr(tmp_path)
    try:
        now = datetime.now(timezone.utc)

        stale = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
            posted_by="test",
            posted_at=now - timedelta(hours=100),
            status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        await mgr.queue.post(stale)

        fresh_id = await _submit_blocked(mgr)

        # Old dependency-blocked job — must not be swept
        dep = Job(job_type=JobType.CUSTOM, payload={}, posted_by="test")
        await mgr.queue.post(dep)
        dep_blocked = Job(
            job_type=JobType.CUSTOM,
            payload={},
            posted_by="test",
            posted_at=now - timedelta(hours=100),
            depends_on=[dep.job_id],
        )
        await mgr.queue.post(dep_blocked)

        count = await mgr.queue.expire_blocked_approvals(now, timeout_hours=72.0)
        assert count == 1

        assert (await mgr.queue.get_job(stale.job_id)).status == JobStatus.FAILED
        assert (await mgr.queue.get_job(fresh_id)).status == JobStatus.BLOCKED
        assert (await mgr.queue.get_job(dep_blocked.job_id)).status == JobStatus.BLOCKED

        entries = await mgr.queue.get_audit_log(job_id=stale.job_id)
        assert any(e.reason == "owner_approval_timeout" for e in entries)
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_phase_approval_timeout_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_TIMEOUT_HOURS", "1")
    mgr = await _make_mgr(tmp_path)
    try:
        stale = Job(
            job_type=JobType.AGENT_ACTION,
            payload={},
            posted_by="test",
            posted_at=datetime.now(timezone.utc) - timedelta(hours=2),
            status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        await mgr.queue.post(stale)

        await AutonomyLoop._phase_approval_timeout(_loop_stub(mgr))
        assert (await mgr.queue.get_job(stale.job_id)).status == JobStatus.FAILED
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Initiative-response sync (host.py respond_to_initiative)
# ---------------------------------------------------------------------------

class _FakeInitiativeStore:
    def __init__(self, initiative):
        self._initiative = initiative
        self.history = []

    def get(self, initiative_id):
        return self._initiative

    def update(self, initiative_id, **kwargs):
        for key, value in kwargs.items():
            setattr(self._initiative, key, value)

    def log_history(self, initiative_id, **kwargs):
        self.history.append((initiative_id, kwargs))


@pytest.mark.asyncio
async def test_initiative_approve_response_unblocks_job(tmp_path):
    from colony_sidecar.api.routers import host as host_router

    mgr = await _make_mgr(tmp_path)
    old_store = host_router._initiative_store
    try:
        job_id = await _submit_blocked(mgr)
        initiative = SimpleNamespace(id="init-1", status="pending", job_id=job_id)
        host_router.set_initiative_store(_FakeInitiativeStore(initiative))

        resp = await host_router.respond_to_initiative(
            "init-1", action="approve", details=None,
        )
        assert resp["success"] is True

        job = await mgr.queue.get_job(job_id)
        assert job.status == JobStatus.QUEUED
        assert job.tags.get("approved_by") == "owner"
        assert job.tags.get("approved_at")
    finally:
        host_router.set_initiative_store(old_store)
        await mgr.stop()


@pytest.mark.asyncio
async def test_initiative_dismiss_response_rejects_job(tmp_path):
    from colony_sidecar.api.routers import host as host_router

    mgr = await _make_mgr(tmp_path)
    old_store = host_router._initiative_store
    try:
        job_id = await _submit_blocked(mgr)
        initiative = SimpleNamespace(id="init-2", status="pending", job_id=job_id)
        host_router.set_initiative_store(_FakeInitiativeStore(initiative))

        resp = await host_router.respond_to_initiative(
            "init-2", action="dismiss", details=None,
        )
        assert resp["success"] is True

        job = await mgr.queue.get_job(job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.tags.get("rejected_by") == "owner"
    finally:
        host_router.set_initiative_store(old_store)
        await mgr.stop()

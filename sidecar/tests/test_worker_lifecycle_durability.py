"""Adversarial claim-attempt, lease, transaction, and outcome durability tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from colony_sidecar.task_queue.models import (
    Job,
    JobStatus,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.task_queue.scheduler import Scheduler


async def _manager(tmp_path, *, claim_timeout_secs=30.0):
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(
        db_path=tmp_path / "queue.db",
        claim_timeout_secs=claim_timeout_secs,
    )


async def _claim(manager, job, worker="worker-a"):
    await manager.queue.post(job)
    claimed = await manager.queue.claim_job(
        worker, WorkerCapabilities(node_id=worker),
    )
    assert claimed is not None
    assert claimed.claim_attempt_id
    return claimed


@pytest.mark.asyncio
async def test_stale_attempt_cannot_start_same_worker_reclaim(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        job = Job()
        first = await _claim(manager, job)
        assert await manager.queue.release_job(
            job.job_id, "worker-a", first.claim_attempt_id,
        )
        second = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert second is not None
        assert second.claim_attempt_id != first.claim_attempt_id

        assert not await manager.queue.start_job(
            job.job_id, "worker-a", first.claim_attempt_id,
        )
        current = await manager.queue.get_job(job.job_id)
        assert current is not None and current.status is JobStatus.CLAIMED
        assert current.claim_attempt_id == second.claim_attempt_id
        assert await manager.queue.start_job(
            job.job_id, "worker-a", second.claim_attempt_id,
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_same_attempt_lifecycle_replays_are_idempotent(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        started = await _claim(manager, Job())
        assert await manager.queue.start_job(
            started.job_id, "worker-a", started.claim_attempt_id,
        )
        assert await manager.queue.start_job(
            started.job_id, "worker-a", started.claim_attempt_id,
        )
        assert await manager.queue.fail_job(
            started.job_id,
            "worker-a",
            "retryable failure",
            claim_attempt_id=started.claim_attempt_id,
        )
        assert await manager.queue.fail_job(
            started.job_id,
            "worker-a",
            "lost response retry",
            claim_attempt_id=started.claim_attempt_id,
        )

        released = await _claim(manager, Job())
        assert await manager.queue.release_job(
            released.job_id, "worker-a", released.claim_attempt_id,
        )
        assert await manager.queue.release_job(
            released.job_id, "worker-a", released.claim_attempt_id,
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_pre_migration_null_attempt_holds_restart_and_cannot_finish(
        tmp_path, monkeypatch):
    """A new server never downgrades to the legacy NULL attempt protocol."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    db_path = tmp_path / "queue.db"
    manager = await _manager(tmp_path)
    try:
        legacy = Job()
        await manager.queue.post(legacy)
        await manager.queue._db.execute(
            """UPDATE jobs SET status = ?, claimed_by = ?, claimed_at = ?,
                      claim_attempt_id = NULL
               WHERE job_id = ?""",
            (
                JobStatus.CLAIMED.value,
                "legacy-worker",
                datetime.now(timezone.utc).isoformat(),
                legacy.job_id,
            ),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        readiness = manager.queue.execution_readiness()
        assert readiness["ready"] is False
        assert readiness["routing_ready"] is False
        assert "incompatible_active_attempts" in readiness["reason"]
        assert await manager.queue.start_job(
            legacy.job_id, "legacy-worker", claim_attempt_id=None,
        ) is False
        result = await manager.queue.complete_job(
            legacy.job_id,
            "legacy-worker",
            {"status": "verified"},
            claim_attempt_id=None,
        )
        assert result["transitioned"] is False
        with pytest.raises(Exception, match="incompatible_active_attempts"):
            await manager.queue.claim_job(
                "typed-worker",
                WorkerCapabilities(node_id="typed-worker"),
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_missing_dependencies_are_rejected_and_never_unblocked(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        with pytest.raises(ValueError, match="does not exist"):
            await manager.queue.post(Job(depends_on=["missing-job"]))

        legacy = Job()
        await manager.queue.post(legacy)
        await manager.queue._db.execute(
            "UPDATE jobs SET status = ?, depends_on = ?, tags = ? WHERE job_id = ?",
            (
                JobStatus.BLOCKED.value,
                json.dumps(["missing-legacy-dependency"]),
                json.dumps({"hold_kind": "dependency"}),
                legacy.job_id,
            ),
        )
        await manager.queue._db.commit()
        assert await manager.queue.unblock_ready_jobs() == 1
        stored = await manager.queue.get_job(legacy.job_id)
        assert stored is not None and stored.status is JobStatus.FAILED
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_start_rejects_deadline_and_claim_lease_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path, claim_timeout_secs=0.02)
    try:
        deadline_job = Job(
            deadline=datetime.now(timezone.utc) + timedelta(milliseconds=20),
        )
        deadline_claim = await _claim(manager, deadline_job)
        await asyncio.sleep(0.04)
        assert not await manager.queue.start_job(
            deadline_job.job_id, "worker-a", deadline_claim.claim_attempt_id,
        )
        expired = await manager.queue.get_job(deadline_job.job_id)
        assert expired is not None and expired.status is JobStatus.FAILED

        lease_job = Job()
        lease_claim = await _claim(manager, lease_job)
        await asyncio.sleep(0.04)
        assert not await manager.queue.start_job(
            lease_job.job_id, "worker-a", lease_claim.claim_attempt_id,
        )
        released = await manager.queue.get_job(lease_job.job_id)
        assert released is not None and released.status is JobStatus.QUEUED
        assert released.claim_attempt_id is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_late_completion_becomes_server_timeout_not_success(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        job = Job(timeout_secs=0.02)
        claimed = await _claim(manager, job)
        assert await manager.queue.start_job(
            job.job_id, "worker-a", claimed.claim_attempt_id,
        )
        await asyncio.sleep(0.04)
        result = await manager.queue.complete_job(
            job.job_id,
            "worker-a",
            {"status": "verified"},
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert result["transitioned"] is False
        assert result["outcome_reason"] == "server_execution_timeout"
        stored = await manager.queue.get_job(job.job_id)
        assert stored is not None and stored.status is JobStatus.QUEUED
        assert stored.result is not None
        assert stored.result.error == "server_execution_timeout"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_timing_never_reuses_prior_same_worker_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        job = Job(max_retries=3)
        first = await _claim(manager, job)
        assert await manager.queue.start_job(
            job.job_id, "worker-a", first.claim_attempt_id,
        )
        assert await manager.queue.fail_job(
            job.job_id,
            "worker-a",
            "attempt-1",
            claim_attempt_id=first.claim_attempt_id,
        )
        await asyncio.sleep(0.01)
        second = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert second is not None and second.claimed_at is not None
        assert await manager.queue.fail_job(
            job.job_id,
            "worker-a",
            "pre-start-attempt-2",
            claim_attempt_id=second.claim_attempt_id,
        )
        stored = await manager.queue.get_job(job.job_id)
        assert stored is not None and stored.result is not None
        assert stored.result.started_at is not None
        assert stored.result.started_at >= second.claimed_at
        assert stored.result.claim_attempt_id == second.claim_attempt_id
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reads_wait_for_transaction_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    job = Job()
    claimed = await _claim(manager, job)
    entered = asyncio.Event()
    hold = asyncio.Event()
    original_audit = manager.queue._audit

    async def paused_audit(job_id, from_status, to_status, **kwargs):
        if job_id == job.job_id and to_status == JobStatus.RUNNING.value:
            entered.set()
            await hold.wait()
        return await original_audit(job_id, from_status, to_status, **kwargs)

    manager.queue._audit = paused_audit
    starter = asyncio.create_task(manager.queue.start_job(
        job.job_id, "worker-a", claimed.claim_attempt_id,
    ))
    try:
        await entered.wait()
        reader = asyncio.create_task(manager.queue.get_job(job.job_id))
        await asyncio.sleep(0)
        assert not reader.done()
        starter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starter
        observed = await reader
        assert observed is not None and observed.status is JobStatus.CLAIMED
    finally:
        hold.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_escape_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    job = Job()
    claimed = await _claim(manager, job)
    audit_entered = asyncio.Event()
    audit_hold = asyncio.Event()
    rollback_entered = asyncio.Event()
    rollback_hold = asyncio.Event()
    original_audit = manager.queue._audit
    original_rollback = manager.queue._db.rollback

    async def paused_audit(job_id, from_status, to_status, **kwargs):
        if job_id == job.job_id and to_status == JobStatus.RUNNING.value:
            audit_entered.set()
            await audit_hold.wait()
        return await original_audit(job_id, from_status, to_status, **kwargs)

    async def paused_rollback():
        rollback_entered.set()
        await rollback_hold.wait()
        return await original_rollback()

    manager.queue._audit = paused_audit
    manager.queue._db.rollback = paused_rollback
    starter = asyncio.create_task(manager.queue.start_job(
        job.job_id, "worker-a", claimed.claim_attempt_id,
    ))
    try:
        await audit_entered.wait()
        starter.cancel()
        await rollback_entered.wait()
        starter.cancel()
        await asyncio.sleep(0)
        assert not starter.done()
        rollback_hold.set()
        with pytest.raises(asyncio.CancelledError):
            await starter
        manager.queue._db.rollback = original_rollback
        manager.queue._audit = original_audit
        await manager.queue.register_worker(WorkerCapabilities(node_id="other"))
        stored = await manager.queue.get_job(job.job_id)
        audit = await manager.queue.get_audit_log(job.job_id)
        assert stored is not None and stored.status is JobStatus.CLAIMED
        assert not any(entry.to_status == "running" for entry in audit)
    finally:
        rollback_hold.set()
        audit_hold.set()
        manager.queue._db.rollback = original_rollback
        manager.queue._audit = original_audit
        await manager.stop()


@pytest.mark.asyncio
async def test_slow_outcome_delivery_never_blocks_queue_reads(
        tmp_path, monkeypatch):
    """Durable governor work runs outside the queue transaction/response."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    blocking = _BlockingOutcomeGovernor()
    manager.queue.configure_governance(blocking)
    try:
        claimed = await _claim(manager, Job())
        assert await manager.queue.start_job(
            claimed.job_id, "worker-a", claimed.claim_attempt_id,
        )
        completion = await asyncio.wait_for(
            manager.queue.complete_job(
                claimed.job_id,
                "worker-a",
                {"status": "verified"},
                claim_attempt_id=claimed.claim_attempt_id,
            ),
            timeout=1,
        )
        assert completion["transitioned"] is True
        await blocking.entered.wait()
        stats = await asyncio.wait_for(manager.queue.get_queue_stats(), timeout=1)
        assert stats.by_status.get("completed") == 1
    finally:
        blocking.hold.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_scheduler_uses_separate_claim_and_heartbeat_timeouts(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path, claim_timeout_secs=10.0)
    scheduler = Scheduler(
        manager.queue,
        heartbeat_timeout_secs=10.0,
        claim_timeout_secs=0.02,
    )
    try:
        stale_claim = await _claim(manager, Job(), worker="claim-worker")
        running = await _claim(manager, Job(), worker="running-worker")
        assert await manager.queue.start_job(
            running.job_id, "running-worker", running.claim_attempt_id,
        )
        await asyncio.sleep(0.04)
        await scheduler.tick_once()

        claimed_job = await manager.queue.get_job(stale_claim.job_id)
        running_job = await manager.queue.get_job(running.job_id)
        assert claimed_job is not None and claimed_job.status is JobStatus.QUEUED
        assert claimed_job.retry_count == 0
        assert running_job is not None and running_job.status is JobStatus.RUNNING
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_running_liveness_loss_is_durable_failure_but_claim_loss_is_neutral(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        running = await _claim(
            manager, Job(max_retries=1), worker="running-worker",
        )
        assert await manager.queue.start_job(
            running.job_id, "running-worker", running.claim_attempt_id,
        )
        claimed = await _claim(
            manager, Job(max_retries=1), worker="claimed-worker",
        )

        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert await manager.queue.abandon_silent_jobs(
            future, timeout_secs=60, claim_timeout_secs=30,
        ) == 2
        running_stored = await manager.queue.get_job(running.job_id)
        claimed_stored = await manager.queue.get_job(claimed.job_id)
        assert running_stored.status is JobStatus.QUEUED
        assert running_stored.retry_count == 1
        assert running_stored.result.error == "worker_heartbeat_timeout"
        assert claimed_stored.status is JobStatus.QUEUED
        assert claimed_stored.retry_count == 0
        outcomes = await manager.queue.pending_worker_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0]["claim_attempt_id"] == running.claim_attempt_id
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_node_death_releases_claim_and_records_running_failure(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        first = await _claim(manager, Job(), worker="dead-node")
        second_job = Job(priority=first.priority)
        await manager.queue.post(second_job)
        # Capacity one prevents a second claim until the first starts, but a
        # running job still counts; use a separate direct capability ceiling.
        running = first
        assert await manager.queue.start_job(
            running.job_id, "dead-node", running.claim_attempt_id,
        )
        caps = WorkerCapabilities(node_id="dead-node", max_concurrent=2)
        claimed = await manager.queue.claim_job("dead-node", caps)
        assert claimed is not None
        affected = await manager.queue.abandon_jobs_for_node("dead-node")
        assert set(affected) == {running.job_id, claimed.job_id}
        assert (await manager.queue.get_job(running.job_id)).retry_count == 1
        assert (await manager.queue.get_job(claimed.job_id)).retry_count == 0
        outcomes = await manager.queue.pending_worker_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0]["claim_attempt_id"] == running.claim_attempt_id
    finally:
        await manager.stop()


class _BlockingOutcomeGovernor:
    def __init__(self):
        self.entered = asyncio.Event()
        self.hold = asyncio.Event()

    def audit_report(self, job, output):  # noqa: ARG002
        return {"verdict": "clean", "findings": []}

    def classify_completion_outcome(self, output, verdict):  # noqa: ARG002
        return "success", "verified_completion"

    async def record_outcome(self, *args, **kwargs):
        self.entered.set()
        await self.hold.wait()


class _RecordingOutcomeGovernor(_BlockingOutcomeGovernor):
    def __init__(self):
        super().__init__()
        self.events = []
        self.modes = []

    async def record_outcome(self, *args, **kwargs):
        self.events.append(kwargs.get("event_id"))
        self.modes.append(kwargs.get("event_mode"))


@pytest.mark.asyncio
async def test_completion_outcome_outbox_recovers_after_cancel_and_restart(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    db_path = tmp_path / "queue.db"
    manager = await _manager(tmp_path)
    blocking = _BlockingOutcomeGovernor()
    manager.queue.configure_governance(blocking)
    job = Job()
    claimed = await _claim(manager, job)
    assert await manager.queue.start_job(
        job.job_id, "worker-a", claimed.claim_attempt_id,
    )
    completion = await manager.queue.complete_job(
        job.job_id,
        "worker-a",
        {"status": "verified", "summary": "done"},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert completion["transitioned"] is True
    await blocking.entered.wait()
    drains = list(manager.queue._outcome_tasks)
    assert len(drains) == 1
    drains[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await drains[0]
    completed = await manager.queue.get_job(job.job_id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
    pending = await manager.queue.pending_worker_outcomes()
    assert len(pending) == 1 and pending[0]["state"] == "pending"
    event_id = pending[0]["event_id"]
    await manager.stop()

    TaskQueueManager._instance = None
    restarted = await TaskQueueManager.initialize(db_path=db_path)
    recording = _RecordingOutcomeGovernor()
    restarted.queue.configure_governance(recording)
    try:
        assert await restarted.queue.drain_worker_outcomes() == 1
        assert recording.events == [event_id]
        assert await restarted.queue.drain_worker_outcomes() == 0
        assert recording.events == [event_id]
        rows = await restarted.queue.pending_worker_outcomes(include_delivered=True)
        assert rows[0]["state"] == "delivered"
    finally:
        blocking.hold.set()
        await restarted.stop()


@pytest.mark.asyncio
async def test_outbox_preserves_claim_mode_across_runtime_flip(
        tmp_path, monkeypatch):
    """A shadow-authorized attempt can never become live trust evidence."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    manager = await _manager(tmp_path)
    governor = _RecordingOutcomeGovernor()
    manager.queue.configure_governance(governor)
    try:
        claimed = await _claim(manager, Job())
        assert claimed.tags["governor_mode"] == "shadow"
        assert await manager.queue.start_job(
            claimed.job_id, "worker-a", claimed.claim_attempt_id,
        )
        monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
        result = await manager.queue.complete_job(
            claimed.job_id,
            "worker-a",
            {"status": "verified"},
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert result["transitioned"] is True
        await manager.queue.drain_worker_outcomes()
        assert governor.modes == ["shadow"]
    finally:
        governor.hold.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_prune_preserves_job_until_pending_outcome_is_delivered(
        tmp_path, monkeypatch):
    """Retention must not orphan durable competence evidence."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    old_job = Job(posted_at=datetime.now(timezone.utc) - timedelta(days=60))
    claimed = await _claim(manager, old_job)
    assert await manager.queue.start_job(
        old_job.job_id, "worker-a", claimed.claim_attempt_id,
    )
    await manager.queue.complete_job(
        old_job.job_id,
        "worker-a",
        {"status": "verified"},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    try:
        pending = await manager.queue.pending_worker_outcomes()
        assert len(pending) == 1
        assert await manager.queue.prune_old_jobs() == 0
        assert await manager.queue.get_job(old_job.job_id) is not None

        await manager.queue._mark_worker_outcome(
            pending[0]["event_id"], delivered=True,
        )
        assert await manager.queue.prune_old_jobs() == 1
        assert await manager.queue.get_job(old_job.job_id) is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_late_complete_cannot_reverse_failed_attempt(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        job = Job(max_retries=1)
        first = await _claim(manager, job)
        assert await manager.queue.start_job(
            job.job_id, "worker-a", first.claim_attempt_id,
        )
        assert await manager.queue.fail_job(
            job.job_id,
            "worker-a",
            "first failure",
            claim_attempt_id=first.claim_attempt_id,
        )
        rejected = await manager.queue.complete_job(
            job.job_id,
            "worker-a",
            {"status": "completed"},
            claim_attempt_id=first.claim_attempt_id,
        )
        assert rejected["transitioned"] is False
        assert rejected["outcome_reason"] == "claim_attempt_already_failed"
        assert (await manager.queue.get_job(job.job_id)).status is JobStatus.QUEUED

        second = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert second is not None
        assert await manager.queue.start_job(
            job.job_id, "worker-a", second.claim_attempt_id,
        )
        assert await manager.queue.fail_job(
            job.job_id,
            "worker-a",
            "terminal failure",
            claim_attempt_id=second.claim_attempt_id,
        )
        rejected = await manager.queue.complete_job(
            job.job_id,
            "worker-a",
            {"status": "completed"},
            claim_attempt_id=second.claim_attempt_id,
        )
        assert rejected["transitioned"] is False
        assert (await manager.queue.get_job(job.job_id)).status is JobStatus.FAILED
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_deadline_maintenance_never_rewrites_neutral(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        job = Job(
            status=JobStatus.NEUTRAL,
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await manager.queue.post(job)
        assert await manager.queue.expire_past_deadlines(
            datetime.now(timezone.utc),
        ) == 0
        assert (await manager.queue.get_job(job.job_id)).status is JobStatus.NEUTRAL
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_shadow_effect_is_held_without_execution(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    manager = await _manager(tmp_path)
    try:
        job = Job(payload={"risk": "mutation", "action": "write"})
        await manager.queue.post(job)
        assert await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        ) is None
        held = await manager.queue.get_job(job.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["hold_kind"] == "shadow_effect"
        assert held.tags["blocked_reason"] == "shadow_effect"
        assert await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        ) is None
        assert (await manager.queue.get_job(job.job_id)).status is JobStatus.BLOCKED
    finally:
        await manager.stop()

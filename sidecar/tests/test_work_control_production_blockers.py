"""Adversarial regressions for the WorkControl production-review blockers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.authority import required_scope
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.task_queue.action_receipts import (
    ActionReceiptAttestationV1,
)
from colony_sidecar.task_queue.contract import queue_contract_identity
from colony_sidecar.task_queue.handlers.registry import build_default_handlers
from colony_sidecar.task_queue.models import (
    Job,
    JobResult,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import QueueManager
from colony_sidecar.task_queue.work_control import (
    WorkControlError,
    interrupt_capability,
    steer_capability,
)
from colony_sidecar.task_queue.worker import JobHandler, WorkerNode
from colony_sidecar.work_orders import WorkOrderV1


@pytest.fixture(autouse=True)
def _control_environment(monkeypatch):
    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "live")
    monkeypatch.setenv("COLONY_WORK_CONTROL_ACK_TIMEOUT_SECS", "30")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_RELEASE_COMMIT", "a" * 40)
    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", "b" * 64,
    )


@pytest.fixture
async def queue(tmp_path):
    manager = QueueManager(tmp_path / "queue.db")
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()


def _caps(
    job_type: JobType = JobType.CUSTOM,
    *,
    node_id: str = "worker-1",
    interrupt: bool = False,
    steer: bool = False,
) -> WorkerCapabilities:
    capabilities = set()
    if interrupt:
        capabilities.add(interrupt_capability(job_type))
    if steer:
        capabilities.add(steer_capability(job_type))
    return WorkerCapabilities(
        node_id=node_id,
        capabilities=capabilities,
        job_types={job_type},
        max_concurrent=4,
    )


async def _start(
    queue: QueueManager,
    job: Job,
    *,
    interrupt: bool = False,
    steer: bool = False,
):
    await queue.post(job)
    caps = _caps(
        job.job_type, interrupt=interrupt, steer=steer,
    )
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None and claimed.claim_attempt_id
    assert await queue.start_job(
        claimed.job_id, "worker-1", claimed.claim_attempt_id,
    )
    return claimed, caps


async def _persist_exact_p6_effect_retry(
    queue: QueueManager,
    *,
    job_id: str,
):
    """Reproduce P6's effectful RUNNING -> QUEUED retry transaction."""

    claimed, caps = await _start(
        queue,
        Job(
            job_id=job_id,
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    assert queue._db is not None
    completed_at = datetime.now(timezone.utc)
    result = {
        "job_id": claimed.job_id,
        "worker_node_id": "worker-1",
        "status": JobStatus.FAILED.value,
        "output": {},
        "error": "p6_transport_loss",
        "started_at": (
            completed_at - timedelta(milliseconds=10)
        ).isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": 0.01,
        "claim_attempt_id": claimed.claim_attempt_id,
    }
    changed = await queue._db.execute(
        """UPDATE jobs
           SET status = ?, retry_count = retry_count + 1,
               claimed_by = NULL, claimed_at = NULL,
               claim_attempt_id = NULL, claim_expires_at = NULL,
               last_heartbeat = NULL, result = ?
           WHERE job_id = ? AND status = ? AND claimed_by = ?
             AND claim_attempt_id = ?""",
        (
            JobStatus.QUEUED.value,
            json.dumps(result),
            claimed.job_id,
            JobStatus.RUNNING.value,
            "worker-1",
            claimed.claim_attempt_id,
        ),
    )
    assert changed.rowcount == 1
    await queue._audit(
        claimed.job_id,
        JobStatus.RUNNING.value,
        JobStatus.QUEUED.value,
        node_id="worker-1",
        claim_attempt_id=claimed.claim_attempt_id,
        reason="retry 1/3: p6_transport_loss",
    )
    await queue._db.commit()
    return claimed, caps


async def _append_ledger_attempt(
    queue: QueueManager,
    *,
    job_id: str,
    attempt_id: str,
    worker_id: str,
    started: bool,
) -> None:
    """Append an older-writer attempt without invoking the new claim fence."""

    assert queue._db is not None
    await queue._audit(
        job_id,
        JobStatus.QUEUED.value,
        JobStatus.CLAIMED.value,
        node_id=worker_id,
        claim_attempt_id=attempt_id,
        reason="legacy_claim",
    )
    if started:
        await queue._audit(
            job_id,
            JobStatus.CLAIMED.value,
            JobStatus.RUNNING.value,
            node_id=worker_id,
            claim_attempt_id=attempt_id,
            reason="legacy_start",
        )
        await queue._audit(
            job_id,
            JobStatus.RUNNING.value,
            JobStatus.QUEUED.value,
            node_id=worker_id,
            claim_attempt_id=attempt_id,
            reason="legacy_transport_loss",
        )
    else:
        await queue._audit(
            job_id,
            JobStatus.CLAIMED.value,
            JobStatus.QUEUED.value,
            node_id=worker_id,
            claim_attempt_id=attempt_id,
            reason="legacy_claim_released_before_start",
        )
    await queue._db.commit()


def _request(projection, operation_id, operation, *, parameters=None):
    selected = next(
        item for item in projection["allowed_operations"]
        if item["operation"] == operation
    )
    attempt = None
    if selected["attempt_id_required"]:
        attempt = (
            projection["state"]["active_attempt_id"]
            or projection["state"]["last_attempt_id"]
        )
    return {
        "operation_id": operation_id,
        "operation": operation,
        "target_id": projection["target_id"],
        "run_id": projection["run_id"],
        "attempt_id": attempt,
        "expected_revision": projection["revision"],
        "expected_state_digest": projection["state_digest"],
        "parameters": parameters or {},
        "reason": "production-review regression",
        "requested_by": "test-operator",
    }


class _SafeHandler(JobHandler):
    work_control_interrupt_safe = True

    async def execute(self, job):
        await asyncio.Event().wait()


class _UnsafeHandler(JobHandler):
    async def execute(self, job):
        await asyncio.Event().wait()


class _IdempotentSteerHandler(_SafeHandler):
    work_control_steer_idempotent = True

    def __init__(self):
        self.steer_calls = 0

    async def apply_steer(
        self,
        job,
        *,
        directive,
        context_refs,
        operation_id,
        authority_digest,
    ):
        self.steer_calls += 1
        return {
            "authority_digest": authority_digest,
            "authority_expanded": False,
        }


def _effectful_work_order(project_id: str) -> Job:
    now = datetime.now(timezone.utc)
    project = SimpleNamespace(
        id=project_id,
        title="Attester compatibility",
        objective="Exercise the exact current WorkOrder attester",
        source="owner",
        subject_person_id="owner",
        entity_ids=(),
        created_at=now.timestamp(),
    )
    step = SimpleNamespace(
        id=f"step-{project_id}",
        ordinal=0,
        description="Apply the bounded mutation and retain evidence",
        action_kind="directed",
        boundary_subject="owner",
        work_order_issued_at=now.timestamp(),
        created_at=now.timestamp(),
    )
    order = WorkOrderV1.for_project_step(project, step, now=now)
    return Job(
        job_id=order.work_order_id,
        job_type=JobType.AGENT_ACTION,
        payload=order.payload(),
    )


@pytest.mark.asyncio
async def test_delayed_attempt_a_stop_never_cancels_attempt_b():
    attempt_b = "attempt-b"
    job = Job(
        job_id="same-target",
        status=JobStatus.RUNNING,
        claimed_by="worker-1",
        claim_attempt_id=attempt_b,
    )

    class Queue:
        def __init__(self):
            self.acks = []

        async def get_job(self, _job_id):
            return job

        async def acknowledge_work_control_operation(self, **payload):
            self.acks.append(payload)
            return payload

    fake = Queue()
    worker = WorkerNode(
        "worker-1", fake,
        handlers={JobType.CUSTOM: _SafeHandler()},
    )
    attempt_b_task = asyncio.create_task(asyncio.Event().wait())
    worker._running_jobs[job.job_id] = attempt_b_task
    worker._job_attempt_ids[job.job_id] = attempt_b
    stale = {
        "operation": "cancel",
        "operation_id": "stop-attempt-a",
        "target_id": job.job_id,
        "attempt_id": "attempt-a",
        "ack_deadline": (
            datetime.now(timezone.utc) + timedelta(seconds=30)
        ).isoformat(),
    }
    try:
        await worker._apply_work_control(stale)
        assert not attempt_b_task.done()
        assert fake.acks[-1]["outcome"] == "rejected"
        assert "attempt" in fake.acks[-1]["details"]["reason"]
    finally:
        attempt_b_task.cancel()
        await asyncio.gather(attempt_b_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_p6_effect_retry_is_quarantined_transactionally_on_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    path = tmp_path / "p6-effect-retry.db"
    p6 = QueueManager(path)
    await p6.start()
    claimed, _ = await _persist_exact_p6_effect_retry(
        p6, job_id="p6-effect-retry",
    )
    assert (await p6.get_job(claimed.job_id)).status is JobStatus.QUEUED
    await p6.stop()

    migrated = QueueManager(path)
    await migrated.start()
    try:
        quarantined = await migrated.get_job(claimed.job_id)
        assert quarantined.status is JobStatus.NEUTRAL
        assert quarantined.claim_attempt_id == claimed.claim_attempt_id
        assert quarantined.result.claim_attempt_id == claimed.claim_attempt_id
        assert quarantined.tags["ambiguous_prior_effects"] == "true"
        assert quarantined.tags["verification_pending"] == "true"

        contenders = [
            WorkerCapabilities(
                node_id=f"contender-{index}",
                job_types={JobType.SYSTEM_MAINTENANCE},
            )
            for index in range(2)
        ]
        for caps in contenders:
            await migrated.register_worker(caps)
        assert await asyncio.gather(*(
            migrated.claim_job(caps.node_id, caps) for caps in contenders
        )) == [None, None]

        projection = await migrated.work_control.inspect(claimed.job_id)
        await migrated.reconcile_work_effect(
            reconciliation_id="p6-exact-not-applied",
            target_id=claimed.job_id,
            attempt_id=claimed.claim_attempt_id,
            authority_digest=projection["authority_digest"],
            finding="not_applied",
            evidence_refs=["effect-ledger:p6-retry:not-applied"],
            observed_at=datetime.now(timezone.utc).isoformat(),
            summary="the exact P6 attempt has no committed effect",
            verifier_identity="independent-effect-verifier",
            verifier_type="effect_ledger",
        )
        retry_projection = await migrated.work_control.inspect(claimed.job_id)
        await migrated.work_control.operate(**_request(
            retry_projection, "p6-safe-retry", "retry",
        ))
        winners = await asyncio.gather(*(
            migrated.claim_job(caps.node_id, caps) for caps in contenders
        ))
        assert sum(item is not None for item in winners) == 1
    finally:
        await migrated.stop()


@pytest.mark.asyncio
async def test_claim_boundary_fences_concurrent_legacy_effect_retry(
    queue, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    claimed, _ = await _persist_exact_p6_effect_retry(
        queue, job_id="direct-p6-effect-retry",
    )
    contenders = [
        WorkerCapabilities(
            node_id=f"direct-contender-{index}",
            job_types={JobType.SYSTEM_MAINTENANCE},
        )
        for index in range(2)
    ]
    for caps in contenders:
        await queue.register_worker(caps)
    claims = await asyncio.gather(*(
        queue.claim_job(caps.node_id, caps) for caps in contenders
    ))
    assert claims == [None, None]
    quarantined = await queue.get_job(claimed.job_id)
    assert quarantined.status is JobStatus.NEUTRAL
    assert quarantined.claim_attempt_id == claimed.claim_attempt_id


@pytest.mark.asyncio
async def test_restart_fences_started_a_hidden_by_newer_unstarted_b(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    path = tmp_path / "historical-attempts.db"
    original = QueueManager(path)
    await original.start()
    attempt_a, _ = await _persist_exact_p6_effect_retry(
        original, job_id="started-a-hidden-by-b",
    )
    await _append_ledger_attempt(
        original,
        job_id=attempt_a.job_id,
        attempt_id="newer-unstarted-b",
        worker_id="worker-b",
        started=False,
    )
    before = await original._work_control_attempt_truth(
        await original.get_job(attempt_a.job_id)
    )
    assert before["last_attempt_id"] == "newer-unstarted-b"
    assert before["blocking_attempt_id"] == attempt_a.claim_attempt_id
    assert before["unresolved_attempt_ids"] == [
        attempt_a.claim_attempt_id
    ]
    await original.stop()

    restarted = QueueManager(path)
    await restarted.start()
    try:
        fenced = await restarted.get_job(attempt_a.job_id)
        assert fenced.status is JobStatus.NEUTRAL
        assert fenced.claim_attempt_id == attempt_a.claim_attempt_id
        contenders = [
            WorkerCapabilities(
                node_id=f"history-contender-{index}",
                job_types={JobType.SYSTEM_MAINTENANCE},
            )
            for index in range(2)
        ]
        for caps in contenders:
            await restarted.register_worker(caps)
        assert await asyncio.gather(*(
            restarted.claim_job(caps.node_id, caps) for caps in contenders
        )) == [None, None]
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_multiple_started_attempts_each_require_exact_negative_proof(
    queue, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    attempt_a, _ = await _persist_exact_p6_effect_retry(
        queue, job_id="multiple-started-effects",
    )
    await _append_ledger_attempt(
        queue,
        job_id=attempt_a.job_id,
        attempt_id="newer-started-b",
        worker_id="worker-b",
        started=True,
    )
    contenders = [
        WorkerCapabilities(
            node_id=f"multi-contender-{index}",
            job_types={JobType.SYSTEM_MAINTENANCE},
        )
        for index in range(2)
    ]
    for caps in contenders:
        await queue.register_worker(caps)
    assert await asyncio.gather(*(
        queue.claim_job(caps.node_id, caps) for caps in contenders
    )) == [None, None]

    quarantined = await queue.get_job(attempt_a.job_id)
    assert quarantined.claim_attempt_id == "newer-started-b"
    projection = await queue.work_control.inspect(attempt_a.job_id)
    first = await queue.reconcile_work_effect(
        reconciliation_id="negative-proof-b",
        target_id=attempt_a.job_id,
        attempt_id="newer-started-b",
        authority_digest=projection["authority_digest"],
        finding="not_applied",
        evidence_refs=["effect-ledger:attempt-b:not-applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="attempt B did not commit",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert first["job_status"] == JobStatus.NEUTRAL.value
    assert first["effect_disposition"] == "ambiguous_prior_effects"
    assert first["remaining_unresolved_attempt_ids"] == [
        attempt_a.claim_attempt_id
    ]
    still_held = await queue.get_job(attempt_a.job_id)
    assert still_held.claim_attempt_id == attempt_a.claim_attempt_id
    assert "retry" not in {
        item["operation"] for item in (
            await queue.work_control.inspect(attempt_a.job_id)
        )["allowed_operations"]
    }

    second = await queue.reconcile_work_effect(
        reconciliation_id="negative-proof-a",
        target_id=attempt_a.job_id,
        attempt_id=attempt_a.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="not_applied",
        evidence_refs=["effect-ledger:attempt-a:not-applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="attempt A did not commit",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert second["job_status"] == JobStatus.CANCELLED.value
    assert second["effect_disposition"] == "verified_not_applied"
    truth = await queue._work_control_attempt_truth(
        await queue.get_job(attempt_a.job_id)
    )
    assert set(truth["not_applied_attempt_ids"]) == {
        attempt_a.claim_attempt_id,
        "newer-started-b",
    }
    assert "retry" in {
        item["operation"] for item in (
            await queue.work_control.inspect(attempt_a.job_id)
        )["allowed_operations"]
    }


@pytest.mark.asyncio
async def test_applied_attempt_forbids_retry_but_does_not_hide_unresolved_one(
    queue,
):
    attempt_a, _ = await _persist_exact_p6_effect_retry(
        queue, job_id="applied-and-unresolved-history",
    )
    await _append_ledger_attempt(
        queue,
        job_id=attempt_a.job_id,
        attempt_id="applied-attempt-b",
        worker_id="worker-b",
        started=True,
    )
    caps = WorkerCapabilities(
        node_id="mixed-history-contender",
        job_types={JobType.SYSTEM_MAINTENANCE},
    )
    await queue.register_worker(caps)
    assert await queue.claim_job(caps.node_id, caps) is None
    projection = await queue.work_control.inspect(attempt_a.job_id)
    applied = await queue.reconcile_work_effect(
        reconciliation_id="mixed-history-applied-b",
        target_id=attempt_a.job_id,
        attempt_id="applied-attempt-b",
        authority_digest=projection["authority_digest"],
        finding="applied",
        evidence_refs=["effect-ledger:attempt-b:applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="B applied while A remains unresolved",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert applied["effect_disposition"] == "ambiguous_prior_effects"
    held = await queue.get_job(attempt_a.job_id)
    assert held.claim_attempt_id == attempt_a.claim_attempt_id
    assert held.tags["ambiguous_prior_effects"] == "true"

    resolved = await queue.reconcile_work_effect(
        reconciliation_id="mixed-history-negative-a",
        target_id=attempt_a.job_id,
        attempt_id=attempt_a.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="not_applied",
        evidence_refs=["effect-ledger:attempt-a:not-applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="A did not apply",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert resolved["effect_disposition"] == "verified_applied"
    stored = await queue.get_job(attempt_a.job_id)
    assert stored.tags["effect_reconciliation_finding"] == "applied"
    assert stored.tags["effect_reconciliation_attempt_id"] == (
        "applied-attempt-b"
    )
    assert stored.tags["semantic_attestation_pending"] == "true"
    final = await queue.work_control.inspect(attempt_a.job_id)
    assert final["state"]["effect_disposition"] == "verified_applied"
    assert "retry" not in {
        item["operation"] for item in final["allowed_operations"]
    }


@pytest.mark.asyncio
async def test_legacy_retry_fence_preserves_verified_applied_truth(
    queue, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    claimed, _ = await _persist_exact_p6_effect_retry(
        queue, job_id="p6-applied-truth",
    )
    caps = WorkerCapabilities(
        node_id="applied-contender",
        job_types={JobType.SYSTEM_MAINTENANCE},
    )
    await queue.register_worker(caps)
    assert await queue.claim_job(caps.node_id, caps) is None
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.reconcile_work_effect(
        reconciliation_id="p6-exact-applied",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="applied",
        evidence_refs=["effect-ledger:p6-retry:applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="the exact P6 effect is durably present",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert queue._db is not None
    await queue._db.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?",
        (JobStatus.QUEUED.value, claimed.job_id),
    )
    await queue._db.commit()
    assert await queue.claim_job(caps.node_id, caps) is None
    preserved = await queue.get_job(claimed.job_id)
    assert preserved.status is JobStatus.NEUTRAL
    assert preserved.tags["effect_reconciliation_finding"] == "applied"
    assert (
        await queue.work_control.inspect(claimed.job_id)
    )["state"]["effect_disposition"] == "verified_applied"

    # The legacy ABANDONED retry path is an independent execution boundary;
    # positive effect truth must fence it just as strictly as QUEUED claims.
    await queue._db.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?",
        (JobStatus.ABANDONED.value, claimed.job_id),
    )
    await queue._db.commit()
    assert await queue.requeue_retryable_jobs(
        datetime.now(timezone.utc)
    ) == 0
    abandoned_fenced = await queue.get_job(claimed.job_id)
    assert abandoned_fenced.status is JobStatus.NEUTRAL
    assert abandoned_fenced.tags["semantic_attestation_pending"] == "true"


@pytest.mark.asyncio
async def test_ack_authority_migrates_and_replays_from_durable_receipt(
    tmp_path,
):
    path = tmp_path / "ack-authority-migration.db"
    first = QueueManager(path)
    await first.start()
    claimed, _ = await _start(
        first, Job(job_id="ack-authority-restart"), interrupt=True,
    )
    projection = await first.work_control.inspect(claimed.job_id)
    await first.work_control.operate(**_request(
        projection, "ack-authority-op", "interrupt",
    ))
    await first.stop()

    # Reproduce the immediately preceding on-disk schema and prove startup's
    # additive migration preserves pending work rather than rebuilding it.
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "ALTER TABLE work_control_operations DROP COLUMN ack_authority"
        )

    migrated = QueueManager(path)
    await migrated.start()
    assert migrated._db is not None
    columns = {
        str(row[1])
        for row in await (
            await migrated._db.execute(
                "PRAGMA table_info(work_control_operations)"
            )
        ).fetchall()
    }
    assert "ack_authority" in columns
    authority_at_ack = {
        "authority_kind": "scoped_worker_principal",
        "principal_id": "worker-principal-before-rotation",
        "credential_id": "worker-credential-before-rotation",
        "worker_id": "worker-1",
        "required_scope": "workers:lifecycle",
    }
    acknowledged = await migrated.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="ack-authority-op",
        attempt_id=claimed.claim_attempt_id,
        outcome="stopped",
        details={"cooperative_stop": True},
        ack_authority=authority_at_ack,
    )
    durable_digest = acknowledged["receipt_digest"]
    await migrated.stop()

    restarted = QueueManager(path)
    await restarted.start()
    try:
        restored = await restarted.work_control.receipt(
            claimed.job_id, "ack-authority-op",
        )
        assert restored["ack_authority"] == authority_at_ack
        assert restored["receipt_digest"] == durable_digest
        assert restored["receipt_events"][-1]["ack_authority"] == (
            authority_at_ack
        )

        # Credential rotation cannot rewrite history on an acknowledgement
        # replay; the original transport-attested authority remains canonical.
        replay = await restarted.work_control.acknowledge(
            worker_id="worker-1",
            operation_id="ack-authority-op",
            attempt_id=claimed.claim_attempt_id,
            outcome="stopped",
            details={"cooperative_stop": True},
            ack_authority={
                "authority_kind": "scoped_worker_principal",
                "principal_id": "worker-principal-after-rotation",
                "credential_id": "worker-credential-after-rotation",
                "worker_id": "worker-1",
                "required_scope": "workers:lifecycle",
            },
        )
        assert replay["replayed"] is True
        assert replay["ack_authority"] == authority_at_ack
        assert replay["receipt_digest"] == durable_digest

        # The append-only outcome is also an integrity anchor for the mutable
        # operation projection after restart.
        await restarted._db.execute(
            "UPDATE work_control_operations SET ack_authority = ? "
            "WHERE operation_id = ?",
            (json.dumps({"authority_kind": "tampered"}), "ack-authority-op"),
        )
        await restarted._db.commit()
        with pytest.raises(RuntimeError, match="authority drift"):
            await restarted.work_control.receipt(
                claimed.job_id, "ack-authority-op",
            )
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_delivery_reconciles_expired_controls_before_return(queue):
    claimed, _ = await _start(
        queue, Job(job_id="expired-delivery"), interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection, "expired-before-poll", "interrupt",
    ))
    assert queue._db is not None
    await queue._db.execute(
        "UPDATE work_control_operations SET ack_deadline = ? "
        "WHERE operation_id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "expired-before-poll",
        ),
    )
    await queue._db.commit()

    assert await queue.work_control.pending_for_worker("worker-1") == []
    receipt = await queue.work_control.receipt(
        claimed.job_id, "expired-before-poll",
    )
    assert receipt["status"] == "expired"
    assert receipt["ack_authority"]["authority_kind"] == (
        "server_deadline_reconciler"
    )


@pytest.mark.asyncio
async def test_steer_lease_is_bounded_by_job_deadline_but_stop_survives(queue):
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=250)
    claimed, _ = await _start(
        queue,
        Job(job_id="deadline-control", deadline=deadline),
        steer=True,
        interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    steer = await queue.work_control.operate(**_request(
        projection,
        "deadline-steer",
        "steer",
        parameters={"directive": "stay within the same authority"},
    ))
    assert datetime.fromisoformat(steer["ack_deadline"]) <= deadline

    # Close the pending steer, then let the immutable job deadline elapse
    # while its exact worker attempt remains active.
    await queue.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="deadline-steer",
        attempt_id=claimed.claim_attempt_id,
        outcome="rejected",
        details={"reason": "test close"},
    )
    await asyncio.sleep(0.3)
    expired_projection = await queue.work_control.inspect(claimed.job_id)
    allowed = {
        item["operation"] for item in expired_projection["allowed_operations"]
    }
    assert "steer" not in allowed
    assert {"interrupt", "cancel"}.issubset(allowed)


@pytest.mark.asyncio
async def test_lifecycle_winner_appends_final_steer_outcome_projection(queue):
    claimed, _ = await _start(
        queue, Job(job_id="steer-lifecycle-race"), steer=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection,
        "losing-steer",
        "steer",
        parameters={"directive": "guidance that loses to completion"},
    ))
    completion = await queue.complete_job(
        claimed.job_id,
        "worker-1",
        {"status": "completed", "action_plane": {"state": "completed"}},
        claim_attempt_id=claimed.claim_attempt_id,
        server_attested=True,
    )
    assert completion["transitioned"] is True
    receipt = await queue.work_control.receipt(
        claimed.job_id, "losing-steer",
    )
    current = await queue.work_control.inspect(claimed.job_id)
    outcome = receipt["receipt_events"][-1]
    assert receipt["status"] == "superseded"
    assert receipt["to_job_status"] == JobStatus.COMPLETED.value
    assert receipt["result_revision"] == current["revision"]
    assert receipt["result_state_digest"] == current["state_digest"]
    assert outcome["to_job_status"] == JobStatus.COMPLETED.value
    assert outcome["result_revision"] == current["revision"]
    assert outcome["result_state_digest"] == current["state_digest"]


@pytest.mark.asyncio
async def test_pruning_preserves_ambiguity_and_tombstones_retired_ids(queue):
    old = datetime.now(timezone.utc) - timedelta(days=60)
    ambiguous = Job(
        job_id="never-prune-ambiguity",
        posted_at=old,
        status=JobStatus.NEUTRAL,
        tags={
            "verification_pending": "true",
            "ambiguous_prior_effects": "true",
        },
        result=JobResult(
            job_id="never-prune-ambiguity",
            worker_node_id="worker-1",
            status=JobStatus.NEUTRAL,
        ),
    )
    retired = Job(
        job_id="retired-deterministic-id",
        posted_at=old,
        status=JobStatus.CANCELLED,
    )
    await queue.post(ambiguous)
    await queue.post(retired)
    assert await queue.prune_old_jobs(0, 0) == 1
    assert await queue.get_job(ambiguous.job_id) is not None
    assert await queue.get_job(retired.job_id) is None
    assert queue._db is not None
    cursor = await queue._db.execute(
        "SELECT * FROM job_tombstones WHERE job_id = ?",
        (retired.job_id,),
    )
    tombstone = await cursor.fetchone()
    assert tombstone is not None and len(tombstone["job_digest"]) == 64
    with pytest.raises(ValueError, match="durably retired"):
        await queue.post(Job(job_id=retired.job_id))


@pytest.mark.asyncio
async def test_old_not_applied_job_survives_until_retry_is_consumed(
    queue, monkeypatch,
):
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    old = datetime.now(timezone.utc) - timedelta(days=60)
    claimed, _ = await _start(
        queue,
        Job(
            job_id="retained-negative-reconciliation",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
            posted_at=old,
            max_retries=3,
        ),
    )
    assert await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "response lost",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.reconcile_work_effect(
        reconciliation_id="retained-negative-proof",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="not_applied",
        evidence_refs=["effect-ledger:negative:retention"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="exact effect was not applied",
        verifier_identity="independent-effect-verifier",
        verifier_type="effect_ledger",
    )
    assert queue._db is not None
    await queue._db.execute(
        "DELETE FROM worker_outcome_outbox WHERE job_id = ?",
        (claimed.job_id,),
    )
    await queue._db.commit()
    assert await queue.prune_old_jobs() == 0
    retained = await queue.get_job(claimed.job_id)
    assert retained is not None
    assert retained.tags["effect_reconciliation_retry_pending"] == "true"

    retry_projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        retry_projection, "consume-negative-proof", "retry",
    ))
    cancel_projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        cancel_projection, "terminal-after-consumption", "cancel",
    ))
    consumed = await queue.get_job(claimed.job_id)
    assert consumed.tags.get("effect_reconciliation_consumed_at")

    # Simulate the bounded retention window elapsing after consumption.
    terminal_old = (
        datetime.now(timezone.utc) - timedelta(days=60)
    ).isoformat()
    raw = await (
        await queue._db.execute(
            "SELECT tags, result FROM jobs WHERE job_id = ?",
            (claimed.job_id,),
        )
    ).fetchone()
    tags = json.loads(raw["tags"])
    tags["effect_reconciliation_consumed_at"] = terminal_old
    result = json.loads(raw["result"])
    result["completed_at"] = terminal_old
    await queue._db.execute(
        """UPDATE jobs SET posted_at = ?, tags = ?, result = ?
           WHERE job_id = ?""",
        (terminal_old, json.dumps(tags), json.dumps(result), claimed.job_id),
    )
    await queue._db.execute(
        "UPDATE work_effect_reconciliations SET created_at = ? "
        "WHERE target_id = ?",
        (terminal_old, claimed.job_id),
    )
    await queue._db.commit()
    assert await queue.prune_old_jobs() == 1
    assert await queue.get_job(claimed.job_id) is None
    with pytest.raises(ValueError, match="durably retired"):
        await queue.post(Job(job_id=claimed.job_id))


@pytest.mark.asyncio
async def test_old_applied_job_waits_for_semantic_attestation_before_prune(
    queue, monkeypatch,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "worker-1")
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    old = datetime.now(timezone.utc) - timedelta(days=60)
    candidate = Job(
        job_id="retained-applied-reconciliation",
        job_type=JobType.AGENT_ACTION,
        payload={
            "action_hint": "commitment_mark_complete",
            "ID": "retained-applied-reconciliation",
            "initiative_id": "retention-test",
        },
        posted_at=old,
    )
    await queue.post(candidate)
    caps = WorkerCapabilities(
        node_id="worker-1",
        capabilities={item.name for item in candidate.capabilities},
        job_types={JobType.AGENT_ACTION},
    )
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None and claimed.claim_attempt_id
    assert await queue.start_job(
        claimed.job_id, "worker-1", claimed.claim_attempt_id,
    )
    completion = await queue.complete_job(
        claimed.job_id,
        "worker-1",
        {"status": "failed", "action_plane": {"state": "failed"}},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert completion["job_status"] == JobStatus.NEUTRAL.value
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.reconcile_work_effect(
        reconciliation_id="retained-applied-proof",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="applied",
        evidence_refs=["effect-ledger:applied:retention"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="effect applied; semantic result is pending",
        verifier_identity="effect-observer",
        verifier_type="effect_ledger",
    )
    assert queue._db is not None
    await queue._db.execute(
        "DELETE FROM worker_outcome_outbox WHERE job_id = ?",
        (claimed.job_id,),
    )
    await queue._db.commit()
    assert await queue.prune_old_jobs() == 0
    pending = await queue.get_job(claimed.job_id)
    assert pending.tags["semantic_attestation_pending"] == "true"

    attestation = ActionReceiptAttestationV1.from_payload({
        "schema": "ActionReceiptAttestationV1",
        "version": 1,
        "job_id": claimed.job_id,
        "action_digest": pending.tags["action_digest"],
        "claim_attempt_id": claimed.claim_attempt_id,
        "effect_class": "mutation",
        "terminal_outcome": "succeeded",
        "receipt_refs": ["commitment-ledger:retention:completed"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "summary": "canonical ledger confirms semantic success",
    })
    attested = await queue.attest_action_success(
        claimed.job_id,
        attestation=attestation,
        verifier_identity="semantic-action-verifier",
    )
    assert attested is not None and attested["replayed"] is False
    terminal = await queue.get_job(claimed.job_id)
    assert terminal.status is JobStatus.COMPLETED
    assert "semantic_attestation_pending" not in terminal.tags
    assert terminal.tags.get("effect_reconciliation_consumed_at")

    terminal_old = (
        datetime.now(timezone.utc) - timedelta(days=60)
    ).isoformat()
    raw = await (
        await queue._db.execute(
            "SELECT tags, result FROM jobs WHERE job_id = ?",
            (claimed.job_id,),
        )
    ).fetchone()
    tags = json.loads(raw["tags"])
    tags["effect_reconciliation_consumed_at"] = terminal_old
    result = json.loads(raw["result"])
    result["completed_at"] = terminal_old
    await queue._db.execute(
        """UPDATE jobs SET posted_at = ?, tags = ?, result = ?
           WHERE job_id = ?""",
        (terminal_old, json.dumps(tags), json.dumps(result), claimed.job_id),
    )
    await queue._db.execute(
        "UPDATE work_effect_reconciliations SET created_at = ? "
        "WHERE target_id = ?",
        (terminal_old, claimed.job_id),
    )
    await queue._db.execute(
        "DELETE FROM worker_outcome_outbox WHERE job_id = ?",
        (claimed.job_id,),
    )
    await queue._db.commit()
    assert await queue.prune_old_jobs() == 1
    assert await queue.get_job(claimed.job_id) is None
    with pytest.raises(ValueError, match="durably retired"):
        await queue.post(Job(job_id=claimed.job_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic_state", ["failed", "cancelled", "skipped"])
async def test_started_effect_semantic_non_success_stays_ambiguous(
    queue, semantic_state,
):
    claimed, _ = await _start(
        queue,
        Job(
            job_id=f"effect-semantic-{semantic_state}",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    completion = await queue.complete_job(
        claimed.job_id,
        "worker-1",
        {
            "status": semantic_state,
            "action_plane": {"state": semantic_state},
        },
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert completion["job_status"] == JobStatus.NEUTRAL.value
    stored = await queue.get_job(claimed.job_id)
    assert stored.status is JobStatus.NEUTRAL
    assert stored.claim_attempt_id == claimed.claim_attempt_id
    assert stored.tags["verification_pending"] == "true"
    assert stored.tags["ambiguous_prior_effects"] == "true"
    assert stored.tags["blocked_reason"] == "ambiguous_prior_effects"
    assert "retry" not in {
        item["operation"]
        for item in (
            await queue.work_control.inspect(claimed.job_id)
        )["allowed_operations"]
    }


@pytest.mark.asyncio
async def test_exact_independent_not_applied_enables_only_safe_retry(
    queue, monkeypatch,
):
    claimed, _ = await _start(
        queue,
        Job(
            job_id="effect-not-applied",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    assert await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "transport lost",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    with pytest.raises(WorkControlError, match="exact unresolved"):
        await queue.reconcile_work_effect(
            reconciliation_id="wrong-attempt-proof",
            target_id=claimed.job_id,
            attempt_id="other-attempt",
            authority_digest=projection["authority_digest"],
            finding="not_applied",
            evidence_refs=["ledger:exact-negative-proof"],
            observed_at=datetime.now(timezone.utc).isoformat(),
            summary="no effect found",
            verifier_identity="independent-verifier",
            verifier_type="test",
        )
    with pytest.raises(WorkControlError, match="executor cannot"):
        await queue.reconcile_work_effect(
            reconciliation_id="self-proof",
            target_id=claimed.job_id,
            attempt_id=claimed.claim_attempt_id,
            authority_digest=projection["authority_digest"],
            finding="not_applied",
            evidence_refs=["ledger:exact-negative-proof"],
            observed_at=datetime.now(timezone.utc).isoformat(),
            summary="no effect found",
            verifier_identity="worker-1",
            verifier_type="test",
        )
    receipt = await queue.reconcile_work_effect(
        reconciliation_id="verified-not-applied",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="not_applied",
        evidence_refs=["ledger:exact-negative-proof"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="independent ledger proves no mutation landed",
        verifier_identity="independent-verifier",
        verifier_type="test",
    )
    assert receipt["job_status"] == JobStatus.CANCELLED.value
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    retry_projection = await queue.work_control.inspect(claimed.job_id)
    assert retry_projection["state"]["effect_disposition"] == (
        "verified_not_applied"
    )
    assert "retry" in {
        item["operation"] for item in retry_projection["allowed_operations"]
    }
    await queue.work_control.operate(**_request(
        retry_projection, "retry-after-negative-proof", "retry",
    ))
    assert (await queue.get_job(claimed.job_id)).status is JobStatus.QUEUED


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["direct", "http"])
@pytest.mark.parametrize("finding", ["applied", "not_applied"])
async def test_reconciliation_rejects_evidence_before_exact_ambiguity(
    queue, tmp_path, monkeypatch, path_kind, finding,
):
    claimed, _ = await _start(
        queue,
        Job(
            job_id=f"stale-{path_kind}-{finding}",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    assert await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "response lost",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    reconciliation_id = f"stale-{path_kind}-{finding}-proof"
    payload = {
        "schema": "WorkEffectReconciliationV1",
        "version": 1,
        "reconciliation_id": reconciliation_id,
        "target_id": claimed.job_id,
        "attempt_id": claimed.claim_attempt_id,
        "authority_digest": projection["authority_digest"],
        "finding": finding,
        "evidence_refs": [f"effect-ledger:{finding}:exact-attempt"],
        "observed_at": "1970-01-01T00:00:00+00:00",
        "summary": "historically stale observation",
    }
    if path_kind == "direct":
        with pytest.raises(
            WorkControlError, match="at or after the exact attempt"
        ) as rejected:
            await queue.reconcile_work_effect(
                reconciliation_id=reconciliation_id,
                target_id=claimed.job_id,
                attempt_id=claimed.claim_attempt_id,
                authority_digest=projection["authority_digest"],
                finding=finding,
                evidence_refs=payload["evidence_refs"],
                observed_at=payload["observed_at"],
                summary=payload["summary"],
                verifier_identity="effect-verifier",
                verifier_type="test",
            )
        assert rejected.value.code == "effect_evidence_predates_ambiguity"
        payload["observed_at"] = datetime.now(timezone.utc).isoformat()
        accepted = await queue.reconcile_work_effect(
            reconciliation_id=reconciliation_id,
            target_id=claimed.job_id,
            attempt_id=claimed.claim_attempt_id,
            authority_digest=projection["authority_digest"],
            finding=finding,
            evidence_refs=payload["evidence_refs"],
            observed_at=payload["observed_at"],
            summary=payload["summary"],
            verifier_identity="effect-verifier",
            verifier_type="test",
        )
        assert accepted["finding"] == finding
    else:
        ring = tmp_path / f"stale-{finding}-keyring.json"
        _write_keyring(ring)
        app = FastAPI()
        app.add_middleware(ApiKeyMiddleware, keyring_path=str(ring))
        app.include_router(queue_router.router)
        monkeypatch.setattr(
            queue_router,
            "_get_queue",
            lambda: SimpleNamespace(queue=queue),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            rejected = await client.post(
                "/v1/host/queue/work/reconciliations",
                headers={"Authorization": "Bearer verifier-secret"},
                json=payload,
            )
            assert rejected.status_code == 422
            assert rejected.json()["detail"]["code"] == (
                "effect_evidence_predates_ambiguity"
            )
            payload["observed_at"] = datetime.now(timezone.utc).isoformat()
            accepted = await client.post(
                "/v1/host/queue/work/reconciliations",
                headers={"Authorization": "Bearer verifier-secret"},
                json=payload,
            )
        assert accepted.status_code == 200
        assert accepted.json()["finding"] == finding


@pytest.mark.asyncio
async def test_verified_applied_closes_ambiguity_without_claiming_success(queue):
    claimed, _ = await _start(
        queue,
        Job(
            job_id="effect-applied",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    assert await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "response lost",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    receipt = await queue.reconcile_work_effect(
        reconciliation_id="verified-applied",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="applied",
        evidence_refs=["remote-ledger:effect-id-1"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="effect is present; semantic success is not asserted",
        verifier_identity="independent-verifier",
        verifier_type="test",
    )
    assert receipt["job_status"] == JobStatus.NEUTRAL.value
    resolved = await queue.get_job(claimed.job_id)
    assert "verification_pending" not in resolved.tags
    assert "ambiguous_prior_effects" not in resolved.tags
    assert resolved.tags["effect_reconciliation_finding"] == "applied"
    final_projection = await queue.work_control.inspect(claimed.job_id)
    assert final_projection["state"]["effect_disposition"] == "verified_applied"
    assert "retry" not in {
        item["operation"] for item in final_projection["allowed_operations"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("attester_kind", ["action_receipt", "work_order"])
async def test_verified_applied_remains_compatible_with_current_attesters(
    queue, monkeypatch, attester_kind,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "worker-1")
    monkeypatch.setattr(
        QueueManager,
        "_server_approval_provenance_valid",
        staticmethod(lambda _job: True),
    )
    if attester_kind == "work_order":
        candidate = _effectful_work_order("work-control-attester")
    else:
        candidate = Job(
            job_id="action-receipt-attester",
            job_type=JobType.AGENT_ACTION,
            payload={
                "action_hint": "commitment_mark_complete",
                "ID": "work-control-attester",
                "initiative_id": "initiative-attester",
            },
        )
    await queue.post(candidate)
    caps = WorkerCapabilities(
        node_id="worker-1",
        capabilities={
            item.name for item in candidate.capabilities
        },
        job_types={JobType.AGENT_ACTION},
        max_concurrent=1,
    )
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None and claimed.job_id == candidate.job_id
    assert claimed.claim_attempt_id
    assert await queue.start_job(
        claimed.job_id, "worker-1", claimed.claim_attempt_id,
    )
    completion = await queue.complete_job(
        claimed.job_id,
        "worker-1",
        {"status": "failed", "action_plane": {"state": "failed"}},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert completion["job_status"] == JobStatus.NEUTRAL.value

    projection = await queue.work_control.inspect(claimed.job_id)
    reconciled = await queue.reconcile_work_effect(
        reconciliation_id=f"{attester_kind}-applied-proof",
        target_id=claimed.job_id,
        attempt_id=claimed.claim_attempt_id,
        authority_digest=projection["authority_digest"],
        finding="applied",
        evidence_refs=[f"effect-ledger:{attester_kind}:applied"],
        observed_at=datetime.now(timezone.utc).isoformat(),
        summary="independent effect ledger proves the exact attempt applied",
        verifier_identity="effect-observer",
        verifier_type="effect_ledger",
    )
    assert reconciled["job_status"] == JobStatus.NEUTRAL.value

    if attester_kind == "action_receipt":
        pending = await queue.get_job(claimed.job_id)
        attestation = ActionReceiptAttestationV1.from_payload({
            "schema": "ActionReceiptAttestationV1",
            "version": 1,
            "job_id": claimed.job_id,
            "action_digest": pending.tags["action_digest"],
            "claim_attempt_id": claimed.claim_attempt_id,
            "effect_class": "mutation",
            "terminal_outcome": "succeeded",
            "receipt_refs": ["commitment-ledger:work-control:completed"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "summary": "the canonical commitment ledger confirms success",
        })
        attested = await queue.attest_action_success(
            claimed.job_id,
            attestation=attestation,
            verifier_identity="current-action-receipt-verifier",
        )
        assert attested is not None and attested["replayed"] is False
    else:
        attested = await queue.attest_job_success(
            claimed.job_id,
            report={
                "status": "verified",
                "claim_attempt_id": claimed.claim_attempt_id,
                "execution_result": {
                    "schema": "ExecutionResultV1",
                    "version": 1,
                    "work_order_id": claimed.job_id,
                    "work_order_digest": claimed.payload[
                        "work_order_digest"
                    ],
                    "terminal_outcome": "succeeded",
                    "verification_result": "verified",
                    "verifier_identity": "current-work-order-verifier",
                },
            },
            verifier_identity="current-work-order-verifier",
        )
        assert attested is True
    completed = await queue.get_job(claimed.job_id)
    assert completed.status is JobStatus.COMPLETED
    assert completed.tags["success_attested"] == "true"
    assert completed.tags["effect_reconciliation_finding"] == "applied"


@pytest.mark.asyncio
async def test_durable_steer_outcome_retries_ack_before_handler_reapply(queue):
    claimed, _ = await _start(
        queue, Job(job_id="durable-steer"), steer=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection,
        "durable-steer-op",
        "steer",
        parameters={"directive": "one durable application"},
    ))
    delivery = (await queue.work_control.pending_for_worker("worker-1"))[0]
    report = {
        "authority_digest": delivery["authority_digest"],
        "authority_expanded": False,
    }
    await queue.record_work_control_worker_outcome(
        worker_id="worker-1",
        operation_id="durable-steer-op",
        attempt_id=claimed.claim_attempt_id,
        outcome="applied",
        details=report,
    )
    handler = _IdempotentSteerHandler()
    worker = WorkerNode(
        "worker-1", queue, handlers={JobType.CUSTOM: handler},
    )
    task = asyncio.create_task(asyncio.Event().wait())
    worker._running_jobs[claimed.job_id] = task
    worker._job_attempt_ids[claimed.job_id] = claimed.claim_attempt_id
    try:
        await worker._apply_work_control(delivery)
        assert handler.steer_calls == 0
        receipt = await queue.work_control.receipt(
            claimed.job_id, "durable-steer-op",
        )
        assert receipt["status"] == "applied"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_work_control_capabilities_are_rebuilt_from_final_handler_map(
    monkeypatch,
):
    preseeded = WorkerCapabilities(
        node_id="worker-1",
        capabilities={
            interrupt_capability(JobType.CUSTOM),
            steer_capability(JobType.CUSTOM),
            "ordinary-capability",
        },
        job_types={JobType.CUSTOM},
    )
    worker = WorkerNode(
        "worker-1",
        object(),
        handlers={JobType.CUSTOM: _UnsafeHandler()},
        capabilities=preseeded,
    )
    assert worker._capabilities.capabilities == {"ordinary-capability"}

    safe = WorkerNode(
        "worker-2",
        object(),
        handlers={JobType.CUSTOM: _IdempotentSteerHandler()},
    )
    assert interrupt_capability(JobType.CUSTOM) in (
        safe._capabilities.capabilities
    )
    assert steer_capability(JobType.CUSTOM) in safe._capabilities.capabilities
    safe.register_handler(JobType.CUSTOM, _UnsafeHandler())
    assert not any(
        value.startswith("work_control:")
        for value in safe._capabilities.capabilities
    )

    # Colony's bundled production handlers opt in to neither control. A host
    # adapter must supply the exact per-handler semantics; live mode alone is
    # never an active-controls claim.
    bundled = WorkerNode(
        "colony-bundled",
        object(),
        handlers=build_default_handlers(),
        capabilities=WorkerCapabilities(node_id="colony-bundled"),
    )
    assert not any(
        value.startswith("work_control:")
        for value in bundled._capabilities.capabilities
    )
    contract = queue_contract_identity()["work_control"]
    assert contract["bundled_production_handler_opt_ins"] == []
    assert contract["colony_supplies_host_control_adapter"] is False
    assert contract["active_control_posture"] == (
        "inactive_until_host_adapter_registers_exact_handler_semantics"
    )

    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "off")
    disabled = WorkerNode(
        "worker-3",
        object(),
        handlers={JobType.CUSTOM: _IdempotentSteerHandler()},
        capabilities=WorkerCapabilities(
            node_id="worker-3",
            capabilities={steer_capability(JobType.CUSTOM)},
        ),
    )
    assert not any(
        value.startswith("work_control:")
        for value in disabled._capabilities.capabilities
    )


@pytest.mark.asyncio
async def test_live_pending_control_is_terminalized_on_off_restart(
    tmp_path, monkeypatch,
):
    path = tmp_path / "off-restart.db"
    live = QueueManager(path)
    await live.start()
    claimed, _ = await _start(
        live, Job(job_id="off-restart-control"), steer=True,
    )
    projection = await live.work_control.inspect(claimed.job_id)
    await live.work_control.operate(**_request(
        projection,
        "off-restart-steer",
        "steer",
        parameters={"directive": "must never run after off"},
    ))
    delivered_before_off = (
        await live.work_control.pending_for_worker("worker-1")
    )[0]
    await live.stop()

    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "off")
    inactive = QueueManager(path)
    await inactive.start()
    handler = _IdempotentSteerHandler()
    worker = WorkerNode(
        "worker-1", inactive, handlers={JobType.CUSTOM: handler},
    )
    local_task = asyncio.create_task(asyncio.Event().wait())
    worker._running_jobs[claimed.job_id] = local_task
    worker._job_attempt_ids[claimed.job_id] = claimed.claim_attempt_id
    try:
        await worker._apply_work_control(delivered_before_off)
        assert handler.steer_calls == 0
        assert not local_task.done()
        assert await inactive.work_control.pending_for_worker("worker-1") == []

        receipt = await inactive.work_control.receipt(
            claimed.job_id, "off-restart-steer",
        )
        assert receipt["status"] == "superseded"
        assert receipt["acknowledgement"]["outcome"] == "inactive"
        assert receipt["ack_authority"] == {
            "authority_kind": "server_control_mode_reconciler",
            "observed_mode": "off",
        }

        ring = tmp_path / "off-keyring.json"
        _write_keyring(ring)
        app = FastAPI()
        app.add_middleware(ApiKeyMiddleware, keyring_path=str(ring))
        app.include_router(queue_router.router)
        monkeypatch.setattr(
            queue_router, "_get_queue",
            lambda: SimpleNamespace(queue=inactive),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get(
                "/v1/host/queue/workers/worker-1/controls",
                headers={"Authorization": "Bearer worker-secret"},
            )
        assert response.status_code == 200
        assert response.json() == []

        monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "live")
        assert await inactive.work_control.pending_for_worker("worker-1") == []
        replay = await inactive.work_control.receipt(
            claimed.job_id, "off-restart-steer",
        )
        assert replay["status"] == "superseded"
        assert replay["ack_authority"]["authority_kind"] == (
            "server_control_mode_reconciler"
        )
    finally:
        local_task.cancel()
        await asyncio.gather(local_task, return_exceptions=True)
        await inactive.stop()


@pytest.mark.asyncio
async def test_claim_lease_expiry_finalizes_all_attempt_controls(queue):
    job = Job(job_id="claim-lease-control")
    await queue.post(job)
    caps = _caps(steer=True)
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None and claimed.claim_attempt_id
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection,
        "claim-lease-steer",
        "steer",
        parameters={"directive": "old claim guidance"},
    ))
    assert queue._db is not None
    await queue._db.execute(
        "UPDATE jobs SET claim_expires_at = ? WHERE job_id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            claimed.job_id,
        ),
    )
    await queue._db.commit()
    assert await queue.abandon_silent_jobs(
        datetime.now(timezone.utc),
        timeout_secs=300,
        claim_timeout_secs=300,
    ) == 1
    assert await queue.work_control.pending_for_worker("worker-1") == []
    receipt = await queue.work_control.receipt(
        claimed.job_id, "claim-lease-steer",
    )
    assert receipt["status"] == "superseded"
    assert receipt["ack_authority"]["authority_kind"] == (
        "server_lifecycle_winner"
    )
    assert (await queue.get_job(claimed.job_id)).status is JobStatus.QUEUED


@pytest.mark.asyncio
async def test_delivery_release_race_and_restart_leave_no_stale_steer(
    tmp_path,
):
    path = tmp_path / "stale-race.db"
    manager = QueueManager(path)
    await manager.start()
    claimed, _ = await _start(
        manager, Job(job_id="stale-race"), steer=True,
    )
    projection = await manager.work_control.inspect(claimed.job_id)
    await manager.work_control.operate(**_request(
        projection,
        "stale-race-steer",
        "steer",
        parameters={"directive": "attempt A only"},
    ))
    delivery, released = await asyncio.gather(
        manager.work_control.pending_for_worker("worker-1"),
        manager.release_job(
            claimed.job_id,
            "worker-1",
            claim_attempt_id=claimed.claim_attempt_id,
        ),
    )
    assert released is True
    assert delivery == [] or [
        item["operation_id"] for item in delivery
    ] == ["stale-race-steer"]
    assert await manager.work_control.pending_for_worker("worker-1") == []
    await manager.stop()

    restarted = QueueManager(path)
    await restarted.start()
    try:
        assert await restarted.work_control.pending_for_worker("worker-1") == []
        receipt = await restarted.work_control.receipt(
            claimed.job_id, "stale-race-steer",
        )
        assert receipt["status"] == "superseded"
        assert receipt["ack_authority"]["authority_kind"] == (
            "server_lifecycle_winner"
        )
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_restart_reconciles_legacy_stale_steer_before_delivery(
    tmp_path,
):
    path = tmp_path / "legacy-stale-steer.db"
    manager = QueueManager(path)
    await manager.start()
    claimed, _ = await _start(
        manager, Job(job_id="legacy-stale-steer"), steer=True,
    )
    projection = await manager.work_control.inspect(claimed.job_id)
    await manager.work_control.operate(**_request(
        projection,
        "legacy-stale-steer-op",
        "steer",
        parameters={"directive": "orphaned attempt guidance"},
    ))
    assert manager._db is not None
    await manager._db.execute(
        """UPDATE jobs SET status = ?, claimed_by = NULL,
                  claimed_at = NULL, claim_attempt_id = NULL,
                  claim_expires_at = NULL, last_heartbeat = NULL
           WHERE job_id = ?""",
        (JobStatus.QUEUED.value, claimed.job_id),
    )
    await manager._db.commit()
    await manager.stop()

    restarted = QueueManager(path)
    await restarted.start()
    try:
        assert await restarted.work_control.pending_for_worker("worker-1") == []
        receipt = await restarted.work_control.receipt(
            claimed.job_id, "legacy-stale-steer-op",
        )
        assert receipt["status"] == "superseded"
        assert receipt["ack_authority"] == {
            "authority_kind": "server_attempt_reconciler",
        }
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_precedence_receipts_survive_restart_and_key_rotation(
    tmp_path, monkeypatch,
):
    path = tmp_path / "precedence.db"
    manager = QueueManager(path)
    await manager.start()
    claimed, _ = await _start(
        manager,
        Job(job_id="precedence-controls"),
        steer=True,
        interrupt=True,
    )
    old_authority = {
        "authority_kind": "scoped_principal",
        "principal_id": "operator-before-rotation",
        "credential_id": "operator-old",
        "required_scope": "work:control",
    }
    projection = await manager.work_control.inspect(claimed.job_id)
    steer_request = _request(
        projection,
        "precedence-steer",
        "steer",
        parameters={"directive": "lower-priority guidance"},
    )
    steer_request["request_authority"] = old_authority
    await manager.work_control.operate(**steer_request)

    projection = await manager.work_control.inspect(claimed.job_id)
    interrupt_request = _request(
        projection, "precedence-interrupt", "interrupt",
    )
    interrupt_request["request_authority"] = old_authority
    await manager.work_control.operate(**interrupt_request)

    projection = await manager.work_control.inspect(claimed.job_id)
    cancel_request = _request(
        projection, "precedence-cancel", "cancel",
    )
    cancel_request["request_authority"] = old_authority
    await manager.work_control.operate(**cancel_request)
    await manager.stop()

    restarted = QueueManager(path)
    await restarted.start()
    try:
        expected = {
            "precedence-steer": (
                "precedence-interrupt", "interrupt",
            ),
            "precedence-interrupt": (
                "precedence-cancel", "cancel",
            ),
        }
        for operation_id, (winner_id, winner_type) in expected.items():
            receipt = await restarted.work_control.receipt(
                claimed.job_id, operation_id,
            )
            assert receipt["status"] == "superseded"
            assert receipt["ack_authority"] == {
                "authority_kind": "server_control_precedence",
                "superseding_operation_id": winner_id,
                "superseding_operation_type": winner_type,
            }
            assert receipt["receipt_events"][-1]["ack_authority"] == (
                receipt["ack_authority"]
            )

        ring = tmp_path / "rotated-operator-keyring.json"
        _write_keyring(
            ring,
            operator_secret="operator-secret-after-rotation",
            operator_credential_id="operator-new",
        )
        app = FastAPI()
        app.add_middleware(ApiKeyMiddleware, keyring_path=str(ring))
        app.include_router(queue_router.router)
        monkeypatch.setattr(
            queue_router, "_get_queue",
            lambda: SimpleNamespace(queue=restarted),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            rotated = await client.get(
                "/v1/host/queue/work/operations/receipt",
                params={
                    "target_id": claimed.job_id,
                    "operation_id": "precedence-steer",
                },
                headers={
                    "Authorization": "Bearer operator-secret-after-rotation",
                },
            )
        assert rotated.status_code == 200
        assert rotated.json()["ack_authority"]["authority_kind"] == (
            "server_control_precedence"
        )
        assert rotated.json()["request_authority"] == old_authority
    finally:
        await restarted.stop()


def _write_keyring(
    path,
    *,
    operator_secret="operator-secret",
    operator_credential_id="operator-current",
):
    path.write_text(json.dumps({
        "version": 1,
        "principals": [
            {
                "principal": "operator",
                "status": "active",
                "scopes": ["work:read", "work:control"],
                "audiences": [],
                "credentials": [{
                    "id": operator_credential_id,
                    "secret": operator_secret,
                    "status": "active",
                }],
            },
            {
                "principal": "worker-principal",
                "status": "active",
                "scopes": ["workers:lifecycle"],
                "audiences": [],
                "worker_grants": [{
                    "node_id": "worker-1",
                    "capabilities": [
                        interrupt_capability(JobType.CUSTOM),
                        steer_capability(JobType.CUSTOM),
                    ],
                    "capacity": {},
                    "max_concurrent": 4,
                    "job_types": [JobType.CUSTOM.value],
                }],
                "credentials": [{
                    "id": "worker-current",
                    "secret": "worker-secret",
                    "status": "active",
                }],
            },
            {
                "principal": "effect-verifier",
                "status": "active",
                "scopes": ["workers:attest"],
                "audiences": [],
                "credentials": [{
                    "id": "effect-verifier-current",
                    "secret": "verifier-secret",
                    "status": "active",
                }],
            },
        ],
    }))
    path.chmod(0o600)


@pytest.mark.asyncio
async def test_slash_ids_are_routable_and_ack_authority_is_durable(
    queue, tmp_path, monkeypatch,
):
    ring = tmp_path / "keyring.json"
    _write_keyring(ring)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(ring))
    app.include_router(queue_router.router)
    monkeypatch.setattr(
        queue_router, "_get_queue", lambda: SimpleNamespace(queue=queue),
    )
    await queue.post(Job(job_id="target/with/slash"))
    operator = {"Authorization": "Bearer operator-secret"}
    worker_headers = {"Authorization": "Bearer worker-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        inspected = await client.get(
            "/v1/host/queue/work",
            params={"target_id": "target/with/slash"},
            headers=operator,
        )
        assert inspected.status_code == 200
        projection = inspected.json()
        operation = {
            "schema": "WorkControlOperationV1",
            "version": 1,
            "operation_id": "operator/cancel/1",
            "operation": "cancel",
            "target_id": "target/with/slash",
            "run_id": projection["run_id"],
            "attempt_id": None,
            "expected_revision": projection["revision"],
            "expected_state_digest": projection["state_digest"],
            "parameters": {},
            "reason": "slash-safe",
        }
        applied = await client.post(
            "/v1/host/queue/work/operations",
            json=operation,
            headers=operator,
        )
        receipt = await client.get(
            "/v1/host/queue/work/operations/receipt",
            params={
                "target_id": "target/with/slash",
                "operation_id": "operator/cancel/1",
            },
            headers=operator,
        )
        assert applied.status_code == 200
        assert receipt.status_code == 200

        claimed, _ = await _start(
            queue, Job(job_id="active/target"), interrupt=True,
        )
        active_projection = await queue.work_control.inspect(claimed.job_id)
        await queue.work_control.operate(**_request(
            active_projection, "worker/stop/1", "interrupt",
        ))
        ack = await client.post(
            "/v1/host/queue/workers/controls/ack",
            headers=worker_headers,
            json={
                "schema": "WorkControlAckV1",
                "version": 1,
                "node_id": "worker-1",
                "operation_id": "worker/stop/1",
                "attempt_id": claimed.claim_attempt_id,
                "outcome": "stopped",
                "details": {"cooperative_stop": True},
            },
        )
        assert ack.status_code == 200
    stop_receipt = await queue.work_control.receipt(
        "active/target", "worker/stop/1",
    )
    assert stop_receipt["ack_authority"] == {
        "authority_kind": "scoped_worker_principal",
        "credential_id": "worker-current",
        "principal_id": "worker-principal",
        "required_scope": "workers:lifecycle",
        "worker_authority_mode": "enforce",
        "worker_id": "worker-1",
    }
    assert required_scope("GET", "/v1/host/queue/work") == "work:read"
    assert required_scope(
        "POST", "/v1/host/queue/work/reconciliations",
    ) == "workers:attest"


@pytest.mark.asyncio
async def test_trusted_status_update_cannot_bypass_active_or_ambiguity(queue):
    claimed, _ = await _start(queue, Job(job_id="active-bypass"))
    assert not await queue.update_job_status(
        claimed.job_id, JobStatus.CANCELLED, reason="forbidden bypass",
    )
    active = await queue.get_job(claimed.job_id)
    assert active.status is JobStatus.RUNNING
    assert active.claim_attempt_id == claimed.claim_attempt_id

    effect, _ = await _start(
        queue,
        Job(
            job_id="ambiguity-bypass",
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "external_state_change"},
        ),
    )
    assert await queue.fail_job(
        effect.job_id,
        "worker-1",
        "lost",
        claim_attempt_id=effect.claim_attempt_id,
    )
    assert not await queue.update_job_status(
        effect.job_id, JobStatus.QUEUED, reason="forbidden ambiguity bypass",
    )
    assert (await queue.get_job(effect.job_id)).status is JobStatus.NEUTRAL


@pytest.mark.asyncio
async def test_off_disables_controls_but_retains_lifecycle_safety(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "off")
    manager = QueueManager(tmp_path / "off.db")
    await manager.start()
    try:
        claimed, _ = await _start(
            manager,
            Job(
                job_id="off-effect",
                job_type=JobType.SYSTEM_MAINTENANCE,
                payload={"action": "external_state_change"},
            ),
        )
        assert not await manager.cancel_job(claimed.job_id)
        assert await manager.fail_job(
            claimed.job_id,
            "worker-1",
            "lost",
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert (await manager.get_job(claimed.job_id)).status is JobStatus.NEUTRAL
        projection = await manager.work_control.inspect(claimed.job_id)
        assert projection["mode"] == "off"
        assert projection["allowed_operations"] == []
        contract = queue_contract_identity()["work_control"]
        assert "lifecycle safety corrections retained" in contract["off_posture"]
        assert contract["bundled_production_handler_opt_ins"] == []
        assert contract["active_control_requires_explicit_handler_opt_in"] is True
    finally:
        await manager.stop()

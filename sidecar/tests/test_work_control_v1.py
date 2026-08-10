"""Adversarial contract tests for generic WorkControlV1."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.authority import required_scope
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.task_queue.models import (
    Job,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import QueueManager
from colony_sidecar.task_queue.scheduler import Scheduler
from colony_sidecar.task_queue.work_control import (
    WorkControlError,
    interrupt_capability,
    steer_capability,
)
from colony_sidecar.task_queue.worker import JobHandler, WorkerNode


@pytest.fixture(autouse=True)
def _live_control(monkeypatch):
    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "live")
    monkeypatch.setenv("COLONY_WORK_CONTROL_ACK_TIMEOUT_SECS", "30")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "true")


@pytest.fixture
async def queue(tmp_path):
    manager = QueueManager(tmp_path / "queue.db")
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()


def _caps(
    node_id: str,
    job_type: JobType,
    *,
    interrupt: bool = False,
    steer: bool = False,
) -> WorkerCapabilities:
    controls = set()
    if interrupt:
        controls.add(interrupt_capability(job_type))
    if steer:
        controls.add(steer_capability(job_type))
    return WorkerCapabilities(
        node_id=node_id,
        capabilities=controls,
        job_types={job_type},
        max_concurrent=4,
    )


async def _post_claim_start(
    queue: QueueManager,
    *,
    job_id: str,
    job_type: JobType = JobType.CUSTOM,
    payload=None,
    interrupt: bool = False,
    steer: bool = False,
    max_retries: int = 3,
):
    job = Job(
        job_id=job_id,
        job_type=job_type,
        payload=dict(payload or {"description": job_id}),
        max_retries=max_retries,
    )
    await queue.post(job)
    caps = _caps(
        "worker-1", job_type, interrupt=interrupt, steer=steer,
    )
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None
    assert claimed.claim_attempt_id
    assert await queue.start_job(
        claimed.job_id,
        "worker-1",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    return claimed, caps


def _request(projection, operation_id, operation, *, parameters=None):
    attempt = None
    spec = next(
        item for item in projection["allowed_operations"]
        if item["operation"] == operation
    )
    if spec["attempt_id_required"]:
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
        "reason": "test",
        "requested_by": "test-operator",
        "request_authority": {
            "authority_kind": "scoped_principal",
            "principal_id": "test-operator",
            "credential_id": "test-current",
            "required_scope": "work:control",
        },
    }


@pytest.mark.asyncio
async def test_projection_run_authority_and_restart_are_stable(tmp_path):
    path = tmp_path / "restart.db"
    first = QueueManager(path)
    await first.start()
    await first.post(Job(job_id="stable-target", payload={"value": 1}))
    initial = await first.work_control.inspect("stable-target")
    replay = await first.work_control.inspect("stable-target")
    assert replay == initial
    await first.stop()

    reopened = QueueManager(path)
    await reopened.start()
    try:
        restored = await reopened.work_control.inspect("stable-target")
        assert restored == initial
        assert restored["run_id"].startswith("run-")
        assert len(restored["authority_digest"]) == 64
    finally:
        await reopened.stop()


@pytest.mark.asyncio
async def test_exact_replay_and_concurrent_cas_apply_once(queue):
    await queue.post(Job(job_id="cas-target"))
    projection = await queue.work_control.inspect("cas-target")
    one = _request(projection, "cancel-one", "cancel")
    two = _request(projection, "cancel-two", "cancel")
    results = await asyncio.gather(
        queue.work_control.operate(**one),
        queue.work_control.operate(**two),
        return_exceptions=True,
    )
    receipts = [item for item in results if isinstance(item, dict)]
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(receipts) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], WorkControlError)
    stored = await queue.get_job("cas-target")
    assert stored.status is JobStatus.CANCELLED

    winning = one if receipts[0]["operation_id"] == "cancel-one" else two
    exact_replay = await queue.work_control.operate(**winning)
    assert exact_replay["replayed"] is True
    assert [event["phase"] for event in exact_replay["receipt_events"]] == [
        "accepted", "outcome",
    ]
    with pytest.raises(WorkControlError, match="different request"):
        await queue.work_control.operate(**{
            **winning,
            "reason": "changed request",
        })


@pytest.mark.asyncio
async def test_read_only_interrupt_ack_resume_and_stale_attempt_rejection(queue):
    claimed, caps = await _post_claim_start(
        queue, job_id="interrupt-read", interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    receipt = await queue.work_control.operate(**_request(
        projection, "interrupt-op", "interrupt",
    ))
    assert receipt["status"] == "pending_ack"
    assert not (await queue.complete_job(
        claimed.job_id,
        "worker-1",
        {"status": "completed", "action_plane": {"state": "completed"}},
        claim_attempt_id=claimed.claim_attempt_id,
        server_attested=True,
    ))["transitioned"]

    pending = await queue.work_control.pending_for_worker("worker-1")
    assert [item["operation_id"] for item in pending] == ["interrupt-op"]
    stopped = await queue.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="interrupt-op",
        attempt_id=claimed.claim_attempt_id,
        outcome="stopped",
        details={"cooperative_stop": True},
    )
    assert stopped["status"] == "applied"
    assert [event["phase"] for event in stopped["receipt_events"]] == [
        "accepted", "outcome",
    ]
    held = await queue.get_job(claimed.job_id)
    assert held.status is JobStatus.BLOCKED
    assert held.tags["hold_kind"] == "work_control"
    assert held.claimed_by is None
    assert held.claim_attempt_id is None

    resume_projection = await queue.work_control.inspect(claimed.job_id)
    resumed = await queue.work_control.operate(**_request(
        resume_projection, "resume-op", "retry",
    ))
    assert resumed["status"] == "applied"
    assert (await queue.get_job(claimed.job_id)).retry_count == 1
    resumed_replay = await queue.work_control.operate(**_request(
        resume_projection, "resume-op", "retry",
    ))
    assert resumed_replay["replayed"] is True
    assert (await queue.get_job(claimed.job_id)).retry_count == 1

    reclaimed = await queue.claim_job("worker-1", caps)
    assert reclaimed is not None
    assert reclaimed.claim_attempt_id != claimed.claim_attempt_id
    assert not await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "stale callback",
        claim_attempt_id=claimed.claim_attempt_id,
    )


@pytest.mark.asyncio
async def test_steer_binds_authority_and_never_changes_job_envelope(queue):
    claimed, _caps_value = await _post_claim_start(
        queue, job_id="steer-read", steer=True,
        payload={"description": "immutable", "scope": "bounded"},
    )
    before = await queue.get_job(claimed.job_id)
    envelope = (
        json.dumps(before.payload, sort_keys=True),
        [(item.name, item.minimum, item.preferred) for item in before.capabilities],
        before.timeout_secs,
        before.max_retries,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    receipt = await queue.work_control.operate(**_request(
        projection,
        "steer-op",
        "steer",
        parameters={
            "directive": "Prefer the verified source and explain uncertainty",
            "context_refs": ["memory:fact-1"],
        },
    ))
    delivery = (await queue.work_control.pending_for_worker("worker-1"))[0]
    assert delivery["authority_digest"] == projection["authority_digest"]
    with pytest.raises(WorkControlError, match="not bound"):
        await queue.work_control.acknowledge(
            worker_id="other-worker",
            operation_id="steer-op",
            attempt_id=claimed.claim_attempt_id,
            outcome="applied",
        )
    details = {
        "authority_digest": delivery["authority_digest"],
        "authority_expanded": False,
    }
    await queue.record_work_control_worker_outcome(
        worker_id="worker-1",
        operation_id="steer-op",
        attempt_id=claimed.claim_attempt_id,
        outcome="applied",
        details=details,
    )
    applied = await queue.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="steer-op",
        attempt_id=claimed.claim_attempt_id,
        outcome="applied",
        details=details,
    )
    assert receipt["receipt_events"][0] == applied["receipt_events"][0]
    after = await queue.get_job(claimed.job_id)
    assert (
        json.dumps(after.payload, sort_keys=True),
        [(item.name, item.minimum, item.preferred) for item in after.capabilities],
        after.timeout_secs,
        after.max_retries,
    ) == envelope


@pytest.mark.asyncio
async def test_emergency_interrupt_supersedes_pending_steer(queue):
    claimed, _ = await _post_claim_start(
        queue, job_id="precedence", steer=True, interrupt=True,
    )
    first = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        first, "guidance-first", "steer",
        parameters={"directive": "try another safe method"},
    ))
    pending_projection = await queue.work_control.inspect(claimed.job_id)
    assert {item["operation"] for item in pending_projection["allowed_operations"]} == {
        "interrupt", "cancel",
    }
    await queue.work_control.operate(**_request(
        pending_projection, "stop-second", "interrupt",
    ))
    guidance = await queue.work_control.receipt(
        claimed.job_id, "guidance-first",
    )
    assert guidance["status"] == "superseded"
    assert guidance["receipt_events"][-1]["phase"] == "outcome"
    pending = await queue.work_control.pending_for_worker("worker-1")
    assert [item["operation_id"] for item in pending] == ["stop-second"]


@pytest.mark.asyncio
async def test_claimed_cancel_is_atomic_and_prevents_start(queue):
    job = Job(job_id="claimed-cancel")
    await queue.post(job)
    caps = _caps("worker-1", JobType.CUSTOM)
    await queue.register_worker(caps)
    claimed = await queue.claim_job("worker-1", caps)
    assert claimed is not None
    projection = await queue.work_control.inspect(job.job_id)
    assert "cancel" in {
        item["operation"] for item in projection["allowed_operations"]
    }
    await queue.work_control.operate(**_request(
        projection, "claimed-cancel-op", "cancel",
    ))
    cancelled = await queue.get_job(job.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.claimed_by is None
    assert cancelled.claimed_at is None
    assert cancelled.claim_attempt_id is None
    assert cancelled.claim_expires_at is None
    assert cancelled.last_heartbeat is None
    assert not await queue.start_job(
        job.job_id,
        "worker-1",
        claim_attempt_id=claimed.claim_attempt_id,
    )


@pytest.mark.asyncio
async def test_started_effectful_stop_fail_and_release_never_requeue(queue):
    claimed, _ = await _post_claim_start(
        queue,
        job_id="effect-stop",
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={"action": "db_vacuum"},
        interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection, "effect-cancel", "cancel",
    ))
    result = await queue.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="effect-cancel",
        attempt_id=claimed.claim_attempt_id,
        outcome="stopped",
        details={"cooperative_stop": True},
    )
    assert result["to_job_status"] == JobStatus.NEUTRAL.value
    ambiguous = await queue.get_job(claimed.job_id)
    assert ambiguous.status is JobStatus.NEUTRAL
    assert ambiguous.tags["ambiguous_prior_effects"] == "true"
    retry_projection = await queue.work_control.inspect(claimed.job_id)
    assert "retry" not in {
        item["operation"] for item in retry_projection["allowed_operations"]
    }
    with pytest.raises(WorkControlError, match="prior effectful attempt"):
        await queue.work_control.operate(
            operation_id="forbidden-retry",
            operation="retry",
            target_id=claimed.job_id,
            run_id=retry_projection["run_id"],
            attempt_id=retry_projection["state"]["last_attempt_id"],
            expected_revision=retry_projection["revision"],
            expected_state_digest=retry_projection["state_digest"],
        )

    failed, _ = await _post_claim_start(
        queue,
        job_id="effect-fail",
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={"action": "disk_cleanup"},
    )
    assert await queue.fail_job(
        failed.job_id,
        "worker-1",
        "connection lost",
        claim_attempt_id=failed.claim_attempt_id,
    )
    assert (await queue.get_job(failed.job_id)).status is JobStatus.NEUTRAL
    # Response-loss replay for the exact terminalized attempt is idempotent.
    assert await queue.fail_job(
        failed.job_id,
        "worker-1",
        "connection lost",
        claim_attempt_id=failed.claim_attempt_id,
    )

    released, _ = await _post_claim_start(
        queue,
        job_id="effect-release",
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={"action": "log_rotate"},
    )
    assert await queue.release_job(
        released.job_id,
        "worker-1",
        claim_attempt_id=released.claim_attempt_id,
    )
    assert (await queue.get_job(released.job_id)).status is JobStatus.NEUTRAL
    assert await queue.release_job(
        released.job_id,
        "worker-1",
        claim_attempt_id=released.claim_attempt_id,
    )


@pytest.mark.asyncio
async def test_legacy_effectful_abandoned_attempt_is_never_auto_retried(queue):
    claimed, _ = await _post_claim_start(
        queue,
        job_id="legacy-abandoned-effect",
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={"action": "external_state_change"},
        max_retries=3,
    )
    assert queue._db is not None
    # Reproduce a persisted row written by the pre-WorkControl liveness path.
    await queue._db.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?",
        (JobStatus.ABANDONED.value, claimed.job_id),
    )
    await queue._db.commit()

    assert await queue.requeue_retryable_jobs(
        datetime.now(timezone.utc),
    ) == 0
    quarantined = await queue.get_job(claimed.job_id)
    assert quarantined is not None
    assert quarantined.status is JobStatus.NEUTRAL
    assert quarantined.retry_count == 0
    assert quarantined.tags["ambiguous_prior_effects"] == "true"
    assert quarantined.tags["verification_pending"] == "true"
    assert quarantined.result is not None
    assert quarantined.result.status is JobStatus.NEUTRAL
    assert quarantined.result.claim_attempt_id == claimed.claim_attempt_id
    projection = await queue.work_control.inspect(claimed.job_id)
    assert "retry" not in {
        item["operation"] for item in projection["allowed_operations"]
    }


@pytest.mark.asyncio
async def test_retry_cannot_clear_dependency_hold(queue):
    await queue.post(Job(job_id="dependency"))
    child = Job(job_id="dependent", depends_on=["dependency"])
    await queue.post(child)
    assert (await queue.get_job(child.job_id)).status is JobStatus.BLOCKED
    projection = await queue.work_control.inspect(child.job_id)
    await queue.work_control.operate(**_request(
        projection, "cancel-dependent", "cancel",
    ))
    cancelled = await queue.work_control.inspect(child.job_id)
    assert any(
        item.startswith("hold:dependency")
        for item in cancelled["state"]["retry_blockers"]
    )
    assert "retry" not in {
        item["operation"] for item in cancelled["allowed_operations"]
    }


@pytest.mark.asyncio
async def test_expired_control_unblocks_timeout_reconciliation(queue):
    claimed, _ = await _post_claim_start(
        queue, job_id="control-expiry", interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    receipt = await queue.work_control.operate(**_request(
        projection, "expiring-stop", "interrupt",
    ))
    expired_at = datetime.fromisoformat(receipt["ack_deadline"]) + timedelta(
        seconds=1,
    )
    assert await queue.reconcile_expired_work_controls(expired_at) == 1
    expired = await queue.work_control.receipt(
        claimed.job_id, "expiring-stop",
    )
    assert expired["status"] == "expired"
    assert expired["receipt_events"][-1]["phase"] == "outcome"
    # The expired command no longer strands the exact lifecycle callback.
    assert await queue.fail_job(
        claimed.job_id,
        "worker-1",
        "worker heartbeat timeout",
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert (await queue.get_job(claimed.job_id)).status is JobStatus.QUEUED


@pytest.mark.asyncio
async def test_late_ack_cannot_regain_authority_before_scheduler_tick(queue):
    claimed, _ = await _post_claim_start(
        queue, job_id="late-ack", interrupt=True,
    )
    projection = await queue.work_control.inspect(claimed.job_id)
    await queue.work_control.operate(**_request(
        projection, "late-stop", "interrupt",
    ))

    # Reproduce a delayed scheduler: the lease has elapsed in durable state,
    # but no scheduler reconciliation has run yet.
    assert queue._db is not None
    await queue._db.execute(
        "UPDATE work_control_operations SET ack_deadline = ? "
        "WHERE operation_id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "late-stop",
        ),
    )
    await queue._db.commit()

    late = await queue.work_control.acknowledge(
        worker_id="worker-1",
        operation_id="late-stop",
        attempt_id=claimed.claim_attempt_id,
        outcome="stopped",
        details={"cooperative_stop": True},
    )
    assert late["replayed"] is True
    assert late["status"] == "expired"
    assert late["acknowledgement"] == {
        "outcome": "expired",
        "details": {"reason": "worker_ack_deadline_elapsed"},
    }
    assert [event["phase"] for event in late["receipt_events"]] == [
        "accepted", "outcome",
    ]
    # A late worker response did not apply the requested stop. Ordinary
    # liveness/failure handling still owns the exact running attempt.
    still_running = await queue.get_job(claimed.job_id)
    assert still_running is not None
    assert still_running.status is JobStatus.RUNNING
    assert still_running.claim_attempt_id == claimed.claim_attempt_id


class _ControlledHandler(JobHandler):
    work_control_interrupt_safe = True
    work_control_steer_idempotent = True

    async def execute(self, job):
        return {"status": "completed", "action_plane": {"state": "completed"}}

    async def apply_steer(
        self,
        job,
        *,
        directive,
        context_refs,
        operation_id,
        authority_digest,
    ):
        return {
            "authority_digest": authority_digest,
            "authority_expanded": False,
        }


def test_default_off_worker_has_no_control_capability_or_loop(monkeypatch):
    monkeypatch.setenv("COLONY_WORK_CONTROL_MODE", "off")
    worker = WorkerNode(
        "off-worker",
        object(),
        handlers={JobType.CUSTOM: _ControlledHandler()},
    )
    assert not any(
        str(item).startswith("work_control:")
        for item in worker._capabilities.capabilities
    )


def test_work_control_exact_api_scopes():
    assert required_scope(
        "GET", "/v1/host/queue/work/job-1",
    ) == "work:read"
    assert required_scope(
        "POST", "/v1/host/queue/work/job-1/operations",
    ) == "work:control"
    assert required_scope(
        "GET", "/v1/host/queue/workers/node-1/controls",
    ) == "workers:lifecycle"
    assert required_scope(
        "POST", "/v1/host/queue/workers/node-1/controls/op-1/ack",
    ) == "workers:lifecycle"


@pytest.mark.asyncio
async def test_scheduler_expires_control_authority_before_failure_phases():
    calls = []

    class RecordingQueue:
        worker_heartbeat_ttl_secs = 45.0

        async def _phase(self, name, *_args, **_kwargs):
            calls.append(name)
            return 0

        async def reconcile_expired_work_controls(self, *args, **kwargs):
            return await self._phase("control-expiry", *args, **kwargs)

        async def expire_past_deadlines(self, *args, **kwargs):
            return await self._phase("deadline", *args, **kwargs)

        async def expire_execution_timeouts(self, *args, **kwargs):
            return await self._phase("execution-timeout", *args, **kwargs)

        async def reconcile_blocked_approval_authority(self, *args, **kwargs):
            return await self._phase("approval-repair", *args, **kwargs)

        async def expire_blocked_approvals(self, *args, **kwargs):
            return await self._phase("approval-timeout", *args, **kwargs)

        async def abandon_silent_jobs(self, *args, **kwargs):
            return await self._phase("heartbeat", *args, **kwargs)

        async def requeue_retryable_jobs(self, *args, **kwargs):
            return await self._phase("retry", *args, **kwargs)

        async def reconcile_governance_holds(self, *args, **kwargs):
            return await self._phase("governance", *args, **kwargs)

        def _schedule_worker_outcome_drain(self):
            calls.append("outcome-drain")

        async def unblock_ready_jobs(self, *args, **kwargs):
            return await self._phase("dependencies", *args, **kwargs)

        async def get_queued_jobs_sorted(self, *args, **kwargs):
            calls.append("assignment")
            return []

    scheduler = Scheduler(RecordingQueue())
    await scheduler.tick_once()
    assert calls[0] == "control-expiry"
    assert calls.index("control-expiry") < calls.index("deadline")
    assert calls.index("control-expiry") < calls.index("execution-timeout")
    assert calls.index("control-expiry") < calls.index("heartbeat")
    assert calls.index("control-expiry") < calls.index("retry")


@pytest.mark.asyncio
async def test_work_control_http_requires_exact_principal_and_executes_cas(
    queue, tmp_path, monkeypatch,
):
    await queue.post(Job(job_id="http-control"))
    keyring = tmp_path / "work-control-keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [
            {
                "principal": "operator-deck",
                "status": "active",
                "scopes": ["work:read", "work:control"],
                "audiences": [],
                "credentials": [{
                    "id": "current",
                    "secret": "operator-secret",
                    "status": "active",
                }],
            },
            {
                "principal": "other-operator",
                "status": "active",
                "scopes": ["work:read", "work:control"],
                "audiences": [],
                "credentials": [{
                    "id": "current",
                    "secret": "other-secret",
                    "status": "active",
                }],
            },
        ],
    }))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key="legacy-secret",
        keyring_path=str(keyring),
    )
    app.include_router(queue_router.router)
    monkeypatch.setattr(
        queue_router,
        "_get_queue",
        lambda: SimpleNamespace(queue=queue),
    )

    scoped_headers = {"Authorization": "Bearer operator-secret"}
    legacy_headers = {"Authorization": "Bearer legacy-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        inspected = await client.get(
            "/v1/host/queue/work/http-control",
            headers=scoped_headers,
        )
        assert inspected.status_code == 200
        projection = inspected.json()
        body = {
            "schema": "WorkControlOperationV1",
            "version": 1,
            "operation_id": "http-cancel",
            "operation": "cancel",
            "target_id": "http-control",
            "run_id": projection["run_id"],
            "attempt_id": None,
            "expected_revision": projection["revision"],
            "expected_state_digest": projection["state_digest"],
            "parameters": {},
            "reason": "operator requested cancellation",
        }
        legacy = await client.post(
            "/v1/host/queue/work/http-control/operations",
            headers=legacy_headers,
            json=body,
        )
        applied = await client.post(
            "/v1/host/queue/work/http-control/operations",
            headers=scoped_headers,
            json=body,
        )
        cross_principal_reuse = await client.post(
            "/v1/host/queue/work/http-control/operations",
            headers={"Authorization": "Bearer other-secret"},
            json=body,
        )
        receipt = await client.get(
            "/v1/host/queue/work/http-control/operations/http-cancel",
            headers=scoped_headers,
        )

    assert legacy.status_code == 403
    assert legacy.json()["detail"]["code"] == (
        "exact_work_control_principal_required"
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert applied.json()["requested_by"] == "operator-deck"
    assert cross_principal_reuse.status_code == 409
    assert cross_principal_reuse.json()["detail"]["code"] == (
        "operation_id_conflict"
    )
    assert receipt.status_code == 200
    assert receipt.json()["operation_id"] == "http-cancel"
    assert (await queue.get_job("http-control")).status is JobStatus.CANCELLED

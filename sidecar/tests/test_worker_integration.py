"""Integration test: submit → WorkerNode executes → status=completed."""

from __future__ import annotations

import asyncio

import pytest

from datetime import datetime, timedelta, timezone

from colony_sidecar.task_queue.models import (
    Job, JobType, WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.task_queue.worker import JobHandler, WorkerNode


class _EchoHandler(JobHandler):
    """Test-only handler that echoes the payload back."""

    async def execute(self, job) -> dict:
        return {"echo": job.payload}


class _FailingHandler(JobHandler):
    async def execute(self, job) -> dict:
        raise RuntimeError("intentional failure")


async def _wait_for_status(queue, job_id: str, status: str, timeout: float = 10.0):
    """Poll the queue until the job reaches the given status or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        job = await queue.get_job(job_id)
        if job is not None and job.status.value == status:
            return job
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"Timed out waiting for job {job_id} to reach status={status}"
    )


@pytest.mark.asyncio
async def test_worker_executes_submitted_job(tmp_path):
    # Reset singleton so successive tests don't collide.
    TaskQueueManager._instance = None
    mgr = await TaskQueueManager.initialize(db_path=tmp_path / "q.db")

    worker = WorkerNode(
        node_id="test-node-1",
        queue=mgr.queue,
        handlers={JobType.CUSTOM: _EchoHandler()},
        poll_interval_secs=0.05,
        heartbeat_interval_secs=60.0,
    )
    worker_task = asyncio.create_task(worker.start())
    try:
        submitted = await mgr.submit(
            task_type="custom",
            params={"hello": "world"},
        )
        job = await _wait_for_status(mgr.queue, submitted["id"], "completed")
        assert job.result is not None
        assert job.result.output == {"echo": {"hello": "world"}}
    finally:
        await worker.stop(drain_timeout=2.0)
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass
        await mgr.queue.stop()


@pytest.mark.asyncio
async def test_worker_marks_job_failed_on_handler_exception(tmp_path):
    TaskQueueManager._instance = None
    mgr = await TaskQueueManager.initialize(db_path=tmp_path / "q.db")

    worker = WorkerNode(
        node_id="test-node-2",
        queue=mgr.queue,
        handlers={JobType.CUSTOM: _FailingHandler()},
        poll_interval_secs=0.05,
        heartbeat_interval_secs=60.0,
    )
    worker_task = asyncio.create_task(worker.start())
    try:
        submitted = await mgr.submit(task_type="custom", params={})
        # Retry behavior may bounce it back to queued; poll for terminal state.
        deadline = asyncio.get_event_loop().time() + 10
        job = None
        while asyncio.get_event_loop().time() < deadline:
            job = await mgr.queue.get_job(submitted["id"])
            if job is not None and job.status.value in {"failed", "completed"}:
                break
            await asyncio.sleep(0.1)
        assert job is not None
        assert job.status.value == "failed"
        assert job.result is not None
        assert "intentional failure" in (job.result.error or "")
    finally:
        await worker.stop(drain_timeout=2.0)
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass
        await mgr.queue.stop()


@pytest.mark.asyncio
async def test_dead_worker_lease_expires_and_job_requeues(tmp_path):
    """Item 5 lease guarantee: a claimed job whose worker stops heart-beating
    is abandoned and requeued so another worker can pick it up."""
    TaskQueueManager._instance = None
    mgr = await TaskQueueManager.initialize(db_path=tmp_path / "q.db")
    queue = mgr.queue
    try:
        job_id = await queue.post(Job(job_type=JobType.CUSTOM, max_retries=2))
        caps = WorkerCapabilities(node_id="dead-node")
        claimed = await queue.claim_job("dead-node", caps)
        assert claimed is not None and claimed.job_id == job_id

        # Worker goes silent: advance the clock past the lease timeout.
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        abandoned = await queue.abandon_silent_jobs(future, timeout_secs=60)
        assert abandoned == 1
        job = await queue.get_job(job_id)
        assert job.status.value == "abandoned"

        requeued = await queue.requeue_retryable_jobs(future)
        assert requeued == 1
        job = await queue.get_job(job_id)
        assert job.status.value == "queued"
        assert job.retry_count == 1
        assert job.claimed_by is None  # lease released for the next worker
    finally:
        await queue.stop()

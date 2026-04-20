"""Integration test: submit → WorkerNode executes → status=completed."""

from __future__ import annotations

import asyncio

import pytest

from colony_sidecar.task_queue.models import JobType
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

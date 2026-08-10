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


class _ThoughtEchoHandler(_EchoHandler):
    """Test double carrying the production thought-only attestation marker."""

    thought_only = True


class _FailingHandler(JobHandler):
    async def execute(self, job) -> dict:
        raise RuntimeError("intentional failure")


class _RecordingHandler(JobHandler):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, job) -> dict:
        self.calls += 1
        return {"unexpected": job.job_id}


class _BlockingHandler(JobHandler):
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def execute(self, job) -> dict:  # noqa: ARG002
        self.entered.set()
        await asyncio.Event().wait()


class _StartRefusingQueue:
    async def start_job(
        self, job_id, worker_id, claim_attempt_id=None,  # noqa: ARG002
    ):
        return False

    async def complete_job(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("completion must not run after rejected start")

    async def fail_job(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("failure must not run after rejected start")


class _CompletionRefusingQueue:
    async def start_job(self, *args, **kwargs):
        return True

    async def complete_job(self, *args, **kwargs):
        return {"transitioned": False, "outcome_reason": "stale_attempt"}

    async def fail_job(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("a rejected completion is already server-resolved")


class _LifecycleQueue:
    def __init__(self, *, fail_register=False, attest=True):
        self.fail_register = fail_register
        self.attest = attest
        self.events = []
        self.registered = asyncio.Event()

    async def register_worker(self, capabilities):
        self.events.append(("register", capabilities))
        self.registered.set()
        if self.fail_register:
            raise RuntimeError("register failed")

    async def deregister_worker(self, node_id):
        self.events.append(("deregister", node_id))

    def set_thought_runtime_ready(self, ready, *, node_id="", reason=""):
        self.events.append(("thought", ready, node_id, reason))
        return bool(ready and self.attest)


class _BlockedClaimQueue(_LifecycleQueue):
    def __init__(self):
        super().__init__()
        self.claim_started = asyncio.Event()
        self.return_claim = asyncio.Event()
        self.released = []
        self.job = Job(
            job_type=JobType.CUSTOM,
            claim_attempt_id="attempt-after-stop",
        )

    async def claim_job(self, _node_id, _capabilities):
        self.claim_started.set()
        try:
            await self.return_claim.wait()
        except asyncio.CancelledError:
            # Simulate a transport/client that finishes its response despite
            # task cancellation; WorkerNode still owns the returned lease.
            await self.return_claim.wait()
        return self.job

    async def release_job(
        self, job_id, node_id, *, claim_attempt_id,
    ):
        self.released.append((job_id, node_id, claim_attempt_id))
        return True


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
    is released so another worker can pick it up without spending a retry."""
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
        assert job.status.value == "queued"
        assert job.retry_count == 0
        assert job.claimed_by is None  # lease released for the next worker
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_expired_job_cannot_be_claimed_before_scheduler_sweep(tmp_path):
    """Claim is its own deadline gate; scheduler ordering is not authority."""

    TaskQueueManager._instance = None
    mgr = await TaskQueueManager.initialize(db_path=tmp_path / "q.db")
    queue = mgr.queue
    try:
        await queue.post(Job(
            job_id="expired-custom",
            job_type=JobType.CUSTOM,
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        caps = WorkerCapabilities(
            node_id="late-worker",
            job_types={JobType.CUSTOM},
        )
        assert await queue.claim_job("late-worker", caps) is None
        still_queued = await queue.get_job("expired-custom")
        assert still_queued.status.value == "queued"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_worker_never_executes_handler_after_start_authority_rejection():
    handler = _RecordingHandler()
    worker = WorkerNode(
        node_id="worker-a",
        queue=_StartRefusingQueue(),
        handlers={JobType.CUSTOM: handler},
    )
    await worker._execute_job(Job(job_type=JobType.CUSTOM))
    assert handler.calls == 0
    assert worker._job_start_times == {}


@pytest.mark.asyncio
async def test_worker_never_learns_from_server_rejected_completion():
    learned = []
    worker = WorkerNode(
        node_id="worker-a",
        queue=_CompletionRefusingQueue(),
        handlers={JobType.CUSTOM: _EchoHandler()},
        skill_learning_service=object(),
    )

    async def record_hook(job, output):
        learned.append((job.job_id, output))

    worker._fire_skill_hook = record_hook
    await worker._execute_job(Job(
        job_type=JobType.CUSTOM,
        claim_attempt_id="stale-attempt",
    ))
    await asyncio.sleep(0)
    assert learned == []


@pytest.mark.asyncio
async def test_worker_never_learns_from_semantic_failed_completion():
    learned = []

    class SemanticFailureQueue(_CompletionRefusingQueue):
        async def complete_job(self, *args, **kwargs):
            return {
                "transitioned": True,
                "job_status": "failed",
                "governor_outcome": "failure",
            }

    worker = WorkerNode(
        node_id="worker-a",
        queue=SemanticFailureQueue(),
        handlers={JobType.CUSTOM: _EchoHandler()},
        skill_learning_service=object(),
    )

    async def record_hook(job, output):
        learned.append((job.job_id, output))

    worker._fire_skill_hook = record_hook
    await worker._execute_job(Job(
        job_type=JobType.CUSTOM,
        claim_attempt_id="attempt-1",
    ))
    await asyncio.sleep(0)
    assert learned == []


@pytest.mark.asyncio
async def test_worker_stop_cancels_and_closes_jobs_after_drain_timeout(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "q.db")
    handler = _BlockingHandler()
    worker = WorkerNode(
        node_id="shutdown-worker",
        queue=manager.queue,
        handlers={JobType.CUSTOM: handler},
        poll_interval_secs=0.01,
        heartbeat_interval_secs=60,
    )
    worker_task = asyncio.create_task(worker.start())
    job = Job(job_type=JobType.CUSTOM)
    await manager.queue.post(job)
    try:
        await asyncio.wait_for(handler.entered.wait(), timeout=2)
        await worker.stop(drain_timeout=0.01)
        stored = await manager.queue.get_job(job.job_id)
        assert stored is not None and stored.status.value == "queued"
        assert stored.claimed_by is None
        assert stored.result.error == "worker_shutdown_drain_timeout"
        assert worker._running_jobs == {}
    finally:
        await worker_task
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_thought_worker_registration_failure_clears_all_lifecycle_state():
    queue = _LifecycleQueue(fail_register=True)
    worker = WorkerNode(
        node_id="thought-node",
        queue=queue,
        handlers={JobType.THOUGHT: _ThoughtEchoHandler()},
    )

    with pytest.raises(RuntimeError, match="register failed"):
        await worker.start()

    assert worker._running is False
    assert worker._registered is False
    assert ("thought", False, "thought-node", "thought_worker_loop_exited") in (
        queue.events
    )
    assert ("deregister", "thought-node") in queue.events


@pytest.mark.asyncio
async def test_thought_worker_rejected_owner_attestation_deregisters_cleanly():
    queue = _LifecycleQueue(attest=False)
    worker = WorkerNode(
        node_id="wrong-node",
        queue=queue,
        handlers={JobType.THOUGHT: _ThoughtEchoHandler()},
    )

    with pytest.raises(RuntimeError, match="does not match"):
        await worker.start()

    assert worker._running is False
    assert worker._registered is False
    assert queue.events[-1] == ("deregister", "wrong-node")
    assert [event[1] for event in queue.events if event[0] == "thought"] == [
        True, False,
    ]


@pytest.mark.asyncio
async def test_thought_worker_loop_exception_clears_readiness_and_registration(
    monkeypatch,
):
    queue = _LifecycleQueue(attest=True)
    worker = WorkerNode(
        node_id="thought-node",
        queue=queue,
        handlers={JobType.THOUGHT: _ThoughtEchoHandler()},
    )

    async def crash():
        raise RuntimeError("worker loop crashed")

    sibling_cancelled = asyncio.Event()

    async def block_forever():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    monkeypatch.setattr(worker, "_poll_loop", crash)
    monkeypatch.setattr(worker, "_heartbeat_loop", block_forever)
    with pytest.raises(RuntimeError, match="worker loop crashed"):
        await worker.start()

    assert worker._running is False
    assert worker._registered is False
    assert sibling_cancelled.is_set()
    thought_events = [event for event in queue.events if event[0] == "thought"]
    assert thought_events[0][1:] == (True, "thought-node", "")
    assert thought_events[-1][1:] == (
        False, "thought-node", "thought_worker_loop_exited",
    )
    assert queue.events[-1] == ("deregister", "thought-node")


@pytest.mark.asyncio
async def test_normal_early_loop_exit_cancels_forever_sibling(monkeypatch):
    queue = _LifecycleQueue()
    worker = WorkerNode(
        node_id="early-exit-node",
        queue=queue,
        handlers={JobType.CUSTOM: _EchoHandler()},
    )
    sibling_cancelled = asyncio.Event()

    async def returns_early():
        return None

    async def block_forever():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    monkeypatch.setattr(worker, "_poll_loop", returns_early)
    monkeypatch.setattr(worker, "_heartbeat_loop", block_forever)
    with pytest.raises(RuntimeError, match="loop exited unexpectedly"):
        await worker.start()

    assert sibling_cancelled.is_set()
    assert worker._registered is False
    assert queue.events[-1] == ("deregister", "early-exit-node")


@pytest.mark.asyncio
async def test_register_handler_is_rejected_after_worker_start(monkeypatch):
    queue = _LifecycleQueue()
    worker = WorkerNode(
        node_id="custom-node",
        queue=queue,
        handlers={JobType.CUSTOM: _EchoHandler()},
    )

    async def until_stopped():
        while worker._running:
            await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_poll_loop", until_stopped)
    monkeypatch.setattr(worker, "_heartbeat_loop", until_stopped)
    running = asyncio.create_task(worker.start())
    await asyncio.wait_for(queue.registered.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="before the worker starts"):
        worker.register_handler(JobType.THOUGHT, _EchoHandler())
    await worker.stop()
    await asyncio.wait_for(running, timeout=1)
    assert JobType.THOUGHT not in worker._handlers


@pytest.mark.asyncio
async def test_stop_releases_claim_returned_after_loop_cancellation():
    queue = _BlockedClaimQueue()
    handler = _RecordingHandler()
    worker = WorkerNode(
        node_id="blocked-claim-node",
        queue=queue,
        handlers={JobType.CUSTOM: handler},
        poll_interval_secs=0.01,
        heartbeat_interval_secs=60,
    )
    running = asyncio.create_task(worker.start())
    await asyncio.wait_for(queue.claim_started.wait(), timeout=1)

    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    queue.return_claim.set()
    await asyncio.wait_for(stopping, timeout=1)
    await asyncio.wait_for(running, timeout=1)

    assert handler.calls == 0
    assert queue.released == [(
        queue.job.job_id,
        "blocked-claim-node",
        "attempt-after-stop",
    )]
    assert worker._running_jobs == {}

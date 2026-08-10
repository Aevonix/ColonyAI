"""Worker registry truth shared by scheduling, claims, and operator stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.task_queue.models import Job, WorkerCapabilities
from colony_sidecar.task_queue.queue_manager import QueueManager, TaskQueueManager
from colony_sidecar.task_queue.scheduler import Scheduler


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_stats_and_scheduler_candidates_exclude_stale_workers(tmp_path):
    """Reproduce the live bug without relying on the new clock seam."""

    queue = QueueManager(db_path=tmp_path / "queue.db")
    await queue.start()
    try:
        await queue.register_worker(WorkerCapabilities(node_id="fresh"))
        await queue.register_worker(WorkerCapabilities(node_id="stale"))
        fresh_at = datetime.now(timezone.utc)
        stale_at = fresh_at - timedelta(minutes=10)
        await queue._db.execute(
            "UPDATE workers SET last_seen = ? WHERE node_id = ?",
            (fresh_at.isoformat(), "fresh"),
        )
        await queue._db.execute(
            "UPDATE workers SET last_seen = ? WHERE node_id = ?",
            (stale_at.isoformat(), "stale"),
        )
        await queue._db.commit()

        stats = await queue.get_queue_stats()
        assert stats.total_workers == 2
        # Historical behavior counted both rows here even though one worker
        # had been silent for ten heartbeat windows.
        assert stats.available_workers == 1
        assert stats.registered_workers == 2
        assert stats.active_workers == 1
        assert stats.stale_workers == 1
        assert stats.worker_heartbeat_ttl_secs == 60.0

        candidates = await queue.get_available_workers()
        assert [worker.node_id for worker in candidates] == ["fresh"]

        governance = await queue.governance_status()
        assert governance["maintenance_ready"] is True
        assert governance["execution_ready"] is True
        assert governance["available_workers"] == 1
        assert governance["stale_workers"] == 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_injected_clock_drives_claim_truth_and_heartbeat_recovery(tmp_path):
    clock = _Clock(datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc))
    queue = QueueManager(
        db_path=tmp_path / "queue.db",
        worker_heartbeat_ttl_secs=60.0,
        clock=clock,
    )
    await queue.start()
    try:
        caps = WorkerCapabilities(node_id="worker-a")
        await queue.register_worker(caps)
        job_id = await queue.post(Job())

        clock.advance(60)
        at_boundary = await queue.get_queue_stats()
        assert at_boundary.active_workers == 1
        assert at_boundary.available_workers == 1

        clock.advance(1)
        assert await queue.claim_job("worker-a", caps) is None
        stale = await queue.get_queue_stats()
        assert stale.active_workers == 0
        assert stale.stale_workers == 1
        assert stale.available_workers == 0

        assert await queue.send_heartbeat("worker-a", []) == 0
        caps.available = False
        await queue.register_worker(caps)
        assert await queue.claim_job("worker-a", caps) is None
        caps.available = True
        await queue.register_worker(caps)
        await queue.update_worker_load("worker-a", 1.0)
        assert await queue.claim_job("worker-a", caps) is None
        await queue.update_worker_load("worker-a", 0.0)
        recovered = await queue.claim_job("worker-a", caps)
        assert recovered is not None
        assert recovered.job_id == job_id
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_unregistered_direct_claim_compatibility_is_preserved(tmp_path):
    """QueueManager's historical direct-call API remains source compatible."""

    queue = QueueManager(db_path=tmp_path / "queue.db")
    await queue.start()
    try:
        job_id = await queue.post(Job())
        caps = WorkerCapabilities(node_id="legacy-direct")
        claimed = await queue.claim_job("legacy-direct", caps)
        assert claimed is not None
        assert claimed.job_id == job_id
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_queue_stats_api_exposes_additive_worker_truth(tmp_path):
    TaskQueueManager._instance = None
    clock = _Clock(datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc))
    manager = await TaskQueueManager.initialize(
        db_path=tmp_path / "queue.db",
        worker_heartbeat_ttl_secs=60.0,
        clock=clock,
    )
    try:
        await manager.queue.register_worker(
            WorkerCapabilities(node_id="old-worker"),
        )
        clock.advance(61)
        await manager.queue.register_worker(
            WorkerCapabilities(node_id="fresh-worker"),
        )

        payload = await queue_router.queue_stats()
        assert payload["total_workers"] == 2
        assert payload["registered_workers"] == 2
        assert payload["active_workers"] == 1
        assert payload["stale_workers"] == 1
        assert payload["available_workers"] == 1
        assert payload["worker_heartbeat_ttl_secs"] == 60.0
        assert payload["governance"]["maintenance_ready"] is True
        assert payload["governance"]["execution_ready"] is True
        assert payload["governance"]["registered_workers"] == 2
        assert payload["governance"]["active_workers"] == 1
        assert payload["governance"]["stale_workers"] == 1
        assert payload["governance"]["available_workers"] == 1

        # Capacity and liveness are distinct: a fresh, full worker leaves no
        # available slot but the execution plane remains active.
        await manager.queue.update_worker_load("fresh-worker", 1.0)
        full = await queue_router.queue_stats()
        assert full["active_workers"] == 1
        assert full["available_workers"] == 0
        assert full["governance"]["execution_ready"] is True

        clock.advance(61)
        expired = await queue_router.queue_stats()
        assert expired["total_workers"] == 2
        assert expired["registered_workers"] == 2
        assert expired["active_workers"] == 0
        assert expired["stale_workers"] == 2
        assert expired["available_workers"] == 0
        assert expired["governance"]["maintenance_ready"] is True
        assert expired["governance"]["execution_ready"] is False
        assert (
            expired["governance"]["execution_readiness_reason"]
            == "no_fresh_workers"
        )
    finally:
        await manager.stop()


def test_queue_and_scheduler_share_one_bounded_worker_ttl(tmp_path):
    queue = QueueManager(
        db_path=tmp_path / "queue.db",
        worker_heartbeat_ttl_secs=45.0,
    )
    scheduler = Scheduler(queue)
    assert queue.worker_heartbeat_ttl_secs == 45.0
    assert scheduler.worker_heartbeat_ttl_secs == 45.0

    with pytest.raises(
        ValueError,
        match="worker_heartbeat_ttl_secs must be between",
    ):
        QueueManager(
            db_path=tmp_path / "too-small.db",
            worker_heartbeat_ttl_secs=0.5,
        )
    with pytest.raises(
        ValueError,
        match="worker_heartbeat_ttl_secs must be between",
    ):
        QueueManager(
            db_path=tmp_path / "too-large.db",
            worker_heartbeat_ttl_secs=3601.0,
        )

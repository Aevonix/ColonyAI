"""Distributed Task Queue — Queen Scheduler.

Runs on the Sovereign node. Drives five scheduling phases per tick:
1. Expire past deadlines
2. Abandon silent jobs (heartbeat timeout)
3. Requeue retryable jobs
4. Unblock ready jobs (deps met)
5. Assign queued jobs to available workers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from colony_sidecar.task_queue.models import WorkerCapabilities
from colony_sidecar.task_queue.queue_manager import QueueManager

logger = logging.getLogger(__name__)


class Scheduler:
    """Priority-aware, capability-matching job scheduler.

    Runs on the Sovereign node. The Regent runs a passive replica
    and takes over if the Sovereign is unavailable.

    Usage::

        scheduler = Scheduler(queue=queue_manager)
        task = asyncio.create_task(scheduler.run())
        ...
        await scheduler.stop()
        await task
    """

    def __init__(
        self,
        queue: QueueManager,
        tick_interval_secs: float = 2.0,
        heartbeat_timeout_secs: float = 60.0,
        claim_timeout_secs: float = 30.0,
        no_worker_warning_secs: float = 300.0,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._queue = queue
        self._tick = tick_interval_secs
        self._heartbeat_timeout = heartbeat_timeout_secs
        self._claim_timeout = claim_timeout_secs
        self._no_worker_warning = no_worker_warning_secs
        self._event_bus = event_bus
        self._running = False

    async def run(self) -> None:
        """Main scheduling loop. Blocks until stop() is called."""
        self._running = True
        logger.info("Scheduler started (tick=%.1fs)", self._tick)
        while self._running:
            try:
                await self._tick_once()
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self._tick)
        logger.info("Scheduler stopped")

    async def stop(self) -> None:
        """Signal the scheduling loop to exit after the current tick."""
        self._running = False

    async def tick_once(self) -> None:
        """Public single-tick for testing or manual scheduling."""
        await self._tick_once()

    async def _tick_once(self) -> None:
        now = datetime.now(timezone.utc)

        # Phase 1: Expire deadlines
        expired = await self._queue.expire_past_deadlines(now)
        if expired:
            logger.info("Scheduler: expired %d deadline-past jobs", expired)

        # Phase 2: Detect abandoned jobs (heartbeat timeout)
        abandoned = await self._queue.abandon_silent_jobs(
            now, timeout_secs=self._heartbeat_timeout
        )
        if abandoned:
            logger.info("Scheduler: abandoned %d silent jobs", abandoned)

        # Phase 3: Redistribute abandoned/retryable jobs
        requeued = await self._queue.requeue_retryable_jobs(now)
        if requeued:
            logger.info("Scheduler: requeued %d retryable jobs", requeued)

        # Phase 4: Unblock jobs whose dependencies are now met
        unblocked = await self._queue.unblock_ready_jobs()
        if unblocked:
            logger.info("Scheduler: unblocked %d dependent jobs", unblocked)

        # Phase 5: Assign QUEUED jobs to available workers
        await self._assign_queued_jobs(now)

    async def _assign_queued_jobs(self, now: datetime) -> None:
        queued = await self._queue.get_queued_jobs_sorted(now)
        if not queued:
            return

        workers = await self._queue.get_available_workers()

        for job in queued:
            best_worker: Optional[WorkerCapabilities] = None
            best_score = 0.0

            for worker in workers:
                # Headroom bonus: prefer workers with more open slots
                running_count = round(worker.load * worker.max_concurrent)
                headroom = worker.max_concurrent - running_count
                score = worker.affinity_score(job)
                if headroom >= 2:
                    score += 0.05 * (headroom - 1)
                if score > best_score:
                    best_score = score
                    best_worker = worker

            if best_worker is None:
                if job.deadline is not None:
                    remaining = (job.deadline - now).total_seconds()
                    if 0 < remaining < self._no_worker_warning:
                        logger.warning(
                            "No capable worker for job %s (type=%s, required=%s, "
                            "deadline in %.0fs)",
                            job.job_id, job.job_type,
                            job.required_capabilities(), remaining,
                        )
            else:
                await self._queue.notify_worker(best_worker.node_id, job.job_id)

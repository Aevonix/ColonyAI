"""Distributed Task Queue — Queen Scheduler.

Runs on the Sovereign node. Drives five scheduling phases per tick:
1. Expire past deadlines
2. Abandon silent jobs (heartbeat timeout)
3. Requeue retryable jobs
4. Reconcile explicit governance holds
5. Unblock ready jobs (deps met)
6. Assign queued jobs to available workers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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
        heartbeat_timeout_secs: Optional[float] = None,
        claim_timeout_secs: float = 30.0,
        no_worker_warning_secs: float = 300.0,
        event_bus: Optional[Any] = None,
        readiness_callback: Optional[Callable[[bool, str], None]] = None,
        approval_timeout_hours: Optional[float] = None,
    ) -> None:
        self._queue = queue
        self._tick = tick_interval_secs
        if heartbeat_timeout_secs is not None:
            # Backward-compatible constructor overrides update the queue-owned
            # value rather than creating a second liveness interpretation.
            self._queue.configure_worker_heartbeat_ttl(
                heartbeat_timeout_secs
            )
        self._claim_timeout = claim_timeout_secs
        self._no_worker_warning = no_worker_warning_secs
        self._event_bus = event_bus
        self._readiness_callback = readiness_callback
        if approval_timeout_hours is None:
            try:
                import os
                approval_timeout_hours = float(os.environ.get(
                    "COLONY_APPROVAL_TIMEOUT_HOURS", "72"
                ))
            except (TypeError, ValueError):
                approval_timeout_hours = 72.0
        if approval_timeout_hours <= 0:
            raise ValueError("approval_timeout_hours must be positive")
        self._approval_timeout_hours = approval_timeout_hours
        self._running = False
        self._last_tick_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._tick_count = 0

    @property
    def worker_heartbeat_ttl_secs(self) -> float:
        return self._queue.worker_heartbeat_ttl_secs

    @property
    def health(self) -> dict:
        return {
            "running": self._running,
            "healthy": self._last_error is None,
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
            "tick_count": self._tick_count,
        }

    async def run(self) -> None:
        """Main scheduling loop. Blocks until stop() is called."""
        self._running = True
        logger.info("Scheduler started (tick=%.1fs)", self._tick)
        while self._running:
            try:
                await self.tick_once()
            except Exception as exc:
                self._last_error = str(exc)[:500]
                if self._readiness_callback is not None:
                    self._readiness_callback(False, self._last_error)
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self._tick)
        logger.info("Scheduler stopped")

    async def stop(self) -> None:
        """Signal the scheduling loop to exit after the current tick."""
        self._running = False
        if self._readiness_callback is not None:
            self._readiness_callback(False, "scheduler_stopped")

    async def tick_once(self) -> None:
        """Public single-tick for testing or manual scheduling."""
        await self._tick_once()
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        self._last_error = None
        self._tick_count += 1
        if self._readiness_callback is not None:
            self._readiness_callback(True, "scheduler_tick_ok")

    async def _tick_once(self) -> None:
        now = datetime.now(timezone.utc)

        # Expire bounded WorkControl acknowledgement leases first. Otherwise
        # a dead/uncooperative worker's pending stop could suppress the very
        # timeout/failure transition needed to close its attempt.
        expired_controls = await self._queue.reconcile_expired_work_controls(
            now,
        )
        if expired_controls:
            logger.info(
                "Scheduler: reconciled %d expired WorkControl commands",
                expired_controls,
            )

        # Phase 1: Expire deadlines
        expired = await self._queue.expire_past_deadlines(now)
        if expired:
            logger.info("Scheduler: expired %d deadline-past jobs", expired)

        execution_timeouts = await self._queue.expire_execution_timeouts(now)
        if execution_timeouts:
            logger.info(
                "Scheduler: expired %d execution timeouts",
                execution_timeouts,
            )

        approval_repairs = await self._queue.reconcile_blocked_approval_authority()
        if approval_repairs:
            logger.info(
                "Scheduler: reconciled %d owner-approval holds",
                approval_repairs,
            )

        approval_timeouts = await self._queue.expire_blocked_approvals(
            now, timeout_hours=self._approval_timeout_hours,
        )
        if approval_timeouts:
            logger.info(
                "Scheduler: expired %d owner-approval holds",
                approval_timeouts,
            )

        # Phase 2: Detect abandoned jobs (heartbeat timeout)
        abandoned = await self._queue.abandon_silent_jobs(
            now,
            timeout_secs=self.worker_heartbeat_ttl_secs,
            claim_timeout_secs=self._claim_timeout,
        )
        if abandoned:
            logger.info("Scheduler: abandoned %d silent jobs", abandoned)

        # Phase 3: Redistribute abandoned/retryable jobs
        requeued = await self._queue.requeue_retryable_jobs(now)
        if requeued:
            logger.info("Scheduler: requeued %d retryable jobs", requeued)

        # Phase 4: Re-evaluate only explicit boundary/governor holds. This
        # safely handles directive lifts and mode rollback without conflating
        # them with dependency or approval state.
        governance_released = await self._queue.reconcile_governance_holds()
        if governance_released:
            logger.info(
                "Scheduler: released %d governance-held jobs",
                governance_released,
            )

        # Durable competence/governor events survive cancellation and process
        # restart. Drain them independently from worker execution enablement.
        # Governor delivery may involve an LLM or outbound notice. Schedule
        # its bounded reconciler without ever stalling lease/deadline phases.
        self._queue._schedule_worker_outcome_drain()

        # Phase 5: Unblock jobs whose dependencies are now met
        unblocked = await self._queue.unblock_ready_jobs()
        if unblocked:
            logger.info("Scheduler: unblocked %d dependent jobs", unblocked)

        # Reconcile source-owned runtime holds after dependencies so a
        # rollback/re-enable cycle converges before worker notification.  The
        # actual claim/start boundaries independently re-evaluate the fence.
        source_reconciler = getattr(
            self._queue, "reconcile_runtime_claim_holds", None,
        )
        source_released = (
            await source_reconciler() if callable(source_reconciler) else 0
        )
        if source_released:
            logger.info(
                "Scheduler: released %d source-runtime-held jobs",
                source_released,
            )

        # Phase 6: Assign QUEUED jobs to available workers
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

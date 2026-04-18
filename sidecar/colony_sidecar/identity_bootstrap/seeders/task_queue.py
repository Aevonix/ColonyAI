"""Task queue seeder — registers bootstrap worker capabilities and posts a self-check job."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TaskQueueSeeder:
    name = "task_queue"

    def __init__(self, queue_manager: Optional[Any] = None) -> None:
        self._queue_manager = queue_manager

    async def seed(self, corpus: Any) -> None:
        queue = self._queue_manager
        if queue is None:
            logger.debug("task_queue: no queue_manager — skipping")
            return

        try:
            from colony_sidecar.task_queue.models import (
                Job,
                JobType,
                JobPriority,
                WorkerCapabilities,
            )
        except ImportError as exc:
            logger.debug("task_queue: import failed — skipping: %s", exc)
            return

        colony_id = corpus.colony_id

        # Register bootstrap worker capabilities
        caps = WorkerCapabilities(
            node_id=f"bootstrap-{colony_id[:8]}",
            capabilities={"bootstrap", "self_check", "identity"},
            capacity={"cpu": 1.0},
            max_concurrent=1,
            job_types={JobType.SYSTEM_MAINTENANCE, JobType.MONITORING},
            available=True,
            load=0.0,
            registered_at=datetime.now(timezone.utc),
        )
        try:
            await queue.register_worker(caps)
        except Exception as exc:
            logger.debug("task_queue: register_worker failed: %s", exc)

        # Post the initial self-check job
        job_id = f"job-bootstrap-selfcheck-{colony_id[:8]}"
        job = Job(
            job_id=job_id,
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={
                "action": "identity_self_check",
                "colony_id": colony_id,
                "corpus_version": corpus.corpus_version,
                "triggered_by": "identity_bootstrap",
            },
            priority=JobPriority.LOW,
            posted_by=f"bootstrap-{colony_id[:8]}",
            tags={"type": "bootstrap", "colony_id": colony_id[:8]},
        )
        try:
            await queue.post(job)
            logger.info("task_queue: self-check job posted (id=%s)", job_id)
        except Exception as exc:
            logger.debug("task_queue: post job failed: %s", exc)

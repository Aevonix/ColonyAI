"""GoalQueueBridge — integrate Goal Engine with the distributed task queue.

Converts Subtask nodes into task queue Jobs, dispatches ready subtasks,
and processes job results to update goal/subtask state.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Protocol

from colony_sidecar.goals.models import (
    Goal,
    GoalDAG,
    GoalPriority,
    GoalStatus,
    Subtask,
    SubtaskStatus,
)
from colony_sidecar.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobResult,
    JobType,
)

logger = logging.getLogger(__name__)

# Priority mapping: GoalPriority → JobPriority
_PRIORITY_MAP: Dict[int, JobPriority] = {
    GoalPriority.BACKGROUND: JobPriority.BACKGROUND,
    GoalPriority.LOW:        JobPriority.LOW,
    GoalPriority.NORMAL:     JobPriority.NORMAL,
    GoalPriority.HIGH:       JobPriority.HIGH,
    GoalPriority.CRITICAL:   JobPriority.CRITICAL,
}


def _map_priority(goal_priority: GoalPriority) -> JobPriority:
    return _PRIORITY_MAP.get(goal_priority.value, JobPriority.NORMAL)


def _job_type_from_string(job_type: str) -> JobType:
    """Convert subtask job_type string to JobType enum (with fallback)."""
    try:
        return JobType(job_type)
    except ValueError:
        return JobType.CUSTOM


class QueueBackend(Protocol):
    """Minimal interface expected from the task queue."""

    def post(self, job: Job) -> str:
        """Post a job and return its job_id."""
        ...


class InMemoryQueueBackend:
    """Simple in-memory queue backend for testing without a real queue."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._posted_order: List[str] = []

    def post(self, job: Job) -> str:
        self._jobs[job.job_id] = job
        self._posted_order.append(job.job_id)
        logger.debug("InMemoryQueue: posted job %s (type=%s)", job.job_id, job.job_type)
        return job.job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[Job]:
        return [self._jobs[jid] for jid in self._posted_order if jid in self._jobs]


class GoalQueueBridge:
    """Bridge between the Goal Engine and the distributed task queue."""

    def __init__(self, queue: Optional[QueueBackend] = None) -> None:
        self._queue: QueueBackend = queue or InMemoryQueueBackend()

    def subtask_to_job(self, goal: Goal, subtask: Subtask) -> Job:
        """Convert a ready Subtask into a task queue Job.

        Priority mapping:
          GoalPriority.CRITICAL → JobPriority.CRITICAL
          GoalPriority.HIGH     → JobPriority.HIGH
          GoalPriority.NORMAL   → JobPriority.NORMAL
          GoalPriority.LOW      → JobPriority.LOW
          GoalPriority.BACKGROUND → JobPriority.BACKGROUND

        Deadline: goal.deadline is forwarded if set.
        """
        return Job(
            job_type=_job_type_from_string(subtask.job_type),
            payload={
                "goal_id":    goal.goal_id,
                "subtask_id": subtask.subtask_id,
                **subtask.payload,
            },
            priority=_map_priority(goal.priority),
            capabilities=[
                JobCapabilityRequirement(name=cap)
                for cap in subtask.capabilities
            ],
            deadline=goal.deadline,
            tags={
                "goal_id":    goal.goal_id,
                "goal_title": goal.title[:64],
            },
        )

    def dispatch_ready_subtasks(self, goal: Goal, dag: GoalDAG) -> int:
        """Dispatch all ready subtasks to the task queue.

        Returns the number of jobs posted.
        """
        count = 0
        for subtask in dag.ready_subtasks():
            job = self.subtask_to_job(goal, subtask)
            job_id = self._queue.post(job)
            subtask.job_id = job_id
            subtask.status = SubtaskStatus.DISPATCHED
            count += 1
            logger.info(
                "Dispatched subtask %s (job_id=%s) for goal %s",
                subtask.subtask_id, job_id, goal.goal_id,
            )
        return count

    def cancel_all_jobs(self, dag: GoalDAG) -> None:
        """Cancel all dispatched/running jobs in the DAG (best-effort)."""
        for subtask in dag.subtasks.values():
            if subtask.status in {SubtaskStatus.DISPATCHED, SubtaskStatus.RUNNING}:
                subtask.status = SubtaskStatus.FAILED
                subtask.error = "Job cancelled: goal abandoned"
                logger.info("Cancelled job for subtask %s", subtask.subtask_id)

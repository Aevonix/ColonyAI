"""Task Queue event types for the Colony event bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from colony_sidecar.events.types import Event


@dataclass
class JobPostedEvent(Event):
    """Emitted when a new job is added to the queue."""

    job_id: str = ""
    job_type: str = ""
    priority: int = 50
    posted_by: str = ""


@dataclass
class JobClaimedEvent(Event):
    """Emitted when a worker claims a job."""

    job_id: str = ""
    worker_node_id: str = ""


@dataclass
class JobStartedEvent(Event):
    """Emitted when a worker transitions a job from CLAIMED → RUNNING."""

    job_id: str = ""
    worker_node_id: str = ""


@dataclass
class JobCompletedEvent(Event):
    """Emitted when a job finishes successfully."""

    job_id: str = ""
    worker_node_id: str = ""
    duration_seconds: Optional[float] = None


@dataclass
class JobFailedEvent(Event):
    """Emitted when a job transitions to FAILED."""

    job_id: str = ""
    worker_node_id: str = ""
    error: str = ""
    retry_count: int = 0
    will_retry: bool = False


@dataclass
class JobExpiredEvent(Event):
    """Emitted when a job's deadline passes before it completes."""

    job_id: str = ""
    job_type: str = ""
    deadline: Optional[datetime] = None


@dataclass
class JobRedistributedEvent(Event):
    """Emitted when an abandoned job is re-queued after worker failure."""

    job_id: str = ""
    prior_worker_id: str = ""
    retry_count: int = 0


@dataclass
class JobCancelledEvent(Event):
    """Emitted when a job is explicitly cancelled."""

    job_id: str = ""
    reason: str = ""


@dataclass
class JobBlockedEvent(Event):
    """Emitted when a job enters BLOCKED state (waiting on dependencies)."""

    job_id: str = ""
    depends_on: list = field(default_factory=list)


@dataclass
class JobUnblockedEvent(Event):
    """Emitted when all a job's dependencies have completed."""

    job_id: str = ""


@dataclass
class WorkerRegisteredEvent(Event):
    """Emitted when a worker registers with the scheduler."""

    worker_node_id: str = ""
    capabilities: list = field(default_factory=list)


@dataclass
class WorkerDeregisteredEvent(Event):
    """Emitted when a worker deregisters from the scheduler."""

    worker_node_id: str = ""


@dataclass
class NoCapableWorkerEvent(Event):
    """Emitted when a job with an approaching deadline has no eligible worker."""

    job_id: str = ""
    job_type: str = ""
    required_capabilities: list = field(default_factory=list)
    deadline_in_secs: Optional[float] = None

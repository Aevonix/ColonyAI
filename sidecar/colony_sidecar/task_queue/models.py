"""Distributed Task Queue — core data models.

Defines the Job lifecycle, WorkerCapabilities, and supporting types
for the Colony hardware-aware distributed job scheduler.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class JobPriority(int, Enum):
    """Job scheduling priority. Higher value = higher priority."""

    BACKGROUND = 0    # Maintenance, archival, non-urgent analysis
    LOW = 10          # Research, exploration, long-running batch
    NORMAL = 50       # Default priority
    HIGH = 80         # User-initiated tasks, time-sensitive analysis
    CRITICAL = 100    # System health, monitoring, security responses


class JobStatus(str, Enum):
    """Job lifecycle states."""

    QUEUED = "queued"           # Waiting to be claimed
    CLAIMED = "claimed"         # Reserved by a worker, not yet started
    RUNNING = "running"         # Actively executing
    COMPLETED = "completed"     # Finished successfully
    FAILED = "failed"           # Finished with error, may be retried
    ABANDONED = "abandoned"     # Worker died; eligible for redistribution
    CANCELLED = "cancelled"     # Explicitly cancelled
    BLOCKED = "blocked"         # Waiting on dependency jobs


class JobType(str, Enum):
    """Built-in job type categories."""

    INFERENCE = "inference"
    TRAINING = "training"
    DATA_PROCESSING = "data_processing"
    SYSTEM_MAINTENANCE = "system_maintenance"
    RESEARCH = "research"
    MONITORING = "monitoring"
    SYNTHESIS = "synthesis"
    DESKTOP = "desktop"
    BROWSER = "browser"
    CUSTOM = "custom"


@dataclass
class JobCapabilityRequirement:
    """A single capability requirement for job assignment.

    Attributes:
        name:     Capability name (e.g., "gpu", "cuda", "apple_silicon").
        minimum:  Optional minimum numeric threshold (e.g., min VRAM GB).
        preferred: If True, prefer workers with this capability but don't
                   require it.
    """

    name: str
    minimum: Optional[float] = None
    preferred: bool = False


@dataclass
class JobResult:
    """Output produced by a completed job."""

    job_id: str
    worker_node_id: str
    status: JobStatus
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None

    @property
    def succeeded(self) -> bool:
        return self.status == JobStatus.COMPLETED

    def elapsed(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


@dataclass
class Job:
    """A unit of work in the distributed task queue.

    Attributes:
        job_id:         Globally unique identifier.
        job_type:       High-level category for routing and monitoring.
        payload:        Arbitrary job-specific parameters.
        priority:       Scheduling priority (higher = sooner).
        capabilities:   Hardware/software requirements for assignment.
        deadline:       Optional absolute deadline; jobs past deadline are
                        moved to FAILED without retry.
        max_retries:    Maximum number of FAILED → QUEUED re-queues.
        retry_count:    Current retry attempt number.
        timeout_secs:   Maximum wall-clock execution time.
        depends_on:     List of job_ids that must be COMPLETED first.
        posted_by:      node_id of the posting node.
        posted_at:      Timestamp when the job entered the queue.
        status:         Current job lifecycle state.
        claimed_by:     node_id of the worker that has claimed this job.
        claimed_at:     Timestamp of the most recent claim.
        last_heartbeat: Timestamp of the worker's most recent heartbeat.
        result:         Final result (populated on completion or failure).
        tags:           Arbitrary key-value metadata for filtering.
    """

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_type: JobType = JobType.CUSTOM
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: JobPriority = JobPriority.NORMAL
    capabilities: List[JobCapabilityRequirement] = field(default_factory=list)
    deadline: Optional[datetime] = None
    max_retries: int = 3
    retry_count: int = 0
    timeout_secs: float = 3600.0
    depends_on: List[str] = field(default_factory=list)
    posted_by: str = ""
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: JobStatus = JobStatus.QUEUED
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    result: Optional[JobResult] = None
    tags: Dict[str, str] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }

    def is_expired(self) -> bool:
        if self.deadline is None:
            return False
        return datetime.now(timezone.utc) > self.deadline

    def can_retry(self) -> bool:
        return (
            self.status in {JobStatus.FAILED, JobStatus.ABANDONED}
            and self.retry_count < self.max_retries
            and not self.is_expired()
        )

    def required_capabilities(self) -> List[str]:
        return [r.name for r in self.capabilities if not r.preferred]

    def preferred_capabilities(self) -> List[str]:
        return [r.name for r in self.capabilities if r.preferred]


@dataclass
class WorkerCapabilities:
    """Hardware and software capabilities advertised by a worker node.

    Attributes:
        node_id:        Mesh node identifier.
        capabilities:   Set of capability names this node provides.
        capacity:       Numeric capacity map (e.g., {"gpu_vram_gb": 80.0,
                        "ram_gb": 192.0, "cpu_cores": 64}).
        max_concurrent: Maximum number of jobs this worker runs simultaneously.
        job_types:      Job types this node is willing to accept. Empty set
                        means all types are accepted.
        available:      Whether the node is currently accepting new jobs.
        load:           Current load factor 0.0–1.0.
        registered_at:  When this worker last registered with the scheduler.
        last_seen:      Timestamp of most recent heartbeat.
    """

    node_id: str
    capabilities: set = field(default_factory=set)
    capacity: Dict[str, float] = field(default_factory=dict)
    max_concurrent: int = 4
    job_types: set = field(default_factory=set)
    available: bool = True
    load: float = 0.0
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def can_accept(self, job: Job) -> bool:
        """Return True if this worker satisfies all required capabilities."""
        required = set(job.required_capabilities())
        if not required.issubset(self.capabilities):
            return False
        if self.job_types and job.job_type not in self.job_types:
            return False
        if not self.available:
            return False
        if self.load >= 1.0:
            return False
        for req in job.capabilities:
            if req.minimum is not None and not req.preferred:
                actual = self.capacity.get(req.name, 0.0)
                if actual < req.minimum:
                    return False
        return True

    def affinity_score(self, job: Job) -> float:
        """Compute how well this worker matches a job (higher is better).

        Score components:
        - Required capability match: binary (0 if not met)
        - Preferred capabilities present: +0.1 per match
        - Available headroom: (1 - load) × 0.5
        - Capacity surplus for numeric requirements: up to 0.3
        """
        if not self.can_accept(job):
            return 0.0
        score = 1.0
        for name in job.preferred_capabilities():
            if name in self.capabilities:
                score += 0.1
        score += (1.0 - self.load) * 0.5
        for req in job.capabilities:
            if req.minimum is not None and req.name in self.capacity:
                surplus = self.capacity[req.name] - (req.minimum or 0.0)
                score += min(surplus / max(req.minimum or 1.0, 1.0), 1.0) * 0.15
        return score


@dataclass
class HeartbeatPayload:
    """Progress update sent by a worker alongside a heartbeat."""

    node_id: str
    job_ids: List[str]
    timestamp: datetime
    progress: Dict[str, float] = field(default_factory=dict)   # job_id → 0.0–1.0
    log_lines: Dict[str, List[str]] = field(default_factory=dict)  # job_id → logs


@dataclass
class QueueStats:
    """Counts per status and per job type."""

    by_status: Dict[str, int] = field(default_factory=dict)
    by_type: Dict[str, int] = field(default_factory=dict)
    total_workers: int = 0
    available_workers: int = 0


@dataclass
class AuditEntry:
    """An immutable audit record for a job state transition."""

    id: int
    job_id: str
    timestamp: datetime
    from_status: Optional[str]
    to_status: str
    node_id: Optional[str]
    reason: Optional[str]
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FederatedJob:
    """A job posted by a federated peer colony."""

    job: Job
    source_colony_id: str
    source_colony_trust_level: int
    signature: str           # Ed25519 signature from source colony
    payment_intent: Optional[str] = None


class CircularDependencyError(ValueError):
    """Raised when a job dependency graph contains a cycle."""


def deadline_urgency(job: Job, now: datetime) -> float:
    """Returns 0.0 if no deadline, otherwise urgency in [0, 100]."""
    if job.deadline is None:
        return 0.0
    remaining = (job.deadline - now).total_seconds()
    if remaining <= 0:
        return 100.0
    return max(0.0, min(100.0, (300.0 / max(remaining, 1.0)) * 100.0))

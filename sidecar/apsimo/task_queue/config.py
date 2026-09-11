"""Configuration for the Colony distributed task queue."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path


DEFAULT_WORKER_HEARTBEAT_TTL_SECS = 60.0
MIN_WORKER_HEARTBEAT_TTL_SECS = 1.0
MAX_WORKER_HEARTBEAT_TTL_SECS = 3600.0


def validate_worker_heartbeat_ttl(value: object) -> float:
    """Return the one bounded worker-liveness TTL used by queue consumers."""

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "worker_heartbeat_ttl_secs must be between "
            f"{MIN_WORKER_HEARTBEAT_TTL_SECS:g} and "
            f"{MAX_WORKER_HEARTBEAT_TTL_SECS:g}"
        ) from exc
    if (
        not math.isfinite(parsed)
        or parsed < MIN_WORKER_HEARTBEAT_TTL_SECS
        or parsed > MAX_WORKER_HEARTBEAT_TTL_SECS
    ):
        raise ValueError(
            "worker_heartbeat_ttl_secs must be between "
            f"{MIN_WORKER_HEARTBEAT_TTL_SECS:g} and "
            f"{MAX_WORKER_HEARTBEAT_TTL_SECS:g}"
        )
    return parsed


@dataclass
class TaskQueueConfig:
    """Configuration for the distributed task queue.

    All timeouts are in seconds unless otherwise stated.
    """

    # Storage
    db_path: Path = field(default_factory=lambda: Path("~/.colony/task_queue.db"))

    # Scheduling
    scheduler_tick_secs: float = 2.0
    """How often the scheduler runs its assignment loop."""

    # Worker health
    heartbeat_interval_secs: float = 15.0
    """How often workers send heartbeats."""

    heartbeat_timeout_secs: float = DEFAULT_WORKER_HEARTBEAT_TTL_SECS
    """Time after which a silent worker's jobs are abandoned."""

    claim_timeout_secs: float = 30.0
    """Time after which a CLAIMED but not yet RUNNING job is re-queued."""

    # Deadlines
    no_worker_warning_secs: float = 300.0
    """Emit warning when a job with a deadline within this window has no eligible worker."""

    # Retries
    default_max_retries: int = 3
    """Default retry limit for jobs that do not specify one."""

    # History retention
    completed_job_retention_days: int = 30
    failed_job_retention_days: int = 7

    # Capacity
    default_worker_max_concurrent: int = 4
    """Default maximum concurrent jobs per worker if not specified."""

    # Regent sync
    regent_sync_interval_secs: float = 5.0
    """How often the Regent syncs queue state from the Sovereign."""

    def resolved_db_path(self) -> Path:
        """Return the db_path with ~ expanded."""
        return Path(self.db_path).expanduser()

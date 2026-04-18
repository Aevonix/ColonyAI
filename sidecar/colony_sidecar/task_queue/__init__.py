"""Colony Distributed Task Queue.

Hardware-aware distributed job scheduling across the Colony mesh.
The Queen (Sovereign) schedules; Workers (Vassals) execute.

Public API::

    from colony_sidecar.task_queue import (
        Job, JobType, JobStatus, JobPriority,
        JobCapabilityRequirement, JobResult,
        WorkerCapabilities, QueueManager, WorkerNode,
        JobHandler, Scheduler, TaskQueueConfig,
    )
"""

from colony_sidecar.task_queue.config import TaskQueueConfig
from colony_sidecar.task_queue.mesh_integration import QueueMeshEventHandler
from colony_sidecar.task_queue.models import (
    AuditEntry,
    CircularDependencyError,
    FederatedJob,
    HeartbeatPayload,
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobResult,
    JobStatus,
    JobType,
    QueueStats,
    WorkerCapabilities,
    deadline_urgency,
)
from colony_sidecar.task_queue.queue_manager import QueueManager
from colony_sidecar.task_queue.scheduler import Scheduler
from colony_sidecar.task_queue.worker import JobHandler, WorkerNode, detect_local_capabilities

__all__ = [
    # Models
    "Job",
    "JobType",
    "JobStatus",
    "JobPriority",
    "JobCapabilityRequirement",
    "JobResult",
    "WorkerCapabilities",
    "HeartbeatPayload",
    "QueueStats",
    "AuditEntry",
    "FederatedJob",
    "CircularDependencyError",
    "deadline_urgency",
    # Core components
    "QueueManager",
    "WorkerNode",
    "JobHandler",
    "Scheduler",
    # Config
    "TaskQueueConfig",
    # Mesh integration
    "QueueMeshEventHandler",
    # Utilities
    "detect_local_capabilities",
]

"""PacoMind Distributed Task Queue.

Hardware-aware distributed job scheduling across the PacoMind mesh.
The Queen (Sovereign) schedules; Workers (Vassals) execute.

Public API::

    from pacomind.task_queue import (
        Job, JobType, JobStatus, JobPriority,
        JobCapabilityRequirement, JobResult,
        WorkerCapabilities, QueueManager, WorkerNode,
        JobHandler, Scheduler, TaskQueueConfig,
    )
"""

from pacomind.task_queue.config import TaskQueueConfig
from pacomind.task_queue.mesh_integration import QueueMeshEventHandler
from pacomind.task_queue.models import (
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
from pacomind.task_queue.queue_manager import QueueManager
from pacomind.task_queue.scheduler import Scheduler
from pacomind.task_queue.worker import JobHandler, WorkerNode, detect_local_capabilities
from pacomind.task_queue.work_control import (
    WorkControlError,
    WorkControlService,
)

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
    "WorkControlService",
    "WorkControlError",
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

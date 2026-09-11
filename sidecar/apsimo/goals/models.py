"""Colony Goal Engine — core data models.

Defines Goal, Subtask, GoalDAG and supporting types for the
Colony DAG-based goal decomposition and lifecycle management system.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalStatus(str, Enum):
    """Goal lifecycle states."""
    PROPOSED   = "proposed"    # Inferred or suggested; awaiting user acceptance
    ACCEPTED   = "accepted"    # User confirmed; ready for decomposition
    ACTIVE     = "active"      # Decomposed and executing
    BLOCKED    = "blocked"     # Cannot proceed; waiting on external factor
    COMPLETED  = "completed"   # All subtasks done; outcome achieved
    ABANDONED  = "abandoned"   # Given up; reason recorded


class GoalSource(str, Enum):
    """How the goal was created."""
    EXPLICIT   = "explicit"    # User stated the goal directly
    INFERRED   = "inferred"    # Detected from conversation context
    RECURRING  = "recurring"   # Generated from a recurring schedule
    DELEGATED  = "delegated"   # Sent by another Colony agent via federation


class GoalPriority(int, Enum):
    """Goal scheduling priority. Higher = more urgent."""
    BACKGROUND = 0
    LOW        = 10
    NORMAL     = 50
    HIGH       = 80
    CRITICAL   = 100


class SubtaskStatus(str, Enum):
    """Subtask execution states within a goal's DAG."""
    PENDING    = "pending"     # Not yet dispatched
    DISPATCHED = "dispatched"  # Job posted to task queue
    RUNNING    = "running"     # Job claimed by a worker
    COMPLETED  = "completed"   # Job finished successfully
    FAILED     = "failed"      # Job failed; may trigger replan
    SKIPPED    = "skipped"     # Bypassed via replan strategy
    BLOCKED    = "blocked"     # Waiting on dependency subtasks


@dataclass
class GoalOutcome:
    """Describes the desired end state of a completed goal."""
    description: str
    success_criteria: List[str] = field(default_factory=list)
    measurable: bool = False
    target_value: Optional[float] = None
    target_unit: Optional[str] = None


@dataclass
class Goal:
    """A user-level objective tracked by the Goal Engine.

    Attributes:
        goal_id:         Globally unique identifier.
        title:           Short human-readable label (≤ 80 chars).
        description:     Full description of the objective.
        source:          How this goal was created.
        status:          Current lifecycle state.
        priority:        Scheduling priority score.
        outcome:         Desired end state description.
        deadline:        Optional absolute deadline.
        parent_goal_id:  If this is a sub-goal, the parent's ID.
        tags:            Arbitrary metadata for filtering and grouping.
        context:         Conversation or event context that generated this goal.
        created_at:      When the goal was created.
        updated_at:      When the goal was last modified.
        accepted_at:     When the user accepted the goal.
        completed_at:    When the goal reached COMPLETED.
        abandoned_at:    When the goal was abandoned.
        abandon_reason:  Why the goal was abandoned.
        replan_count:    Number of times the DAG has been replanned.
        estimated_hours: Current completion time estimate in hours.
        progress_pct:    Fraction of subtasks completed (0.0–1.0).
    """
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    source: GoalSource = GoalSource.EXPLICIT
    status: GoalStatus = GoalStatus.PROPOSED
    priority: GoalPriority = GoalPriority.NORMAL
    outcome: Optional[GoalOutcome] = None
    deadline: Optional[datetime] = None
    parent_goal_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    accepted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    abandoned_at: Optional[datetime] = None
    abandon_reason: Optional[str] = None
    replan_count: int = 0
    estimated_hours: Optional[float] = None
    progress_pct: float = 0.0

    # Initiative management fields (v0.7.10)
    last_initiative_at: Optional[datetime] = None
    snoozed_until: Optional[datetime] = None
    snooze_count: int = 0
    dismissal_reason: Optional[str] = None

    def is_terminal(self) -> bool:
        # Compare by string value so the check is robust if GoalStatus is patched
        # in a test environment (e.g. pytest-xdist workers that share sys.modules).
        status_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        return status_val in ("completed", "abandoned")

    def is_overdue(self) -> bool:
        if self.deadline is None:
            return False
        return datetime.now(timezone.utc) > self.deadline

    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds() / 3600.0


@dataclass
class Subtask:
    """A concrete unit of work within a goal's DAG.

    Attributes:
        subtask_id:       Unique identifier within the goal.
        goal_id:          Parent goal.
        title:            Human-readable description.
        job_type:         Task queue job type for dispatch.
        payload:          Job-specific parameters.
        capabilities:     Required worker capabilities.
        depends_on:       List of subtask_ids that must complete first.
        status:           Current execution state.
        job_id:           Task queue job_id if dispatched.
        result:           Job result if completed.
        depth:            DAG depth level (root = 0).
        is_critical_path: True if this subtask is on the critical path.
        retry_count:      How many times this subtask has been retried.
        max_retries:      Maximum retries before escalating to replan.
        estimated_hours:  Expected execution duration.
        started_at:       When the job started executing.
        completed_at:     When the job completed.
        error:            Error message if failed.
    """
    subtask_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal_id: str = ""
    title: str = ""
    job_type: str = "custom"
    payload: Dict[str, Any] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    status: SubtaskStatus = SubtaskStatus.PENDING
    job_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    depth: int = 0
    is_critical_path: bool = False
    retry_count: int = 0
    max_retries: int = 2
    estimated_hours: Optional[float] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def is_ready(self, completed_ids: set) -> bool:
        """Return True when all dependencies have completed."""
        return all(dep in completed_ids for dep in self.depends_on)

    def duration_hours(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() / 3600.0
        return None


@dataclass
class GoalDAG:
    """The directed acyclic graph of subtasks for a single goal.

    Attributes:
        goal_id:        Parent goal identifier.
        subtasks:       All subtask nodes keyed by subtask_id.
        root_ids:       Subtask IDs with no dependencies (entry points).
        leaf_ids:       Subtask IDs with no dependents (exit points).
        critical_path:  Ordered list of subtask_ids on the critical path.
        max_depth:      Deepest level in the DAG.
        created_at:     When this DAG version was created.
        version:        Incremented on each replan.
    """
    goal_id: str
    subtasks: Dict[str, Subtask] = field(default_factory=dict)
    root_ids: List[str] = field(default_factory=list)
    leaf_ids: List[str] = field(default_factory=list)
    critical_path: List[str] = field(default_factory=list)
    max_depth: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1

    def add_subtask(self, subtask: Subtask) -> None:
        self.subtasks[subtask.subtask_id] = subtask

    def ready_subtasks(self) -> List[Subtask]:
        """Return all PENDING subtasks whose dependencies are met."""
        completed = {
            sid for sid, s in self.subtasks.items()
            if s.status == SubtaskStatus.COMPLETED
        }
        return [
            s for s in self.subtasks.values()
            if s.status == SubtaskStatus.PENDING and s.is_ready(completed)
        ]

    def is_complete(self) -> bool:
        return all(
            s.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED}
            for s in self.subtasks.values()
        )

    def completion_fraction(self) -> float:
        if not self.subtasks:
            return 0.0
        done = sum(
            1 for s in self.subtasks.values()
            if s.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED}
        )
        return done / len(self.subtasks)

    def validate(self) -> List[str]:
        """Validate DAG structure. Returns list of error messages."""
        errors: List[str] = []
        ids = set(self.subtasks.keys())

        # All dependencies must reference known subtasks
        for s in self.subtasks.values():
            for dep in s.depends_on:
                if dep not in ids:
                    errors.append(f"Subtask {s.subtask_id} has unknown dep {dep}")
            # No self-dependency
            if s.subtask_id in s.depends_on:
                errors.append(f"Subtask {s.subtask_id} depends on itself")

        # No cycles (DFS-based check)
        visited: set = set()
        rec_stack: set = set()

        def has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            node = self.subtasks.get(node_id)
            deps = node.depends_on if node else []
            for dep in deps:
                if dep not in visited:
                    if has_cycle(dep):
                        return True
                elif dep in rec_stack:
                    return True
            rec_stack.discard(node_id)
            return False

        for sid in ids:
            if sid not in visited:
                if has_cycle(sid):
                    errors.append(f"Cycle detected involving subtask {sid}")
                    break

        return errors


@dataclass
class GoalTransitionRecord:
    """An audit record for a goal state transition."""
    goal_id: str
    from_status: str
    to_status: str
    trigger: str
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GoalSummary:
    """Human-readable summary for the briefing system."""
    goal_id: str
    title: str
    status: str
    priority: int
    progress_pct: float
    estimated_hours: Optional[float]
    deadline: Optional[datetime]
    subtask_count: int
    completed_subtasks: int
    is_overdue: bool
    replan_count: int

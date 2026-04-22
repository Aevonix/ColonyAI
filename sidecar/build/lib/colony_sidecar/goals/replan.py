"""ReplanEngine — adaptive replanning with failure strategies.

Classifies failures and selects the appropriate recovery strategy:
  TRANSIENT     → RETRY (up to max_retries)
  CAPABILITY    → SUBSTITUTE or ESCALATE
  DEPENDENCY    → PARTIAL_REPLAN
  SCOPE_CHANGE  → FULL_REPLAN (user confirmation if replan_count >= 2)
  EXTERNAL      → RETRY with backoff, then SUBSTITUTE
  UNRECOVERABLE → ESCALATE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from colony_sidecar.goals.models import (
    Goal,
    GoalDAG,
    GoalPriority,
    GoalStatus,
    Subtask,
    SubtaskStatus,
)

logger = logging.getLogger(__name__)

MAX_REPLANS_DEFAULT = 5


class FailureClass(str, Enum):
    """Classification of subtask failure for replan strategy selection."""
    TRANSIENT     = "transient"      # Temporary failure; retry expected to work
    CAPABILITY    = "capability"     # Required capability not available
    DEPENDENCY    = "dependency"     # Upstream subtask produced bad output
    SCOPE_CHANGE  = "scope_change"   # Goal requirements changed mid-execution
    EXTERNAL      = "external"       # External service, API, or resource failure
    UNRECOVERABLE = "unrecoverable"  # No recovery path exists


class ReplanStrategy(str, Enum):
    """Available strategies for recovering from subtask failures."""
    RETRY          = "retry"          # Re-queue the same subtask
    SUBSTITUTE     = "substitute"     # Replace with an alternative subtask
    SKIP           = "skip"           # Mark subtask as SKIPPED; continue
    PARTIAL_REPLAN = "partial_replan" # Rebuild the failed subtask's subtree
    FULL_REPLAN    = "full_replan"    # Rebuild the entire DAG
    ESCALATE       = "escalate"       # Surface to user for manual intervention


@dataclass
class FailureAnalysis:
    """Result of classifying a subtask failure."""
    subtask_id: str
    failure_class: FailureClass
    error_message: str
    retry_viable: bool
    affected_subtasks: List[str]    # Other subtasks that may be invalidated
    suggested_strategy: str


@dataclass
class ReplanResult:
    """Describes the proposed changes from a replan operation."""
    goal_id: str
    strategy: ReplanStrategy
    requires_user_approval: bool
    new_dag: Optional[GoalDAG]          # Set for FULL_REPLAN / PARTIAL_REPLAN
    retried_subtask_ids: List[str] = field(default_factory=list)
    skipped_subtask_ids: List[str] = field(default_factory=list)
    substituted_subtask_ids: List[str] = field(default_factory=list)
    escalation_message: Optional[str] = None
    dag_diff: Optional[str] = None      # Human-readable diff description
    estimated_completion_delta_hours: float = 0.0


# ── Failure classification heuristics ─────────────────────────────────────────

_TRANSIENT_PATTERNS = [
    "timeout", "timed out", "connection reset", "temporary",
    "retry", "rate limit", "too many requests", "503", "504",
]
_CAPABILITY_PATTERNS = [
    "capability", "no worker", "no node", "unsatisfied requirement",
    "capability not available", "hardware not available",
]
_EXTERNAL_PATTERNS = [
    "api error", "http error", "network error", "connection refused",
    "dns", "certificate", "ssl", "404", "401", "403",
]
_UNRECOVERABLE_PATTERNS = [
    "permission denied", "out of disk", "oom", "memory error",
    "corrupt", "unrecoverable", "fatal",
]


def _classify_error(error_message: str) -> FailureClass:
    """Heuristically classify an error message."""
    msg = (error_message or "").lower()
    if any(p in msg for p in _UNRECOVERABLE_PATTERNS):
        return FailureClass.UNRECOVERABLE
    if any(p in msg for p in _CAPABILITY_PATTERNS):
        return FailureClass.CAPABILITY
    # Check EXTERNAL before TRANSIENT: explicit API/network failures are external
    if any(p in msg for p in _EXTERNAL_PATTERNS):
        return FailureClass.EXTERNAL
    if any(p in msg for p in _TRANSIENT_PATTERNS):
        return FailureClass.TRANSIENT
    # Default to TRANSIENT for unknown errors (optimistic)
    return FailureClass.TRANSIENT


def _downstream_subtasks(dag: GoalDAG, failed_id: str) -> List[str]:
    """Return all subtask IDs that transitively depend on failed_id."""
    affected: List[str] = []
    visited: set = set()

    def dfs(node_id: str) -> None:
        for sid, s in dag.subtasks.items():
            if node_id in s.depends_on and sid not in visited:
                visited.add(sid)
                affected.append(sid)
                dfs(sid)

    dfs(failed_id)
    return affected


class ReplanEngine:
    """Generate recovery plans for goal execution failures."""

    def __init__(self, max_replans: int = MAX_REPLANS_DEFAULT) -> None:
        self.max_replans = max_replans

    def analyze_failure(
        self,
        goal: Goal,
        dag: GoalDAG,
        failed_subtask: Subtask,
    ) -> FailureAnalysis:
        """Classify the failure and identify affected downstream subtasks."""
        error = failed_subtask.error or ""
        failure_class = _classify_error(error)

        # Override: if max retries exhausted, push toward non-retry strategy
        retry_viable = (
            failure_class in {FailureClass.TRANSIENT, FailureClass.EXTERNAL}
            and failed_subtask.retry_count < failed_subtask.max_retries
        )

        affected = _downstream_subtasks(dag, failed_subtask.subtask_id)

        # Select suggested strategy
        if failure_class == FailureClass.UNRECOVERABLE:
            strategy = ReplanStrategy.ESCALATE.value
        elif failure_class == FailureClass.CAPABILITY:
            strategy = ReplanStrategy.SUBSTITUTE.value
        elif failure_class == FailureClass.DEPENDENCY:
            strategy = ReplanStrategy.PARTIAL_REPLAN.value
        elif failure_class == FailureClass.SCOPE_CHANGE:
            strategy = ReplanStrategy.FULL_REPLAN.value
        elif retry_viable:
            strategy = ReplanStrategy.RETRY.value
        else:
            strategy = ReplanStrategy.SUBSTITUTE.value

        return FailureAnalysis(
            subtask_id=failed_subtask.subtask_id,
            failure_class=failure_class,
            error_message=error,
            retry_viable=retry_viable,
            affected_subtasks=affected,
            suggested_strategy=strategy,
        )

    def generate_replan(
        self,
        goal: Goal,
        dag: GoalDAG,
        analysis: FailureAnalysis,
    ) -> ReplanResult:
        """Generate a recovery plan for the given failure.

        Selection logic:
        - TRANSIENT failures → RETRY (up to max_retries)
        - CAPABILITY failures → SUBSTITUTE or ESCALATE
        - DEPENDENCY failures → PARTIAL_REPLAN
        - SCOPE_CHANGE → FULL_REPLAN (with user confirmation if replan_count >= 2)
        - EXTERNAL failures → RETRY with backoff, then SUBSTITUTE
        - UNRECOVERABLE → ESCALATE

        Returns a ReplanResult describing the proposed changes.
        """
        # Circuit breaker
        if goal.replan_count >= self.max_replans:
            return ReplanResult(
                goal_id=goal.goal_id,
                strategy=ReplanStrategy.ESCALATE,
                requires_user_approval=True,
                new_dag=None,
                escalation_message=(
                    f"Goal has been replanned {goal.replan_count} times "
                    f"(max {self.max_replans}). Manual intervention required."
                ),
            )

        strategy = ReplanStrategy(analysis.suggested_strategy)
        requires_approval = False

        if strategy == ReplanStrategy.RETRY:
            return self._plan_retry(goal, dag, analysis)

        if strategy == ReplanStrategy.SUBSTITUTE:
            return self._plan_substitute(goal, dag, analysis)

        if strategy == ReplanStrategy.SKIP:
            return self._plan_skip(goal, dag, analysis)

        if strategy == ReplanStrategy.PARTIAL_REPLAN:
            new_dag = self._partial_replan(goal, dag, analysis)
            requires_approval = False
            return ReplanResult(
                goal_id=goal.goal_id,
                strategy=strategy,
                requires_user_approval=requires_approval,
                new_dag=new_dag,
                dag_diff=f"Rebuilt subtree rooted at {analysis.subtask_id}",
            )

        if strategy == ReplanStrategy.FULL_REPLAN:
            # Require user approval after 2 replans
            requires_approval = goal.replan_count >= 2
            new_dag = self._full_replan(goal, dag)
            return ReplanResult(
                goal_id=goal.goal_id,
                strategy=strategy,
                requires_user_approval=requires_approval,
                new_dag=new_dag,
                dag_diff="Full DAG rebuild",
            )

        # ESCALATE
        return ReplanResult(
            goal_id=goal.goal_id,
            strategy=ReplanStrategy.ESCALATE,
            requires_user_approval=True,
            new_dag=None,
            escalation_message=analysis.error_message or "Subtask failure requires manual intervention.",
        )

    def apply_replan(
        self,
        goal: Goal,
        dag: GoalDAG,
        replan: ReplanResult,
    ) -> GoalDAG:
        """Apply the approved replan to produce a new (or updated) DAG version."""
        if replan.strategy == ReplanStrategy.RETRY:
            for sid in replan.retried_subtask_ids:
                subtask = dag.subtasks.get(sid)
                if subtask:
                    subtask.status = SubtaskStatus.PENDING
                    subtask.retry_count += 1
                    subtask.error = None
            goal.replan_count += 1
            return dag

        if replan.strategy == ReplanStrategy.SKIP:
            for sid in replan.skipped_subtask_ids:
                subtask = dag.subtasks.get(sid)
                if subtask:
                    subtask.status = SubtaskStatus.SKIPPED
            goal.replan_count += 1
            return dag

        if replan.new_dag is not None:
            replan.new_dag.version = dag.version + 1
            goal.replan_count += 1
            return replan.new_dag

        goal.replan_count += 1
        return dag

    # ── Private helpers ────────────────────────────────────────────────────────

    def _plan_retry(
        self, goal: Goal, dag: GoalDAG, analysis: FailureAnalysis
    ) -> ReplanResult:
        subtask = dag.subtasks.get(analysis.subtask_id)
        if subtask is None or subtask.retry_count >= subtask.max_retries:
            # Escalate if retries exhausted
            return ReplanResult(
                goal_id=goal.goal_id,
                strategy=ReplanStrategy.ESCALATE,
                requires_user_approval=True,
                new_dag=None,
                escalation_message=f"Subtask {analysis.subtask_id} exhausted retries.",
            )
        return ReplanResult(
            goal_id=goal.goal_id,
            strategy=ReplanStrategy.RETRY,
            requires_user_approval=False,
            new_dag=None,
            retried_subtask_ids=[analysis.subtask_id],
        )

    def _plan_substitute(
        self, goal: Goal, dag: GoalDAG, analysis: FailureAnalysis
    ) -> ReplanResult:
        """Replace the failed subtask with a capability-agnostic fallback."""
        subtask = dag.subtasks.get(analysis.subtask_id)
        if subtask is None:
            return ReplanResult(
                goal_id=goal.goal_id,
                strategy=ReplanStrategy.ESCALATE,
                requires_user_approval=True,
                new_dag=None,
                escalation_message=f"Subtask {analysis.subtask_id} not found for substitution.",
            )
        # Reset capabilities and mark as pending
        new_dag = GoalDAG(
            goal_id=dag.goal_id,
            subtasks=dict(dag.subtasks),
            root_ids=list(dag.root_ids),
            leaf_ids=list(dag.leaf_ids),
            critical_path=list(dag.critical_path),
            max_depth=dag.max_depth,
            version=dag.version,
            created_at=dag.created_at,
        )
        replacement = Subtask(
            subtask_id=subtask.subtask_id,
            goal_id=subtask.goal_id,
            title=subtask.title + " [fallback]",
            job_type="custom",   # Capability-agnostic fallback
            capabilities=[],
            depends_on=list(subtask.depends_on),
            estimated_hours=subtask.estimated_hours,
            payload=subtask.payload,
            max_retries=subtask.max_retries,
        )
        new_dag.subtasks[subtask.subtask_id] = replacement
        return ReplanResult(
            goal_id=goal.goal_id,
            strategy=ReplanStrategy.SUBSTITUTE,
            requires_user_approval=False,
            new_dag=new_dag,
            substituted_subtask_ids=[subtask.subtask_id],
            dag_diff=f"Substituted {subtask.subtask_id} with capability-agnostic fallback",
        )

    def _plan_skip(
        self, goal: Goal, dag: GoalDAG, analysis: FailureAnalysis
    ) -> ReplanResult:
        return ReplanResult(
            goal_id=goal.goal_id,
            strategy=ReplanStrategy.SKIP,
            requires_user_approval=False,
            new_dag=None,
            skipped_subtask_ids=[analysis.subtask_id],
        )

    def _partial_replan(self, goal: Goal, dag: GoalDAG, analysis: FailureAnalysis) -> GoalDAG:
        """Rebuild the failed subtask and its entire downstream subtree."""
        import uuid as _uuid

        affected = set(analysis.affected_subtasks + [analysis.subtask_id])
        new_dag = GoalDAG(
            goal_id=dag.goal_id,
            subtasks={
                sid: s for sid, s in dag.subtasks.items()
                if sid not in affected
            },
            root_ids=list(dag.root_ids),
            leaf_ids=list(dag.leaf_ids),
            critical_path=list(dag.critical_path),
            max_depth=dag.max_depth,
            version=dag.version,
            created_at=dag.created_at,
        )

        # Re-add the failed subtask as pending with incremented retry info
        original = dag.subtasks.get(analysis.subtask_id)
        if original:
            new_subtask = Subtask(
                subtask_id=original.subtask_id,
                goal_id=original.goal_id,
                title=original.title,
                job_type=original.job_type,
                capabilities=list(original.capabilities),
                depends_on=list(original.depends_on),
                estimated_hours=original.estimated_hours,
                payload=dict(original.payload),
                max_retries=original.max_retries,
                retry_count=original.retry_count,
            )
            new_dag.subtasks[new_subtask.subtask_id] = new_subtask

        # Re-add downstream subtasks as fresh pending
        for sid in analysis.affected_subtasks:
            original_s = dag.subtasks.get(sid)
            if original_s:
                fresh = Subtask(
                    subtask_id=original_s.subtask_id,
                    goal_id=original_s.goal_id,
                    title=original_s.title,
                    job_type=original_s.job_type,
                    capabilities=list(original_s.capabilities),
                    depends_on=list(original_s.depends_on),
                    estimated_hours=original_s.estimated_hours,
                    payload=dict(original_s.payload),
                    max_retries=original_s.max_retries,
                )
                new_dag.subtasks[fresh.subtask_id] = fresh

        return new_dag

    def _full_replan(self, goal: Goal, dag: GoalDAG) -> GoalDAG:
        """Create a new GoalDAG, preserving completed subtasks."""
        completed_ids = {
            sid for sid, s in dag.subtasks.items()
            if s.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED}
        }
        new_dag = GoalDAG(
            goal_id=dag.goal_id,
            subtasks={
                sid: s for sid, s in dag.subtasks.items()
                if sid in completed_ids
            },
            version=dag.version,
            created_at=dag.created_at,
        )
        # Reset all non-completed subtasks to pending
        for sid, s in dag.subtasks.items():
            if sid not in completed_ids:
                new_s = Subtask(
                    subtask_id=s.subtask_id,
                    goal_id=s.goal_id,
                    title=s.title,
                    job_type=s.job_type,
                    capabilities=list(s.capabilities),
                    depends_on=list(s.depends_on),
                    estimated_hours=s.estimated_hours,
                    payload=dict(s.payload),
                    max_retries=s.max_retries,
                )
                new_dag.subtasks[new_s.subtask_id] = new_s
        return new_dag

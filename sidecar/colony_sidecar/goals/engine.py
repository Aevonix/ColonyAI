"""GoalEngine — top-level coordinator for the Colony Goal Engine.

Wires together inference, decomposition, lifecycle management,
queue integration, and intelligence layer reporting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.goals.config import GoalEngineConfig
from colony_sidecar.goals.decomposer import GoalDecomposer
from colony_sidecar.goals.inference import (
    ConversationMessage,
    GoalDeduplicator,
    GoalInferencePipeline,
    InferenceCandidate,
)
from colony_sidecar.goals.models import (
    Goal,
    GoalDAG,
    GoalPriority,
    GoalSource,
    GoalStatus,
    GoalSummary,
    GoalTransitionRecord,
)
from colony_sidecar.goals.priority import GoalProgressTracker, GoalPriorityScorer, UserPreferenceProfile
from colony_sidecar.goals.queue_bridge import GoalQueueBridge
from colony_sidecar.goals.replan import (
    FailureAnalysis,
    ReplanEngine,
    ReplanResult,
    ReplanStrategy,
)
from colony_sidecar.goals.store import GoalNotFoundError, GoalStore
from colony_sidecar.task_queue.models import JobResult

logger = logging.getLogger(__name__)


class GoalEngine:
    """Top-level coordinator for the Colony Goal Engine.

    Wires together inference, decomposition, lifecycle management,
    queue integration, and intelligence layer reporting.
    """

    def __init__(
        self,
        store: Optional[GoalStore] = None,
        decomposer: Optional[GoalDecomposer] = None,
        queue_bridge: Optional[GoalQueueBridge] = None,
        meta_learner: Optional[Any] = None,
        config: Optional[GoalEngineConfig] = None,
        quorum_manager: Optional[Any] = None,
    ) -> None:
        self.config = config or GoalEngineConfig()
        self._store = store or GoalStore(db_path=self.config.db_path)
        self._decomposer = decomposer or GoalDecomposer()
        self._queue_bridge = queue_bridge or GoalQueueBridge()
        self._meta_learner = meta_learner
        self._quorum = quorum_manager  # Optional[QuorumManager]
        self._inference = GoalInferencePipeline()
        self._deduplicator = GoalDeduplicator()
        self._scorer = GoalPriorityScorer()
        self._tracker = GoalProgressTracker()
        self._replan_engine = ReplanEngine(max_replans=self.config.max_replans)
        self._preferences = UserPreferenceProfile()

    # ── Quorum guard ───────────────────────────────────────────────────────────

    def _require_quorum(self, operation: str) -> None:
        """Raise QuorumError if the mesh quorum is unavailable.

        No-ops when no quorum_manager was provided (e.g. single-node or tests).
        """
        if self._quorum is not None:
            self._quorum.require_quorum(operation)

    # ── Goal Lifecycle ─────────────────────────────────────────────────────────

    def propose_goal(
        self,
        title: str,
        description: str = "",
        source: GoalSource = GoalSource.EXPLICIT,
        priority: GoalPriority = GoalPriority.NORMAL,
        deadline: Optional[datetime] = None,
        context: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        parent_goal_id: Optional[str] = None,
    ) -> Goal:
        """Create a new goal in PROPOSED state."""
        self._require_quorum("goal_propose")
        goal = Goal(
            title=title[:80],
            description=description,
            source=source,
            status=GoalStatus.PROPOSED,
            priority=priority,
            deadline=deadline,
            context=context or {},
            tags=tags or {},
            parent_goal_id=parent_goal_id,
        )
        self._store.save_goal(goal)
        logger.info("Proposed goal %s: %r", goal.goal_id, goal.title)
        return goal

    def accept_goal(self, goal_id: str) -> Goal:
        """Transition a PROPOSED goal to ACCEPTED and queue decomposition."""
        self._require_quorum("goal_accept")
        goal = self._store.get_goal(goal_id)
        if goal.status != GoalStatus.PROPOSED:
            raise ValueError(
                f"Cannot accept goal {goal_id} in state {goal.status.value}"
            )
        old_status = goal.status
        goal.status = GoalStatus.ACCEPTED
        goal.accepted_at = datetime.now(timezone.utc)
        self._store.save_goal(goal)
        self._store.log_transition(goal_id, old_status, GoalStatus.ACCEPTED, "user_accepted")
        logger.info("Accepted goal %s", goal_id)
        return goal

    def activate_goal(self, goal_id: str) -> Goal:
        """Decompose an ACCEPTED goal and dispatch initial subtasks."""
        self._require_quorum("goal_activate")
        goal = self._store.get_goal(goal_id)
        if goal.status != GoalStatus.ACCEPTED:
            raise ValueError(
                f"Cannot activate goal {goal_id} in state {goal.status.value}"
            )

        # Decompose
        dag = self._decomposer.decompose(goal)
        self._store.save_dag(dag)

        # Dispatch ready subtasks
        dispatched = self._queue_bridge.dispatch_ready_subtasks(goal, dag)
        self._store.save_dag(dag)  # Updated subtask statuses

        old_status = goal.status
        goal.status = GoalStatus.ACTIVE
        goal.estimated_hours = self._estimate_total_hours(dag)
        self._store.save_goal(goal)
        self._store.log_transition(goal_id, old_status, GoalStatus.ACTIVE, "decomposition_complete")
        logger.info(
            "Activated goal %s: %d subtasks, %d dispatched",
            goal_id, len(dag.subtasks), dispatched,
        )
        return goal

    def abandon_goal(self, goal_id: str, reason: str) -> Goal:
        """Transition any non-terminal goal to ABANDONED."""
        self._require_quorum("goal_abandon")
        goal = self._store.get_goal(goal_id)
        if goal.is_terminal():
            raise ValueError(
                f"Cannot abandon terminal goal {goal_id} (status={goal.status.value})"
            )

        # Cancel running jobs
        dag = self._store.get_dag(goal_id)
        if dag:
            self._queue_bridge.cancel_all_jobs(dag)
            self._store.save_dag(dag)

        old_status = goal.status
        goal.status = GoalStatus.ABANDONED
        goal.abandoned_at = datetime.now(timezone.utc)
        goal.abandon_reason = reason
        self._store.save_goal(goal)
        self._store.log_transition(goal_id, old_status, GoalStatus.ABANDONED, "user_abandoned",
                                   metadata={"reason": reason})
        logger.info("Abandoned goal %s: %s", goal_id, reason)

        # Emit telemetry
        if self.config.telemetry_enabled:
            self._emit_telemetry(goal, dag, success=False)

        return goal

    def block_goal(
        self,
        goal_id: str,
        reason: str,
        condition_type: Optional[str] = None,
        condition_params: Optional[Dict[str, Any]] = None,
    ) -> Goal:
        """Transition an ACTIVE goal to BLOCKED.

        With a ``condition_type`` (email_reply | deployment_health |
        delivery_status | api_response | custom), the goal blocks on an
        EXTERNAL condition: the autonomy loop's condition sweep polls it at
        the type's cadence and unblocks the goal automatically when it's met.
        Without one, the goal stays blocked until something explicitly
        unblocks it."""
        self._require_quorum("goal_block")
        goal = self._store.get_goal(goal_id)
        if goal.status != GoalStatus.ACTIVE:
            raise ValueError(
                f"Cannot block goal {goal_id} in state {goal.status.value}"
            )
        old_status = goal.status
        goal.status = GoalStatus.BLOCKED
        goal.context["block_reason"] = reason
        if condition_type:
            goal.context["condition_type"] = condition_type
            goal.context["condition_params"] = condition_params or {}
            goal.context.pop("condition_last_check", None)
        self._store.save_goal(goal)
        self._store.log_transition(goal_id, old_status, GoalStatus.BLOCKED, "blocked",
                                   metadata={"reason": reason,
                                             **({"condition_type": condition_type}
                                                if condition_type else {})})
        logger.warning("Goal %s blocked: %s%s", goal_id, reason,
                       f" (awaiting {condition_type})" if condition_type else "")
        return goal

    def unblock_goal(self, goal_id: str) -> Goal:
        """Transition a BLOCKED goal back to ACTIVE."""
        self._require_quorum("goal_unblock")
        goal = self._store.get_goal(goal_id)
        if goal.status != GoalStatus.BLOCKED:
            raise ValueError(
                f"Cannot unblock goal {goal_id} in state {goal.status.value}"
            )
        old_status = goal.status
        goal.status = GoalStatus.ACTIVE
        goal.context.pop("block_reason", None)
        goal.context.pop("condition_type", None)
        goal.context.pop("condition_params", None)
        goal.context.pop("condition_last_check", None)
        self._store.save_goal(goal)
        self._store.log_transition(goal_id, old_status, GoalStatus.ACTIVE, "unblocked")

        # Dispatch any newly ready subtasks
        dag = self._store.get_dag(goal_id)
        if dag:
            self._queue_bridge.dispatch_ready_subtasks(goal, dag)
            self._store.save_dag(dag)

        logger.info("Unblocked goal %s", goal_id)
        return goal

    # ── Query ──────────────────────────────────────────────────────────────────

    def get_goal(self, goal_id: str) -> Goal:
        return self._store.get_goal(goal_id)

    def list_goals(
        self,
        status: Optional[GoalStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Goal]:
        return self._store.list_goals(status=status, limit=limit, offset=offset)

    def get_dag(self, goal_id: str) -> Optional[GoalDAG]:
        return self._store.get_dag(goal_id)

    def get_audit_trail(self, goal_id: str) -> List[GoalTransitionRecord]:
        return self._store.get_audit_trail(goal_id)

    def get_active_summary(self) -> List[GoalSummary]:
        """Return human-readable summaries of all active goals."""
        active_goals = self._store.list_goals(status=GoalStatus.ACTIVE)
        summaries = []
        for goal in active_goals:
            dag = self._store.get_dag(goal.goal_id)
            subtask_count = len(dag.subtasks) if dag else 0
            from colony_sidecar.goals.models import SubtaskStatus
            completed = (
                sum(1 for s in dag.subtasks.values()
                    if s.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED})
                if dag else 0
            )
            summaries.append(GoalSummary(
                goal_id=goal.goal_id,
                title=goal.title,
                status=goal.status.value,
                priority=goal.priority.value,
                progress_pct=goal.progress_pct,
                estimated_hours=goal.estimated_hours,
                deadline=goal.deadline,
                subtask_count=subtask_count,
                completed_subtasks=completed,
                is_overdue=goal.is_overdue(),
                replan_count=goal.replan_count,
            ))
        return summaries

    # ── Task Management (v0.7.10) ─────────────────────────────────────────────

    def complete_task(self, goal_id: str) -> bool:
        """Mark a goal/task as completed."""
        return self._store.complete_task(goal_id)

    def snooze_task(self, goal_id: str, hours: int, reason: str = "") -> bool:
        """Snooze a goal/task for N hours."""
        return self._store.snooze_task(goal_id, hours, reason)

    def dismiss_task(self, goal_id: str, reason: str = "stale") -> bool:
        """Dismiss a goal/task as no longer relevant."""
        return self._store.dismiss_task(goal_id, reason)

    def get_active_tasks(self, cooldown_hours: float = 12.0) -> List[Goal]:
        """Get goals that should generate initiatives."""
        return self._store.get_active_tasks(cooldown_hours)

    def mark_initiative_generated(self, goal_id: str) -> bool:
        """Mark that an initiative was just generated for this goal."""
        return self._store.mark_initiative_generated(goal_id)

    # ── Event Handlers ─────────────────────────────────────────────────────────

    def on_job_completed(self, job_result: JobResult) -> None:
        """Handle a completed task queue job."""
        goal_id = job_result.output.get("goal_id")
        subtask_id = job_result.output.get("subtask_id")

        if not goal_id or not subtask_id:
            logger.warning("Received job result without goal_id/subtask_id: %s", job_result.job_id)
            return

        try:
            goal = self._store.get_goal(goal_id)
        except GoalNotFoundError:
            logger.warning("on_job_completed: goal %s not found", goal_id)
            return

        dag = self._store.get_dag(goal_id)
        if dag is None:
            logger.warning("on_job_completed: no DAG for goal %s", goal_id)
            return

        subtask = dag.subtasks.get(subtask_id)
        if subtask is None:
            logger.warning("on_job_completed: subtask %s not found in DAG", subtask_id)
            return

        if job_result.succeeded:
            from colony_sidecar.goals.models import SubtaskStatus
            subtask.status = SubtaskStatus.COMPLETED
            subtask.result = job_result.output
            subtask.completed_at = job_result.completed_at or datetime.now(timezone.utc)
        else:
            from colony_sidecar.goals.models import SubtaskStatus
            subtask.status = SubtaskStatus.FAILED
            subtask.error = job_result.error
            self._trigger_replan(goal, dag, subtask)
            self._store.save_dag(dag)
            self._store.save_goal(goal)
            return

        # Update goal progress
        goal.progress_pct = self._tracker.update_progress(goal_id, dag)

        # Check for goal completion
        if dag.is_complete():
            self._complete_goal(goal, dag)
        else:
            # Dispatch any newly ready subtasks
            self._queue_bridge.dispatch_ready_subtasks(goal, dag)

            # Check if goal is now blocked
            block_reason = self._tracker.check_blocked(goal, dag)
            if block_reason and goal.status == GoalStatus.ACTIVE:
                self.block_goal(goal_id, block_reason)

        self._store.save_goal(goal)
        self._store.save_dag(dag)

    def on_message(
        self,
        message: ConversationMessage,
        history: Optional[List[ConversationMessage]] = None,
    ) -> Optional[InferenceCandidate]:
        """Process a conversation message for goal inference.

        Returns a candidate if a new goal was inferred, else None.
        """
        if not self.config.inference_enabled:
            return None

        candidate = self._inference.process_message(message, history=history)
        if candidate is None:
            return None

        # Deduplication check
        existing = self._store.list_goals(limit=100)
        similarity = self._deduplicator.check(candidate, existing)

        if similarity and similarity.recommendation == "duplicate":
            logger.debug(
                "Inference candidate %r is duplicate of goal %s (score=%.2f)",
                candidate.title, similarity.goal_id_b, similarity.similarity_score,
            )
            return None

        if similarity and similarity.recommendation == "merge":
            try:
                base = self._store.get_goal(similarity.goal_id_b)
                self._deduplicator.merge(base, candidate)
                self._store.save_goal(base)
                logger.info("Merged inference candidate into goal %s", base.goal_id)
            except GoalNotFoundError:
                pass
            return candidate

        # Preserve inference provenance and any detected deadline so the signal
        # isn't silently lost. suggested_deadline is a matched phrase (e.g.
        # "due by", "before the meeting"), not a parseable datetime, so it is
        # carried as a context hint rather than the typed `deadline` field —
        # downstream / the LLM can resolve it to a concrete date.
        inferred_context: Dict[str, Any] = {
            "inference_confidence": round(candidate.confidence, 3),
            "inference_signals": [s.value for s in candidate.signals],
            "source_messages": candidate.source_messages,
        }
        if candidate.suggested_deadline:
            inferred_context["deadline_hint"] = candidate.suggested_deadline

        goal = self.propose_goal(
            title=candidate.title,
            description=candidate.description,
            source=GoalSource.INFERRED,
            priority=candidate.priority,
            context=inferred_context,
        )

        # Auto-accept if confidence is high enough; otherwise leave PROPOSED for
        # the user to confirm.
        if candidate.should_auto_accept(self.config.auto_accept_threshold):
            self.accept_goal(goal.goal_id)
            logger.info("Auto-accepted inferred goal %s: %r", goal.goal_id, goal.title)

        return candidate

    # ── Replan ─────────────────────────────────────────────────────────────────

    def trigger_replan(
        self,
        goal_id: str,
        reason: str,
        strategy: Optional[ReplanStrategy] = None,
    ) -> ReplanResult:
        """Manually trigger a replan for a goal."""
        goal = self._store.get_goal(goal_id)
        dag = self._store.get_dag(goal_id)
        if dag is None:
            raise ValueError(f"No DAG found for goal {goal_id}")

        # Find the first failed subtask to analyse
        from colony_sidecar.goals.models import SubtaskStatus
        failed = next(
            (s for s in dag.subtasks.values() if s.status == SubtaskStatus.FAILED),
            None,
        )

        if failed is None:
            # Create a synthetic analysis for scope change
            from colony_sidecar.goals.replan import FailureAnalysis, FailureClass
            analysis = FailureAnalysis(
                subtask_id=next(iter(dag.subtasks), ""),
                failure_class=FailureClass.SCOPE_CHANGE,
                error_message=reason,
                retry_viable=False,
                affected_subtasks=list(dag.subtasks.keys()),
                suggested_strategy=(strategy or ReplanStrategy.FULL_REPLAN).value,
            )
        else:
            analysis = self._replan_engine.analyze_failure(goal, dag, failed)
            if strategy:
                analysis.suggested_strategy = strategy.value

        replan_result = self._replan_engine.generate_replan(goal, dag, analysis)

        if not replan_result.requires_user_approval:
            new_dag = self._replan_engine.apply_replan(goal, dag, replan_result)
            self._store.save_goal(goal)
            self._store.save_dag(new_dag)
            self._queue_bridge.dispatch_ready_subtasks(goal, new_dag)
            self._store.save_dag(new_dag)

        return replan_result

    # ── Private helpers ────────────────────────────────────────────────────────

    def _trigger_replan(self, goal: Goal, dag: GoalDAG, failed_subtask) -> None:
        """Internal: automatically replan after subtask failure."""
        analysis = self._replan_engine.analyze_failure(goal, dag, failed_subtask)
        replan_result = self._replan_engine.generate_replan(goal, dag, analysis)

        if replan_result.requires_user_approval:
            logger.warning(
                "Goal %s replan requires user approval: %s",
                goal.goal_id, replan_result.escalation_message,
            )
            # Block the goal pending user action
            if goal.status == GoalStatus.ACTIVE:
                old_status = goal.status
                goal.status = GoalStatus.BLOCKED
                goal.context["escalation"] = replan_result.escalation_message
                self._store.log_transition(
                    goal.goal_id, old_status, GoalStatus.BLOCKED,
                    "replan_requires_approval",
                    metadata={"strategy": replan_result.strategy.value},
                )
            return

        new_dag = self._replan_engine.apply_replan(goal, dag, replan_result)
        # Mutate the dag dict in-place so the caller's reference is updated
        dag.subtasks = new_dag.subtasks
        dag.version = new_dag.version
        dag.root_ids = new_dag.root_ids
        dag.leaf_ids = new_dag.leaf_ids
        dag.critical_path = new_dag.critical_path

    def _complete_goal(self, goal: Goal, dag: GoalDAG) -> None:
        """Finalise a goal after all subtasks complete."""
        old_status = goal.status
        goal.status = GoalStatus.COMPLETED
        goal.completed_at = datetime.now(timezone.utc)
        goal.progress_pct = 1.0
        self._store.log_transition(
            goal.goal_id, old_status, GoalStatus.COMPLETED, "all_subtasks_complete"
        )
        logger.info("Goal %s completed: %r", goal.goal_id, goal.title)

        # Update preference profile
        domain = goal.tags.get("domain", "")
        if domain:
            self._preferences.update_domain_completion(domain, success=True)

        # Emit telemetry
        if self.config.telemetry_enabled:
            self._emit_telemetry(goal, dag, success=True)

    def _emit_telemetry(self, goal: Goal, dag: Optional[GoalDAG], success: bool) -> None:
        """Emit goal completion telemetry to MetaLearner (best-effort)."""
        if self._meta_learner is None:
            return
        try:
            from colony_sidecar.goals.models import SubtaskStatus
            failed_count = 0
            subtask_types: Dict[str, int] = {}
            if dag:
                failed_count = sum(
                    1 for s in dag.subtasks.values()
                    if s.status == SubtaskStatus.FAILED
                )
                for s in dag.subtasks.values():
                    subtask_types[s.job_type] = subtask_types.get(s.job_type, 0) + 1

            actual_hours = 0.0
            if goal.accepted_at and goal.completed_at:
                actual_hours = (goal.completed_at - goal.accepted_at).total_seconds() / 3600.0
            elif goal.accepted_at and goal.abandoned_at:
                actual_hours = (goal.abandoned_at - goal.accepted_at).total_seconds() / 3600.0

            estimation_error = None
            if goal.estimated_hours and goal.estimated_hours > 0:
                estimation_error = (actual_hours - goal.estimated_hours) / goal.estimated_hours

            telemetry = {
                "goal_id": goal.goal_id,
                "goal_title": goal.title,
                "goal_source": goal.source.value,
                "priority": goal.priority.value,
                "total_subtasks": len(dag.subtasks) if dag else 0,
                "failed_subtasks": failed_count,
                "replan_count": goal.replan_count,
                "estimated_hours": goal.estimated_hours,
                "actual_hours": actual_hours,
                "estimation_error": estimation_error,
                "subtask_type_breakdown": subtask_types,
                "success": success,
                "completed_at": (goal.completed_at or goal.abandoned_at or datetime.now(timezone.utc)).isoformat(),
            }
            # MetaLearner may expose record_metric or a custom hook
            if hasattr(self._meta_learner, "_metric_hooks"):
                for hook in self._meta_learner._metric_hooks:
                    hook(telemetry)
        except Exception as exc:
            logger.warning("Failed to emit goal telemetry: %s", exc)

    def _estimate_total_hours(self, dag: GoalDAG) -> Optional[float]:
        """Estimate total goal hours from the critical path."""
        if not dag.critical_path:
            return None
        total = sum(
            (dag.subtasks[sid].estimated_hours or 1.0)
            for sid in dag.critical_path
            if sid in dag.subtasks
        )
        return round(total, 2) if total > 0 else None

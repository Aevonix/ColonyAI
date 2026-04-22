"""Goal Priority Scoring and Progress Tracking.

GoalPriorityScorer: composite priority score with urgency decay, importance,
    user preferences, blocking bonus, and age decay.

GoalProgressTracker: weighted completion fraction and completion time estimation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from colony_sidecar.goals.models import Goal, GoalDAG, GoalPriority, SubtaskStatus


@dataclass
class PriorityScore:
    """Composite priority score with component breakdown."""
    total: float                    # 0.0–100.0 final score
    urgency: float                  # Deadline proximity component
    importance: float               # User-stated importance component
    preference_bonus: float         # Learned preference modifier
    blocking_penalty: float         # Bonus if goal is blocking other goals
    age_decay: float                # Score decay for stale goals
    breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class UserPreferenceProfile:
    """Learned user preferences influencing goal prioritization.

    Attributes:
        domain_weights:       Per-domain priority multipliers (e.g., "health": 1.3).
        preferred_hours:      Hours of day when the user prefers work to execute.
        completion_patterns:  Historical goal completion rates by category.
        avoidance_domains:    Domains the user tends to defer or abandon.
    """
    domain_weights: Dict[str, float] = field(default_factory=dict)
    preferred_hours: List[int] = field(default_factory=list)
    completion_patterns: Dict[str, float] = field(default_factory=dict)
    avoidance_domains: List[str] = field(default_factory=list)

    # EMA smoothing factor for learning (α = 0.3 → weights recent behavior ~3× more)
    ema_alpha: float = 0.3

    def update_domain_completion(self, domain: str, success: bool) -> None:
        """Update completion pattern for a domain using EMA."""
        value = 1.0 if success else 0.0
        if domain in self.completion_patterns:
            old = self.completion_patterns[domain]
            self.completion_patterns[domain] = self.ema_alpha * value + (1 - self.ema_alpha) * old
        else:
            self.completion_patterns[domain] = value

    def update_domain_weight(self, domain: str, delta: float) -> None:
        """Adjust domain weight using EMA."""
        current = self.domain_weights.get(domain, 1.0)
        new_weight = self.ema_alpha * (current + delta) + (1 - self.ema_alpha) * current
        self.domain_weights[domain] = max(0.1, min(3.0, new_weight))


def _deadline_urgency(goal: Goal) -> float:
    """Compute urgency in [0, 1] based on deadline proximity.

    urgency(t) = 1 / (1 + exp(-k * (t_remaining_fraction - 0.5)))
    where k = 10 (steepness constant)
    """
    if goal.deadline is None:
        return 0.0

    now = datetime.now(timezone.utc)
    if now >= goal.deadline:
        return 1.0

    total_window = (goal.deadline - goal.created_at).total_seconds()
    if total_window <= 0:
        return 1.0

    remaining = (goal.deadline - now).total_seconds()
    t_remaining_fraction = remaining / total_window

    k = 10.0
    urgency = 1.0 / (1.0 + math.exp(-k * (t_remaining_fraction - 0.5)))
    # Invert so that low remaining fraction = high urgency
    return round(1.0 - urgency, 4)


def _base_importance(goal: Goal) -> float:
    """Derive importance from goal's explicit priority (normalised to [0, 1])."""
    return goal.priority.value / 100.0


def _preference_modifier(goal: Goal, preferences: UserPreferenceProfile) -> float:
    """Compute preference bonus in [0, 1] from user preference profile."""
    # Use goal tags to determine domain
    domain = goal.tags.get("domain", "")
    if not domain:
        return 0.0

    weight = preferences.domain_weights.get(domain, 1.0)
    # Map weight (typically 0.1–3.0) to a bonus in [0, 1]
    bonus = min(1.0, max(0.0, (weight - 1.0) / 2.0))
    return round(bonus, 4)


def _blocking_bonus(goal: Goal, all_goals: Optional[List[Goal]] = None) -> float:
    """Return 1.0 if this goal is a dependency (parent) for other active goals."""
    if all_goals is None:
        return 0.0
    for other in all_goals:
        if other.parent_goal_id == goal.goal_id and not other.is_terminal():
            return 1.0
    return 0.0


def _age_decay(goal: Goal) -> float:
    """Compute staleness penalty in [0, 1] for old untouched goals."""
    age = goal.age_hours()
    # Penalty grows logarithmically; at 168 hours (1 week) = ~0.5
    if age < 1:
        return 0.0
    return round(min(1.0, math.log(age) / math.log(168.0)), 4)


class GoalPriorityScorer:
    """Compute composite priority scores for goals."""

    WEIGHTS = {
        "urgency":          0.35,
        "importance":       0.30,
        "preference_bonus": 0.15,
        "blocking":         0.10,
        "age_decay":       -0.10,  # Negative — reduces priority for very old stale goals
    }

    def score(
        self,
        goal: Goal,
        preferences: Optional[UserPreferenceProfile] = None,
        all_goals: Optional[List[Goal]] = None,
    ) -> PriorityScore:
        """Compute a composite priority score for a goal.

        Components:
        - urgency:    deadline_urgency(goal) — 0.0 if no deadline, 1.0 if overdue
        - importance: base_importance(goal) — from user tags, source, explicit priority
        - preference: preference_modifier(goal, preferences) — domain preference bonus
        - blocking:   is_blocking_bonus(goal) — if other goals depend on this one
        - age_decay:  stale_penalty(goal) — reduces priority for untouched old goals
        """
        prefs = preferences or UserPreferenceProfile()

        urgency   = _deadline_urgency(goal)
        importance = _base_importance(goal)
        preference = _preference_modifier(goal, prefs)
        blocking  = _blocking_bonus(goal, all_goals)
        decay     = _age_decay(goal)

        components = {
            "urgency": urgency,
            "importance": importance,
            "preference_bonus": preference,
            "blocking": blocking,
            "age_decay": decay,
        }

        total = 100.0 * (
            self.WEIGHTS["urgency"]          * urgency
            + self.WEIGHTS["importance"]      * importance
            + self.WEIGHTS["preference_bonus"] * preference
            + self.WEIGHTS["blocking"]        * blocking
            + self.WEIGHTS["age_decay"]       * decay   # this is negative
        )
        total = max(0.0, min(100.0, total))

        return PriorityScore(
            total=round(total, 2),
            urgency=urgency,
            importance=importance,
            preference_bonus=preference,
            blocking_penalty=blocking,
            age_decay=decay,
            breakdown=components,
        )

    def rank_goals(
        self,
        goals: List[Goal],
        preferences: Optional[UserPreferenceProfile] = None,
    ) -> List[Goal]:
        """Return goals sorted by descending priority score."""
        scored = [(g, self.score(g, preferences, goals).total) for g in goals]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [g for g, _ in scored]


class GoalProgressTracker:
    """Track and report goal progress."""

    def update_progress(self, goal_id: str, dag: GoalDAG) -> float:
        """Recompute progress fraction from subtask statuses.

        Weights subtasks on the critical path more heavily (2× weight).

        Returns updated progress_pct (0.0–1.0).
        """
        total_weight = 0.0
        done_weight = 0.0

        for s in dag.subtasks.values():
            weight = 2.0 if s.is_critical_path else 1.0
            total_weight += weight
            if s.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED}:
                done_weight += weight

        return done_weight / total_weight if total_weight > 0 else 0.0

    def estimate_completion(
        self,
        goal: Goal,
        dag: GoalDAG,
        historical_perf: Optional[Dict[str, float]] = None,
    ) -> Optional[datetime]:
        """Estimate completion time based on critical path remaining work.

        Uses three-point estimation:
          estimated_remaining = sum(subtask.estimated_hours * perf_factor)
          for remaining critical path subtasks.

        Returns estimated completion datetime, or None if not estimable.
        """
        if not dag.critical_path:
            return None

        perf = historical_perf or {}
        remaining_hours = 0.0
        for sid in dag.critical_path:
            subtask = dag.subtasks.get(sid)
            if subtask is None:
                continue
            if subtask.status in {SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED}:
                continue
            est = subtask.estimated_hours if subtask.estimated_hours is not None else 1.0
            pf = perf.get(subtask.job_type, 1.0)
            remaining_hours += est * pf

        if remaining_hours <= 0:
            return None

        from datetime import timedelta
        return datetime.now(timezone.utc) + timedelta(hours=remaining_hours)

    def check_blocked(self, goal: "Goal", dag: GoalDAG) -> Optional[str]:
        """Return a reason string if the goal appears blocked, else None."""
        ready = dag.ready_subtasks()
        if ready:
            return None  # Work available

        # Check if there are non-terminal subtasks with unmet deps
        for s in dag.subtasks.values():
            if s.status in {SubtaskStatus.PENDING, SubtaskStatus.BLOCKED}:
                # All deps failed?
                for dep_id in s.depends_on:
                    dep = dag.subtasks.get(dep_id)
                    if dep and dep.status == SubtaskStatus.FAILED:
                        return f"Subtask {s.subtask_id} blocked by failed dependency {dep_id}"
        return None

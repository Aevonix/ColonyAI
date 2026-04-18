"""Goals seeder — seeds foundational system goals into the goal store."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GoalsSeeder:
    name = "goals"

    async def seed(self, corpus: Any) -> None:
        try:
            from colony_sidecar.goals.store import GoalStore
            from colony_sidecar.goals.models import Goal, GoalStatus, GoalSource, GoalPriority, GoalOutcome
        except ImportError as exc:
            logger.debug("goals: import failed — skipping: %s", exc)
            return

        try:
            store = GoalStore.get_instance()
        except Exception as exc:
            logger.debug("goals: GoalStore.get_instance() failed: %s", exc)
            return

        colony_id = corpus.colony_id
        now = _now()

        # Check if bootstrap goal already exists
        bootstrap_goal_id = f"goal-bootstrap-selfknowledge-{colony_id[:8]}"
        try:
            existing = store.get_goal(bootstrap_goal_id)
            if existing is not None:
                logger.debug("goals: bootstrap goals already present — skipping")
                return
        except Exception:
            pass  # GoalNotFoundError is expected on first boot

        goals = [
            Goal(
                goal_id=bootstrap_goal_id,
                title="Maintain self-knowledge",
                description=(
                    "Continuously maintain accurate self-knowledge: monitor subsystem health, "
                    "refresh the self-knowledge corpus when Colony is updated, "
                    "and answer questions about capabilities accurately."
                ),
                source=GoalSource.RECURRING,
                status=GoalStatus.ACTIVE,
                priority=GoalPriority.NORMAL,
                outcome=GoalOutcome(
                    description="Self-knowledge corpus stays current and accurate.",
                    success_criteria=[
                        "World model contains self + all subsystem entities",
                        "Bootstrap verifier passes all 16 checks",
                        "Memory store has foundational identity memories",
                    ],
                    measurable=True,
                ),
                tags=["system", "bootstrap", "self-knowledge"],
                context={"seeded_by": "identity_bootstrap", "corpus_version": corpus.corpus_version},
                created_at=now,
                updated_at=now,
            ),
            Goal(
                goal_id=f"goal-bootstrap-health-{colony_id[:8]}",
                title="Maintain system health",
                description=(
                    "Monitor and maintain Colony subsystem health. "
                    "Detect anomalies, restart failed workers, and alert on critical failures."
                ),
                source=GoalSource.RECURRING,
                status=GoalStatus.ACTIVE,
                priority=GoalPriority.HIGH,
                outcome=GoalOutcome(
                    description="All Colony subsystems operational.",
                    success_criteria=[
                        "Task queue worker running",
                        "World model backend connected",
                        "Autonomy loop ticking",
                    ],
                    measurable=True,
                ),
                tags=["system", "health", "monitoring"],
                context={"seeded_by": "identity_bootstrap"},
                created_at=now,
                updated_at=now,
            ),
            Goal(
                goal_id=f"goal-bootstrap-learning-{colony_id[:8]}",
                title="Continuous skill learning",
                description=(
                    "Observe tool usage patterns and user interactions to identify opportunities "
                    "for new skill creation. Periodically propose and create skills that improve "
                    "efficiency for common task patterns."
                ),
                source=GoalSource.RECURRING,
                status=GoalStatus.ACCEPTED,
                priority=GoalPriority.LOW,
                outcome=GoalOutcome(
                    description="Skill registry grows through autonomous learning.",
                    success_criteria=[
                        "At least one skill proposed per week from tool learning",
                        "New skills pass security scanner",
                    ],
                    measurable=False,
                ),
                tags=["learning", "skills", "automation"],
                context={"seeded_by": "identity_bootstrap"},
                created_at=now,
                updated_at=now,
            ),
        ]

        for goal in goals:
            try:
                store.save_goal(goal)
            except Exception as exc:
                logger.warning("goals: failed to save goal %s: %s", goal.goal_id, exc)

        logger.info("goals: seeded %d system goals", len(goals))

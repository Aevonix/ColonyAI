"""Behavioral correction executor skill.

Applies learned preferences when recurring patterns are detected.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class BehavioralCorrectionSkill(InitiativeExecutorSkill):
    """Skill for applying learned behavioral corrections."""

    skill_name = "behavioral_correction"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        trigger = initiative.trigger_data.get("trigger", "")
        expected_action = initiative.trigger_data.get("action", "")
        recurrence_count = initiative.trigger_data.get("recurrence_count", 0)
        self._log(
            "info",
            "Behavioral correction: %s (recurred %d times)",
            trigger[:80],
            recurrence_count,
        )

        # Store preference in graph for future reference
        if self.graph:
            try:
                await self._store_preference(trigger, expected_action, entity_id)
            except Exception as e:
                self._log("warning", "Failed to store preference: %s", e)

        # For v0.11.1, create a proposal. Full auto-application requires
        # deeper integration with the reasoning pipeline.
        return ExecutionResult.PROPOSAL_CREATED

    async def _store_preference(
        self, trigger: str, expected: str, entity_id: str
    ) -> None:
        """Store a learned preference in the graph."""
        if not hasattr(self.graph, "driver"):
            return

        import uuid

        async with self.graph.driver.session(database=self.graph.database) as session:
            await session.run(
                """
                MERGE (p:Preference {trigger: $trigger})
                SET p.id = coalesce(p.id, $new_id),
                    p.expected = $expected,
                    p.source = "behavioral_correction",
                    p.updated_at = datetime()
            """,
                trigger=trigger[:200],
                expected=expected[:200],
                new_id=str(uuid.uuid4()),
            )

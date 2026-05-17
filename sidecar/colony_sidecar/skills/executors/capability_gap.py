"""Capability gap executor skill.

Registers missing capabilities and creates proposals for fixes.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class CapabilityGapSkill(InitiativeExecutorSkill):
    """Skill for handling missing or broken capabilities."""

    skill_name = "capability_gap"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        failure_mode = initiative.trigger_data.get("failure_mode", "unknown")
        failure_count = initiative.trigger_data.get("failure_count", 0)
        self._log("info", "Capability gap: %s (%s, %d failures)", entity_id, failure_mode, failure_count)

        # For v0.11.1, create a proposal. Auto-fix requires tool sandboxing.
        return ExecutionResult.PROPOSAL_CREATED

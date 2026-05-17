"""Knowledge acquisition executor skill.

Proposes research tasks for low-confidence knowledge areas.
v0.11.1: proposal-only to avoid cross-layer circular dependency
with ResearchPipeline.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class KnowledgeAcquisitionSkill(InitiativeExecutorSkill):
    """Skill for queuing research on knowledge gaps."""

    skill_name = "knowledge_acquisition"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        name = initiative.trigger_data.get("name", "Unknown concept")
        confidence = initiative.trigger_data.get("confidence_score", 0.0)
        self._log("info", "Knowledge gap: %s (confidence %.2f)", name, confidence)

        # v0.11.1: proposal-only. Auto-research deferred to v0.11.2
        # to avoid importing ResearchPipeline into skills/executors.
        return ExecutionResult.PROPOSAL_CREATED

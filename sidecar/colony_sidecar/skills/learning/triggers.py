"""Colony Skills — learning triggers and coordination service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Coroutine, Optional

from colony_sidecar.skills.learning.novelty_detector import NoveltyDetector, NoveltyResult
from colony_sidecar.skills.learning.pattern_extractor import PatternExtractor
from colony_sidecar.skills.models import TaskSolution

logger = logging.getLogger(__name__)


class TriggerCoordinator:
    """Listens for agent:complete events on EventBus and triggers the skill learning pipeline.

    Lighter-weight than SkillLearningService — scores novelty and extracts patterns
    without requiring a SkillPackager.  Wired by calling :meth:`wire_event_bus` with
    the colony EventBus instance after construction.
    """

    def __init__(
        self,
        detector: NoveltyDetector,
        extractor: PatternExtractor,
    ) -> None:
        self._detector = detector
        self._extractor = extractor

    def wire_event_bus(self, bus) -> None:
        """Subscribe to agent:complete events on *bus*."""

        def _sync_handler(event) -> None:
            solution = getattr(event, "solution", None)
            if solution is None:
                return
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._evaluate(solution))
            except Exception:
                pass

        bus.subscribe(
            handler=_sync_handler,
            event_types=[],
            filter_fn=lambda e: getattr(e, "event_type", "") == "agent:complete",
        )
        logger.info("TriggerCoordinator subscribed to agent:complete events")

    async def _evaluate(self, solution: TaskSolution) -> None:
        """Score novelty and extract pattern for a completed agent task."""
        try:
            result: NoveltyResult = await self._detector.score(solution)
            logger.debug(
                "TriggerCoordinator novelty: task='%s' score=%.2f recommendation=%s",
                solution.task_id,
                result.score,
                result.recommendation,
            )
            if result.recommendation not in ("skip",):
                pattern = self._extractor.extract(solution)
                logger.info(
                    "TriggerCoordinator: skill candidate extracted (task=%s, domains=%s)",
                    solution.task_id,
                    getattr(pattern, "domains", []),
                )
        except Exception as exc:
            logger.debug("TriggerCoordinator evaluation error: %s", exc)


class TriggerSource(str, Enum):
    POST_TASK_HOOK = "post_task_hook"
    EXPLICIT_COMMAND = "explicit"
    SCHEDULED_REVIEW = "scheduled"
    FEDERATION_REQUEST = "federation"


@dataclass
class LearningTriggerEvent:
    source: TriggerSource
    solution: TaskSolution
    force_capture: bool = False
    requestor_colony_id: Optional[str] = None


class SkillLearningService:
    """Coordinates learning trigger events with the novelty/extraction pipeline."""

    def __init__(
        self,
        detector: NoveltyDetector,
        extractor: PatternExtractor,
        packager: "SkillPackager",  # noqa: F821
        require_confirmation_below_score: float = 0.80,
    ) -> None:
        self._detector = detector
        self._extractor = extractor
        self._packager = packager
        self._confirm_threshold = require_confirmation_below_score

    async def handle(self, event: LearningTriggerEvent) -> Optional[str]:
        """Process a learning trigger event.

        Returns:
            The new skill_id if captured, None if skipped.
        """
        if not event.force_capture:
            result: NoveltyResult = await self._detector.score(event.solution)
            logger.debug(
                "Novelty score for task '%s': %.2f (%s)",
                event.solution.task_id,
                result.score,
                result.recommendation,
            )
            if result.recommendation == "skip":
                return None
            if (
                result.recommendation == "update_existing"
                and result.score < self._confirm_threshold
            ):
                logger.info(
                    "Partial match to %s — deferring to update workflow.",
                    result.closest_skill_id,
                )
                return None

        pattern = self._extractor.extract(event.solution)
        skill_id = await self._packager.package(
            solution=event.solution,
            pattern=pattern,
            source=event.source,
        )
        logger.info("Skill captured: %s (trigger: %s)", skill_id, event.source)
        return skill_id

    async def post_task_hook(self, solution: TaskSolution) -> None:
        """Wired as a post-task completion callback in colony/task_queue/."""
        event = LearningTriggerEvent(
            source=TriggerSource.POST_TASK_HOOK,
            solution=solution,
        )
        await self.handle(event)

    async def explicit_remember(
        self, solution: TaskSolution, requestor: Optional[str] = None
    ) -> Optional[str]:
        """Called when a user or orchestrator explicitly requests capture."""
        event = LearningTriggerEvent(
            source=TriggerSource.EXPLICIT_COMMAND,
            solution=solution,
            force_capture=True,
            requestor_colony_id=requestor,
        )
        return await self.handle(event)

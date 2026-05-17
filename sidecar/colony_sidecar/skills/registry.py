"""Skill registry for loading and managing initiative executor skills.

Skills are discovered dynamically and can be hot-reloaded.
"""

import importlib
import inspect
import logging
import os
from typing import Any, Dict, List, Optional, Type

from .base import InitiativeExecutorSkill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry of initiative executor skills.

    Loads built-in skills automatically. Additional skills can be
    registered manually or discovered from directories.
    """

    def __init__(self, graph_client=None, event_bus=None, telemetry=None):
        self._skills: Dict[str, InitiativeExecutorSkill] = {}
        self._graph = graph_client
        self._events = event_bus
        self._telemetry = telemetry

        # Auto-load built-in skills
        self._load_builtin_skills()

    def _load_builtin_skills(self) -> None:
        """Load all built-in executor skills."""
        builtin_skills = [
            "colony_sidecar.skills.executors.subsystem_health",
            "colony_sidecar.skills.executors.data_quality",
            "colony_sidecar.skills.executors.operational_hygiene",
        ]

        for module_path in builtin_skills:
            try:
                module = importlib.import_module(module_path)
                # Find skill classes in the module
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if (
                        issubclass(obj, InitiativeExecutorSkill)
                        and obj is not InitiativeExecutorSkill
                        and not getattr(obj, "__abstractmethods__", None)
                    ):
                        skill = obj(
                            graph_client=self._graph,
                            event_bus=self._events,
                            telemetry=self._telemetry,
                        )
                        self.register(skill)
                        logger.info("Loaded built-in skill: %s", skill.skill_name)
            except ImportError as e:
                logger.warning("Failed to load built-in skill module %s: %s", module_path, e)
            except Exception as e:
                logger.error("Error loading built-in skill %s: %s", module_path, e)

    def register(self, skill: InitiativeExecutorSkill) -> None:
        """Register a skill instance."""
        self._skills[skill.skill_name] = skill
        logger.debug("Registered skill: %s", skill.skill_name)

    def unregister(self, skill_name: str) -> None:
        """Unregister a skill by name."""
        if skill_name in self._skills:
            del self._skills[skill_name]
            logger.debug("Unregistered skill: %s", skill_name)

    def get(self, skill_name: str) -> Optional[InitiativeExecutorSkill]:
        """Get a skill by name."""
        return self._skills.get(skill_name)

    def list_skills(self) -> List[str]:
        """List all registered skill names."""
        return list(self._skills.keys())

    async def find_skill_for_category(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[InitiativeExecutorSkill]:
        """Find the first skill that can execute a given category.

        Args:
            category: InitiativeCategory node data
            context: Execution context

        Returns:
            The matching skill, or None if no skill can handle it
        """
        executor_skill_name = category.get("executor_skill")

        for skill in self._skills.values():
            # Fast path: exact skill name match
            if executor_skill_name and skill.skill_name == executor_skill_name:
                if await skill.can_execute(category, context):
                    return skill
                continue

            # Slow path: ask each skill
            try:
                if await skill.can_execute(category, context):
                    return skill
            except Exception as e:
                logger.warning(
                    "Skill %s.can_execute() failed: %s", skill.skill_name, e
                )

        return None

    async def execute(
        self,
        skill_name: str,
        initiative_context: Any,  # InitiativeExecutionContext
    ) -> Any:  # ExecutionResult
        """Execute an initiative using a specific skill.

        Args:
            skill_name: Name of the skill to use
            initiative_context: The initiative execution context

        Returns:
            ExecutionResult
        """
        skill = self._skills.get(skill_name)
        if not skill:
            logger.error("Skill not found: %s", skill_name)
            return None  # type: ignore[return-value]

        try:
            return await skill.execute(initiative_context)
        except Exception as e:
            logger.error("Skill %s execution failed: %s", skill_name, e)
            return None  # type: ignore[return-value]

    def health_check(self) -> Dict[str, Any]:
        """Return health status of all registered skills."""
        return {
            "skills": {
                name: skill.health_check()
                for name, skill in self._skills.items()
            },
            "count": len(self._skills),
        }

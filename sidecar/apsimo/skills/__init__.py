"""Skill-based executor framework for self-initiatives.

Skills are dynamically loaded classes that know how to execute initiatives
of a particular category. They can be hot-reloaded without restarting Colony.
"""

from .base import InitiativeExecutorSkill, ExecutionResult
from .registry import SkillRegistry

__all__ = ["InitiativeExecutorSkill", "ExecutionResult", "SkillRegistry"]

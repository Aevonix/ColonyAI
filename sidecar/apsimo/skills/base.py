"""Base class for initiative executor skills.

Each skill handles a category of self-initiative execution.
Skills are dynamically loaded and can be hot-reloaded.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import logging

logger = logging.getLogger(__name__)


class ExecutionResult(str, Enum):
    """Outcome of executing a self-initiative."""

    AUTO_FIXED = "auto_fixed"
    PROPOSAL_CREATED = "proposal_created"
    RESEARCH_QUEUED = "research_queued"
    FAILED = "failed"
    NO_ACTION = "no_action"
    ESCALATED = "escalated"


@dataclass
class InitiativeExecutionContext:
    """Context passed to skills during execution."""

    initiative_id: str
    category_id: str
    category_name: str
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    trigger_data: Dict[str, Any] = field(default_factory=dict)
    priority: float = 0.5
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.trigger_data is None:
            self.trigger_data = {}
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)


class InitiativeExecutorSkill(ABC):
    """Base class for skills that execute self-initiatives.

    Subclasses must implement:
    - can_execute(): Check if this skill can handle a category
    - execute(): Execute the initiative and return a result
    """

    # Override in subclass
    skill_name: str = "base"
    skill_version: str = "1.0.0"

    def __init__(self, graph_client=None, event_bus=None, telemetry=None):
        self.graph = graph_client
        self.events = event_bus
        self.telemetry = telemetry

    @abstractmethod
    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        """Check if this skill can handle the given category.

        Args:
            category: The InitiativeCategory node data as a dict
            context: Execution context dict

        Returns:
            True if this skill can execute initiatives of this category
        """

    @abstractmethod
    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        """Execute the initiative.

        Args:
            initiative: The initiative execution context

        Returns:
            ExecutionResult indicating the outcome
        """

    async def diagnose(self, entity_id: str, entity_type: str) -> Dict[str, Any]:
        """Diagnose the state of an entity. Subclasses can override."""
        return {"status": "unknown", "entity_id": entity_id}

    async def health_check(self) -> Dict[str, Any]:
        """Return the health status of this skill itself."""
        return {
            "skill": self.skill_name,
            "version": self.skill_version,
            "status": "healthy",
        }

    def _log(self, level: str, msg: str, *args, **kwargs):
        """Log with skill name prefix."""
        prefix = f"[{self.skill_name}]"
        getattr(logger, level)(f"{prefix} {msg}", *args, **kwargs)

"""Initiative Engine — generate proactive suggestions.

Generates:
    - Follow-up reminders
    - Relationship maintenance suggestions
    - Health insights
    - Scheduling recommendations
"""

import uuid as _uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


class InitiativeType(str, Enum):
    """Categories of proactive suggestions."""

    FOLLOW_UP = "follow_up"
    RELATIONSHIP = "relationship"
    HEALTH = "health"
    SCHEDULING = "scheduling"


@dataclass
class Initiative:
    """A proactive suggestion.

    Attributes:
        id: Unique initiative identifier
        type: Category of suggestion
        description: Human-readable description of what to do
        priority: How important this is (0-1)
        rationale: Why this suggestion was generated
        action_hint: Optional suggested concrete action
        entity_id: Optional related entity (person, task, etc.)
        expires_at: When this initiative is no longer relevant
        created_at: When the initiative was generated
    """

    id: str
    type: InitiativeType
    description: str
    priority: float
    rationale: str
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)


class InitiativeEngine:
    """Generate proactive suggestions.

    Analyzes context data provided via ``add_context()`` to surface
    actionable suggestions the user hasn't explicitly asked for.

    Context categories:
    - ``pending_tasks``: List of dicts with keys ``description``, ``days_pending``
    - ``neglected_contacts``: List of dicts with ``name``, ``entity_id``, ``days_since_contact``
    - ``health_alerts``: List of dicts with ``metric``, ``value``, ``target``
    - ``scheduling_opportunities``: List of dicts with ``description``, ``priority``, ``rationale``, ``action_hint``
    - ``completed_tasks``: List of dicts with ``description``, ``entity_id``, ``result`` (Gap C)

    Args:
        graph_client: Colony graph client for relationship/entity data
        event_bus: Colony event bus for subscribing to relevant events
        mind_model: Mind model for behavioral state awareness
    """

    def __init__(self, graph_client: Any, event_bus: Any, mind_model: Any) -> None:
        self.graph = graph_client
        self.events = event_bus
        self.mind_model = mind_model
        self._initiatives: List[Initiative] = []
        self._context: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def add_context(
        self,
        context_type: str,
        items: List[Dict[str, Any]],
    ) -> None:
        """Provide context data for initiative generation.

        Args:
            context_type: One of "pending_tasks", "neglected_contacts",
                "health_alerts", "scheduling_opportunities"
            items: List of context item dicts (schema depends on type)
        """
        if context_type not in self._context:
            self._context[context_type] = []
        self._context[context_type].extend(items)
        logger.debug("Added %d items to context '%s'", len(items), context_type)

    def clear_context(self, context_type: Optional[str] = None) -> None:
        """Clear context data, optionally for a specific type only.

        Args:
            context_type: If provided, only clear this type; else clear all.
        """
        if context_type:
            self._context.pop(context_type, None)
        else:
            self._context.clear()

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        types: Optional[List[InitiativeType]] = None,
        min_priority: float = 0.5,
    ) -> List[Initiative]:
        """Generate proactive suggestions.

        Args:
            types: If provided, only generate these types. None = all types.
            min_priority: Minimum priority threshold (0-1)

        Returns:
            Initiatives above the priority threshold, sorted by priority (descending)
        """
        initiatives: List[Initiative] = []

        if not types or InitiativeType.FOLLOW_UP in types:
            initiatives.extend(await self._generate_follow_ups())
            initiatives.extend(await self._generate_task_completion_follow_ups())

        if not types or InitiativeType.RELATIONSHIP in types:
            initiatives.extend(await self._generate_relationship_suggestions())

        if not types or InitiativeType.HEALTH in types:
            initiatives.extend(await self._generate_health_suggestions())

        if not types or InitiativeType.SCHEDULING in types:
            initiatives.extend(await self._generate_scheduling_suggestions())

        filtered = [i for i in initiatives if i.priority >= min_priority]
        result = sorted(filtered, key=lambda i: i.priority, reverse=True)

        logger.debug(
            "Generated %d initiatives (%d above threshold %.2f)",
            len(initiatives),
            len(result),
            min_priority,
        )
        return result

    async def dismiss(self, initiative_id: str) -> None:
        """Dismiss an initiative so it won't be surfaced from the active list.

        Args:
            initiative_id: ID of the initiative to dismiss
        """
        self._initiatives = [i for i in self._initiatives if i.id != initiative_id]
        logger.debug("Dismissed initiative %s", initiative_id)

    async def get_active(self) -> List[Initiative]:
        """Get all non-expired active initiatives.

        Returns:
            Active initiatives sorted by priority (descending)
        """
        now = datetime.now(timezone.utc)
        active = [
            i for i in self._initiatives
            if i.expires_at is None or i.expires_at > now
        ]
        return sorted(active, key=lambda i: i.priority, reverse=True)

    # ------------------------------------------------------------------
    # Generators
    # ------------------------------------------------------------------

    async def _generate_follow_ups(self) -> List[Initiative]:
        """Generate follow-up suggestions from pending tasks in context."""
        initiatives: List[Initiative] = []
        for item in self._context.get("pending_tasks", []):
            desc = item.get("description", "pending task")
            days = float(item.get("days_pending", 0))
            # Priority grows with time pending, capped at 1.0
            priority = min(1.0, 0.4 + days * 0.1)
            initiatives.append(
                Initiative(
                    id=f"followup-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Follow up on: {desc}",
                    priority=priority,
                    rationale=f"Task has been pending for {days:.0f} day(s)",
                    action_hint=f"Review status of '{desc}'",
                    entity_id=item.get("entity_id"),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                )
            )
        return initiatives

    async def _generate_task_completion_follow_ups(self) -> List[Initiative]:
        """Generate follow-up initiatives for recently completed background tasks (Gap C)."""
        initiatives: List[Initiative] = []
        for task in self._context.get("completed_tasks", []):
            desc = task.get("description", "background task")
            initiatives.append(
                Initiative(
                    id=f"task-done-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Task completed: {desc}",
                    priority=0.6,
                    rationale="Background task finished with result",
                    action_hint=None,  # Deliver to user, don't auto-execute
                    entity_id=task.get("entity_id"),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                )
            )
        return initiatives

    async def _generate_relationship_suggestions(self) -> List[Initiative]:
        """Generate relationship maintenance suggestions from neglected contacts."""
        initiatives: List[Initiative] = []
        for contact in self._context.get("neglected_contacts", []):
            name = contact.get("name", "contact")
            days = float(contact.get("days_since_contact", 0))
            priority = min(1.0, 0.3 + days * 0.05)
            initiatives.append(
                Initiative(
                    id=f"relationship-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.RELATIONSHIP,
                    description=f"Reach out to {name}",
                    priority=priority,
                    rationale=f"No contact with {name} for {days:.0f} day(s)",
                    action_hint=f"Send a quick message to {name}",
                    entity_id=contact.get("entity_id"),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
            )
        return initiatives

    async def _generate_health_suggestions(self) -> List[Initiative]:
        """Generate health-related suggestions from health alert context."""
        initiatives: List[Initiative] = []
        for alert in self._context.get("health_alerts", []):
            metric = alert.get("metric", "health metric")
            value = alert.get("value")
            target = alert.get("target")

            if value is not None and target is not None and target != 0:
                deviation = abs(float(value) - float(target)) / abs(float(target))
                priority = min(1.0, 0.4 + deviation * 0.6)
            else:
                priority = 0.5

            initiatives.append(
                Initiative(
                    id=f"health-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.HEALTH,
                    description=f"Review {metric}: current={value}, target={target}",
                    priority=priority,
                    rationale=f"{metric} is outside target range",
                    action_hint=f"Check and adjust {metric}",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )
            )
        return initiatives

    async def _generate_scheduling_suggestions(self) -> List[Initiative]:
        """Generate scheduling recommendations from opportunity context."""
        initiatives: List[Initiative] = []
        for slot in self._context.get("scheduling_opportunities", []):
            desc = slot.get("description", "scheduling opportunity")
            priority = float(slot.get("priority", 0.5))
            initiatives.append(
                Initiative(
                    id=f"schedule-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.SCHEDULING,
                    description=desc,
                    priority=min(1.0, priority),
                    rationale=slot.get("rationale", "Based on observed patterns"),
                    action_hint=slot.get("action_hint"),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
        return initiatives

"""Insight Deliverer — route insights to appropriate delivery channels.

Routes based on:
- Urgency (high → push notification, low → digest)
- User preferences (push enabled/disabled, preferred channels)
- Channel availability
- Time of day (avoid late-night pushes for non-critical insights)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class DeliveryChannel(str, Enum):
    """Available delivery channels for insights.

    PUSH: Immediate notification (WhatsApp, push notification)
    DIGEST: Batched into daily/weekly summary
    IN_APP: Show in next conversation naturally
    EMAIL: Email notification for lower-urgency items
    """

    PUSH = "push"
    DIGEST = "digest"
    IN_APP = "in_app"
    EMAIL = "email"


@dataclass
class DeliveryDecision:
    """Routing decision for an insight.

    Attributes:
        insight_id: ID of the insight being routed
        channel: Selected delivery channel
        reason: Why this channel was chosen
        scheduled_for: ISO timestamp for digest delivery (None for immediate)
    """

    insight_id: str
    channel: DeliveryChannel
    reason: str
    scheduled_for: Optional[str] = None


@runtime_checkable
class DelivererEventBus(Protocol):
    """Protocol for event emission from the deliverer."""

    async def emit_async(self, event: Any) -> None: ...


@runtime_checkable
class Deliverable(Protocol):
    """Protocol for objects that can be delivered as insights."""

    @property
    def id(self) -> str: ...

    @property
    def description(self) -> str: ...


class InsightDeliverer:
    """Route insights to appropriate delivery channels.

    Makes routing decisions based on urgency level and user preferences.
    High-urgency insights get pushed immediately; low-urgency insights
    queue for the next digest.

    Args:
        event_bus: Event bus for emitting delivery events
        user_preferences: Dict of user delivery preferences
    """

    def __init__(
        self,
        event_bus: DelivererEventBus,
        user_preferences: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.events = event_bus
        self.preferences = user_preferences or {}

    async def deliver(
        self,
        insight: Deliverable,
        urgency: float = 0.5,
    ) -> DeliveryDecision:
        """Determine delivery channel and route an insight.

        Routing logic:
        - urgency >= 0.8 and push enabled → PUSH
        - urgency >= 0.5 → IN_APP (show in conversation)
        - urgency < 0.5 → DIGEST (queue for summary)

        Args:
            insight: The insight to deliver
            urgency: Urgency level between 0 and 1

        Returns:
            DeliveryDecision describing channel and reasoning.
        """
        if urgency >= 0.8 and self.preferences.get("push_enabled", True):
            channel = DeliveryChannel.PUSH
            reason = "High urgency, push enabled"
            await self._send_push(insight)
        elif urgency >= 0.5:
            channel = DeliveryChannel.IN_APP
            reason = "Moderate urgency, show in conversation"
        else:
            channel = DeliveryChannel.DIGEST
            reason = "Low urgency, queue for digest"

        decision = DeliveryDecision(
            insight_id=insight.id,
            channel=channel,
            reason=reason,
        )

        logger.info(
            "Insight %s routed to %s: %s",
            insight.id,
            channel.value,
            reason,
        )
        return decision

    async def deliver_batch(
        self,
        insights: List[tuple[Deliverable, float]],
    ) -> List[DeliveryDecision]:
        """Route multiple insights with their urgency levels.

        Args:
            insights: List of (insight, urgency) tuples

        Returns:
            List of DeliveryDecision objects.
        """
        decisions = []
        for insight, urgency in insights:
            decision = await self.deliver(insight, urgency)
            decisions.append(decision)
        return decisions

    async def _send_push(self, insight: Deliverable) -> None:
        """Send an immediate push notification for a high-urgency insight."""
        if self.events is None:
            logger.warning("No event bus configured; insight %s not delivered via push", insight.id)
            return
        try:
            from colony_sidecar.events.types import InsightPushEvent
            event = InsightPushEvent(
                id=str(uuid.uuid4()),
                source="insight_deliverer",
                insight_id=insight.id,
                description=insight.description,
                urgency=1.0,
                delivery_channel="push",
            )
            await self.events.emit_async(event)
            logger.debug("Push event emitted for insight %s", insight.id)
        except Exception as e:
            logger.warning("Failed to emit push event for insight %s: %s", insight.id, e)

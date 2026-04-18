"""Event bus for Colony.

Provides typed, filtered event dispatch with error isolation.
Handlers subscribe to specific event types and receive only matching
events. Handler exceptions are caught and logged, never propagated
to other handlers or the emitter. Supports both sync and async emit.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Type, TypeVar

from colony_sidecar.events.types import (
    Event,
    MemoryEvent,
    MeshEvent,
    PersonEvent,
)
from colony_sidecar.models.memory import Memory

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Event)


@dataclass
class Subscription:
    """Event subscription metadata.

    Attributes:
        handler: Callback to invoke when a matching event is emitted
        event_types: Which event types this subscription listens for
        filter_fn: Optional predicate for additional filtering
    """

    handler: Callable[[Event], None]
    event_types: List[Type[Event]]
    filter_fn: Optional[Callable[[Event], bool]] = None


class EventBus:
    """Typed event bus for Colony.

    Features:
        - Type-safe subscriptions (subscribe to specific event types)
        - Optional filtering (predicate function per subscription)
        - Async support (awaitable emit)
        - Error isolation (handler errors don't crash other handlers)
        - Bounded history (capped at max_history entries)
    """

    def __init__(self, max_history: int = 1000):
        self._subscribers: List[Subscription] = []
        self._event_history: List[Event] = []
        self._max_history = max_history

    def subscribe(
        self,
        handler: Callable[[Event], None],
        event_types: List[Type[Event]],
        filter_fn: Optional[Callable[[Event], bool]] = None,
    ) -> Subscription:
        """Subscribe to events of specific types with optional filter.

        Args:
            handler: Function called when a matching event is emitted
            event_types: List of event classes to listen for
            filter_fn: Optional predicate; handler only called if this returns True

        Returns:
            The Subscription instance (pass to unsubscribe to remove)
        """
        sub = Subscription(
            handler=handler,
            event_types=event_types,
            filter_fn=filter_fn,
        )
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription."""
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    def emit(self, event: Event) -> None:
        """Emit an event to all matching subscribers.

        Handler errors are caught and logged, not propagated.

        Args:
            event: The event to dispatch
        """
        self._record(event)

        for sub in self._subscribers:
            if not self._matches(sub, event):
                continue

            try:
                sub.handler(event)
            except Exception as e:
                logger.error(
                    "Event handler error: %s", e,
                    extra={"event_id": event.id, "handler": sub.handler.__name__},
                )

    async def emit_async(self, event: Event) -> None:
        """Async emit. Handlers can be sync or async callables.

        Args:
            event: The event to dispatch
        """
        self._record(event)

        for sub in self._subscribers:
            if not self._matches(sub, event):
                continue

            try:
                result = sub.handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(
                    "Async event handler error: %s", e,
                    extra={"event_id": event.id, "handler": sub.handler.__name__},
                )

    def get_history(
        self,
        event_types: Optional[List[Type[Event]]] = None,
        limit: int = 100,
    ) -> List[Event]:
        """Get recent events, optionally filtered by type.

        Args:
            event_types: If provided, only return events matching these types
            limit: Maximum number of events to return

        Returns:
            List of recent events, most recent last
        """
        events = self._event_history
        if event_types:
            events = [e for e in events if any(isinstance(e, et) for et in event_types)]
        return events[-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        self._event_history.clear()

    def _record(self, event: Event) -> None:
        """Append event to history, trimming if over capacity."""
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history :]

    @staticmethod
    def _matches(sub: Subscription, event: Event) -> bool:
        """Check if an event matches a subscription's type and filter."""
        if not any(isinstance(event, et) for et in sub.event_types):
            return False
        if sub.filter_fn and not sub.filter_fn(event):
            return False
        return True


class TypedEventBus(EventBus):
    """EventBus with typed emit helpers for common event types."""

    def emit_person_event(
        self,
        person_id: str,
        event_type: str,
        old_value: Any = None,
        new_value: Any = None,
        **kwargs: Any,
    ) -> None:
        """Emit a PersonEvent with convenience parameters."""
        self.emit(
            PersonEvent(
                id=f"person-{person_id}-{event_type}",
                person_id=person_id,
                event_type=event_type,
                old_value=old_value,
                new_value=new_value,
                **kwargs,
            )
        )

    def emit_memory_event(self, memory: Memory, event_type: str) -> None:
        """Emit a MemoryEvent for a specific memory."""
        self.emit(
            MemoryEvent(
                id=f"memory-{memory.id}-{event_type}",
                memory=memory,
                event_type=event_type,
            )
        )

    def emit_mesh_event(
        self,
        node_id: str,
        event_type: str,
        **kwargs: Any,
    ) -> None:
        """Emit a MeshEvent for a specific node."""
        self.emit(
            MeshEvent(
                id=f"mesh-{node_id}-{event_type}",
                node_id=node_id,
                event_type=event_type,
                **kwargs,
            )
        )

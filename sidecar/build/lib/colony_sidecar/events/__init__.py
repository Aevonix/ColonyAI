"""Colony event bus.

Typed event dispatch for inter-component communication.
Components emit events when state changes, and other components
subscribe to react. The bus provides type filtering, error isolation,
and bounded history.
"""

from .bus import EventBus, Subscription, TypedEventBus
from .types import (
    CognitionEvent,
    Event,
    IntegrationEvent,
    MemoryEvent,
    MeshEvent,
    PersonEvent,
    SignalEvent,
)

__all__ = [
    # Bus
    "EventBus",
    "TypedEventBus",
    "Subscription",
    # Event types
    "Event",
    "PersonEvent",
    "SignalEvent",
    "MemoryEvent",
    "MeshEvent",
    "CognitionEvent",
    "IntegrationEvent",
]

"""Event type definitions for Colony's event bus.

Typed events carry structured payloads across Colony subsystems.
Each event has an id, timestamp, and source component. Specialized
events add domain-specific fields for people, signals, memories,
mesh nodes, cognition, and external integrations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from colony_sidecar.models.mesh import NodeRole
from colony_sidecar.models.memory import Memory
from colony_sidecar.models.signal import Signal


@dataclass
class Event:
    """Base event class.

    All Colony events inherit from this. Provides identity, timing,
    and source tracking.

    Attributes:
        id: Unique event identifier
        timestamp: When the event occurred
        source: Which component emitted the event
    """

    id: str
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "colony"


@dataclass
class PersonEvent(Event):
    """Person-related events.

    Emitted when a person's profile, tier, score, or interaction
    state changes.

    Attributes:
        person_id: The affected person's identifier
        event_type: What happened ("created", "tier_changed", "score_updated", "interaction")
        old_value: Previous value before the change
        new_value: New value after the change
        context: Additional event-specific metadata
    """

    person_id: str = ""
    event_type: str = ""
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalEvent(Event):
    """Signal collection events.

    Emitted when a new behavioral signal is captured.

    Attributes:
        signal: The captured signal instance
    """

    signal: Optional[Signal] = None


@dataclass
class MemoryEvent(Event):
    """Memory storage events.

    Emitted when memories are created, accessed, strengthened, or decayed.

    Attributes:
        memory: The affected memory instance
        event_type: What happened ("created", "accessed", "strengthened", "decayed")
    """

    memory: Optional[Memory] = None
    event_type: str = ""


@dataclass
class MeshEvent(Event):
    """Mesh networking events.

    Emitted when mesh nodes register, come online/offline, or change roles.

    Attributes:
        node_id: The affected node's identifier
        event_type: What happened ("registered", "online", "offline", "role_changed", "election")
        old_role: Previous node role (for role changes)
        new_role: New node role (for role changes)
        metadata: Additional event-specific data
    """

    node_id: str = ""
    event_type: str = ""
    old_role: Optional[NodeRole] = None
    new_role: Optional[NodeRole] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CognitionEvent(Event):
    """Cognitive system events.

    Emitted by Colony's cognitive subsystems (metalearner, synthesis,
    predictor, etc.) when they detect gaps, adjust strategies, or
    compute metrics.

    Attributes:
        component: Which cognitive component emitted ("metalearner", "synthesis", "predictor")
        event_type: What happened ("gap_detected", "strategy_adjusted", "cpi_computed")
        details: Event-specific structured data
    """

    component: str = ""
    event_type: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IntegrationEvent(Event):
    """External integration events.

    Emitted when external data sources sync, receive data, or error.

    Attributes:
        integration: Which integration ("health", "meetings", "calendar")
        event_type: What happened ("data_received", "sync_complete", "error")
        data: Integration-specific payload
    """

    integration: str = ""
    event_type: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InsightPushEvent(Event):
    """High-urgency insight ready for push delivery."""

    insight_id: str = ""
    description: str = ""
    urgency: float = 1.0
    delivery_channel: str = "push"

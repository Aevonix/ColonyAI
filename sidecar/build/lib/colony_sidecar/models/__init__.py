"""Colony foundation data models.

Core dataclasses for people, signals, memories, and mesh nodes.
These models are the shared vocabulary across all Colony subsystems.
"""

from .memory import Memory, MemoryStrength, MemoryType
from .mesh import MeshNode, NodeRole, NodeStatus
from .person import ContactInfo, ContactType, Person, RelationshipTier
from .signal import Signal, SignalStrength, SignalType

__all__ = [
    # Person & relationships
    "Person",
    "ContactInfo",
    "ContactType",
    "RelationshipTier",
    # Behavioral signals
    "Signal",
    "SignalType",
    "SignalStrength",
    # Memory
    "Memory",
    "MemoryType",
    "MemoryStrength",
    # Mesh
    "MeshNode",
    "NodeRole",
    "NodeStatus",
]

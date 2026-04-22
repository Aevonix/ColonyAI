"""Person and relationship data models.

Core identity and relationship tracking for Colony's social graph.
Each person has a tier (computed from behavioral signals) and contact info
across multiple channels.

Two-layer model
---------------
Colony maintains two distinct layers of relationship data:

- **Colony layer** (``score``, ``tier``): Colony's own evidence-based assessment,
  derived from interaction signals observed across connected gateways. A score
  of 80 means Colony has observed strong, consistent engagement — not that the
  owner designated this person as important.

- **Owner layer** (``notes``, manual tier overrides in TrustTierManager): explicit
  guidance the owner provides to inform Colony's initial orientation. Owner input
  carries weight but does not override Colony's evidence once sufficient history
  exists. The ``notes`` field is an owner-layer input: free-form context the
  owner wants Colony to keep in mind.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class RelationshipTier(str, Enum):
    """Relationship tier based on interaction score.

    Score ranges:
        INNER_CIRCLE: 80-100 — closest relationships
        TRUSTED: 60-79 — reliable, frequent contact
        REGULAR: 30-59 — normal interaction level
        PERIPHERAL: 10-29 — infrequent or new contacts
        SILENCED: manual — explicitly muted by user
    """

    INNER_CIRCLE = "inner_circle"
    TRUSTED = "trusted"
    REGULAR = "regular"
    PERIPHERAL = "peripheral"
    SILENCED = "silenced"


class ContactType(str, Enum):
    """Supported communication channels."""

    PHONE = "phone"
    EMAIL = "email"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"


@dataclass
class ContactInfo:
    """A single contact method for a person.

    Attributes:
        type: Communication channel (phone, email, etc.)
        value: The actual contact identifier (number, address, handle)
        label: Optional label like "personal", "work"
        last_used: When this contact method was last used
    """

    type: ContactType
    value: str
    label: Optional[str] = None
    last_used: Optional[datetime] = None


@dataclass
class Person:
    """A person in Colony's social graph.

    Represents anyone the Colony owner interacts with. The owner themselves
    is represented with is_user=True.

    Attributes:
        id: Unique identifier
        name: Display name
        tier: Relationship tier (computed from score or manually set)
        score: Relationship score 0-100, drives tier computation
        aliases: Alternative names or handles
        contacts: List of contact methods
        first_seen: When this person first appeared in the system
        last_interaction: Most recent interaction timestamp
        notes: Free-form notes about the person
        is_user: True if this represents the Colony owner
    """

    id: str
    name: str
    tier: RelationshipTier = RelationshipTier.PERIPHERAL
    score: float = 0.0  # Colony's evidence-based assessment (0–100); not owner-assigned
    aliases: List[str] = field(default_factory=list)
    contacts: List[ContactInfo] = field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_interaction: Optional[datetime] = None
    notes: Optional[str] = None  # Owner-layer input: context the owner wants Colony to know
    is_user: bool = False

"""Colony Graph Schema — Node and edge type definitions.

Every node and relationship type used in the Colony Neo4j graph is modelled
here as a Pydantic ``BaseModel`` (for validation / serialisation) together
with string‐constant edge types.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────
# Trust tiers (mirrors RelationshipScorer thresholds)
# ──────────────────────────────────────────────────────────────────────

class TrustTier(str, Enum):
    INNER_CIRCLE = "inner_circle"
    TRUSTED = "trusted"
    REGULAR = "regular"
    PERIPHERAL = "peripheral"
    SILENCED = "silenced"


# ──────────────────────────────────────────────────────────────────────
# Node types
# ──────────────────────────────────────────────────────────────────────

class Owner(BaseModel):
    """The Colony owner (singleton node — one per graph)."""

    id: str = Field(..., description="Unique owner identifier")
    name: str
    timezone: str = "UTC"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Person(BaseModel):
    """A person in the owner's relationship network."""

    id: str = Field(..., description="Unique person identifier (UUID)")
    name: str
    tier: TrustTier = TrustTier.PERIPHERAL
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    last_interaction: Optional[datetime] = None
    contact_info: Optional[Dict[str, str]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Memory(BaseModel):
    """An episodic / semantic / procedural memory."""

    id: str = Field(..., description="UUID assigned by Neo4j randomUUID()")
    content: str
    type: str = Field(..., description="e.g. episodic, semantic, procedural")
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    embedding: Optional[List[float]] = None
    metadata: Optional[Dict[str, Any]] = None
    sources: Optional[List[str]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    accessed_at: datetime = Field(default_factory=datetime.utcnow)


class Entity(BaseModel):
    """A named entity extracted from memories (person, place, concept …)."""

    name: str = Field(..., description="Canonical entity name (unique)")
    entity_type: Optional[str] = None  # person, place, org, concept
    first_seen: datetime = Field(default_factory=datetime.utcnow)


class Signal(BaseModel):
    """A single behavioral signal emitted by a Person."""

    id: str
    signal_type: str  # message_length, sentiment, response_latency, …
    raw_value: float
    normalized_value: float
    timestamp: datetime
    source: str  # message, reaction, call
    context: Optional[Dict[str, Any]] = None


class Context(BaseModel):
    """Temporal context attached to a Person (job change, vacation, …)."""

    id: str
    label: str
    description: Optional[str] = None
    start_date: date
    end_date: Optional[date] = None
    active: bool = True


class ScoreEvent(BaseModel):
    """Audit record for a relationship score change."""

    id: str
    score: float
    tier: TrustTier
    delta: float
    reason: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Prediction(BaseModel):
    """A forward‐looking behavioural prediction."""

    id: str
    prediction_type: str  # timing, need, action, trajectory
    description: str
    probability: float = Field(ge=0.0, le=1.0)
    person_id: str
    reasoning: List[str] = Field(default_factory=list)
    expires_at: datetime
    resolved: Optional[bool] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────
# Edge (relationship) types
# ──────────────────────────────────────────────────────────────────────

class EdgeType(str, Enum):
    """All relationship types in the Colony graph."""

    # Owner ↔ Person
    KNOWS = "KNOWS"

    # Memory → Entity
    MENTIONS = "MENTIONS"

    # Person → Signal
    EXHIBITED = "EXHIBITED"

    # Person → Context
    HAS_CONTEXT = "HAS_CONTEXT"

    # Person → ScoreEvent
    SCORE_CHANGED = "SCORE_CHANGED"

    # Person → Prediction
    PREDICTED = "PREDICTED"

    # Memory → Memory (causal / logical chains)
    CAUSED_BY = "CAUSED_BY"
    LED_TO = "LED_TO"
    SUPPORTS = "SUPPORTS"
    MERGED_INTO = "MERGED_INTO"

    # Owner → Memory (ownership)
    REMEMBERS = "REMEMBERS"

    # Memory → Person (memory about a person)
    ABOUT = "ABOUT"


# ──────────────────────────────────────────────────────────────────────
# Convenience exports
# ──────────────────────────────────────────────────────────────────────

NODE_TYPES = (Owner, Person, Memory, Entity, Signal, Context, ScoreEvent, Prediction)
EDGE_TYPES = tuple(EdgeType)

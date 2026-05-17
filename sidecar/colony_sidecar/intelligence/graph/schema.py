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


class Agent(BaseModel):
    """An autonomous entity (Colony itself, or other agents/bots)."""

    id: str = Field(..., description="Unique agent identifier")
    name: str
    version: Optional[str] = None
    status: str = "active"
    capabilities: List[str] = Field(default_factory=list)
    health_score: float = Field(default=1.0, ge=0.0, le=1.0)
    last_tick_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Subsystem(BaseModel):
    """A Colony component that can be monitored and restarted."""

    id: str = Field(..., description="Unique subsystem identifier")
    name: str
    status: str = "active"
    latency_ms: Optional[float] = None
    error_rate: Optional[float] = None
    last_check_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Capability(BaseModel):
    """A tool or skill that Colony has or needs."""

    id: str = Field(..., description="Unique capability identifier")
    name: str
    description: Optional[str] = None
    available: bool = True
    status: str = "available"  # available | deprecated | missing | planned
    failure_count: int = 0
    last_failure_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Project(BaseModel):
    """An active work item."""

    id: str = Field(..., description="Unique project identifier")
    name: str
    description: Optional[str] = None
    status: str = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Goal(BaseModel):
    """A goal or objective."""

    id: str = Field(..., description="Unique goal identifier")
    title: str
    description: Optional[str] = None
    status: str = "active"
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Task(BaseModel):
    """An actionable work unit."""

    id: str = Field(..., description="Unique task identifier")
    title: str
    description: Optional[str] = None
    status: str = "pending"
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Pattern(BaseModel):
    """A recurring behavioral pattern."""

    id: str = Field(..., description="Unique pattern identifier")
    name: str
    description: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    occurrences: int = 0
    pattern_type: str = "behavioral"  # behavioral | workflow | preference | correction
    trigger: Optional[str] = None
    action: Optional[str] = None
    recurrence_count: int = 0
    last_triggered_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Concept(BaseModel):
    """A knowledge domain or concept Colony has encountered."""

    id: str = Field(..., description="Unique concept identifier")
    name: str
    domain: str = "general"  # e.g., "technology", "science", "person"
    description: Optional[str] = None
    confidence_score: float = 0.0  # 0 = unknown, 1 = expert
    encounter_count: int = 0
    last_researched_at: Optional[datetime] = None
    last_encountered_at: Optional[datetime] = None
    source: Optional[str] = None  # "web_search", "tool_failure", "owner_query"
    status: str = "open"  # open | researching | learned | archived
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Preference(BaseModel):
    """A learned preference or behavioral rule."""

    id: str = Field(..., description="Unique preference identifier")
    trigger: str
    expected: str
    source: str = "behavioral_correction"  # behavioral_correction, owner_config, inferred
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None


class InitiativeCategory(BaseModel):
    """A dynamic self-initiative category registered at runtime."""

    id: str = Field(..., description="Unique category identifier")
    name: str
    description: Optional[str] = None
    trigger_query: Optional[str] = None
    action_type: str = "auto_fix"  # auto_fix, propose, research, notify
    executor_skill: str
    priority_formula: Optional[str] = None
    cooldown_minutes: int = 30
    auto_execute: bool = True
    requires_approval: bool = False
    effectiveness_score: float = Field(default=0.5, ge=0.0, le=1.0)
    total_triggered: int = 0
    total_successful: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None


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

    # Agent → Person (agent manages relationship with person)
    MANAGES = "MANAGES"

    # Person → Project (person owns/works on project)
    OWNS = "OWNS"

    # Subsystem → Subsystem (component dependency)
    DEPENDS_ON = "DEPENDS_ON"

    # Agent → Capability (agent has tool)
    HAS_CAPABILITY = "HAS_CAPABILITY"

    # Agent → Capability (agent lacks tool)
    NEEDS_CAPABILITY = "NEEDS_CAPABILITY"

    # Agent → Initiative (agent created initiative)
    GENERATED = "GENERATED"

    # Initiative → Subsystem (initiative targets component)
    TARGETS = "TARGETS"

    # Task → Project (task belongs to project)
    BELONGS_TO = "BELONGS_TO"

    # Goal → Goal (goal blocks another)
    BLOCKS = "BLOCKS"

    # Person → Pattern (person exhibits pattern)
    EXHIBITS = "EXHIBITS"

    # Pattern → InitiativeCategory (pattern triggers category)
    TRIGGERS = "TRIGGERS"


# ──────────────────────────────────────────────────────────────────────
# Convenience exports
# ──────────────────────────────────────────────────────────────────────

NODE_TYPES = (
    Owner, Person, Memory, Entity, Signal, Context, ScoreEvent, Prediction,
    Agent, Subsystem, Capability, Project, Goal, Task, Pattern, Concept, Preference, InitiativeCategory,
)
EDGE_TYPES = tuple(EdgeType)

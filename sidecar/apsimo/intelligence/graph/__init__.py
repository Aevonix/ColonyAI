"""Colony Graph Memory System.

Public API:
    - ``ColonyGraph`` — async Neo4j client for memory storage & recall
    - ``GraphConfig``  — connection settings dataclass
    - ``run_migrations`` — idempotent schema setup
    - Schema types (``Owner``, ``Person``, ``Memory``, ``Entity``, etc.)
    - ``EdgeType`` / ``TrustTier`` enums
    - Cypher query constants in ``queries``
"""

from .client import ColonyGraph, GraphConfig
from .migrations import run_migrations, SCHEMA_V1
from .queries import (
    COMPUTE_RELATIONSHIP_SCORES,
    DECAY_ALL,
    ENSURE_OWNER,
    FIND_NEGLECTED_RELATIONSHIPS,
    FIND_SHARED_ENTITIES,
    GET_PERSON_WITH_SIGNALS,
    LINK_PERSON_TO_OWNER,
    PRUNE_WEAK_MEMORIES,
    RECALL_BY_ENTITY,
    RECALL_BY_TYPE,
    RECORD_SCORE_CHANGE,
    STORE_MEMORY,
    STORE_SIGNAL,
    TOUCH_MEMORY,
    TRAVERSE_MEMORY_CONNECTIONS,
)
from .schema import (
    Context,
    EdgeType,
    Entity,
    Memory,
    Owner,
    Person,
    Prediction,
    ScoreEvent,
    Signal,
    TrustTier,
)

__all__ = [
    # Client
    "ColonyGraph",
    "GraphConfig",
    # Migrations
    "run_migrations",
    "SCHEMA_V1",
    # Schema — nodes
    "Owner",
    "Person",
    "Memory",
    "Entity",
    "Signal",
    "Context",
    "ScoreEvent",
    "Prediction",
    # Schema — enums
    "EdgeType",
    "TrustTier",
    # Query constants
    "STORE_MEMORY",
    "RECALL_BY_ENTITY",
    "RECALL_BY_TYPE",
    "DECAY_ALL",
    "PRUNE_WEAK_MEMORIES",
    "TOUCH_MEMORY",
    "GET_PERSON_WITH_SIGNALS",
    "STORE_SIGNAL",
    "COMPUTE_RELATIONSHIP_SCORES",
    "RECORD_SCORE_CHANGE",
    "FIND_NEGLECTED_RELATIONSHIPS",
    "TRAVERSE_MEMORY_CONNECTIONS",
    "FIND_SHARED_ENTITIES",
    "ENSURE_OWNER",
    "LINK_PERSON_TO_OWNER",
]

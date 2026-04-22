"""Colony World Model — persistent, structured entity graph.

Provides seven entity types (Person, Company, Project, Product, Location,
Event, Concept), entity extraction, resolution/deduplication, and graph
traversal. Backed by SQLite (default) or Neo4j.

Quick start::

    from colony_sidecar.world_model import WorldModelStore, WorldModelConfig

    config = WorldModelConfig(backend="sqlite", sqlite_path=":memory:")
    async with WorldModelStore(config) as store:
        entity = await store.upsert_entity(PersonEntity(
            id="we-000-test",
            name="Alice Chen",
            entity_type="person",
            confidence=0.8,
        ))
        result = await store.find_entities("Alice")
"""

from .store import WorldModelStore, GraphNeighborhoodResult, WorldModelStats
from .config import WorldModelConfig
from .entities import (
    BaseEntity,
    PersonEntity,
    CompanyEntity,
    ProjectEntity,
    ProductEntity,
    LocationEntity,
    EventEntity,
    ConceptEntity,
    ENTITY_CLASS_MAP,
    entity_from_dict,
)
from .relationships import WorldRelationship
from .constants import ENTITY_TYPES, RELATIONSHIP_TYPES, EXTERNAL_ID_KEYS
from .confidence import (
    CONFIDENCE_BY_SOURCE,
    compute_property_confidence,
    boost_confidence,
)

__all__ = [
    # Store
    "WorldModelStore",
    "GraphNeighborhoodResult",
    "WorldModelStats",
    "WorldModelConfig",
    # Entities
    "BaseEntity",
    "PersonEntity",
    "CompanyEntity",
    "ProjectEntity",
    "ProductEntity",
    "LocationEntity",
    "EventEntity",
    "ConceptEntity",
    "ENTITY_CLASS_MAP",
    "entity_from_dict",
    # Relationships
    "WorldRelationship",
    # Constants
    "ENTITY_TYPES",
    "RELATIONSHIP_TYPES",
    "EXTERNAL_ID_KEYS",
    # Confidence
    "CONFIDENCE_BY_SOURCE",
    "compute_property_confidence",
    "boost_confidence",
]

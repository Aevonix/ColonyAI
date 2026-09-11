"""World Model entity dataclasses.

Seven entity types form Colony's world model:
  Person, Company, Project, Product, Location, Event, Concept

All share BaseEntity fields. Each adds domain-specific properties.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from datetime import datetime


@dataclass
class BaseEntity:
    """Common properties for all world model entities."""
    id: str                          # we-<timestamp>-<random7>
    name: str                        # canonical display name
    entity_type: str                 # EntityType literal
    aliases: List[str] = field(default_factory=list)
    external_ids: Dict[str, str] = field(default_factory=dict)
    confidence: float = 0.5          # 0.0–1.0 overall entity confidence
    properties: Dict[str, Any] = field(default_factory=dict)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class PersonEntity(BaseEntity):
    """A person Colony has encountered or knows about."""
    entity_type: str = "person"
    email: Optional[str] = None
    phone: Optional[str] = None
    title: Optional[str] = None       # "CTO", "Engineer"
    bio_summary: Optional[str] = None
    linkedin_url: Optional[str] = None
    twitter_handle: Optional[str] = None
    location_id: Optional[str] = None  # FK to LocationEntity


@dataclass
class CompanyEntity(BaseEntity):
    """An organization of any kind."""
    entity_type: str = "company"
    domain: Optional[str] = None       # "acme.com"
    industry: Optional[str] = None     # "SaaS", "Healthcare"
    size_range: Optional[str] = None   # "1-10", "11-50", "51-200", "201-1000", "1000+"
    founded_year: Optional[int] = None
    headquarters_id: Optional[str] = None  # FK to LocationEntity
    linkedin_url: Optional[str] = None
    crunchbase_url: Optional[str] = None
    ticker: Optional[str] = None       # "AAPL", "GOOG"
    description: Optional[str] = None


@dataclass
class ProjectEntity(BaseEntity):
    """A bounded initiative with participants and a goal."""
    entity_type: str = "project"
    description: Optional[str] = None
    status: Optional[str] = None       # "active", "completed", "paused", "cancelled"
    start_date: Optional[str] = None   # ISO8601
    end_date: Optional[str] = None     # ISO8601; None = ongoing
    owner_id: Optional[str] = None     # FK to PersonEntity
    company_id: Optional[str] = None   # FK to CompanyEntity


@dataclass
class ProductEntity(BaseEntity):
    """A product, service, or software artifact."""
    entity_type: str = "product"
    category: Optional[str] = None     # "SaaS", "hardware", "API"
    version: Optional[str] = None
    url: Optional[str] = None
    company_id: Optional[str] = None   # FK to CompanyEntity
    description: Optional[str] = None


@dataclass
class LocationEntity(BaseEntity):
    """A geographic or virtual place."""
    entity_type: str = "location"
    location_type: Optional[str] = None  # "city", "country", "office", "virtual"
    city: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None      # "America/Los_Angeles"


@dataclass
class EventEntity(BaseEntity):
    """A time-bounded occurrence."""
    entity_type: str = "event"
    event_type: Optional[str] = None    # "meeting", "conference", "deadline", "launch"
    start_time: Optional[str] = None    # ISO8601
    end_time: Optional[str] = None      # ISO8601
    location_id: Optional[str] = None
    organizer_id: Optional[str] = None  # FK to PersonEntity or CompanyEntity
    url: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ConceptEntity(BaseEntity):
    """An abstract topic, technology, or category."""
    entity_type: str = "concept"
    concept_type: Optional[str] = None   # "technology", "methodology", "regulation", "topic"
    description: Optional[str] = None
    parent_concept_id: Optional[str] = None  # for hierarchical concepts


# Type union for all entity types
EntityType = (
    Union[PersonEntity, CompanyEntity, ProjectEntity,
    ProductEntity, LocationEntity, EventEntity, ConceptEntity]
)

# Map from entity_type string to dataclass
ENTITY_CLASS_MAP: Dict[str, type] = {
    "person": PersonEntity,
    "company": CompanyEntity,
    "project": ProjectEntity,
    "product": ProductEntity,
    "location": LocationEntity,
    "event": EventEntity,
    "concept": ConceptEntity,
}


def entity_from_dict(data: Dict[str, Any]) -> BaseEntity:
    """Reconstruct an entity from a flat dict (e.g., from SQLite row).

    Maps 'entity_type' to the appropriate subclass and constructs it.
    Unknown types fall back to BaseEntity.
    """
    entity_type = data.get("entity_type", "")
    cls = ENTITY_CLASS_MAP.get(entity_type, BaseEntity)
    # Filter data keys to only those accepted by the dataclass
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)

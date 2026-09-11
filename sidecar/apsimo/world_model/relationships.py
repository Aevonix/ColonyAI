"""World Model relationship dataclass."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class WorldRelationship:
    """A directed relationship between two world model entities."""
    id: str                           # wr-<timestamp>-<random7>
    source_id: str
    target_id: str
    relationship_type: str            # WM_ prefixed type from constants
    confidence: float = 0.5           # 0.0–1.0
    valid_from: Optional[str] = None  # ISO8601; when relationship began
    valid_to: Optional[str] = None    # ISO8601; None = currently active
    properties: Dict[str, Any] = field(default_factory=dict)
    source_observation_id: Optional[str] = None  # extraction provenance
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def is_active(self) -> bool:
        """True if the relationship has no end date (currently active)."""
        return self.valid_to is None

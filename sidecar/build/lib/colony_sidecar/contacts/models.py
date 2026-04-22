"""Colony Contacts — data model dataclasses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Trust tiers ──────────────────────────────────────────────────────────────

TRUST_TIERS = ("inner_circle", "trusted", "regular", "peripheral", "silenced", "unknown")

# Higher index = more permissive
_TIER_RANK = {t: i for i, t in enumerate(("unknown", "silenced", "peripheral", "regular", "trusted", "inner_circle"))}

PRIVACY_LEVELS = ("public", "private", "restricted")
GATEWAYS = ("imessage", "telegram", "email", "sms", "signal", "custom", "internal")

# Default interaction_allowed per trust tier (spec §8.1)
TIER_DEFAULT_INTERACTION: Dict[str, bool] = {
    "inner_circle": True,
    "trusted": True,
    "regular": True,
    "peripheral": False,
    "silenced": False,
    "unknown": True,
}


def more_permissive_tier(a: str, b: str) -> str:
    """Return the more permissive (higher-ranked) trust tier."""
    return a if _TIER_RANK.get(a, 0) >= _TIER_RANK.get(b, 0) else b


def more_restrictive_privacy(a: str, b: str) -> str:
    """Return the more restrictive privacy level."""
    rank = {"public": 0, "private": 1, "restricted": 2}
    return a if rank.get(a, 1) >= rank.get(b, 1) else b


# ── Core entities ─────────────────────────────────────────────────────────────

@dataclass
class Contact:
    """A person known to Colony."""
    contact_id: str
    display_name: Optional[str]
    given_name: Optional[str]
    family_name: Optional[str]
    organization: Optional[str]
    relationship_score: float
    trust_tier: str
    interaction_allowed: bool
    tags: List[str]
    privacy_level: str
    person_node_id: Optional[str]
    notes: Optional[str]
    import_source: str
    first_seen_at: str
    last_interaction_at: Optional[str]
    interaction_count: int
    enrichment_source: List[str]
    enrichment_last_at: Optional[str]
    deleted_at: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "display_name": self.display_name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "organization": self.organization,
            "relationship_score": self.relationship_score,
            "trust_tier": self.trust_tier,
            "interaction_allowed": self.interaction_allowed,
            "tags": self.tags,
            "privacy_level": self.privacy_level,
            "person_node_id": self.person_node_id,
            "notes": self.notes,
            "import_source": self.import_source,
            "first_seen_at": self.first_seen_at,
            "last_interaction_at": self.last_interaction_at,
            "interaction_count": self.interaction_count,
            "enrichment_source": self.enrichment_source,
            "enrichment_last_at": self.enrichment_last_at,
            "deleted_at": self.deleted_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Contact":
        tags_json = row.get("tags_json", "[]")
        tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        enrichment_source_json = row.get("enrichment_source", "[]")
        enrichment_source = (
            json.loads(enrichment_source_json)
            if isinstance(enrichment_source_json, str)
            else (enrichment_source_json or [])
        )
        return cls(
            contact_id=row["contact_id"],
            display_name=row.get("display_name"),
            given_name=row.get("given_name"),
            family_name=row.get("family_name"),
            organization=row.get("organization"),
            relationship_score=float(row.get("relationship_score", 0.0)),
            trust_tier=row.get("trust_tier", "unknown"),
            interaction_allowed=bool(row.get("interaction_allowed", 1)),
            tags=tags,
            privacy_level=row.get("privacy_level", "private"),
            person_node_id=row.get("person_node_id"),
            notes=row.get("notes"),
            import_source=row.get("import_source", "manual"),
            first_seen_at=row.get("first_seen_at", ""),
            last_interaction_at=row.get("last_interaction_at"),
            interaction_count=int(row.get("interaction_count", 0)),
            enrichment_source=enrichment_source,
            enrichment_last_at=row.get("enrichment_last_at"),
            deleted_at=row.get("deleted_at"),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


@dataclass
class ContactHandle:
    """A gateway-specific address for a contact."""
    handle_id: str
    contact_id: str
    gateway: str
    address: str
    is_primary: bool
    verified: bool
    confidence: float
    source: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "contact_id": self.contact_id,
            "gateway": self.gateway,
            "address": self.address,
            "is_primary": self.is_primary,
            "verified": self.verified,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "ContactHandle":
        return cls(
            handle_id=row["handle_id"],
            contact_id=row["contact_id"],
            gateway=row["gateway"],
            address=row["address"],
            is_primary=bool(row.get("is_primary", 0)),
            verified=bool(row.get("verified", 0)),
            confidence=float(row.get("confidence", 1.0)),
            source=row.get("source", "manual"),
            created_at=row.get("created_at", ""),
        )


@dataclass
class MergeProposal:
    """A pending or resolved merge proposal."""
    id: str
    contact_id_a: str
    contact_id_b: str
    confidence: float
    reason: str
    status: str  # pending, approved, rejected, auto_merged
    proposed_at: str
    resolved_at: Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "MergeProposal":
        return cls(
            id=row["id"],
            contact_id_a=row["contact_id_a"],
            contact_id_b=row["contact_id_b"],
            confidence=float(row["confidence"]),
            reason=row["reason"],
            status=row["status"],
            proposed_at=row["proposed_at"],
            resolved_at=row.get("resolved_at"),
        )


@dataclass
class MergeAuditRecord:
    """Immutable record of a completed merge operation."""
    audit_id: str
    canonical_id: str
    absorbed_id: str
    confidence: float
    merge_reason: str
    triggered_by: str
    contact_a_snapshot: Dict[str, Any]
    contact_b_snapshot: Dict[str, Any]
    merged_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.merged_at is None:
            self.merged_at = datetime.now(timezone.utc)

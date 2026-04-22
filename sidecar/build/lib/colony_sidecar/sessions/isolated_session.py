"""IsolatedSession — per-contact sealed context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier


class SessionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"
    SILENCED = "silenced"
    NEEDS_OWNER_REVIEW = "needs_owner_review"
    ESCALATION_PENDING = "escalation_pending"


@dataclass
class ConversationTurn:
    turn_id: str
    role: str               # "contact" | "colony"
    content: str
    timestamp: datetime
    gate_decision: Optional[str] = None   # result code if this was an outbound turn


_DEFAULT_TTL_HOURS = 24


@dataclass
class IsolatedSession:
    session_id: str
    contact_id: str
    gateway: str
    trust_tier: TrustTier
    state: SessionState = SessionState.ACTIVE
    history: list = field(default_factory=list)
    active_topics: set = field(default_factory=set)
    mentioned_entities: set = field(default_factory=set)
    context_start: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    context_token_count: int = 0
    last_active: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    relationship_score: float = 0.0
    owner_guidance: dict = field(default_factory=dict)
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc) + timedelta(hours=_DEFAULT_TTL_HOURS)
    )
    model_override: Optional[str] = None
    model_provider_override: Optional[str] = None

    @classmethod
    def create(
        cls,
        contact_id: str,
        gateway: str,
        trust_tier: TrustTier,
        ttl_hours: int = _DEFAULT_TTL_HOURS,
    ) -> "IsolatedSession":
        return cls(
            session_id=str(uuid.uuid4()),
            contact_id=contact_id,
            gateway=gateway,
            trust_tier=trust_tier,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=ttl_hours),
        )

    def is_expired(self) -> bool:
        return datetime.now(tz=timezone.utc) >= self.expires_at

    def extend(self, ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
        """Reset expiry on activity (token rotation equivalent for stateful sessions)."""
        self.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=ttl_hours)
        self.last_active = datetime.now(tz=timezone.utc)

    def add_entity(self, entity: str) -> None:
        """Add an entity to this session's mention set."""
        self.mentioned_entities.add(entity.lower().strip())

    def get_mentioned_entities_snapshot(self) -> frozenset:
        """Return an immutable snapshot for gate cross-context checking."""
        return frozenset(self.mentioned_entities)

    def add_turn(self, role: str, content: str, gate_decision: Optional[str] = None) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            role=role,
            content=content,
            timestamp=datetime.now(tz=timezone.utc),
            gate_decision=gate_decision,
        )
        self.history.append(turn)
        self.last_active = turn.timestamp
        return turn

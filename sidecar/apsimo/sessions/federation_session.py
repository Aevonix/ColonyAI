"""FederationSession — colony-to-colony isolated session."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class FederationSessionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"
    TRUST_SUSPENDED = "trust_suspended"


@dataclass
class FederationTurn:
    turn_id: str
    role: str               # "local" | "remote"
    content: str
    timestamp: datetime
    gate_decision: Optional[str] = None


@dataclass
class FederationSession:
    """Colony-to-colony session. Contains ONLY bilateral negotiation content.

    MUST NOT contain:
    - Content from contact sessions.
    - Internal deliberation memories (COLONY_ONLY).
    - Any information about local contacts.
    """
    session_id: str
    local_colony_id: str
    remote_colony_id: str
    federated_trust_tier: str           # e.g. "trusted_colony" | "untrusted" | "newly_paired"
    state: FederationSessionState = FederationSessionState.ACTIVE
    history: list = field(default_factory=list)
    mentioned_entities: set = field(default_factory=set)
    shared_memory_ids: list = field(default_factory=list)  # explicitly consented objects
    trust_attestations: list = field(default_factory=list)  # on-chain, read-only
    last_active: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @classmethod
    def create(
        cls,
        local_colony_id: str,
        remote_colony_id: str,
        federated_trust_tier: str = "newly_paired",
    ) -> "FederationSession":
        return cls(
            session_id=str(uuid.uuid4()),
            local_colony_id=local_colony_id,
            remote_colony_id=remote_colony_id,
            federated_trust_tier=federated_trust_tier,
        )

    def add_turn(self, role: str, content: str, gate_decision: Optional[str] = None) -> FederationTurn:
        turn = FederationTurn(
            turn_id=str(uuid.uuid4()),
            role=role,
            content=content,
            timestamp=datetime.now(tz=timezone.utc),
            gate_decision=gate_decision,
        )
        self.history.append(turn)
        self.last_active = turn.timestamp
        return turn

    def get_mentioned_entities_snapshot(self) -> frozenset:
        return frozenset(self.mentioned_entities)

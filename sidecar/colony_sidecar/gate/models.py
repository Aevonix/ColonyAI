"""Gate data models — GatePayload, GateDecision, DispatchResult."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier


class GateResultCode(str, Enum):
    PASS = "pass"
    BLOCK_RECIPIENT = "block_recipient"
    BLOCK_PII = "block_pii"
    BLOCK_CROSS_CONTEXT = "block_cross_context"
    BLOCK_TRUST_TIER = "block_trust_tier"
    BLOCK_INJECTION = "block_injection"
    BLOCK_REVIEW = "block_review"
    PENDING_DELAY = "pending_delay"


@dataclass
class GatePayload:
    """The only view of a response that the gate pipeline sees.

    MUST NOT contain any workspace or reasoning content.
    Fields are immutable after construction.
    """
    response_text: str
    target_contact_id: str
    target_gateway: str
    session_id: str
    trust_tier: TrustTier
    mentioned_entities: frozenset  # entities from THIS session's history
    turn_id: str
    incoming_message_text: str     # original incoming message (for Layer 5)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class GateDecision:
    """Result of gate pipeline evaluation."""
    payload_turn_id: str
    result_code: GateResultCode
    blocked: bool
    blocking_layer: Optional[int]           # 1–7, None if PASS
    block_reason: Optional[str]             # human-readable, used in rejection
    flagged_excerpt: Optional[str]          # redacted excerpt for rejection feedback
    layer_results: dict                     # per-layer detail for audit log
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class DispatchResult:
    blocked: bool
    reason: Optional[str] = None
    gate_decision: Optional[GateDecision] = None

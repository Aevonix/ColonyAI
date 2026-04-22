"""GateAuditLog — append-only audit log for all gate decisions."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from colony_sidecar.gate.models import GateDecision


@dataclass
class GateAuditRecord:
    record_id: str
    turn_id: str
    session_id: str
    contact_id: str
    result_code: str
    blocked: bool
    blocking_layer: Optional[int]
    block_reason: Optional[str]
    layer_results: dict
    sensitivity_level: str
    overrides_applied: list
    evaluated_at: datetime
    response_text_hash: str        # SHA-256 of response_text (not plaintext)


class GateAuditLog(Protocol):
    """Append-only audit log interface."""

    async def record(self, decision: GateDecision) -> None:
        """Write a gate decision to the audit log before dispatch."""
        ...


class InMemoryAuditLog:
    """In-memory audit log for testing."""

    def __init__(self) -> None:
        self.records: list[GateAuditRecord] = []

    async def record(self, decision: GateDecision) -> None:
        rec = GateAuditRecord(
            record_id=str(uuid.uuid4()),
            turn_id=decision.payload_turn_id,
            session_id=decision.layer_results.get("_session_id", ""),
            contact_id=decision.layer_results.get("_contact_id", ""),
            result_code=decision.result_code.value,
            blocked=decision.blocked,
            blocking_layer=decision.blocking_layer,
            block_reason=decision.block_reason,
            layer_results=decision.layer_results,
            sensitivity_level=decision.layer_results.get("_sensitivity", "standard"),
            overrides_applied=decision.layer_results.get("_overrides_applied", []),
            evaluated_at=decision.evaluated_at,
            response_text_hash=decision.layer_results.get("_response_hash", ""),
        )
        self.records.append(rec)

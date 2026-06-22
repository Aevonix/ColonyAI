"""Context durability and freshness rules per initiative type (v0.16.0).

Context snapshots age differently per domain. ``days_since_contact`` is
still true hours later; ``"CI failing on main"`` or ``"meeting in 30
min"`` can become false while the initiative sits in the queue. Each
initiative type therefore declares its context durability:

- ``durable`` — snapshot at creation, safe to persist and read later.
- ``volatile`` — must be refreshed at read time, or checked against a
  freshness TTL before the agent acts on it.

The agent-side contract: a volatile initiative's context carries
``context_captured_at`` (stamped by the autonomy loop). Before acting,
the agent checks ``is_context_fresh()``; if stale, it calls
``POST /v1/host/initiatives/{id}/context/refresh`` which routes to the
engine's per-entity ``rebuild_context()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

DURABLE = "durable"
VOLATILE = "volatile"

# Durability by InitiativeType value. Unlisted types default to durable
# with a conservative short TTL applied by callers that care.
CONTEXT_DURABILITY: Dict[str, str] = {
    # Existing types
    "follow_up": DURABLE,
    "relationship": DURABLE,
    "introduction": DURABLE,
    "health": VOLATILE,
    "scheduling": DURABLE,
    "coding": VOLATILE,
    "subsystem_health": VOLATILE,
    "data_quality": VOLATILE,
    "operational": DURABLE,
    "capability_gap": DURABLE,
    "knowledge_acquisition": DURABLE,
    "behavioral_correction": DURABLE,
    "agent_action": VOLATILE,
    # Autonomous work domains (v0.16.0)
    "commitment": DURABLE,
    "calendar": VOLATILE,
    "research": DURABLE,
    "task": DURABLE,
    "project": DURABLE,
    "system": VOLATILE,
}

# Freshness TTL (seconds) for volatile types: how old a context snapshot
# may be before the agent must refresh it.
CONTEXT_FRESHNESS_TTL_SECONDS: Dict[str, int] = {
    "calendar": 300,
    "coding": 600,
    "system": 300,
    "health": 900,
    "subsystem_health": 600,
    "data_quality": 3600,
    "agent_action": 600,
}

_DEFAULT_VOLATILE_TTL = 600


def durability_for(type_value: str) -> str:
    """Declared durability for an initiative type ('durable'/'volatile')."""
    return CONTEXT_DURABILITY.get(type_value, DURABLE)


def freshness_ttl_for(type_value: str) -> Optional[int]:
    """TTL in seconds for volatile types; None for durable types."""
    if durability_for(type_value) == DURABLE:
        return None
    return CONTEXT_FRESHNESS_TTL_SECONDS.get(type_value, _DEFAULT_VOLATILE_TTL)


def is_context_fresh(
    type_value: str,
    captured_at: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Whether a context snapshot is still safe to act on.

    Durable contexts are always fresh. Volatile contexts without a
    capture timestamp are treated as stale (fail closed).
    """
    ttl = freshness_ttl_for(type_value)
    if ttl is None:
        return True
    if not captured_at:
        return False
    try:
        stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - stamp).total_seconds() <= ttl

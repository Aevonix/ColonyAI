"""Self-sufficient relationship scoring from interaction history + affect.

The behavioral-signal scorer (intelligence/relationships/scorer.py) depends on
:Signal nodes EXHIBITED by a :Person within a window; when that pipeline is sparse
it returns a flat default that is never persisted, leaving every contact's
relationship_score at 0.0. This module derives a meaningful 0..1 closeness score
from data that is ALWAYS reliably tracked — interaction recency + frequency, the
contact's current affect, and the owner-set trust tier — so every contact has a
live, sensible score independent of the behavioral-signal graph.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

# Owner-set standing → base closeness (the "owner guidance" layer).
_TIER_BASE = {
    "inner_circle": 0.92,
    "trusted": 0.72,
    "regular": 0.50,
    "acquaintance": 0.42,
    "peripheral": 0.30,
    "unknown": 0.32,
    "silenced": 0.10,
}


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def compute_relationship_score(
    contact: Any,
    affect_state: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> float:
    """Return a 0.0..1.0 relationship closeness score.

    Blend (weights sum to 1.0):
      0.30 owner trust tier — the owner's explicit standing for this contact
      0.25 recency          — exp decay, ~21-day half-life since last interaction
      0.25 frequency        — log-scaled interaction count (saturates near ~50)
      0.20 affect           — contact's current emotional valence (neutral = 0.5)
    """
    now = now or datetime.now(timezone.utc)

    tier = getattr(contact, "trust_tier", None) or "unknown"
    tier_base = _TIER_BASE.get(tier, 0.40)

    recency = 0.0
    last = _parse_ts(getattr(contact, "last_interaction_at", None))
    if last is not None:
        days = max(0.0, (now - last).total_seconds() / 86400.0)
        recency = math.exp(-days / 21.0)

    count = max(0, int(getattr(contact, "interaction_count", 0) or 0))
    frequency = min(1.0, math.log1p(count) / math.log1p(50))

    affect = 0.5
    if affect_state:
        v = affect_state.get("current_valence", affect_state.get("valence"))
        if v is not None:
            affect = max(0.0, min(1.0, (float(v) + 1.0) / 2.0))

    score = 0.30 * tier_base + 0.25 * recency + 0.25 * frequency + 0.20 * affect
    return round(max(0.0, min(1.0, score)), 4)


def closeness_label(score: float) -> str:
    """Human label for a 0..1 closeness score."""
    if score >= 0.80:
        return "very close"
    if score >= 0.60:
        return "close"
    if score >= 0.40:
        return "familiar"
    if score >= 0.20:
        return "acquaintance"
    return "distant"

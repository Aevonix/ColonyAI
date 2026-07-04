"""Conflict resolution: recency > confidence > source-trust.

Ties on all three axes (within epsilons) are genuinely unresolvable and go
to the owner as an internal review initiative instead of a silent pick.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from colony_sidecar.beliefs.models import Claim

# Generic source-trust ranking. Deployment override:
# COLONY_SOURCE_TRUST="owner:1.0,connector:0.7,inference:0.3"
_DEFAULT_TRUST: Dict[str, float] = {
    "owner": 1.0,
    "user_assertion": 1.0,
    "world_model": 0.7,
    "connector": 0.7,
    "conversation": 0.6,
    "file": 0.6,
    "tool_output": 0.5,
    "inference": 0.35,
}

_RECENCY_EPS_SECS = 86400.0   # claims within a day are "equally recent"
_CONF_EPS = 0.05
_TRUST_EPS = 0.05


def source_trust(source: str) -> float:
    table = dict(_DEFAULT_TRUST)
    raw = os.environ.get("COLONY_SOURCE_TRUST", "")
    for part in raw.split(","):
        if ":" in part:
            k, _, v = part.partition(":")
            try:
                table[k.strip().lower()] = float(v)
            except (TypeError, ValueError):
                continue
    return table.get((source or "").strip().lower(), 0.4)


def pick_winner(a: Claim, b: Claim) -> Optional[Tuple[Claim, Claim]]:
    """(winner, loser), or None when genuinely unresolvable.

    Ordering: recency first (a clearly newer claim supersedes), then
    confidence, then source trust.
    """
    if abs((a.ts or 0.0) - (b.ts or 0.0)) > _RECENCY_EPS_SECS:
        return (a, b) if (a.ts or 0.0) > (b.ts or 0.0) else (b, a)
    if abs(a.confidence - b.confidence) > _CONF_EPS:
        return (a, b) if a.confidence > b.confidence else (b, a)
    ta, tb = source_trust(a.source), source_trust(b.source)
    if abs(ta - tb) > _TRUST_EPS:
        return (a, b) if ta > tb else (b, a)
    return None

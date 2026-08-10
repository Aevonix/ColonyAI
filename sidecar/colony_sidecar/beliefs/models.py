"""Belief-maintenance data model."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def beliefs_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_BELIEFS_MODE", ("off", "shadow", "live"), "shadow")


def beliefs_supervised_live() -> bool:
    """Is the 'supervised' rung unlocked for beliefs? At trust stage
    ask_first the engine may perform REVERSIBLE mutations only
    (supersession that preserves the old value on the superseded node, and
    bounded stale-confidence decay) and records real (non-shadow) outcomes,
    so the beliefs domain can build the track record ask_first -> act_first
    graduation requires. Default off = today's exact behavior.

    H1.2: delegates to the generic rung (self_model/supervised.py) —
    COLONY_SUPERVISED_LIVE_DOMAINS=beliefs and the legacy
    COLONY_BELIEFS_SUPERVISED_LIVE=1 are equivalent."""
    from colony_sidecar.self_model.supervised import supervised_enabled
    return supervised_enabled("beliefs")


def stale_ttl_days() -> float:
    try:
        return max(1.0, float(os.environ.get("COLONY_BELIEFS_STALE_DAYS", "90")))
    except (TypeError, ValueError):
        return 90.0


_NORM_RE = re.compile(r"[^a-z0-9]+")


def norm_value(v: Any) -> str:
    """Comparison-normalized value: lowercase alphanumerics."""
    return _NORM_RE.sub(" ", str(v or "").strip().lower()).strip()


@dataclass
class Claim:
    """One structured belief: subject has predicate = value."""
    subject: str
    predicate: str
    value: str
    confidence: float = 0.5
    ts: float = 0.0                 # when asserted/observed (epoch)
    source: str = "inference"       # source-trust key
    ref: str = ""                   # memory id / entity id it came from
    scope: str = "graph"            # graph | world_model
    meta: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple:
        return (norm_value(self.subject), norm_value(self.predicate))

    def same_value(self, other: "Claim") -> bool:
        a, b = norm_value(self.value), norm_value(other.value)
        if a == b:
            return True
        # containment tolerance: "acme" vs "acme corp"
        return bool(a and b and (a in b or b in a))

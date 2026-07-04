"""Belief maintenance (item 7): drive the graph's epistemic scaffolding.

Detects contradictions (same subject + predicate, conflicting value) across
world-model properties and graph memories, resolves them by recency >
confidence > source-trust (marking losers superseded, with an audit trail),
surfaces genuinely unresolvable conflicts as internal review initiatives,
and decays stale world-state confidence past its TTL.

Modes (COLONY_BELIEFS_MODE, default shadow = calibration): shadow detects,
records and surfaces review initiatives without mutating epistemic state;
live also resolves and decays. Resolutions are journaled (Amendment 1.4).
"""

from colony_sidecar.beliefs.models import Claim, beliefs_mode
from colony_sidecar.beliefs.store import BeliefStore
from colony_sidecar.beliefs.contradictions import detect_conflicts, claims_from_text
from colony_sidecar.beliefs.resolve import pick_winner, source_trust
from colony_sidecar.beliefs.engine import BeliefEngine

__all__ = [
    "Claim", "BeliefStore", "BeliefEngine", "detect_conflicts",
    "claims_from_text", "pick_winner", "source_trust", "beliefs_mode",
]

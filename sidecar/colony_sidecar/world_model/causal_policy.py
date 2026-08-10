"""Causal-edge policy: causal edges are QUERY-ONLY unless explicitly unlocked.

THE INVARIANT
    A causal edge (WM_CAUSES / WM_ENABLES / WM_BLOCKS / WM_INHIBITS, see
    ``CAUSAL_RELATIONSHIP_TYPES``) may be stored, corroborated, decayed and
    read back to answer "why" / "what happens if" questions — but it must
    NEVER, on its own, cause Colony to take or schedule an action. An
    inferred "A causes B" is a belief about the world, not an instruction;
    acting on a fabricated or stale causal edge is exactly the failure mode
    this module exists to prevent.

Every consumer that reads causal edges and could turn them into behavior
(planners, initiative generators, directed actions) MUST gate that path on
``causal_edges_actionable()``. The flag COLONY_CAUSAL_ACT defaults to off,
so the invariant holds by default; flipping it is an explicit owner
decision, revertible via env alone.

This module is intentionally tiny and dependency-free so any layer can
import it without cycles.
"""

from __future__ import annotations

import os

from colony_sidecar.world_model.constants import CAUSAL_RELATIONSHIP_TYPES

__all__ = ["CAUSAL_RELATIONSHIP_TYPES", "causal_edges_actionable", "is_causal"]


def causal_edges_actionable() -> bool:
    """May causal edges influence ACTIONS (not just answers)? Default no.

    COLONY_CAUSAL_ACT=1 is the only unlock; anything else (unset, 0, junk)
    keeps causal edges query-only.
    """
    return os.environ.get("COLONY_CAUSAL_ACT", "0").strip().lower() in (
        "1", "true", "yes", "on")


def is_causal(rel_type: str) -> bool:
    """True when ``rel_type`` is one of the causal relationship types."""
    return str(rel_type or "").strip().upper() in CAUSAL_RELATIONSHIP_TYPES

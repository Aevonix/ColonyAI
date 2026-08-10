"""Causal-edge maintenance primitives (H2.3): contradiction + staleness.

Two fabrication controls for the causal layer, consumed by the
BeliefEngine's periodic run:

1. CONTRADICTION: a positive causal claim (WM_CAUSES / WM_ENABLES) and a
   negative one (WM_BLOCKS / WM_INHIBITS) between the SAME ordered entity
   pair cannot both be right. Opposing pairs become conflict rows + review
   initiatives and are NEVER auto-resolved — not at any trust stage, not in
   live mode. Recency/confidence ordering is how belief conflicts are
   settled, but a causal claim is a model of the world, and silently picking
   a winner would let one confidently-worded fabrication erase a true edge.
   A human (or an explicitly owner-approved flow) closes these.

2. STALENESS: a causal edge that has gone COLONY_CAUSAL_TTL_DAYS (default
   120) without support (creation or corroboration) loses 0.05 confidence
   per maintenance run, floored at 0.2 — never deleted. Staleness is judged
   by the ``last_support_at`` property (stamped by the extractor on create/
   corroborate) so that the decay write itself cannot reset the clock.

This module is pure computation + read-only store access; every WRITE stays
in the BeliefEngine where it is mode-gated (live/supervised) and journaled.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from colony_sidecar.world_model.constants import CAUSAL_RELATIONSHIP_TYPES

logger = logging.getLogger(__name__)

POSITIVE_CAUSAL = frozenset({"WM_CAUSES", "WM_ENABLES"})
NEGATIVE_CAUSAL = frozenset({"WM_BLOCKS", "WM_INHIBITS"})

CAUSAL_DECAY_STEP = 0.05
CAUSAL_DECAY_FLOOR = 0.2

# The property the extractor stamps on every create/corroborate; the decay
# clock reads it (falling back to updated_at/created_at for pre-existing
# edges) and deliberately never advances it on a decay write.
LAST_SUPPORT_PROP = "last_support_at"


def causal_ttl_days() -> float:
    """COLONY_CAUSAL_TTL_DAYS (default 120): days without support before a
    causal edge starts decaying."""
    try:
        v = float(os.environ.get("COLONY_CAUSAL_TTL_DAYS", "120"))
        return v if v > 0 else 120.0
    except (TypeError, ValueError):
        return 120.0


async def load_causal_edges(store: Any, limit_per_type: int = 500) -> List[Any]:
    """All causal edges, via explicitly-typed queries (the only sanctioned
    read path — causal edges are excluded from generic reads by policy)."""
    out: List[Any] = []
    for rel_type in sorted(CAUSAL_RELATIONSHIP_TYPES):
        try:
            rels = await store.query_relationships(
                relationship_type=rel_type, min_confidence=0.0,
                limit=limit_per_type)
        except Exception:
            logger.debug("causal edge load failed for %s", rel_type,
                         exc_info=True)
            continue
        out.extend(rels or [])
    return out


def opposing_pairs(edges: List[Any]) -> List[Tuple[Any, Any]]:
    """(positive_edge, negative_edge) pairs over the same ordered
    (source_id, target_id) — the causal contradictions. Inactive edges
    (valid_to set) are ignored: a closed edge no longer claims anything."""
    by_pair: Dict[Tuple[str, str], Dict[str, List[Any]]] = {}
    for e in edges:
        if getattr(e, "valid_to", None) is not None:
            continue
        rel = str(getattr(e, "relationship_type", "") or "").upper()
        if rel in POSITIVE_CAUSAL:
            polarity = "pos"
        elif rel in NEGATIVE_CAUSAL:
            polarity = "neg"
        else:
            continue
        key = (str(e.source_id), str(e.target_id))
        by_pair.setdefault(key, {"pos": [], "neg": []})[polarity].append(e)
    pairs: List[Tuple[Any, Any]] = []
    for buckets in by_pair.values():
        for pos in buckets["pos"]:
            for neg in buckets["neg"]:
                pairs.append((pos, neg))
    return pairs


def support_timestamp(edge: Any) -> str:
    """The edge's last-support timestamp (ISO8601), preferring the
    extractor-stamped property over store bookkeeping columns."""
    props = getattr(edge, "properties", None) or {}
    return str(props.get(LAST_SUPPORT_PROP)
               or getattr(edge, "updated_at", None)
               or getattr(edge, "created_at", None) or "")


def support_age_days(edge: Any, now: datetime) -> float:
    """Days since the edge was last supported; 0.0 when unparseable (an
    edge we cannot date is never treated as stale — fail toward keeping)."""
    raw = support_timestamp(edge)
    if not raw:
        return 0.0
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def stale_causal_edges(edges: List[Any], now: datetime = None) -> List[Any]:
    """Causal edges past the TTL that still have confidence to lose."""
    now = now or datetime.now(timezone.utc)
    ttl = causal_ttl_days()
    return [e for e in edges
            if getattr(e, "valid_to", None) is None
            and float(getattr(e, "confidence", 0.0) or 0.0) > CAUSAL_DECAY_FLOOR
            and support_age_days(e, now) > ttl]

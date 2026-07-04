"""Relationship provenance + signal floor.

The relationship subsystem must not conflate the AGENT's own contact graph
with the OWNER's social graph. People merely mentioned in (or present on) the
owner's channels are passively-observed third parties: the agent has no
relationship with them and no visibility into the owner's private contact
with them, so "neglected" is unknowable. Relationship maintenance is only
meaningful for DIRECT INTERLOCUTORS: people who have actually exchanged turns
with the agent.

Two gates, applied wherever relationship candidates enter the pipeline
(graph loader, affect feeder, and the relationship generator itself):

1. PROVENANCE: a candidate must be a direct interlocutor -- a recorded
   direct-exchange count (``interaction_count``, bumped per conversation turn
   by the turn/attribution pipeline) at or above a floor. Candidates with no
   direct-exchange evidence are passively-observed third parties and are out
   of scope for relationship initiatives entirely (fail closed).

2. SIGNAL FLOOR: relationship scores must carry real signal. A batch of
   candidates sharing an identical score is an ingestion artifact (one
   ingestion event + uniform default decay), not evidence of anything --
   groups of identical scores larger than a small cap are dropped whole.
   A candidate carrying an explicit score-history count needs at least two
   score events.

Env knobs (generic, deployment-tunable):
    COLONY_RELATIONSHIP_MIN_EXCHANGES   direct-exchange floor (default 3)
    COLONY_RELATIONSHIP_MAX_IDENTICAL   max candidates allowed to share one
                                        identical score (default 2)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def min_direct_exchanges() -> int:
    try:
        return max(1, int(os.environ.get("COLONY_RELATIONSHIP_MIN_EXCHANGES", "3")))
    except (TypeError, ValueError):
        return 3


def max_identical_scores() -> int:
    try:
        return max(1, int(os.environ.get("COLONY_RELATIONSHIP_MAX_IDENTICAL", "2")))
    except (TypeError, ValueError):
        return 2


def is_direct_interlocutor(candidate: Dict[str, Any]) -> bool:
    """True only with recorded direct-exchange evidence at/above the floor.

    Missing evidence means passively observed -- fail closed.
    """
    count = candidate.get("interaction_count")
    try:
        return count is not None and int(count) >= min_direct_exchanges()
    except (TypeError, ValueError):
        return False


def _score_key(candidate: Dict[str, Any]) -> Optional[float]:
    score = candidate.get("relationship_score", candidate.get("score"))
    if score is None:
        return None
    try:
        return round(float(score), 4)
    except (TypeError, ValueError):
        return None


def filter_relationship_candidates(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply provenance + signal-floor gates. Returns the surviving candidates."""
    if not candidates:
        return []

    # Gate 1: provenance -- direct interlocutors only.
    direct = [c for c in candidates if is_direct_interlocutor(c)]
    dropped_observed = len(candidates) - len(direct)

    # Gate 2a: explicit score history, when the candidate carries one.
    with_history = []
    for c in direct:
        events = c.get("score_events")
        if events is not None:
            try:
                if int(events) < 2:
                    continue
            except (TypeError, ValueError):
                continue
        with_history.append(c)

    # Gate 2b: degenerate identical-score batches (ingestion artifact -- one
    # event + uniform default decay). Drop each oversized identical group whole.
    groups: Dict[float, int] = {}
    for c in with_history:
        key = _score_key(c)
        if key is not None:
            groups[key] = groups.get(key, 0) + 1
    cap = max_identical_scores()
    degenerate = {k for k, n in groups.items() if n > cap}
    survivors = [c for c in with_history
                 if _score_key(c) is None or _score_key(c) not in degenerate]

    dropped = len(candidates) - len(survivors)
    if dropped:
        logger.info(
            "relationship signal floor: %d/%d candidate(s) dropped "
            "(observed-third-party=%d, degenerate-score-batch=%d, "
            "floor=%d exchanges, identical-cap=%d)",
            dropped, len(candidates), dropped_observed,
            len(with_history) - len(survivors), min_direct_exchanges(), cap,
        )
    return survivors


async def enrich_interaction_counts(
    candidates: List[Dict[str, Any]],
    contacts_store: Any,
) -> None:
    """Fill ``interaction_count`` (and score, when absent) from the contact
    record's direct-exchange data. Candidates that resolve to no contact keep
    no count and will fail the provenance gate (correct: no direct-exchange
    evidence means passively observed)."""
    if contacts_store is None:
        return
    for c in candidates:
        if c.get("interaction_count") is not None:
            continue
        entity_id = c.get("entity_id") or ""
        contact = None
        try:
            if hasattr(contacts_store, "get"):
                contact = await contacts_store.get(entity_id)
            if contact is None and hasattr(contacts_store, "find_by_person_node_id"):
                contact = await contacts_store.find_by_person_node_id(entity_id)
        except Exception:
            contact = None
        if contact is None:
            continue
        count = getattr(contact, "interaction_count", None)
        if count is None and isinstance(contact, dict):
            count = contact.get("interaction_count")
        if count is not None:
            c["interaction_count"] = int(count)
        if c.get("relationship_score") is None:
            score = getattr(contact, "score", None)
            if score is None and isinstance(contact, dict):
                score = contact.get("score")
            if score is not None:
                c["relationship_score"] = score

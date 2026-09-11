"""Retain direct-exchange provenance without treating scalar closeness as evidence."""

from __future__ import annotations

import os
from typing import Any, Dict, List


def min_direct_exchanges() -> int:
    try:
        return max(1, int(os.environ.get("COLONY_RELATIONSHIP_MIN_EXCHANGES", "3")))
    except (TypeError, ValueError):
        return 3


def is_direct_interlocutor(candidate: Dict[str, Any]) -> bool:
    """True only with recorded direct-exchange evidence at/above the floor.

    Missing evidence means passively observed -- fail closed.
    """
    count = candidate.get("interaction_count")
    try:
        return count is not None and int(count) >= min_direct_exchanges()
    except (TypeError, ValueError):
        return False


def filter_relationship_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Direct-exchange provenance only; scalar closeness never proves usefulness.

    The caller still needs an actual purpose and permission for any outreach.
    Legacy scores are removed before candidates reach planning prompts.
    """
    return [{k: v for k, v in c.items() if k not in {'relationship_score', 'score', 'score_events'}}
            for c in candidates if is_direct_interlocutor(c)]


async def enrich_interaction_counts(
    candidates: List[Dict[str, Any]],
    contacts_store: Any,
) -> None:
    """Fill ``interaction_count`` from the contact
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

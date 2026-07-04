"""Stale world-state decay: entities unseen past the TTL lose confidence.

Graph memory decay already runs daily (_phase_memory_decay + ColonyGraph
half-life machinery); this module covers the WORLD MODEL side, which had no
staleness treatment at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List

from colony_sidecar.beliefs.models import stale_ttl_days

logger = logging.getLogger(__name__)

_DECAY_FACTOR = 0.9
_MIN_CONFIDENCE = 0.1


def _age_days(entity: Any, now: datetime) -> float:
    last = (getattr(entity, "last_seen", None)
            or getattr(entity, "updated_at", None)
            or getattr(entity, "created_at", None))
    if last is None:
        return 0.0
    try:
        if isinstance(last, str):
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0.0, (now - last).total_seconds() / 86400.0)
    except Exception:
        return 0.0


async def stale_entities(world_store: Any, limit: int = 500) -> List[Any]:
    """Entities whose last observation is older than the TTL."""
    if world_store is None:
        return []
    try:
        ents = await world_store.find_entities(query="", min_confidence=0.0,
                                               limit=limit)
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    ttl = stale_ttl_days()
    return [e for e in ents or [] if _age_days(e, now) > ttl]


async def decay_entity(world_store: Any, entity: Any) -> float:
    """Lower one stale entity's confidence (bounded). Returns new value."""
    new_conf = max(_MIN_CONFIDENCE,
                   float(getattr(entity, "confidence", 0.5)) * _DECAY_FACTOR)
    entity.confidence = new_conf
    try:
        await world_store.upsert_entity(entity)
    except Exception:
        logger.debug("stale decay upsert failed", exc_info=True)
    return new_conf

"""Surprise-accumulation consumer: close the loop into the workspace.

The condition worker emits ``surprise.accumulation`` when unresolved
surprises pile up, but nothing ever consumed it — the signal died on the
WebSocket unless a client happened to be watching. This consumer turns the
event into a workspace concern so accumulating surprises land on the
agent's mind (one strengthening concern, keyed stably, never a pile-up).

Clean no-op when the workspace is disabled or not wired: the event still
broadcasts and journals exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DEDUP_KEY = "surprise:accumulation"


def handle_surprise_accumulation(event: Dict[str, Any]) -> bool:
    """Raise/strengthen the accumulation concern. Returns True when a
    concern was bumped, False on the (clean) no-op paths."""
    try:
        from colony_sidecar.self_model.workspace import workspace_enabled
        if not workspace_enabled():
            return False
        from colony_sidecar.api.routers.host import _workspace
        if _workspace is None:
            return False
        payload = event.get("payload") or {}
        try:
            count = int(payload.get("unresolved_count") or 0)
        except (TypeError, ValueError):
            count = 0
        _workspace.bump(
            kind="anomaly",
            summary=(f"{count} unresolved surprise(s) accumulating — "
                     "reality keeps diverging from expectations; review "
                     "and resolve them"),
            dedup_key=_DEDUP_KEY,
            salience=min(0.9, 0.4 + 0.05 * count),
            sources=["surprise-store"])
        return True
    except Exception:
        logger.debug("surprise.accumulation -> workspace failed",
                     exc_info=True)
        return False


def register() -> None:
    """Subscribe the consumer. Enablement is checked at EVENT time (not
    registration time), so a workspace flipped on later just starts working."""
    from colony_sidecar.events.broadcaster import subscribe
    subscribe("surprise.accumulation", handle_surprise_accumulation)

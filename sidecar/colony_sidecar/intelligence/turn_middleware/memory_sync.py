"""Post-turn memory sync — lifted from ``run_agent.py:_memory_sync``.

Host-agnostic. Accepts a plain dataclass describing what just happened
in the turn and routes the distilled signal into whichever continuity
store the host router has been wired with. Returns a
``TurnSyncOutcome`` describing what was touched; the caller is
responsible for emitting any host events.

Two sink shapes are supported via duck-typing so the endpoint works
whether the server wires the in-process
``colony.intelligence.components.session_continuity.SessionContinuity``
(expected when colony-core runs in-process — no HTTP loopback) or the
HTTP-client ``agent.memory_bridge.AgentMemoryBridge`` (useful for
standalone runs where the agent and API are in different processes).

Resolution order:

1. Bridge shape — ``is_available`` property + ``async
   update_continuity(session_id, topics, entities, pending_tasks,
   tools_used)``.
2. Continuity shape — ``async update_context(session_id, topics,
   entities, pending_tasks)`` (note: ``tools_used`` is dropped because
   ``SessionContinuity`` does not persist it today; if it starts doing
   so, extend the branch below.)

Background review (``_spawn_background_review`` in ``run_agent.py``)
is intentionally NOT moved here. It forks a full ``AIAgent`` and
recurses into ``run_conversation``, so it can only cleanly lift out of
``run_agent.py`` when the reasoning loop itself is extracted in
Stage B. OpenClaw owns reasoning today, so this is not blocking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TurnSyncOutcome:
    """Result of one ``sync_turn_memory`` call."""

    continuity_updated: bool = False
    skipped_reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)


async def sync_turn_memory(
    sink: Any,
    session_id: str,
    *,
    topics: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    pending_tasks: Optional[List[str]] = None,
    tools_used: Optional[List[str]] = None,
) -> TurnSyncOutcome:
    """Update session continuity after a turn completes.

    Never raises — unexpected errors are captured on the returned
    outcome so the host stays up even if Colony's continuity store is
    misbehaving.
    """

    outcome = TurnSyncOutcome()

    if sink is None:
        outcome.skipped_reason = "no_sink"
        return outcome

    if not session_id:
        outcome.skipped_reason = "no_session_id"
        return outcome

    safe_topics = list(topics or [])
    safe_entities = list(entities or [])
    safe_pending = list(pending_tasks or [])
    safe_tools = list(tools_used or [])

    # Branch 1 — AgentMemoryBridge shape. ``is_available`` is a property
    # that gates on circuit-breaker state and backend mode.
    if hasattr(sink, "update_continuity"):
        if not getattr(sink, "is_available", True):
            outcome.skipped_reason = "sink_unavailable"
            return outcome
        try:
            await sink.update_continuity(
                session_id=session_id,
                topics=safe_topics,
                entities=safe_entities,
                pending_tasks=safe_pending,
                tools_used=safe_tools,
            )
            outcome.continuity_updated = True
        except Exception as exc:
            logger.debug("sync_turn_memory: update_continuity raised: %s", exc)
            outcome.errors.append(f"update_continuity: {exc}")
        return outcome

    # Branch 2 — in-process SessionContinuity shape. Drops
    # ``tools_used`` because ``update_context`` doesn't accept it today.
    if hasattr(sink, "update_context"):
        try:
            await sink.update_context(
                session_id=session_id,
                topics=safe_topics or None,
                entities=safe_entities or None,
                pending_tasks=safe_pending or None,
            )
            outcome.continuity_updated = True
        except Exception as exc:
            logger.debug("sync_turn_memory: update_context raised: %s", exc)
            outcome.errors.append(f"update_context: {exc}")
        return outcome

    outcome.skipped_reason = "sink_shape_unsupported"
    return outcome

"""Shared lazy-broadcast helper for sidecar subsystems.

Subsystems (consolidator, goal store, briefing store, world-model store,
skills registry) call ``emit(type, payload)`` to push a typed event to
all connected WebSocket subscribers. The import of ``broadcast_event``
is lazy so modules that want to emit events don't take a hard dependency
on the API router.

Errors are swallowed — event emission must never break the subsystem
that triggers it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_broadcast_fn = None

# In-process consumers by event type. emit() used to reach only WebSocket
# subscribers, so an event with no client connected simply died; subsystems
# that need to REACT to an event (not just display it) register here.
_subscribers: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}


def subscribe(event_type: str, fn: Callable[[Dict[str, Any]], Any]) -> None:
    """Register an in-process consumer for one event type.

    Consumers are invoked synchronously from ``emit()`` with the full event
    dict (``type``/``occurred_at``/``payload``); keep them cheap. A consumer
    error is swallowed — emission must never break the emitting subsystem.
    """
    _subscribers.setdefault(event_type, []).append(fn)


def _resolve_broadcaster():
    """Lazy-import ``broadcast_event`` from the host router.

    Returns a no-op lambda if the import fails (unit-test contexts,
    circular-import edge cases, sidecar not yet booted).
    """
    global _broadcast_fn
    if _broadcast_fn is None:
        try:
            from colony_sidecar.api.routers.host import broadcast_event
            _broadcast_fn = broadcast_event
        except ImportError:
            _broadcast_fn = lambda _e: None  # noqa: E731
    return _broadcast_fn


def emit(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Broadcast a typed event to all WebSocket subscribers.

    Also appends the event to the persistent journal so disconnected
    clients can replay missed events via
    ``GET /v1/host/events/replay?since=...``.

    Args:
        event_type: One of the canonical ``HostEventType`` values —
            ``briefing``, ``anomaly``, ``goal_update``,
            ``memory_consolidated``, ``world_model_changed``,
            ``skill_draft_approved``, ``proactive_message``, etc.
        payload: Arbitrary event-specific payload. Keep it small —
            subscribers fetch full records via the REST API when they
            need detail.
    """
    try:
        broadcaster = _resolve_broadcaster()
        event = {
            "type": event_type,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        broadcaster(event)

        # In-process consumers (best-effort, each isolated)
        for fn in _subscribers.get(event_type, ()):
            try:
                fn(event)
            except Exception:
                logger.debug("in-process consumer failed for %s",
                             event_type, exc_info=True)

        # Journal the event for replay (best-effort, non-blocking)
        try:
            from colony_sidecar.events.journal import append_event
            append_event(event_type, payload or {})
        except Exception:
            logger.debug("journal append failed for %s", event_type, exc_info=True)

    except Exception:
        logger.debug("broadcast_event(%s) failed", event_type, exc_info=True)


def reset_broadcaster_for_tests(fn) -> None:
    """Inject a broadcaster for test doubles. Production code never calls this."""
    global _broadcast_fn
    _broadcast_fn = fn

"""World-model expectation resolvers (Mind M3a x world model).

The expectation engine keeps its store generic and lets subsystems register
how a class of predictions gets checked against reality. This module supplies
the two world-model classes:

- ``world-relationship:`` -- "this relationship is still active at the
  horizon". ``detail`` carries ``source_id``/``target_id`` and optionally
  ``relationship_type``.
- ``world-property:`` -- "this entity property still holds this value at the
  horizon". ``detail`` carries ``entity_id``/``key``/``value``.

Resolvers return True (hit), False (miss), or None when the world model
cannot see the subject -- the prediction then stays pending and eventually
goes ``unresolved``, which is excluded from calibration (never a fabricated
miss). The world store is fetched lazily at resolve time so boot order does
not matter, and the async store API is bridged the same way the briefing
aggregators do it (``asyncio.run`` off-loop, a worker thread on-loop).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

RELATIONSHIP_PREFIX = "world-relationship:"
PROPERTY_PREFIX = "world-property:"


def _world_store() -> Any:
    try:
        from colony_sidecar.api.routers.host import _world_store as ws
        return ws
    except Exception:
        return None


def _run_async(coro: Any) -> Any:
    """Run a store coroutine from the engine's synchronous resolver path."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already on an event loop (the autonomy phase) — blocking on this loop
    # would deadlock, so complete the coroutine on its own loop in a worker.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result(timeout=10.0)


def _norm(v: Any) -> str:
    return str(v if v is not None else "").strip().lower()


def resolve_relationship_still_active(prediction: Any) -> Optional[bool]:
    """Hit when at least one matching relationship is still active.

    Causal-typed predictions are NOT resolved here (query-only guard,
    H2.5): a causal edge must never be scored true/false through the
    generic relationship machinery — its falsifiability path is the causal
    subsystem's own. Returning None leaves the prediction unresolved,
    which calibration excludes (never a fabricated hit or miss).
    """
    store = _world_store()
    detail = getattr(prediction, "detail", None) or {}
    source_id = detail.get("source_id")
    target_id = detail.get("target_id")
    if store is None or not source_id or not target_id:
        return None
    try:
        from colony_sidecar.world_model.causal_policy import is_causal
        if is_causal(detail.get("relationship_type") or ""):
            return None
    except Exception:
        return None
    rels = _run_async(store.query_relationships(
        source_id=source_id, target_id=target_id,
        relationship_type=detail.get("relationship_type") or None,
        active_only=False, min_confidence=0.0, limit=50))
    if not rels:
        # never observed -> the world model cannot score this prediction
        return None
    return any(getattr(r, "is_active", False) for r in rels)


def resolve_property_unchanged(prediction: Any) -> Optional[bool]:
    """Hit when the entity property still equals the predicted value."""
    store = _world_store()
    detail = getattr(prediction, "detail", None) or {}
    entity_id = detail.get("entity_id")
    key = detail.get("key")
    if store is None or not entity_id or not key or "value" not in detail:
        return None
    entity = _run_async(store.get_entity(entity_id, min_confidence=0.0))
    if entity is None:
        return None
    props = getattr(entity, "properties", None) or {}
    if key not in props:
        # the property is no longer tracked at all — that is a visibility
        # loss, not evidence the value changed; leave it unresolvable
        return None
    return _norm(props.get(key)) == _norm(detail.get("value"))


def register_world_resolvers(engine: Any) -> None:
    """Attach both world-model resolvers to an ExpectationEngine."""
    if engine is None:
        return
    engine.register_resolver(RELATIONSHIP_PREFIX,
                             resolve_relationship_still_active)
    engine.register_resolver(PROPERTY_PREFIX, resolve_property_unchanged)

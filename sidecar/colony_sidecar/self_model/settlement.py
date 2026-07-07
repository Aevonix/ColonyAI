"""Source settlement: resolving a concern settles what the concern points at.

A concern raised from a durable source (an overdue commitment, an anomaly, a
stale goal) carries that source in its ``sources`` list as ``"<kind>:<id>"``.
Resolving only the concern leaves the source open, and whatever ingests that
source re-raises the concern on the next tick — the owner's resolve silently
undone minutes later. The registry maps a source kind to a settle callback so
every surface (owner deck, agent tool, MCP, API) closes the whole chain with
one call.

Deployment-agnostic: the server wires settlers for whatever stores it runs;
unknown source kinds are skipped, and one failing settler never blocks the
others.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

Settler = Callable[..., Optional[Dict[str, Any]]]

_SETTLERS: Dict[str, Settler] = {}


def register_settler(kind: str, fn: Settler) -> None:
    """Register the settle callback for a source kind (e.g. "commitment").

    The callback is invoked as ``fn(source_id, outcome=..., note=...,
    resolved_by=...)`` and should return a small dict describing the settled
    source (or None when the source no longer exists).
    """
    _SETTLERS[kind] = fn


def registered_kinds() -> List[str]:
    return sorted(_SETTLERS)


def settle_sources(
    sources: Optional[List[str]],
    *,
    outcome: str = "done",
    note: str = "",
    resolved_by: str = "owner",
) -> List[Dict[str, Any]]:
    """Settle every recognized source reference. Returns one entry per source
    that had a registered settler, with ``settled`` and any settler detail."""
    results: List[Dict[str, Any]] = []
    for src in sources or []:
        kind, _, source_id = str(src).partition(":")
        fn = _SETTLERS.get(kind)
        if fn is None or not source_id:
            continue
        entry: Dict[str, Any] = {"source": src, "settled": False}
        try:
            detail = fn(source_id, outcome=outcome, note=note,
                        resolved_by=resolved_by)
            if detail is not None:
                entry["settled"] = True
                if isinstance(detail, dict):
                    entry.update(detail)
        except Exception as exc:
            logger.warning("settler for %s failed: %s", src, exc)
            entry["error"] = str(exc)
        results.append(entry)
    return results

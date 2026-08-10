"""Governed event boundary for the Hermes general plugin.

Colony does not yet expose a viewer-attested event projection suitable for a
shared Hermes process.  The general plugin therefore has no subscriber, cache,
replay cursor, or LLM injection path.  Event-driven context belongs in the
canonical memory provider once the server can attest the exact viewer.
"""

from __future__ import annotations


GOVERNED_EVENT_TYPES: tuple[str, ...] = ()


def event_catalog() -> tuple[str, ...]:
    return GOVERNED_EVENT_TYPES


__all__ = ["GOVERNED_EVENT_TYPES", "event_catalog"]

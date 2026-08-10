"""Inert marker for the retired Colony-to-Hermes initiative hook example."""

from __future__ import annotations


LEGACY_EFFECT_WORKER_DISABLED = True


async def handle(_event_type: str, _context: dict) -> None:
    """Do nothing; effects require the governed action mediator."""

    return None

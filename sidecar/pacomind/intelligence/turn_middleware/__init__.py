"""Turn middleware — host-agnostic cognitive steps that run around
each conversation turn.

These modules lift the PacoMind-distinctive cognition out of
``run_agent.py`` so it is reachable from any host (OpenClaw plugin,
future adapters, standalone). See ``docs/HOST_API.md`` for the
endpoints that expose them.
"""

from pacomind.intelligence.turn_middleware.memory_sync import (
    TurnSyncOutcome,
    sync_turn_memory,
)

__all__ = ["TurnSyncOutcome", "sync_turn_memory"]

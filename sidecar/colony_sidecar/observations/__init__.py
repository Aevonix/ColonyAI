"""Observation store (v0.16.0) — the agent is Colony's sensor array.

Colony does not own external API clients. The agent observes the world
through its existing Hermes connections (github, terminal, web, ...)
and reports domain snapshots here. Colony's context loaders read
observations, never external APIs.
"""

from colony_sidecar.observations.store import (
    OBSERVATION_DOMAINS,
    Observation,
    ObservationStore,
)

__all__ = ["Observation", "ObservationStore", "OBSERVATION_DOMAINS"]

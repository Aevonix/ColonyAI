"""Observation store (v0.16.0) — the agent is PacoMind's sensor array.

PacoMind does not own external API clients. The agent observes the world
through its existing Hermes connections (github, terminal, web, ...)
and reports domain snapshots here. PacoMind's context loaders read
observations, never external APIs.
"""

from pacomind.observations.store import (
    OBSERVATION_DOMAINS,
    Observation,
    ObservationStore,
)

__all__ = ["Observation", "ObservationStore", "OBSERVATION_DOMAINS"]

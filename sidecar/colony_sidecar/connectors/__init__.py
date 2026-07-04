"""Read-only connector framework (cognition item 2, Phase C).

Pull-style senses: each connector polls an external source on its own cadence
and normalizes what it sees into Observations that feed the observation store,
the world-model populator, and (via the populator's audit hook) belief
maintenance. Config is env-only; default off; shadow-first per connector.
Push-style ingress is handled by the host framework's webhook adapter and is
intentionally not built here.
"""

from colony_sidecar.connectors.base import (
    Connector, ConnectorConfig, EntityHint, Observation,
)
from colony_sidecar.connectors.manager import (
    ConnectorManager, connectors_enabled, connectors_mode,
)

__all__ = [
    "Connector", "ConnectorConfig", "EntityHint", "Observation",
    "ConnectorManager", "connectors_enabled", "connectors_mode",
]

"""SubsystemRegistry — lazy access to all wired sidecar subsystems.

The AutonomyLoop uses this instead of taking 18+ constructor arguments.
Each property reads from the host router's module-level wiring and
returns None if the subsystem isn't available. Phases that depend on
a subsystem check for None and skip gracefully.
"""

from __future__ import annotations

from typing import Any, Optional


class SubsystemRegistry:
    """Provides lazy access to all wired sidecar subsystems.

    Reads from the host router's module-level globals. If a subsystem
    isn't wired (e.g. Neo4j not configured), the property returns None
    and any phase depending on it becomes a no-op.
    """

    @property
    def graph(self) -> Any:
        from colony_sidecar.api.routers.host import _graph
        return _graph

    @property
    def goals(self) -> Any:
        from colony_sidecar.api.routers.host import _goals_store
        return _goals_store

    @property
    def initiative(self) -> Any:
        from colony_sidecar.api.routers.host import _metalearner
        return _metalearner  # InitiativeEngine is part of cognition

    @property
    def anomalies(self) -> Any:
        from colony_sidecar.intelligence.components.anomaly_detector import AnomalyDetector
        from colony_sidecar.api.routers.host import _graph
        from colony_sidecar.events.bus import EventBus
        if not hasattr(self, '_anomaly_detector'):
            graph_client = _graph.driver if _graph and hasattr(_graph, 'driver') else None
            event_bus = EventBus()
            self._anomaly_detector = AnomalyDetector(graph_client, event_bus)
        return self._anomaly_detector

    @property
    def queue(self) -> Any:
        from colony_sidecar.api.routers.host import _consolidator
        return _consolidator

    @property
    def briefings(self) -> Any:
        from colony_sidecar.api.routers.host import _briefings_engine
        return _briefings_engine

    @property
    def events(self) -> Any:
        from colony_sidecar.api.routers.host import _event_subscribers
        return _event_subscribers

    @property
    def delivery(self) -> Any:
        from colony_sidecar.api.routers.host import _delivery_bridge
        return _delivery_bridge

    @property
    def cognition(self) -> Any:
        from colony_sidecar.api.routers.host import _metalearner
        return _metalearner

    @property
    def connection_discoverer(self) -> Any:
        from colony_sidecar.api.routers.host import _connection_discoverer
        return _connection_discoverer

    @property
    def learner(self) -> Any:
        from colony_sidecar.api.routers.host import _learner
        return _learner

    @property
    def skills(self) -> Any:
        from colony_sidecar.api.routers.host import _skills_registry
        return _skills_registry

    @property
    def chain(self) -> Any:
        from colony_sidecar.api.routers.host import _chain_manager
        return _chain_manager

    @property
    def secrets(self) -> Any:
        from colony_sidecar.api.routers.host import _secrets_manager
        return _secrets_manager

    @property
    def signal_collector(self) -> Any:
        from colony_sidecar.api.routers.host import _signal_collector
        return _signal_collector

    @property
    def embedder(self) -> Any:
        from colony_sidecar.api.routers.host import _embedder
        return _embedder

    @property
    def response_gate(self) -> Any:
        from colony_sidecar.api.routers.host import _response_gate
        return _response_gate

    @property
    def llm_router(self) -> Any:
        """Get the LLMRouter from the ReasoningLoop if wired."""
        from colony_sidecar.api.routers.host import _reasoning_loop
        if _reasoning_loop is not None:
            return _reasoning_loop._model
        return None

    @property
    def scheduler(self) -> Any:
        from colony_sidecar.api.routers.host import _scheduler
        return _scheduler

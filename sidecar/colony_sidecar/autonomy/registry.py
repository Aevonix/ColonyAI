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
    def world_model(self) -> Any:
        from colony_sidecar.api.routers.host import _world_store
        return _world_store

    @property
    def directives(self) -> Any:
        from colony_sidecar.api.routers.host import _directive_manager
        return _directive_manager

    @property
    def research(self) -> Any:
        from colony_sidecar.api.routers.host import _research_pipeline
        return _research_pipeline

    @property
    def proposal_store(self) -> Any:
        from colony_sidecar.api.routers.host import _proposal_store
        return _proposal_store

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
            # Bug 41: Use shared event bus instead of creating new one
            event_bus = self.events or EventBus()
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

    @property
    def initiative_engine(self) -> Any:
        """Get or create the InitiativeEngine (NOT MetaLearner)."""
        if not hasattr(self, '_initiative_engine'):
            try:
                from colony_sidecar.intelligence.components.initiative_engine import InitiativeEngine
                from colony_sidecar.api.routers.host import _graph, _initiative_store, _goals_store

                self._initiative_engine = InitiativeEngine(
                    graph_client=_graph if _graph and hasattr(_graph, 'driver') else None,
                    event_bus=None,  # Not needed for rule-based generation
                    mind_model=None,
                    store=_initiative_store,
                    goal_store=_goals_store,
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to create InitiativeEngine: %s", e)
                self._initiative_engine = None
        return self._initiative_engine

    @property
    def commitment_store(self) -> Any:
        """Get the CommitmentStore."""
        from colony_sidecar.api.routers.host import _commitment_store  # singular
        return _commitment_store

    @property
    def affect_store(self) -> Any:
        """Get the AffectStore."""
        from colony_sidecar.api.routers.host import _affect_store
        return _affect_store

    @property
    def pattern_store(self) -> Any:
        """Get the PatternStore."""
        from colony_sidecar.api.routers.host import _pattern_store
        return _pattern_store

    # === Multi-Agent Properties (v0.7.0) ===

    @property
    def agent_store(self) -> Any:
        """Get the AgentStore for multi-agent management."""
        from colony_sidecar.api.routers.host import _agent_store
        return _agent_store

    @property
    def initiative_store(self) -> Any:
        """Get the InitiativeStore for initiative persistence."""
        from colony_sidecar.api.routers.host import _initiative_store
        return _initiative_store

    @property
    def assignment_engine(self) -> Any:
        """Get the AssignmentEngine for initiative-to-agent matching."""
        from colony_sidecar.api.routers.host import _assignment_engine
        return _assignment_engine

    @property
    def websocket_manager(self) -> Any:
        """Get the WebSocketManager for remote agent connections."""
        from colony_sidecar.api.routers.host import _websocket_manager
        return _websocket_manager

    @property
    def task_queue(self) -> Any:
        """Get the TaskQueueManager for distributed job scheduling."""
        from colony_sidecar.api.routers.host import _task_queue
        return _task_queue

    @property
    def contacts(self) -> Any:
        """Get the contact store (graduated approval policy, v0.18.0)."""
        from colony_sidecar.api.routers.host import _contacts_store
        return _contacts_store

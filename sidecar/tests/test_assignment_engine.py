"""Tests for AssignmentEngine agent selection logic."""

import tempfile
from pathlib import Path
import pytest

from colony_sidecar.agents.store import AgentStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.initiatives.assignment import AssignmentEngine
from colony_sidecar.initiatives.models import StoredInitiative


class TestAssignmentEngine:
    """Tests for agent assignment logic."""

    @pytest.fixture
    def agent_store(self, tmp_path: Path) -> AgentStore:
        """Create a fresh AgentStore for each test."""
        return AgentStore(state_dir=tmp_path)

    @pytest.fixture
    def initiative_store(self, tmp_path: Path) -> InitiativeStore:
        """Create a fresh InitiativeStore for each test."""
        return InitiativeStore(state_dir=tmp_path)

    @pytest.fixture
    def engine(
        self,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> AssignmentEngine:
        """Create an AssignmentEngine with fresh stores."""
        return AssignmentEngine(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )

    def _create_agent(
        self,
        store: AgentStore,
        agent_id: str,
        capabilities: list[str],
        status: str = "online",
        is_primary: bool = False,
        priority: int = 1,
        max_concurrent: int = 5,
        current_assignments: int = 0,
    ) -> None:
        """Helper to create an agent with specified properties."""
        store.create({
            "agent_id": agent_id,
            "node_id": f"node-{agent_id}",
            "colony_id": "colony-1",
            "name": f"agent-{agent_id}",
            "connection_mode": "local",
            "capabilities": capabilities,
            "is_primary": is_primary,
            "priority": priority,
            "max_concurrent": max_concurrent,
            "current_assignments": current_assignments,
            "status": status,
        })

    def test_select_best_agent_basic(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test basic agent selection."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        self._create_agent(agent_store, "agent-2", ["calendar"])

        # Request messaging capability
        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-1"

    def test_select_best_agent_prefers_primary(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that primary agents are preferred."""
        self._create_agent(agent_store, "agent-1", ["messaging"], is_primary=False)
        self._create_agent(agent_store, "agent-2", ["messaging"], is_primary=True)

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-2"
        assert agent.is_primary is True

    def test_select_best_agent_respects_priority(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that higher priority agents are preferred."""
        self._create_agent(agent_store, "agent-1", ["messaging"], priority=1)
        self._create_agent(agent_store, "agent-2", ["messaging"], priority=3)

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_best_agent_load_balancing(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with lower load are preferred."""
        self._create_agent(
            agent_store, "agent-1", ["messaging"],
            max_concurrent=5, current_assignments=4
        )
        self._create_agent(
            agent_store, "agent-2", ["messaging"],
            max_concurrent=5, current_assignments=1
        )

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_best_agent_excluded_types(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with excluded types are skipped."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        agent_store.update("agent-1", excluded_types=["coding"])

        # Agent should not be selected for coding initiative
        agent = engine.select_best_agent(
            initiative_type="coding",
            required_capabilities=["messaging"],
        )
        assert agent is None

    def test_select_best_agent_included_types(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with included types filter correctly."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        agent_store.update("agent-1", included_types=["notification", "reminder"])

        # Agent should be selected for notification
        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None

        # Agent should NOT be selected for coding (not in included_types)
        agent = engine.select_best_agent(
            initiative_type="coding",
            required_capabilities=["messaging"],
        )
        assert agent is None

    def test_select_best_agent_no_capacity(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents at max capacity are skipped."""
        self._create_agent(
            agent_store, "agent-1", ["messaging"],
            max_concurrent=3, current_assignments=3
        )
        self._create_agent(
            agent_store, "agent-2", ["messaging"],
            max_concurrent=5, current_assignments=2
        )

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_best_agent_offline_skipped(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that offline agents are skipped."""
        self._create_agent(agent_store, "agent-1", ["messaging"], status="offline")
        self._create_agent(agent_store, "agent-2", ["messaging"], status="online")

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["messaging"],
        )
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_best_agent_missing_capability(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents without required capability are skipped."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        self._create_agent(agent_store, "agent-2", ["calendar"])

        agent = engine.select_best_agent(
            initiative_type="notification",
            required_capabilities=["coding"],  # No agent has this
        )
        assert agent is None

    def test_assign_initiative(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test full assignment flow."""
        self._create_agent(agent_store, "agent-1", ["messaging"])

        initiative = initiative_store.create(
            type="notification",
            description="Test initiative",
        )

        result = engine.assign_initiative(initiative.id)
        assert result is not None
        assert result.assigned_agent_id == "agent-1"

        # Check initiative was updated
        updated = initiative_store.get(initiative.id)
        assert updated is not None
        assert updated.status == "assigned"

        # Check agent assignment count was incremented
        agent = agent_store.get("agent-1")
        assert agent is not None
        assert agent.current_assignments == 1

    def test_assign_initiative_preferred_agent(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test assignment with preferred agent."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        self._create_agent(agent_store, "agent-2", ["messaging"], is_primary=True)

        initiative = initiative_store.create(
            type="notification",
            description="Test",
        )

        # Assign with preferred agent
        result = engine.assign_initiative(initiative.id, preferred_agent_id="agent-1")
        assert result is not None
        assert result.assigned_agent_id == "agent-1"

    def test_assign_initiative_no_available_agents(
        self,
        engine: AssignmentEngine,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test assignment fails when no agents available."""
        initiative = initiative_store.create(
            type="notification",
            description="Test",
        )

        result = engine.assign_initiative(initiative.id)
        assert result is None

        # Initiative should remain pending
        unchanged = initiative_store.get(initiative.id)
        assert unchanged is not None
        assert unchanged.status == "pending"


class TestAssignmentScoring:
    """Tests for assignment scoring algorithm."""

    @pytest.fixture
    def engine(self, tmp_path: Path) -> AssignmentEngine:
        """Create an AssignmentEngine for scoring tests."""
        agent_store = AgentStore(state_dir=tmp_path)
        initiative_store = InitiativeStore(state_dir=tmp_path)
        return AssignmentEngine(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )

    def test_score_calculation(self, engine: AssignmentEngine) -> None:
        """Test agent score calculation."""
        from colony_sidecar.agents.models import Agent

        agent = Agent(
            agent_id="test",
            node_id="node-1",
            colony_id="colony-1",
            name="test",
            is_primary=True,
            priority=2,
            max_concurrent=5,
            current_assignments=1,
        )

        # Score = 100 (primary) + 20 (priority * 10) + 80 (load bonus) = 200
        score = engine._score_agent(agent)
        assert score > 0
        
        # Primary should boost score
        assert score >= 100  # At least the primary bonus

    def test_score_load_penalty(self, engine: AssignmentEngine) -> None:
        """Test that higher load reduces score."""
        from colony_sidecar.agents.models import Agent

        low_load = Agent(
            agent_id="low",
            node_id="node-1",
            colony_id="colony-1",
            name="low",
            max_concurrent=5,
            current_assignments=0,
        )

        high_load = Agent(
            agent_id="high",
            node_id="node-2",
            colony_id="colony-1",
            name="high",
            max_concurrent=5,
            current_assignments=4,
        )

        low_score = engine._score_agent(low_load)
        high_score = engine._score_agent(high_load)
        
        # Low load should score higher
        assert low_score > high_score

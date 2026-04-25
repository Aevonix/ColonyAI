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

    def test_select_agent_basic(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test basic agent selection."""
        self._create_agent(agent_store, "agent-1", ["messaging"])
        self._create_agent(agent_store, "agent-2", ["calendar"])

        # Create a mock initiative dict
        initiative = {"type": "notification", "id": "init-1"}

        # Request messaging capability - need to check INITIATIVE_CAPABILITIES mapping
        # "notification" isn't in INITIATIVE_CAPABILITIES, so it allows any agent
        agent = engine.select_agent(initiative)
        assert agent is not None  # Should get one of them

    def test_select_agent_prefers_primary(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that primary agents are preferred for user-facing types."""
        self._create_agent(agent_store, "agent-1", [], is_primary=False)
        self._create_agent(agent_store, "agent-2", [], is_primary=True)

        # "follow_up" is a USER_FACING_TYPE that prefers primary
        initiative = {"type": "follow_up", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None
        assert agent.agent_id == "agent-2"
        assert agent.is_primary is True

    def test_select_agent_respects_priority(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that higher priority agents are preferred."""
        self._create_agent(agent_store, "agent-1", [], priority=1)
        self._create_agent(agent_store, "agent-2", [], priority=3)

        initiative = {"type": "health", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_agent_load_balancing(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with lower load are preferred."""
        self._create_agent(
            agent_store, "agent-1", [],
            max_concurrent=5, current_assignments=4
        )
        self._create_agent(
            agent_store, "agent-2", [],
            max_concurrent=5, current_assignments=1
        )

        initiative = {"type": "health", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_agent_excluded_types(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with excluded types are skipped."""
        self._create_agent(agent_store, "agent-1", [])
        agent_store.update("agent-1", excluded_types=["coding"])

        # Agent should not be selected for coding initiative
        initiative = {"type": "coding", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is None

    def test_select_agent_included_types(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents with included types filter correctly."""
        self._create_agent(agent_store, "agent-1", [])
        agent_store.update("agent-1", included_types=["notification", "reminder"])

        # Agent should be selected for notification (not in INITIATIVE_CAPABILITIES, allows any)
        initiative = {"type": "notification", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None

        # Agent should NOT be selected for coding (not in included_types)
        initiative = {"type": "coding", "id": "init-2"}
        agent = engine.select_agent(initiative)
        assert agent is None

    def test_select_agent_no_capacity(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that agents at max capacity are skipped."""
        self._create_agent(
            agent_store, "agent-1", [],
            max_concurrent=3, current_assignments=3
        )
        self._create_agent(
            agent_store, "agent-2", [],
            max_concurrent=5, current_assignments=2
        )

        initiative = {"type": "health", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_agent_offline_skipped(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that offline agents are skipped."""
        self._create_agent(agent_store, "agent-1", [], status="offline")
        self._create_agent(agent_store, "agent-2", [], status="online")

        initiative = {"type": "health", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is not None
        assert agent.agent_id == "agent-2"

    def test_select_agent_all_offline(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
    ) -> None:
        """Test that no agent is selected when all offline."""
        self._create_agent(agent_store, "agent-1", [], status="offline")

        initiative = {"type": "health", "id": "init-1"}
        agent = engine.select_agent(initiative)
        assert agent is None

    @pytest.mark.asyncio
    async def test_assign_initiative(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test full assignment flow."""
        self._create_agent(agent_store, "agent-1", [])

        initiative = initiative_store.create(
            type="notification",
            description="Test initiative",
        )

        result = await engine.assign(initiative)
        assert result is not None
        assert result.agent_id == "agent-1"

        # Check initiative was updated
        updated = initiative_store.get(initiative.id)
        assert updated is not None
        assert updated.status == "assigned"

        # Check agent assignment count was incremented
        agent = agent_store.get("agent-1")
        assert agent is not None
        assert agent.current_assignments == 1

    @pytest.mark.asyncio
    async def test_assign_initiative_preferred_agent(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test assignment with preferred agent."""
        self._create_agent(agent_store, "agent-1", [])
        self._create_agent(agent_store, "agent-2", [], is_primary=True)

        initiative = initiative_store.create(
            type="notification",
            description="Test",
            preferred_agent_id="agent-1",
        )

        # Assign with preferred agent
        result = await engine.assign(initiative)
        assert result is not None
        assert result.agent_id == "agent-1"

    @pytest.mark.asyncio
    async def test_assign_initiative_no_available_agents(
        self,
        engine: AssignmentEngine,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test assignment fails when no agents available."""
        initiative = initiative_store.create(
            type="notification",
            description="Test",
        )

        result = await engine.assign(initiative)
        assert result is None

        # Initiative should remain pending
        unchanged = initiative_store.get(initiative.id)
        assert unchanged is not None
        assert unchanged.status == "pending"

    @pytest.mark.asyncio
    async def test_auto_assign_pending(
        self,
        engine: AssignmentEngine,
        agent_store: AgentStore,
        initiative_store: InitiativeStore,
    ) -> None:
        """Test auto-assigning all pending initiatives."""
        self._create_agent(agent_store, "agent-1", [], max_concurrent=10)

        # Create multiple initiatives
        initiative_store.create(type="health", description="1")
        initiative_store.create(type="health", description="2")
        initiative_store.create(type="health", description="3")

        # Auto-assign
        assigned_count = await engine.auto_assign_pending()
        assert assigned_count == 3

        # Check all are assigned
        pending = initiative_store.list(status=["pending"])
        assert len(pending) == 0


class TestInitiativeCapabilities:
    """Tests for initiative type → capability mapping."""

    def test_follow_up_needs_no_capabilities(self) -> None:
        """Test that follow_up allows any agent."""
        from colony_sidecar.initiatives.assignment import INITIATIVE_CAPABILITIES
        
        assert INITIATIVE_CAPABILITIES.get("follow_up") == []

    def test_relationship_needs_messaging(self) -> None:
        """Test that relationship needs messaging capability."""
        from colony_sidecar.initiatives.assignment import INITIATIVE_CAPABILITIES
        
        assert "messaging" in INITIATIVE_CAPABILITIES.get("relationship", [])

    def test_scheduling_needs_calendar(self) -> None:
        """Test that scheduling needs calendar capability."""
        from colony_sidecar.initiatives.assignment import INITIATIVE_CAPABILITIES
        
        assert "calendar" in INITIATIVE_CAPABILITIES.get("scheduling", [])

    def test_coding_needs_coding(self) -> None:
        """Test that coding needs coding capability."""
        from colony_sidecar.initiatives.assignment import INITIATIVE_CAPABILITIES
        
        assert "coding" in INITIATIVE_CAPABILITIES.get("coding", [])

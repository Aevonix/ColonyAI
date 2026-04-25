"""Tests for AgentStore and InviteStore."""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from colony_sidecar.agents.store import AgentStore, InviteStore
from colony_sidecar.agents.models import Agent, AgentStatus, AgentMetadata


class TestAgentStore:
    """Tests for AgentStore CRUD operations."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> AgentStore:
        """Create a fresh AgentStore for each test."""
        return AgentStore(state_dir=tmp_path)

    def test_create_agent(self, store: AgentStore) -> None:
        """Test creating an agent."""
        agent = store.create({
            "agent_id": "agent-1",
            "node_id": "node-1",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
            "capabilities": ["messaging", "calendar"],
            "is_primary": True,
        })

        assert agent is not None
        assert agent.agent_id == "agent-1"
        assert agent.node_id == "node-1"
        assert agent.name == "test-agent"
        assert agent.capabilities == ["messaging", "calendar"]
        assert agent.is_primary is True
        assert agent.status == "offline"  # Default

    def test_get_agent(self, store: AgentStore) -> None:
        """Test retrieving an agent."""
        store.create({
            "agent_id": "agent-2",
            "node_id": "node-2",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
        })

        agent = store.get("agent-2")
        assert agent is not None
        assert agent.agent_id == "agent-2"

        # Non-existent agent
        assert store.get("nonexistent") is None

    def test_update_agent(self, store: AgentStore) -> None:
        """Test updating an agent."""
        store.create({
            "agent_id": "agent-3",
            "node_id": "node-3",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
        })

        updated = store.update("agent-3", status="online", priority=2, is_primary=True)
        assert updated is not None
        assert updated.status == "online"
        assert updated.priority == 2
        assert updated.is_primary is True

    def test_delete_agent(self, store: AgentStore) -> None:
        """Test deleting an agent."""
        store.create({
            "agent_id": "agent-4",
            "node_id": "node-4",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
        })

        assert store.delete("agent-4") is True
        assert store.get("agent-4") is None
        assert store.delete("nonexistent") is False

    def test_list_agents(self, store: AgentStore) -> None:
        """Test listing agents with filters."""
        store.create({
            "agent_id": "agent-5",
            "node_id": "node-5",
            "colony_id": "colony-1",
            "name": "agent-a",
            "connection_mode": "local",
            "capabilities": ["messaging"],
            "status": "online",
        })
        store.create({
            "agent_id": "agent-6",
            "node_id": "node-6",
            "colony_id": "colony-1",
            "name": "agent-b",
            "connection_mode": "remote",
            "capabilities": ["calendar"],
            "status": "offline",
        })
        store.create({
            "agent_id": "agent-7",
            "node_id": "node-7",
            "colony_id": "colony-1",
            "name": "agent-c",
            "connection_mode": "local",
            "capabilities": ["messaging", "calendar"],
            "status": "online",
        })

        # List all
        all_agents = store.list()
        assert len(all_agents) == 3

        # Filter by status
        online = store.list(status="online")
        assert len(online) == 2

        # Filter by capability
        messaging = store.list(capability="messaging")
        assert len(messaging) == 2

    def test_revoke_agent(self, store: AgentStore) -> None:
        """Test revoking an agent."""
        store.create({
            "agent_id": "agent-8",
            "node_id": "node-8",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
            "status": "online",
        })

        store.revoke("agent-8")
        agent = store.get("agent-8")
        assert agent is not None
        assert agent.status == "revoked"

    def test_set_online_offline(self, store: AgentStore) -> None:
        """Test setting agent online/offline."""
        store.create({
            "agent_id": "agent-9",
            "node_id": "node-9",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
        })

        store.set_online("agent-9")
        agent = store.get("agent-9")
        assert agent is not None
        assert agent.status == "online"
        assert agent.last_seen_at is not None

        store.set_offline("agent-9")
        agent = store.get("agent-9")
        assert agent is not None
        assert agent.status == "offline"

    def test_increment_assignments(self, store: AgentStore) -> None:
        """Test incrementing/decrementing assignment count."""
        store.create({
            "agent_id": "agent-10",
            "node_id": "node-10",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
            "current_assignments": 0,
        })

        store.increment_assignments("agent-10")
        agent = store.get("agent-10")
        assert agent is not None
        assert agent.current_assignments == 1

        store.increment_assignments("agent-10")
        agent = store.get("agent-10")
        assert agent is not None
        assert agent.current_assignments == 2

        store.decrement_assignments("agent-10")
        agent = store.get("agent-10")
        assert agent is not None
        assert agent.current_assignments == 1

    def test_metadata_serialization(self, store: AgentStore) -> None:
        """Test that metadata is properly serialized/deserialized."""
        metadata = AgentMetadata(
            hostname="test-host",
            platform="darwin",
            version="0.7.0",
            harness="openclaw",
            tz="America/El_Salvador",
        )

        store.create({
            "agent_id": "agent-11",
            "node_id": "node-11",
            "colony_id": "colony-1",
            "name": "test-agent",
            "connection_mode": "local",
            "metadata": metadata,
        })

        agent = store.get("agent-11")
        assert agent is not None
        assert agent.metadata.hostname == "test-host"
        assert agent.metadata.platform == "darwin"
        assert agent.metadata.version == "0.7.0"


class TestInviteStore:
    """Tests for InviteStore operations."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InviteStore:
        """Create a fresh InviteStore for each test."""
        return InviteStore(state_dir=tmp_path)

    def test_create_invite(self, store: InviteStore) -> None:
        """Test creating an invite."""
        invite = store.create(
            colony_id="colony-1",
            capabilities=["messaging", "calendar"],
            is_primary=False,
            max_concurrent=5,
            expires_seconds=900,
            label="Test invite",
        )

        assert invite is not None
        assert invite["setup_code"].startswith("COLONY-")
        assert invite["capabilities"] == ["messaging", "calendar"]
        assert invite["is_primary"] is False

    def test_use_invite(self, store: InviteStore) -> None:
        """Test using an invite."""
        invite = store.create(
            colony_id="colony-1",
            capabilities=["messaging"],
            expires_seconds=900,
        )

        setup_code = invite["setup_code"]
        
        # Use the invite
        used = store.use(setup_code, "node-1", "agent-1")
        assert used is not None
        assert used["used_by_agent_id"] == "agent-1"

        # Try to use again - should fail
        with pytest.raises(ValueError, match="already used"):
            store.use(setup_code, "node-2", "agent-2")

    def test_expired_invite(self, store: InviteStore) -> None:
        """Test that expired invites cannot be used."""
        # Create already-expired invite
        invite = store.create(
            colony_id="colony-1",
            capabilities=["messaging"],
            expires_seconds=-1,  # Expired
        )

        setup_code = invite["setup_code"]
        
        with pytest.raises(ValueError, match="expired"):
            store.use(setup_code, "node-1", "agent-1")

    def test_invalid_setup_code(self, store: InviteStore) -> None:
        """Test that invalid setup codes are rejected."""
        with pytest.raises(ValueError, match="Invalid setup code"):
            store.use("INVALID-CODE", "node-1", "agent-1")

    def test_rate_limiting(self, store: InviteStore) -> None:
        """Test rate limiting on failed attempts."""
        # Try invalid codes until locked out
        for _ in range(5):
            try:
                store.use("INVALID-CODE", "node-1", "agent-1")
            except ValueError:
                pass

        # Should be locked out now
        with pytest.raises(ValueError, match="locked"):
            store.use("ANY-CODE", "node-1", "agent-1")


class TestAgentModel:
    """Tests for Agent model methods."""

    def test_load_property(self) -> None:
        """Test agent load calculation."""
        agent = Agent(
            agent_id="test",
            node_id="node-1",
            colony_id="colony-1",
            name="test",
            max_concurrent=5,
            current_assignments=2,
        )
        assert agent.load == 0.4

        # Full load
        agent.current_assignments = 5
        assert agent.load == 1.0

    def test_has_capacity(self) -> None:
        """Test capacity check."""
        agent = Agent(
            agent_id="test",
            node_id="node-1",
            colony_id="colony-1",
            name="test",
            max_concurrent=5,
            current_assignments=2,
            status="online",
        )
        assert agent.has_capacity is True

        # No capacity
        agent.current_assignments = 5
        assert agent.has_capacity is False

        # Offline
        agent.status = "offline"
        assert agent.has_capacity is False

    def test_can_handle_type(self) -> None:
        """Test initiative type filtering."""
        agent = Agent(
            agent_id="test",
            node_id="node-1",
            colony_id="colony-1",
            name="test",
            excluded_types=["coding"],
            included_types=["messaging", "calendar"],
        )

        assert agent.can_handle_type("messaging") is True
        assert agent.can_handle_type("coding") is False
        assert agent.can_handle_type("research") is False  # Not in included_types

"""Tests for multi-agent API endpoints."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
import pytest
from fastapi.testclient import TestClient

from colony_sidecar.agents.store import AgentStore, InviteStore
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.initiatives.assignment import AssignmentEngine
from colony_sidecar.agents.websocket import WebSocketManager


class TestAgentEndpoints:
    """Tests for agent management API endpoints."""

    @pytest.fixture
    def stores(self, tmp_path: Path) -> tuple[AgentStore, InviteStore, InitiativeStore]:
        """Create fresh stores for testing."""
        return (
            AgentStore(state_dir=tmp_path),
            InviteStore(state_dir=tmp_path),
            InitiativeStore(state_dir=tmp_path),
        )

    @pytest.fixture
    def client(self, stores: tuple[AgentStore, InviteStore, InitiativeStore]) -> TestClient:
        """Create a test client with stores injected."""
        from colony_sidecar.api.routers.host import (
            set_agent_store,
            set_invite_store,
            set_initiative_store,
            set_assignment_engine,
            set_websocket_manager,
        )
        from colony_sidecar.server import create_app

        agent_store, invite_store, initiative_store = stores

        set_agent_store(agent_store)
        set_invite_store(invite_store)
        set_initiative_store(initiative_store)
        
        assignment_engine = AssignmentEngine(agent_store, initiative_store)
        set_assignment_engine(assignment_engine)
        
        websocket_manager = WebSocketManager(agent_store, initiative_store)
        set_websocket_manager(websocket_manager)

        app = create_app()
        return TestClient(app)

    def test_invite_agent(self, client: TestClient) -> None:
        """Test POST /agents/invite."""
        response = client.post(
            "/v1/host/agents/invite",
            json={
                "expires_in_seconds": 900,
                "max_uses": 1,
                "granted_capabilities": ["messaging", "calendar"],
                "granted_is_primary": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "code" in data
        assert data["code"].startswith("COLONY-")
        assert "setup_command" in data
        assert "expires_at" in data

    def test_connect_agent_invalid_code(self, client: TestClient) -> None:
        """Test POST /agents/connect with invalid code."""
        response = client.post(
            "/v1/host/agents/connect",
            json={
                "setup_code": "INVALID-CODE",
                "name": "test-agent",
            },
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["detail"]

    def test_connect_agent_valid(self, client: TestClient) -> None:
        """Test POST /agents/connect with valid code."""
        # Create invite first
        invite_resp = client.post(
            "/v1/host/agents/invite",
            json={
                "granted_capabilities": ["messaging"],
                "granted_is_primary": False,
            },
        )
        code = invite_resp.json()["code"]

        # Connect using the code
        response = client.post(
            "/v1/host/agents/connect",
            json={
                "setup_code": code,
                "name": "test-agent",
                "node_public_key": "test-key",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "agent_id" in data
        assert "node_id" in data
        assert data["capabilities"] == ["messaging"]

    def test_register_local_agent(self, client: TestClient) -> None:
        """Test POST /agents/register for local agent."""
        response = client.post(
            "/v1/host/agents/register",
            json={
                "name": "local-agent",
                "connection_mode": "local",
                "capabilities": ["messaging"],
                "is_primary": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "agent_id" in data
        assert "node_id" in data
        assert data.get("websocket_url") is None  # Local mode

    def test_list_agents(self, client: TestClient) -> None:
        """Test GET /agents."""
        # Create some agents
        client.post(
            "/v1/host/agents/register",
            json={"name": "agent-1", "connection_mode": "local", "capabilities": ["messaging"]},
        )
        client.post(
            "/v1/host/agents/register",
            json={"name": "agent-2", "connection_mode": "local", "capabilities": ["calendar"]},
        )

        response = client.get("/v1/host/agents")
        assert response.status_code == 200
        data = response.json()
        assert len(data["agents"]) == 2

    def test_list_agents_filter_status(self, client: TestClient) -> None:
        """Test GET /agents?status=online."""
        # Create agent
        resp = client.post(
            "/v1/host/agents/register",
            json={"name": "agent-1", "connection_mode": "local", "capabilities": ["messaging"]},
        )
        agent_id = resp.json()["agent_id"]

        # Update status to online
        client.post(f"/v1/host/agents/{agent_id}/heartbeat", json={"status": "online"})

        # Filter by online
        response = client.get("/v1/host/agents?status=online")
        assert response.status_code == 200
        data = response.json()
        assert len(data["agents"]) == 1
        assert data["agents"][0]["status"] == "online"

    def test_get_agent(self, client: TestClient) -> None:
        """Test GET /agents/{agent_id}."""
        resp = client.post(
            "/v1/host/agents/register",
            json={"name": "agent-1", "connection_mode": "local", "capabilities": ["messaging"]},
        )
        agent_id = resp.json()["agent_id"]

        response = client.get(f"/v1/host/agents/{agent_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == agent_id
        assert data["name"] == "agent-1"

    def test_get_agent_not_found(self, client: TestClient) -> None:
        """Test GET /agents/{agent_id} with non-existent agent."""
        response = client.get("/v1/host/agents/nonexistent")
        assert response.status_code == 404

    def test_agent_heartbeat(self, client: TestClient) -> None:
        """Test POST /agents/{agent_id}/heartbeat."""
        resp = client.post(
            "/v1/host/agents/register",
            json={"name": "agent-1", "connection_mode": "local", "capabilities": ["messaging"]},
        )
        agent_id = resp.json()["agent_id"]

        response = client.post(
            f"/v1/host/agents/{agent_id}/heartbeat",
            json={"status": "online", "current_assignments": 2},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        # Verify status updated
        agent_resp = client.get(f"/v1/host/agents/{agent_id}")
        assert agent_resp.json()["status"] == "online"
        assert agent_resp.json()["current_assignments"] == 2

    def test_revoke_agent(self, client: TestClient) -> None:
        """Test DELETE /agents/{agent_id}."""
        resp = client.post(
            "/v1/host/agents/register",
            json={"name": "agent-1", "connection_mode": "local", "capabilities": ["messaging"]},
        )
        agent_id = resp.json()["agent_id"]

        response = client.delete(f"/v1/host/agents/{agent_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"

        # Verify agent is revoked
        agent_resp = client.get(f"/v1/host/agents/{agent_id}")
        assert agent_resp.json()["status"] == "revoked"


class TestInitiativeEndpoints:
    """Tests for initiative management API endpoints."""

    @pytest.fixture
    def stores(self, tmp_path: Path) -> tuple[AgentStore, InitiativeStore]:
        """Create fresh stores for testing."""
        return (
            AgentStore(state_dir=tmp_path),
            InitiativeStore(state_dir=tmp_path),
        )

    @pytest.fixture
    def client(self, stores: tuple[AgentStore, InitiativeStore]) -> TestClient:
        """Create a test client with stores injected."""
        from colony_sidecar.api.routers.host import (
            set_agent_store,
            set_initiative_store,
            set_assignment_engine,
            set_websocket_manager,
        )
        from colony_sidecar.server import create_app

        agent_store, initiative_store = stores

        set_agent_store(agent_store)
        set_initiative_store(initiative_store)
        
        assignment_engine = AssignmentEngine(agent_store, initiative_store)
        set_assignment_engine(assignment_engine)
        
        websocket_manager = WebSocketManager(agent_store, initiative_store)
        set_websocket_manager(websocket_manager)

        app = create_app()
        return TestClient(app)

    def test_create_initiative(self, client: TestClient) -> None:
        """Test POST /initiatives."""
        response = client.post(
            "/v1/host/initiatives",
            json={
                "initiative_type": "notification",
                "description": "Test notification",
                "priority": 80,
                "timeout_seconds": 300,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["initiative_type"] == "notification"
        assert data["status"] == "pending"

    def test_list_initiatives(self, client: TestClient) -> None:
        """Test GET /initiatives."""
        client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "notification", "description": "Test 1"},
        )
        client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "task", "description": "Test 2"},
        )

        response = client.get("/v1/host/initiatives")
        assert response.status_code == 200
        data = response.json()
        assert len(data["initiatives"]) == 2

    def test_get_initiative(self, client: TestClient) -> None:
        """Test GET /initiatives/{id}."""
        resp = client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "notification", "description": "Test"},
        )
        initiative_id = resp.json()["id"]

        response = client.get(f"/v1/host/initiatives/{initiative_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == initiative_id

    def test_claim_initiative(
        self,
        client: TestClient,
        stores: tuple[AgentStore, InitiativeStore],
    ) -> None:
        """Test POST /initiatives/{id}/claim."""
        agent_store, _ = stores
        
        # Create agent
        agent_store.create({
            "agent_id": "agent-1",
            "node_id": "node-1",
            "colony_id": "colony-1",
            "name": "test",
            "connection_mode": "local",
            "capabilities": ["messaging"],
        })

        # Create initiative
        resp = client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "notification", "description": "Test"},
        )
        initiative_id = resp.json()["id"]

        # Claim it
        response = client.post(
            f"/v1/host/initiatives/{initiative_id}/claim",
            json={"agent_id": "agent-1"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "claimed"

    def test_complete_initiative(
        self,
        client: TestClient,
        stores: tuple[AgentStore, InitiativeStore],
    ) -> None:
        """Test POST /initiatives/{id}/complete."""
        agent_store, _ = stores
        
        # Create agent
        agent_store.create({
            "agent_id": "agent-1",
            "node_id": "node-1",
            "colony_id": "colony-1",
            "name": "test",
            "connection_mode": "local",
        })

        # Create and assign initiative
        resp = client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "notification", "description": "Test"},
        )
        initiative_id = resp.json()["id"]
        client.post(
            f"/v1/host/initiatives/{initiative_id}/claim",
            json={"agent_id": "agent-1"},
        )

        # Complete it
        response = client.post(
            f"/v1/host/initiatives/{initiative_id}/complete",
            json={
                "agent_id": "agent-1",
                "result": {"status": "success"},
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    def test_cancel_initiative(self, client: TestClient) -> None:
        """Test POST /initiatives/{id}/cancel."""
        resp = client.post(
            "/v1/host/initiatives",
            json={"initiative_type": "notification", "description": "Test"},
        )
        initiative_id = resp.json()["id"]

        response = client.post(
            f"/v1/host/initiatives/{initiative_id}/cancel",
            json={"reason": "User cancelled"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

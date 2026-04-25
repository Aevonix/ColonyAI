"""Tests for Colony Agent SDK."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import pytest
import asyncio

from colony_sidecar.agent import AgentClient, AgentConfig
from colony_sidecar.agent.models import NodeCertificate


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_load_from_dict(self) -> None:
        """Test loading config from dictionary."""
        config = AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
            capabilities=["messaging"],
            is_primary=True,
            max_concurrent=5,
        )

        assert config.agent_id == "agent-1"
        assert config.name == "test-agent"
        assert config.capabilities == ["messaging"]

    def test_optional_fields(self) -> None:
        """Test optional fields with defaults."""
        config = AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
        )

        assert config.capabilities == []
        assert config.is_primary is False
        assert config.max_concurrent == 5
        assert config.priority == 1
        assert config.excluded_types == []
        assert config.metadata == {}

    def test_node_certificate(self) -> None:
        """Test node certificate parsing."""
        config = AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
            node_cert=NodeCertificate(
                colony_id="colony-1",
                node_id="node-1",
                signature="sig-123",
                issued_at="2026-04-25T00:00:00Z",
            ),
        )

        assert config.node_cert is not None
        assert config.node_cert.signature == "sig-123"


class TestAgentClient:
    """Tests for AgentClient."""

    @pytest.fixture
    def config(self, tmp_path: Path) -> AgentConfig:
        """Create a test config."""
        return AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
            websocket_url="ws://localhost:7777/v1/host/agents/agent-1/stream",
            capabilities=["messaging"],
            node_cert=NodeCertificate(
                colony_id="colony-1",
                node_id="node-1",
                signature="test-sig",
                issued_at="2026-04-25T00:00:00Z",
            ),
        )

    @pytest.fixture
    def logger(self) -> Mock:
        """Create a mock logger."""
        return Mock()

    def test_init_with_config(self, config: AgentConfig, logger: Mock) -> None:
        """Test initialization with config object."""
        client = AgentClient(config=config, logger=logger)
        assert client.config.agent_id == "agent-1"

    def test_on_initiative_handler(self, config: AgentConfig, logger: Mock) -> None:
        """Test setting initiative handler."""
        client = AgentClient(config=config, logger=logger)

        handler_called = False

        @client.on_initiative
        async def handler(initiative):
            nonlocal handler_called
            handler_called = True

        assert client._handlers["initiative"] is not None

    @pytest.mark.asyncio
    async def test_acknowledge(self, config: AgentConfig, logger: Mock) -> None:
        """Test acknowledge method."""
        client = AgentClient(config=config, logger=logger)
        client._ws = Mock()
        client._ws.send = Mock()
        client._ws.readyState = 1  # WebSocket.OPEN

        result = await client.acknowledge("init-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_complete(self, config: AgentConfig, logger: Mock) -> None:
        """Test complete method."""
        client = AgentClient(config=config, logger=logger)
        client._ws = Mock()
        client._ws.send = Mock()
        client._ws.readyState = 1

        result = await client.complete("init-1", result="Done")
        assert result is True

    @pytest.mark.asyncio
    async def test_fail(self, config: AgentConfig, logger: Mock) -> None:
        """Test fail method."""
        client = AgentClient(config=config, logger=logger)
        client._ws = Mock()
        client._ws.send = Mock()
        client._ws.readyState = 1

        result = await client.fail("init-1", reason="Error occurred")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_when_disconnected(self, config: AgentConfig, logger: Mock) -> None:
        """Test send fails when disconnected."""
        client = AgentClient(config=config, logger=logger)
        client._ws = None

        result = await client.acknowledge("init-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_sequencing(self, config: AgentConfig, logger: Mock) -> None:
        """Test message sequencing."""
        client = AgentClient(config=config, logger=logger)
        client._ws = Mock()
        client._ws.send = Mock()
        client._ws.readyState = 1

        # Send multiple messages
        await client.acknowledge("init-1")
        seq1 = client._seq
        await client.acknowledge("init-2")
        seq2 = client._seq

        assert seq2 == seq1 + 1


class TestAgentClientIntegration:
    """Integration tests for AgentClient with mock WebSocket."""

    @pytest.fixture
    def config(self) -> AgentConfig:
        """Create a test config."""
        return AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
            websocket_url="ws://localhost:7777/test",
        )

    @pytest.fixture
    def logger(self) -> Mock:
        """Create a mock logger."""
        return Mock()

    @pytest.mark.asyncio
    async def test_handle_initiative_message(self, config: AgentConfig, logger: Mock) -> None:
        """Test handling initiative message from Colony."""
        client = AgentClient(config=config, logger=logger)

        received_initiatives = []

        @client.on_initiative
        async def handle_initiative(initiative):
            received_initiatives.append(initiative)

        # Simulate receiving initiative message
        await client._handle_message({
            "type": "initiative",
            "seq": 1,
            "initiative": {
                "id": "init-1",
                "type": "notification",
                "description": "Test initiative",
                "priority": 0.8,
                "assigned_at": "2026-04-25T00:00:00Z",
            },
        })

        assert len(received_initiatives) == 1
        assert received_initiatives[0]["id"] == "init-1"

    @pytest.mark.asyncio
    async def test_handle_ping_message(self, config: AgentConfig, logger: Mock) -> None:
        """Test handling ping message."""
        client = AgentClient(config=config, logger=logger)
        client._ws = Mock()
        client._ws.send = Mock()
        client._ws.readyState = 1

        # Handle ping
        await client._handle_message({"type": "ping", "seq": 1})

        # Should have sent pong (checked via send being called)
        assert client._ws.send.called

    @pytest.mark.asyncio
    async def test_handle_disconnect_message(self, config: AgentConfig, logger: Mock) -> None:
        """Test handling disconnect message."""
        client = AgentClient(config=config, logger=logger)

        disconnect_reason = None

        @client.on_disconnect
        def handle_disconnect(reason: str):
            nonlocal disconnect_reason
            disconnect_reason = reason

        # Handle disconnect
        await client._handle_message({
            "type": "disconnect",
            "reason": "agent_revoked",
        })

        # Should have triggered disconnect
        assert disconnect_reason == "agent_revoked"


class TestReconnection:
    """Tests for reconnection logic."""

    @pytest.fixture
    def config(self) -> AgentConfig:
        """Create a test config."""
        return AgentConfig(
            agent_id="agent-1",
            node_id="node-1",
            colony_id="colony-1",
            name="test-agent",
            websocket_url="ws://localhost:7777/test",
        )

    @pytest.fixture
    def logger(self) -> Mock:
        """Create a mock logger."""
        return Mock()

    def test_exponential_backoff(self, config: AgentConfig, logger: Mock) -> None:
        """Test exponential backoff for reconnection."""
        client = AgentClient(config=config, logger=logger)

        # Initial delay
        assert client._reconnect_delay == 1000

        # After one reconnect
        client._schedule_reconnect()
        assert client._reconnect_delay == 2000

        # After another
        client._schedule_reconnect()
        assert client._reconnect_delay == 4000

        # Cap at max
        client._reconnect_delay = 50000
        client._schedule_reconnect()
        assert client._reconnect_delay == 60000  # max_reconnect_delay

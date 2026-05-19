"""Colony Agent Client — WebSocket client for remote agents.

Usage:
    from colony.agent import AgentClient

    client = AgentClient(config_path="~/.colony/agent.json")

    @client.on_initiative
    async def handle_initiative(initiative):
        print(f"Received: {initiative['description']}")
        await client.acknowledge(initiative["id"])
        result = await process_initiative(initiative)
        await client.complete(initiative["id"], result=result)

    await client.start()
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import websockets

from .models import AgentConfig


class AgentClient:
    """WebSocket client for remote Colony agents.

    Handles:
    - WebSocket connection with auth
    - Heartbeat loop
    - Initiative delivery
    - Reconnection with exponential backoff
    """

    def __init__(
        self,
        config_path: str = "~/.colony/agent.json",
        config: Optional[AgentConfig] = None,
    ) -> None:
        """Initialize agent client.

        Args:
            config_path: Path to agent config file
            config: Optional pre-loaded config (skips file read)
        """
        if config:
            self.config = config
        else:
            config_file = Path(config_path).expanduser()
            if not config_file.exists():
                raise FileNotFoundError(f"Agent config not found: {config_file}")
            self.config = AgentConfig.parse_file(config_file)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._seq = 0
        self._pending_acks: Dict[int, asyncio.Future] = {}
        self._handlers: Dict[str, Callable] = {
            "initiative": None,
            "config": None,
            "disconnect": None,
        }

        # Reconnection state
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    def on_initiative(self, handler: Callable) -> Callable:
        """Register initiative handler."""
        self._handlers["initiative"] = handler
        return handler

    def on_config(self, handler: Callable) -> Callable:
        """Register config update handler."""
        self._handlers["config"] = handler
        return handler

    def on_disconnect(self, handler: Callable) -> Callable:
        """Register disconnect handler."""
        self._handlers["disconnect"] = handler
        return handler

    async def start(self) -> None:
        """Connect to Colony and start message loop."""
        if not self.config.websocket_url:
            raise ValueError("No websocket_url in config (local mode?)")

        self._running = True

        while self._running:
            try:
                await self._connect()
                await self._message_loop()
            except Exception as e:
                if not self._running:
                    break
                print(f"Connection error: {e}, reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay,
                )

    async def stop(self) -> None:
        """Disconnect from Colony."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect(self) -> None:
        """Establish WebSocket connection with auth."""
        headers = {}
        if self.config.node_cert:
            # Sign challenge with node cert
            headers["Authorization"] = f"Bearer {self.config.node_cert.signature}"
        headers["X-Agent-Id"] = self.config.agent_id

        self._ws = await websockets.connect(
            self.config.websocket_url,
            extra_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        )
        self._reconnect_delay = 1.0  # Reset on successful connection
        print(f"Connected to Colony: {self.config.websocket_url}")

        # Start heartbeat task
        asyncio.create_task(self._heartbeat_loop())

    async def _message_loop(self) -> None:
        """Process incoming messages."""
        if not self._ws:
            return

        async for message in self._ws:
            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                print(f"Invalid JSON: {message}")
            except Exception as e:
                print(f"Error handling message: {e}")

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message."""
        msg_type = data.get("type")
        seq = data.get("seq")

        # Handle ACKs for our messages
        if msg_type == "ack" and seq in self._pending_acks:
            self._pending_acks[seq].set_result(data)
            return

        # Handle initiative delivery
        if msg_type == "initiative":
            if self._handlers["initiative"]:
                await self._handlers["initiative"](data.get("initiative", {}))
            # Auto-ack receipt
            await self._send_ack(seq)

        # Handle config update
        elif msg_type == "config":
            if self._handlers["config"]:
                await self._handlers["config"](data.get("config", {}))

        # Handle disconnect notice
        elif msg_type == "disconnect":
            if self._handlers["disconnect"]:
                await self._handlers["disconnect"](data.get("reason", "unknown"))
            await self.stop()

        # Handle ping
        elif msg_type == "ping":
            await self._send({"type": "pong", "seq": seq})

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        # The colony side tracks current_assignments via the agents table
        # (incremented/decremented when initiatives are assigned/completed),
        # so the heartbeat does not need to carry it — the server only uses
        # this message to update last_seen_at.
        while self._running and self._ws:
            try:
                await self._send({
                    "type": "heartbeat",
                    "status": "online",
                })
                await asyncio.sleep(30)
            except Exception:
                break

    async def _send(self, message: Dict[str, Any]) -> bool:
        """Send message with sequencing."""
        if not self._ws:
            return False

        self._seq += 1
        message["seq"] = self._seq

        try:
            await self._ws.send(json.dumps(message))
            return True
        except Exception as e:
            print(f"Send error: {e}")
            return False

    async def _send_ack(self, seq: Optional[int]) -> None:
        """Send acknowledgment."""
        await self._send({"type": "ack", "ack_seq": seq})

    # --- Public API ---

    async def acknowledge(self, initiative_id: str) -> bool:
        """Acknowledge initiative receipt."""
        return await self._send({
            "type": "initiative_ack",
            "initiative_id": initiative_id,
        })

    async def complete(
        self,
        initiative_id: str,
        result: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark initiative as completed."""
        return await self._send({
            "type": "initiative_complete",
            "initiative_id": initiative_id,
            "result": result,
            "result_metadata": metadata or {},
        })

    async def fail(
        self,
        initiative_id: str,
        reason: str,
        retry: bool = True,
    ) -> bool:
        """Mark initiative as failed."""
        return await self._send({
            "type": "initiative_fail",
            "initiative_id": initiative_id,
            "reason": reason,
            "retry": retry,
        })

    async def delegate(
        self,
        initiative_id: str,
        reason: str,
        target_agent_id: Optional[str] = None,
    ) -> bool:
        """Delegate initiative to another agent."""
        return await self._send({
            "type": "initiative_delegate",
            "initiative_id": initiative_id,
            "reason": reason,
            "target_agent_id": target_agent_id,
        })

    async def update_status(
        self,
        status: str = "online",
        current_assignments: int = 0,
    ) -> bool:
        """Update agent status."""
        return await self._send({
            "type": "status_update",
            "status": status,
            "current_assignments": current_assignments,
        })

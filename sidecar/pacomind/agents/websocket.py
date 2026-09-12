"""WebSocket manager for remote agent connections.

Provides:
- Challenge-response authentication
- Connection management
- Initiative delivery
- Ping/pong timeout
- Circuit breaker integration
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections for remote agents."""

    # Timeouts
    AUTH_TIMEOUT = 30  # Seconds to complete auth
    SESSION_TIMEOUT = timedelta(hours=24)  # Re-auth after 24h
    PING_INTERVAL = 30  # Seconds between pings
    PONG_TIMEOUT = 10  # Seconds to wait for pong
    ACK_TIMEOUT = 30  # Seconds to wait for initiative ACK
    MAX_TIMESTAMP_SKEW = 300  # 5 minutes

    # Limits
    MAX_CONNECTIONS = 100
    MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB
    MAX_CONNECT_ATTEMPTS = 5
    ATTEMPT_WINDOW = timedelta(minutes=1)

    def __init__(
        self,
        agent_store: Any,
        initiative_store: Any,
        circuit_breaker: Optional[Any] = None,
        dead_letter_queue: Optional[Any] = None,
    ):
        self._agent_store = agent_store
        self._initiative_store = initiative_store
        self._circuit_breaker = circuit_breaker
        self._dlq = dead_letter_queue

        # Active connections
        self._active_connections: Dict[str, WebSocket] = {}
        self._connection_info: Dict[str, Dict[str, Any]] = {}

        # Rate limiting
        self._connect_attempts: Dict[str, List[float]] = defaultdict(list)

        # Pending auth challenges
        self._pending_challenges: Dict[str, str] = {}

        # Pending ACKs
        self._pending_acks: Dict[str, asyncio.Future] = {}

        # Message sequencing
        self._seq: Dict[str, int] = defaultdict(int)

        # Running tasks
        self._tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Connection Handling
    # ------------------------------------------------------------------

    async def handle_connection(
        self,
        websocket: WebSocket,
        agent_id: str,
        client_ip: str,
    ) -> None:
        """Handle WebSocket connection from remote agent."""
        # Check max connections
        if len(self._active_connections) >= self.MAX_CONNECTIONS:
            await websocket.close(code=1013, reason="Max connections reached")
            return

        # Rate limit check
        if not await self._check_rate_limit(client_ip):
            await websocket.close(code=4003, reason="Rate limited")
            return

        # Get agent
        agent = await self._agent_store.get(agent_id)
        if not agent:
            await websocket.close(code=4004, reason="Agent not found")
            return

        # Check if revoked
        if self._agent_store.is_revoked(agent.node_id):
            await websocket.close(code=4003, reason="Agent revoked")
            return

        # Check if can reconnect
        if not agent.status.can_reconnect():
            await websocket.close(code=4003, reason=f"Agent status: {agent.status}")
            return

        # Accept connection
        await websocket.accept()

        try:
            # Authenticate
            if not await self._authenticate(websocket, agent, client_ip):
                return

            # Register connection
            self._active_connections[agent_id] = websocket
            self._connection_info[agent_id] = {
                "connected_at": datetime.now(timezone.utc),
                "client_ip": client_ip,
                "last_pong": time.time(),
            }

            # Mark agent online
            await self._agent_store.set_online(
                agent_id,
                websocket_connected=True,
                metadata={"last_connection_ip": client_ip},
            )

            logger.info("Agent %s connected from %s", agent_id, client_ip)

            # Start ping task
            ping_task = asyncio.create_task(
                self._ping_task(agent_id, websocket)
            )
            self._tasks.add(ping_task)

            # Message loop
            await self._message_loop(agent_id, websocket)

        except WebSocketDisconnect:
            logger.info("Agent %s disconnected", agent_id)
        except Exception as e:
            logger.error("Agent %s WebSocket error: %s", agent_id, e)
        finally:
            await self._cleanup_connection(agent_id)

    async def _check_rate_limit(self, ip: str) -> bool:
        """Check if IP is rate limited."""
        now = time.time()
        window_start = now - self.ATTEMPT_WINDOW.total_seconds()

        # Clean old attempts
        self._connect_attempts[ip] = [
            t for t in self._connect_attempts[ip] if t > window_start
        ]

        # Check limit
        if len(self._connect_attempts[ip]) >= self.MAX_CONNECT_ATTEMPTS:
            return False

        # Record attempt
        self._connect_attempts[ip].append(now)
        return True

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(
        self,
        websocket: WebSocket,
        agent: Any,
        client_ip: str,
    ) -> bool:
        """Challenge-response authentication."""
        import secrets

        # Generate challenge
        nonce = secrets.token_hex(16)
        timestamp = int(time.time())

        self._pending_challenges[agent.agent_id] = nonce

        # Send challenge
        await websocket.send_json({
            "type": "auth_challenge",
            "nonce": nonce,
            "timestamp": timestamp,
        })

        # Wait for response
        try:
            data = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=self.AUTH_TIMEOUT,
            )
            response = json.loads(data)
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="Auth timeout")
            return False
        except json.JSONDecodeError:
            await websocket.close(code=4001, reason="Invalid JSON")
            return False

        # Verify response
        if response.get("type") != "auth_response":
            await websocket.close(code=4001, reason="Expected auth_response")
            return False

        if response.get("nonce") != nonce:
            await websocket.close(code=4001, reason="Nonce mismatch")
            return False

        # Verify timestamp (replay prevention)
        response_ts = response.get("timestamp", 0)
        if abs(time.time() - response_ts) > self.MAX_TIMESTAMP_SKEW:
            await websocket.close(code=4001, reason="Timestamp skew too large")
            return False

        # Verify signature
        signed_payload = f"{nonce}:{response_ts}:{agent.agent_id}".encode()
        signature = response.get("signature", "")

        if not self._verify_signature(agent.node_cert, signed_payload, signature):
            await websocket.close(code=4003, reason="Invalid signature")
            return False

        # Clean up challenge
        self._pending_challenges.pop(agent.agent_id, None)

        # Send success
        await websocket.send_json({"type": "connected"})

        return True

    def _verify_signature(
        self,
        cert: Optional[Dict[str, Any]],
        message: bytes,
        signature_hex: str,
    ) -> bool:
        """Verify Ed25519 signature."""
        if not cert:
            return False

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pubkey_hex = cert.get("node_public_key_ed25519")
            if not pubkey_hex:
                return False

            pub_bytes = bytes.fromhex(pubkey_hex)
            pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
            sig_bytes = bytes.fromhex(signature_hex)
            pub_key.verify(sig_bytes, message)
            return True
        except Exception as e:
            logger.warning("Signature verification failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Message Handling
    # ------------------------------------------------------------------

    async def _message_loop(
        self,
        agent_id: str,
        websocket: WebSocket,
    ) -> None:
        """Handle incoming WebSocket messages."""
        while agent_id in self._active_connections:
            try:
                data = await websocket.receive_text()

                # Check message size
                if len(data) > self.MAX_MESSAGE_SIZE:
                    await websocket.send_json({
                        "type": "error",
                        "error": "message_too_large",
                        "message": f"Message exceeds {self.MAX_MESSAGE_SIZE} bytes",
                    })
                    continue

                # Parse message
                try:
                    message = json.loads(data)
                except json.JSONDecodeError as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": "invalid_json",
                        "message": str(e),
                    })
                    continue

                # Validate structure
                if not isinstance(message, dict):
                    await websocket.send_json({
                        "type": "error",
                        "error": "invalid_message",
                        "message": "Message must be a JSON object",
                    })
                    continue

                msg_type = message.get("type")
                if not msg_type:
                    await websocket.send_json({
                        "type": "error",
                        "error": "missing_type",
                        "message": "Message must have 'type' field",
                    })
                    continue

                # Handle message
                await self._handle_message(agent_id, websocket, message)

            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error("Message handling error for agent %s: %s", agent_id, e)

    async def _handle_message(
        self,
        agent_id: str,
        websocket: WebSocket,
        message: Dict[str, Any],
    ) -> None:
        """Handle specific message type."""
        msg_type = message.get("type")

        if msg_type == "pong":
            # Update last pong time
            if agent_id in self._connection_info:
                self._connection_info[agent_id]["last_pong"] = time.time()

        elif msg_type == "acknowledge":
            # Initiative acknowledgment
            initiative_id = message.get("initiative_id")
            if initiative_id and initiative_id in self._pending_acks:
                future = self._pending_acks.pop(initiative_id)
                if not future.done():
                    future.set_result(True)

                # Update store
                await self._initiative_store.acknowledge(initiative_id, agent_id)

        elif msg_type == "complete":
            # Initiative completed
            initiative_id = message.get("initiative_id")
            result = message.get("result")
            metadata = message.get("metadata", {})

            await self._initiative_store.complete(
                initiative_id,
                agent_id,
                result=result,
                result_metadata=metadata,
            )
            await self._agent_store.decrement_assignments(agent_id)

        elif msg_type == "fail":
            # Initiative failed
            initiative_id = message.get("initiative_id")
            reason = message.get("reason", "Unknown")
            retry = message.get("retry", False)

            await self._initiative_store.fail(
                initiative_id,
                agent_id,
                reason=reason,
                retry=retry,
            )
            if not retry:
                await self._agent_store.decrement_assignments(agent_id)

        elif msg_type == "heartbeat":
            # Heartbeat response
            await self._agent_store.update(
                agent_id,
                last_seen_at=datetime.now(timezone.utc),
            )

        else:
            logger.warning("Unknown message type from agent %s: %s", agent_id, msg_type)

    # ------------------------------------------------------------------
    # Ping/Pong
    # ------------------------------------------------------------------

    async def _ping_task(self, agent_id: str, websocket: WebSocket) -> None:
        """Send periodic pings and check for timeout."""
        while agent_id in self._active_connections:
            await asyncio.sleep(self.PING_INTERVAL)

            if agent_id not in self._connection_info:
                break

            info = self._connection_info[agent_id]
            last_activity = info.get("last_pong", time.time())

            # Check for timeout
            if time.time() - last_activity > self.PING_INTERVAL + self.PONG_TIMEOUT:
                logger.warning("Agent %s ping timeout", agent_id)
                try:
                    await websocket.close(code=4001, reason="Ping timeout")
                except Exception:
                    pass
                break

            # Send ping
            try:
                seq = self._next_seq(agent_id)
                await websocket.send_json({
                    "type": "ping",
                    "seq": seq,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.error("Failed to send ping to agent %s: %s", agent_id, e)
                break

    def _next_seq(self, agent_id: str) -> int:
        """Get next sequence number for agent."""
        self._seq[agent_id] += 1
        return self._seq[agent_id]

    # ------------------------------------------------------------------
    # Initiative Delivery
    # ------------------------------------------------------------------

    async def send_initiative(
        self,
        agent_id: str,
        initiative: Dict[str, Any],
    ) -> bool:
        """Send initiative to agent and wait for ACK."""
        # Check circuit breaker
        if self._circuit_breaker and self._circuit_breaker.is_open(agent_id):
            priority = initiative.get("priority", 1)
            if priority < 2:  # Not urgent
                logger.warning("Circuit breaker open for agent %s", agent_id)
                return False

        websocket = self._active_connections.get(agent_id)
        if not websocket:
            return False

        seq = self._next_seq(agent_id)

        # Send initiative
        try:
            await websocket.send_json({
                "type": "initiative",
                "seq": seq,
                "initiative": initiative,
            })
        except Exception as e:
            logger.error("Failed to send initiative to agent %s: %s", agent_id, e)
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(agent_id)
            return False

        # Wait for ACK
        ack_future = asyncio.Future()
        self._pending_acks[initiative["id"]] = ack_future

        try:
            await asyncio.wait_for(ack_future, timeout=self.ACK_TIMEOUT)
            if self._circuit_breaker:
                self._circuit_breaker.record_success(agent_id)
            return True
        except asyncio.TimeoutError:
            logger.warning("No ACK from agent %s for initiative %s", agent_id, initiative["id"])
            self._pending_acks.pop(initiative["id"], None)

            if self._circuit_breaker:
                self._circuit_breaker.record_failure(agent_id)

            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup_connection(self, agent_id: str) -> None:
        """Clean up after agent disconnects."""
        # Remove from active
        self._active_connections.pop(agent_id, None)
        self._connection_info.pop(agent_id, None)

        # Mark agent offline
        await self._agent_store.set_offline(agent_id)

        # Reassign pending initiatives
        await self._initiative_store.reassign_from_agent(agent_id, only_pending=True)

        # Cancel pending ACKs
        for init_id, future in list(self._pending_acks.items()):
            # Note: We'd need to track which agent each ACK belongs to
            pass

    def get_connection_count(self) -> int:
        """Get number of active connections."""
        return len(self._active_connections)

    def get_connected_agents(self) -> List[str]:
        """Get list of connected agent IDs."""
        return list(self._active_connections.keys())

    async def close_all(self) -> None:
        """Close all connections."""
        for agent_id, websocket in list(self._active_connections.items()):
            try:
                await websocket.close(code=1001, reason="Server shutdown")
            except Exception:
                pass

        self._active_connections.clear()
        self._connection_info.clear()

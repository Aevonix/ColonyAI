# Multi-Agent Colony v0.7.0 — Deep Analysis #3

> **Analysis Date:** 2026-04-25 (Third Pass)
> **Analyst:** DevAgent
> **Goal:** Find ANYTHING missed in first two analyses

---

## Executive Summary

After thorough review of the spec and existing codebase, **19 additional gaps** were found:

| Category | Critical | Moderate | Minor |
|----------|----------|----------|-------|
| Protocol | 2 | 1 | 1 |
| Agent-Side | 2 | 2 | 1 |
| Error Handling | 1 | 3 | 1 |
| Observability | 0 | 2 | 1 |
| Testing | 0 | 2 | 1 |

**Overall Risk Assessment:** MEDIUM

---

## Part 1: Protocol Gaps

### Gap 1: WebSocket Reconnection Logic Missing

**Problem:** Spec shows agent connecting, but what happens when connection drops?

**Current Spec:** Part 4.3 shows connection lifecycle, but no reconnection logic.

**Solution:** Add exponential backoff reconnection:

```python
# agents/websocket_client.py (NEW FILE for remote agents)

class AgentWebSocketClient:
    """WebSocket client for remote agents."""
    
    INITIAL_RETRY_DELAY = 1.0  # seconds
    MAX_RETRY_DELAY = 60.0
    RETRY_MULTIPLIER = 2.0
    
    def __init__(self, agent_config: dict):
        self._config = agent_config
        self._ws: Optional[WebSocketClientProtocol] = None
        self._retry_delay = self.INITIAL_RETRY_DELAY
        self._running = False
    
    async def connect(self) -> None:
        """Connect with exponential backoff retry."""
        self._running = True
        
        while self._running:
            try:
                await self._do_connect()
                # Reset retry delay on successful connection
                self._retry_delay = self.INITIAL_RETRY_DELAY
            except Exception as e:
                logger.warning("WebSocket connection failed: %s", e)
                await self._retry_with_backoff()
    
    async def _do_connect(self) -> None:
        """Perform WebSocket connection and message handling."""
        ws_url = self._config["websocket_url"]
        agent_id = self._config["agent_id"]
        
        async with websockets.connect(
            ws_url,
            additional_headers={
                "Authorization": f"Bearer {self._sign_auth()}",
                "X-Agent-Id": agent_id,
            },
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connected to Colony")
            
            # Handle challenge-response auth
            await self._authenticate(ws)
            
            # Message loop
            await self._message_loop(ws)
    
    async def _retry_with_backoff(self) -> None:
        """Wait with exponential backoff before retry."""
        logger.info("Retrying in %.1f seconds...", self._retry_delay)
        await asyncio.sleep(self._retry_delay)
        self._retry_delay = min(
            self._retry_delay * self.RETRY_MULTIPLIER,
            self.MAX_RETRY_DELAY,
        )
    
    def disconnect(self) -> None:
        """Signal disconnection."""
        self._running = False
```

---

### Gap 2: No Ping/Pong Timeout Handling

**Problem:** Spec shows ping/pong, but what if agent doesn't respond to ping?

**Current Spec:** Part 4.3 shows ping every 30s, but no timeout handling.

**Solution:**

```python
# agents/websocket.py

class WebSocketManager:
    PING_INTERVAL = 30  # seconds
    PONG_TIMEOUT = 10   # seconds
    
    async def _message_loop(self, agent_id: str, websocket: WebSocket):
        """Handle messages with ping/pong timeout."""
        last_pong = time.time()
        
        async def ping_task():
            while agent_id in self._active_connections:
                await asyncio.sleep(self.PING_INTERVAL)
                
                # Check if pong received
                if time.time() - last_pong > self.PING_INTERVAL + self.PONG_TIMEOUT:
                    logger.warning("Agent %s ping timeout, disconnecting", agent_id)
                    await websocket.close(code=4001, reason="Ping timeout")
                    return
                
                # Send ping
                await websocket.send_json({
                    "type": "ping",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        
        # Start ping task
        ping = asyncio.create_task(ping_task())
        
        try:
            async for message in websocket.iter_messages():
                data = json.loads(message)
                
                if data.get("type") == "pong":
                    last_pong = time.time()
                    continue
                
                # Handle other messages
                await self._handle_message(agent_id, data)
        finally:
            ping.cancel()
```

---

### Gap 3: WebSocket Message Ordering Not Guaranteed

**Problem:** Multiple initiatives sent rapidly may arrive out of order.

**Current Spec:** Part 4.2 shows message types, but no sequence numbers.

**Solution:** Add sequence numbers:

```python
# Message format with sequence number
{
    "type": "initiative",
    "seq": 42,  # Monotonically increasing
    "initiative": {...}
}

# Agent acknowledges with seq
{
    "type": "acknowledge",
    "seq": 42,
    "initiative_id": "init-123"
}
```

**Colony side:**

```python
class WebSocketManager:
    _seq_counter: int = 0
    
    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter
    
    async def send_initiative(self, agent_id: str, initiative: dict) -> bool:
        ws = self._active_connections.get(agent_id)
        if not ws:
            return False
        
        seq = self._next_seq()
        
        await ws.send_json({
            "type": "initiative",
            "seq": seq,
            "initiative": initiative,
        })
        
        # Wait for ACK with matching seq
        # ...
```

---

### Gap 4: No Binary Message Support

**Problem:** Large initiatives (with attachments) would exceed 1 MB limit.

**Current Spec:** Part 4.1.2 limits to 1 MB, but no binary/compressed support.

**Solution:** Support compressed JSON:

```python
import gzip
import json

async def send_large_initiative(self, agent_id: str, initiative: dict) -> bool:
    """Send large initiative with compression."""
    ws = self._active_connections.get(agent_id)
    if not ws:
        return False
    
    raw = json.dumps(initiative).encode()
    
    if len(raw) > self.MAX_MESSAGE_SIZE:
        # Compress
        compressed = gzip.compress(raw)
        
        if len(compressed) > self.MAX_MESSAGE_SIZE:
            logger.error("Initiative too large even after compression: %d bytes", len(compressed))
            return False
        
        # Send as binary with compression flag
        await ws.send_bytes(b"\x01" + compressed)  # 0x01 = gzip flag
    else:
        # Send as text (existing path)
        await ws.send_json({"type": "initiative", "initiative": initiative})
```

---

## Part 2: Agent-Side Implementation Gaps

### Gap 5: Agent Config Schema Not Defined

**Problem:** Spec shows `agent.json` contents but no schema for validation.

**Solution:**

```python
# agents/models.py

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class NodeCertificate(BaseModel):
    """Node certificate structure."""
    colony_id: str
    node_id: str
    node_public_key_ed25519: str
    issued_at: datetime
    expires_at: Optional[datetime] = None
    signature: str

class AgentConfig(BaseModel):
    """Agent configuration file schema."""
    
    agent_id: str
    node_id: str
    colony_id: str
    colony_url: str
    websocket_url: str
    name: str
    capabilities: List[str] = Field(default_factory=list)
    is_primary: bool = False
    max_concurrent: int = 5
    node_cert: NodeCertificate
    connection_mode: str = "remote"
    registered_at: datetime
    
    # Optional fields
    priority: int = 1
    excluded_types: List[str] = Field(default_factory=list)
    included_types: List[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    
    class Config:
        # Allow extra fields for forward compatibility
        extra = "allow"
```

---

### Gap 6: No Agent Status Tracking

**Problem:** Agent doesn't track its own status changes.

**Solution:**

```python
# agents/status.py

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

class AgentState(str, Enum):
    """Agent operational state."""
    INITIALIZING = "initializing"
    IDLE = "idle"
    PROCESSING = "processing"
    ERROR = "error"
    DISCONNECTED = "disconnected"

@dataclass
class AgentStatus:
    """Track agent's own status."""
    state: AgentState = AgentState.INITIALIZING
    current_initiatives: list = field(default_factory=list)
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_count: int = 0
    last_error: Optional[str] = None
    
    def to_message(self) -> dict:
        """Convert to WebSocket status message."""
        return {
            "type": "status",
            "status": self.state.value,
            "current_assignments": len(self.current_initiatives),
            "load": len(self.current_initiatives) / 5.0,  # Assume max 5
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "error_count": self.error_count,
        }
```

---

### Gap 7: Agent Side Message Handler Not Defined

**Problem:** Spec shows Colony → Agent messages, but no handler implementation.

**Solution:**

```python
# agents/message_handler.py

from abc import ABC, abstractmethod
from typing import Any, Callable

class AgentMessageHandler:
    """Handle incoming messages from Colony."""
    
    def __init__(self, agent_config: dict, status: AgentStatus):
        self._config = agent_config
        self._status = status
        self._handlers: dict[str, Callable] = {
            "initiative": self._handle_initiative,
            "ping": self._handle_ping,
            "config": self._handle_config,
            "disconnect": self._handle_disconnect,
            "reauth_required": self._handle_reauth,
        }
        self._initiative_handlers: list[Callable] = []
    
    async def handle(self, message: dict) -> Optional[dict]:
        """Handle incoming message and return response if needed."""
        msg_type = message.get("type")
        
        handler = self._handlers.get(msg_type)
        if handler:
            return await handler(message)
        
        logger.warning("Unknown message type: %s", msg_type)
        return None
    
    async def _handle_initiative(self, message: dict) -> dict:
        """Handle initiative assignment."""
        initiative = message.get("initiative", {})
        initiative_id = initiative.get("id")
        
        # Add to current initiatives
        self._status.current_initiatives.append(initiative_id)
        
        # Notify registered handlers
        for handler in self._initiative_handlers:
            try:
                await handler(initiative)
            except Exception as e:
                logger.error("Initiative handler error: %s", e)
        
        return {
            "type": "acknowledge",
            "initiative_id": initiative_id,
        }
    
    async def _handle_ping(self, message: dict) -> dict:
        """Handle ping."""
        self._status.last_heartbeat = datetime.now(timezone.utc)
        return {
            "type": "pong",
            "timestamp": message.get("timestamp"),
        }
    
    async def _handle_config(self, message: dict) -> None:
        """Handle config update."""
        config = message.get("config", {})
        
        # Update config
        for key, value in config.items():
            if key in self._config:
                self._config[key] = value
                logger.info("Config updated: %s = %s", key, value)
    
    async def _handle_disconnect(self, message: dict) -> None:
        """Handle disconnect notice."""
        reason = message.get("reason", "unknown")
        reconnect = message.get("reconnect", False)
        
        logger.warning("Colony requested disconnect: %s", reason)
        
        if not reconnect:
            # Permanent disconnect (revoked?)
            self._status.state = AgentState.DISCONNECTED
    
    async def _handle_reauth(self, message: dict) -> None:
        """Handle re-authentication request."""
        logger.info("Re-authentication required")
        # Trigger re-auth flow
    
    def on_initiative(self, handler: Callable) -> None:
        """Register initiative handler."""
        self._initiative_handlers.append(handler)
```

---

### Gap 8: No Local Agent HTTP Client

**Problem:** Remote agents use WebSocket, but local agents need HTTP client for push.

**Current Spec:** Part 7.2 shows `deliver_initiative()` checking `connection_mode`, but agent side not shown.

**Solution:**

```python
# agents/local_client.py

import aiohttp
from typing import Optional

class LocalAgentClient:
    """HTTP client for local agents."""
    
    def __init__(self, agent_config: dict, gateway_url: str):
        self._config = agent_config
        self._gateway_url = gateway_url
    
    async def send_heartbeat(self, status: AgentStatus) -> bool:
        """Send heartbeat to Colony."""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self._gateway_url}/v1/host/agents/{self._config['agent_id']}/heartbeat",
                    json=status.to_message(),
                    headers={"Authorization": f"Bearer {self._config.get('api_key', '')}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
            except Exception as e:
                logger.error("Heartbeat failed: %s", e)
                return False
    
    async def acknowledge_initiative(self, initiative_id: str) -> bool:
        """Acknowledge initiative via HTTP."""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self._gateway_url}/v1/host/initiatives/{initiative_id}/acknowledge",
                    json={"agent_id": self._config["agent_id"]},
                    headers={"Authorization": f"Bearer {self._config.get('api_key', '')}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
            except Exception as e:
                logger.error("Acknowledge failed: %s", e)
                return False
```

---

## Part 3: Error Handling Gaps

### Gap 9: No Dead Letter Queue for Failed Initiatives

**Problem:** Initiatives that fail max_attempts are marked `failed` but then what?

**Current Spec:** Part 23.4 shows reassignment, but no dead letter handling.

**Solution:**

```python
# initiatives/store.py

async def mark_failed(
    self,
    initiative_id: str,
    reason: str,
) -> None:
    """Mark initiative as failed and optionally add to dead letter queue."""
    await self.update(
        initiative_id,
        status="failed",
        failed_reason=reason,
        failed_at=datetime.now(timezone.utc),
    )
    
    # Add to dead letter queue for manual review
    initiative = await self.get(initiative_id)
    
    await self._add_to_dead_letter_queue(initiative, reason)

async def _add_to_dead_letter_queue(
    self,
    initiative: dict,
    reason: str,
) -> None:
    """Add failed initiative to dead letter queue."""
    dlq_path = self._state_dir / "dead-letter-queue.jsonl"
    
    entry = {
        "initiative_id": initiative["id"],
        "type": initiative["type"],
        "description": initiative["description"],
        "reason": reason,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "attempt_count": initiative.get("attempt_count", 0),
    }
    
    with open(dlq_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**CLI for dead letter queue:**

```bash
# List failed initiatives
colony initiative dead-letter --list

# Retry a failed initiative
colony initiative dead-letter --retry init-123

# Dismiss a failed initiative
colony initiative dead-letter --dismiss init-123
```

---

### Gap 10: No Circuit Breaker for Agent Delivery

**Problem:** If agent keeps failing to receive initiatives, Colony keeps trying.

**Solution:**

```python
# agents/websocket.py

class CircuitBreaker:
    """Circuit breaker for agent delivery."""
    
    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 300  # 5 minutes
    
    def __init__(self):
        self._failures: dict[str, int] = {}  # agent_id -> failure count
        self._opened_at: dict[str, float] = {}  # agent_id -> timestamp
    
    def record_failure(self, agent_id: str) -> None:
        """Record delivery failure."""
        self._failures[agent_id] = self._failures.get(agent_id, 0) + 1
        
        if self._failures[agent_id] >= self.FAILURE_THRESHOLD:
            self._opened_at[agent_id] = time.time()
            logger.warning("Circuit breaker OPEN for agent %s", agent_id)
    
    def record_success(self, agent_id: str) -> None:
        """Record delivery success."""
        self._failures[agent_id] = 0
        self._opened_at.pop(agent_id, None)
    
    def is_open(self, agent_id: str) -> bool:
        """Check if circuit breaker is open."""
        if agent_id not in self._opened_at:
            return False
        
        # Check if recovery timeout passed
        if time.time() - self._opened_at[agent_id] > self.RECOVERY_TIMEOUT:
            # Half-open state - allow one attempt
            logger.info("Circuit breaker HALF-OPEN for agent %s", agent_id)
            return False
        
        return True

class WebSocketManager:
    def __init__(self):
        # ... existing code ...
        self._circuit_breaker = CircuitBreaker()
    
    async def send_initiative(self, agent_id: str, initiative: dict) -> bool:
        """Send initiative with circuit breaker."""
        
        # Check circuit breaker
        if self._circuit_breaker.is_open(agent_id):
            logger.warning("Circuit breaker open for agent %s, skipping delivery", agent_id)
            return False
        
        try:
            result = await self._do_send_initiative(agent_id, initiative)
            if result:
                self._circuit_breaker.record_success(agent_id)
            else:
                self._circuit_breaker.record_failure(agent_id)
            return result
        except Exception as e:
            self._circuit_breaker.record_failure(agent_id)
            raise
```

---

### Gap 11: No Graceful Degradation When All Agents Offline

**Problem:** What happens when all agents are offline?

**Solution:**

```python
# initiatives/store.py

async def get_pending_with_no_agents(self) -> list:
    """Get initiatives that can't be assigned (no agents available)."""
    # Called when no agents are online
    # Returns initiatives that should be queued for later
    return await self.list(status="pending")

# autonomy/loop.py

async def _phase_queue_assignment(self) -> None:
    """Attempt to assign pending initiatives."""
    store = self._registry.initiative_store
    agent_store = self._registry.agent_store
    
    if not store or not agent_store:
        return
    
    # Get online agents
    agents = await agent_store.list(status="online")
    
    if not agents:
        # No agents available - queue for later
        pending = await store.list(status="pending")
        
        if pending:
            logger.warning(
                "No agents online, %d initiatives queued for later",
                len(pending),
            )
            
            # Optionally notify user
            # await self._notify_no_agents_available()
        
        return
    
    # ... existing assignment logic ...
```

---

### Gap 12: No Transaction Rollback for Multi-Step Operations

**Problem:** Initiative assignment involves multiple DB updates; partial failure could corrupt state.

**Solution:**

```python
# initiatives/store.py

import sqlite3

class InitiativeStore:
    async def assign_initiative(
        self,
        initiative_id: str,
        agent_id: str,
    ) -> bool:
        """Assign initiative with transaction."""
        
        conn = self._db
        cursor = conn.cursor()
        
        try:
            # Begin transaction
            cursor.execute("BEGIN IMMEDIATE")
            
            # 1. Check initiative is still pending
            cursor.execute(
                "SELECT status FROM initiatives WHERE id = ?",
                [initiative_id],
            )
            row = cursor.fetchone()
            
            if not row or row["status"] != "pending":
                conn.rollback()
                return False
            
            # 2. Update initiative
            cursor.execute(
                """UPDATE initiatives 
                   SET status = 'assigned',
                       assigned_agent_id = ?,
                       assigned_at = ?
                   WHERE id = ?""",
                [agent_id, datetime.now(timezone.utc).isoformat(), initiative_id],
            )
            
            # 3. Update agent's current_assignments
            cursor.execute(
                """UPDATE agents 
                   SET current_assignments = current_assignments + 1
                   WHERE agent_id = ?""",
                [agent_id],
            )
            
            # 4. Log to assignment_history
            cursor.execute(
                """INSERT INTO assignment_history 
                   (initiative_id, agent_id, action, timestamp)
                   VALUES (?, ?, 'assigned', ?)""",
                [initiative_id, agent_id, datetime.now(timezone.utc).isoformat()],
            )
            
            # Commit transaction
            conn.commit()
            return True
            
        except Exception as e:
            conn.rollback()
            logger.error("Transaction failed: %s", e)
            raise
```

---

## Part 4: Observability Gaps

### Gap 13: No Metrics Export

**Problem:** No way to monitor system health externally.

**Solution:**

```python
# api/routers/metrics.py (NEW FILE)

from fastapi import APIRouter
from prometheus_client import Counter, Gauge, Histogram, generate_latest

router = APIRouter(prefix="/metrics", tags=["metrics"])

# Counters
INITIATIVES_CREATED = Counter(
    "colony_initiatives_created_total",
    "Total initiatives created",
    ["type", "status"],
)

INITIATIVES_ASSIGNED = Counter(
    "colony_initiatives_assigned_total",
    "Total initiatives assigned",
    ["agent_id"],
)

AGENT_CONNECTIONS = Counter(
    "colony_agent_connections_total",
    "Total agent connections",
    ["agent_id", "status"],
)

# Gauges
AGENTS_ONLINE = Gauge(
    "colony_agents_online",
    "Number of online agents",
)

INITIATIVES_PENDING = Gauge(
    "colony_initiatives_pending",
    "Number of pending initiatives",
)

INITIATIVES_IN_PROGRESS = Gauge(
    "colony_initiatives_in_progress",
    "Number of in-progress initiatives",
)

# Histograms
INITIATIVE_ASSIGNMENT_TIME = Histogram(
    "colony_initiative_assignment_seconds",
    "Time to assign initiative",
)

@router.get("")
async def metrics():
    """Export Prometheus metrics."""
    return Response(
        content=generate_latest(),
        media_type="text/plain",
    )

# Update gauges periodically
async def update_gauges():
    """Update metric gauges from database."""
    while True:
        try:
            # Update agent count
            online = await agent_store.count(status="online")
            AGENTS_ONLINE.set(online)
            
            # Update initiative counts
            pending = await initiative_store.count(status="pending")
            INITIATIVES_PENDING.set(pending)
            
            in_progress = await initiative_store.count(
                status=["assigned", "acknowledged"]
            )
            INITIATIVES_IN_PROGRESS.set(in_progress)
            
        except Exception as e:
            logger.error("Failed to update metrics: %s", e)
        
        await asyncio.sleep(60)
```

---

### Gap 14: No Structured Logging Format

**Problem:** Logs are unstructured, hard to parse/analyze.

**Solution:**

```python
# logging_config.py

import logging
import json
from datetime import datetime, timezone

class JSONFormatter(logging.Formatter):
    """JSON log formatter."""
    
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add extra fields
        if hasattr(record, "agent_id"):
            entry["agent_id"] = record.agent_id
        
        if hasattr(record, "initiative_id"):
            entry["initiative_id"] = record.initiative_id
        
        # Add exception info
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(entry)

# Usage
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
    ],
)

for handler in logging.root.handlers:
    handler.setFormatter(JSONFormatter())

# Log with context
logger.info(
    "Initiative assigned",
    extra={
        "agent_id": "agent-123",
        "initiative_id": "init-456",
    },
)
```

---

### Gap 15: No Health Check Details

**Problem:** `/v1/host/agents/health` returns basic status, but no component details.

**Solution:**

```python
@router.get("/health/detailed")
async def detailed_health() -> dict:
    """Detailed health check for monitoring."""
    
    health = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {},
    }
    
    # Check Neo4j
    try:
        if _graph and hasattr(_graph, "driver"):
            _graph.driver.verify_connectivity()
            health["components"]["neo4j"] = {"status": "ok"}
        else:
            health["components"]["neo4j"] = {"status": "not_configured"}
    except Exception as e:
        health["components"]["neo4j"] = {"status": "error", "error": str(e)}
        health["status"] = "degraded"
    
    # Check SQLite
    try:
        if _agent_store:
            _agent_store._db.execute("SELECT 1")
            health["components"]["sqlite_agents"] = {"status": "ok"}
        
        if _initiative_store:
            _initiative_store._db.execute("SELECT 1")
            health["components"]["sqlite_initiatives"] = {"status": "ok"}
    except Exception as e:
        health["components"]["sqlite"] = {"status": "error", "error": str(e)}
        health["status"] = "degraded"
    
    # Check WebSocket manager
    try:
        if _ws_manager:
            active = len(_ws_manager._active_connections)
            health["components"]["websocket"] = {
                "status": "ok",
                "active_connections": active,
            }
    except Exception as e:
        health["components"]["websocket"] = {"status": "error", "error": str(e)}
    
    # Agent stats
    try:
        if _agent_store:
            online = await _agent_store.count(status="online")
            total = await _agent_store.count()
            health["components"]["agents"] = {
                "status": "ok",
                "online": online,
                "total": total,
            }
    except Exception as e:
        health["components"]["agents"] = {"status": "error", "error": str(e)}
    
    # Initiative stats
    try:
        if _initiative_store:
            pending = await _initiative_store.count(status="pending")
            assigned = await _initiative_store.count(status="assigned")
            health["components"]["initiatives"] = {
                "status": "ok",
                "pending": pending,
                "assigned": assigned,
            }
    except Exception as e:
        health["components"]["initiatives"] = {"status": "error", "error": str(e)}
    
    return health
```

---

## Part 5: Testing Gaps

### Gap 16: No Integration Test Scenarios Defined

**Problem:** Spec shows components but no test scenarios.

**Solution:**

```python
# tests/integration/test_multi_agent.py

import pytest
import asyncio
from colony_sidecar.agents.store import AgentStore
from colony_sidecar.agents.websocket import WebSocketManager
from colony_sidecar.initiatives.store import InitiativeStore

class TestMultiAgent:
    """Integration tests for multi-agent functionality."""
    
    @pytest.fixture
    async def stores(self, tmp_path):
        """Create test stores."""
        agent_store = AgentStore(tmp_path)
        initiative_store = InitiativeStore(tmp_path)
        yield agent_store, initiative_store
        agent_store.close()
        initiative_store.close()
    
    @pytest.mark.asyncio
    async def test_agent_registration(self, stores):
        """Test agent can register."""
        agent_store, _ = stores
        
        agent = await agent_store.register(
            name="test-agent",
            node_id="node-123",
            connection_mode="local",
            capabilities=["messaging"],
        )
        
        assert agent["agent_id"]
        assert agent["status"] == "offline"
        assert agent["name"] == "test-agent"
    
    @pytest.mark.asyncio
    async def test_initiative_assignment(self, stores):
        """Test initiative assignment to agent."""
        agent_store, initiative_store = stores
        
        # Register agent
        agent = await agent_store.register(
            name="test-agent",
            node_id="node-123",
            connection_mode="local",
            capabilities=["messaging"],
        )
        
        # Set agent online
        await agent_store.update(agent["agent_id"], status="online")
        
        # Create initiative
        initiative = await initiative_store.create({
            "type": "follow_up",
            "description": "Test initiative",
            "priority": 0.5,
        })
        
        # Assignment should work
        assigned = await initiative_store.assign(
            initiative["id"],
            agent["agent_id"],
        )
        
        assert assigned
        
        # Check initiative status
        updated = await initiative_store.get(initiative["id"])
        assert updated["status"] == "assigned"
        assert updated["assigned_agent_id"] == agent["agent_id"]
    
    @pytest.mark.asyncio
    async def test_initiative_reassignment_on_offline(self, stores):
        """Test initiative reassignment when agent goes offline."""
        agent_store, initiative_store = stores
        
        # Register two agents
        agent1 = await agent_store.register(
            name="agent-1",
            node_id="node-1",
            connection_mode="local",
        )
        agent2 = await agent_store.register(
            name="agent-2",
            node_id="node-2",
            connection_mode="local",
        )
        
        # Set both online
        await agent_store.update(agent1["agent_id"], status="online")
        await agent_store.update(agent2["agent_id"], status="online")
        
        # Create and assign initiative
        initiative = await initiative_store.create({
            "type": "follow_up",
            "description": "Test",
        })
        await initiative_store.assign(initiative["id"], agent1["agent_id"])
        
        # Agent 1 goes offline
        await agent_store.update(agent1["agent_id"], status="offline")
        
        # Trigger reassignment
        reassigned = await initiative_store.reassign_agent_initiatives(
            agent1["agent_id"],
        )
        
        # Check initiative was reassigned
        updated = await initiative_store.get(initiative["id"])
        assert updated["status"] == "pending"
    
    @pytest.mark.asyncio
    async def test_websocket_auth(self, stores):
        """Test WebSocket challenge-response auth."""
        agent_store, _ = stores
        
        # Register agent
        agent = await agent_store.register(
            name="test-agent",
            node_id="node-123",
            connection_mode="remote",
        )
        
        # Simulate WebSocket connection
        ws_manager = WebSocketManager(agent_store)
        
        # ... auth flow test ...
```

---

### Gap 17: No Load Testing Considerations

**Problem:** No guidance on system capacity.

**Solution:**

```python
# tests/load/test_capacity.py

import pytest
import asyncio
from colony_sidecar.agents.store import AgentStore
from colony_sidecar.initiatives.store import InitiativeStore

class TestCapacity:
    """Load tests for capacity planning."""
    
    @pytest.mark.asyncio
    async def test_100_agents_concurrent(self, tmp_path):
        """Test system with 100 concurrent agents."""
        agent_store = AgentStore(tmp_path)
        
        # Register 100 agents
        tasks = []
        for i in range(100):
            tasks.append(agent_store.register(
                name=f"agent-{i}",
                node_id=f"node-{i}",
                connection_mode="remote",
            ))
        
        agents = await asyncio.gather(*tasks)
        assert len(agents) == 100
        
        # Set all online
        tasks = []
        for agent in agents:
            tasks.append(agent_store.update(agent["agent_id"], status="online"))
        
        await asyncio.gather(*tasks)
        
        # Verify all online
        online = await agent_store.count(status="online")
        assert online == 100
    
    @pytest.mark.asyncio
    async def test_1000_initiatives(self, tmp_path):
        """Test system with 1000 initiatives."""
        agent_store = AgentStore(tmp_path)
        initiative_store = InitiativeStore(tmp_path)
        
        # Create agent
        agent = await agent_store.register(
            name="load-agent",
            node_id="node-load",
            connection_mode="local",
        )
        await agent_store.update(agent["agent_id"], status="online")
        
        # Create 1000 initiatives
        tasks = []
        for i in range(1000):
            tasks.append(initiative_store.create({
                "type": "follow_up",
                "description": f"Initiative {i}",
                "priority": 0.5,
            }))
        
        initiatives = await asyncio.gather(*tasks)
        assert len(initiatives) == 1000
        
        # Check pending count
        pending = await initiative_store.count(status="pending")
        assert pending == 1000
```

---

## Part 6: Documentation Gaps

### Gap 18: No Agent SDK Documentation

**Problem:** Remote agents need SDK for WebSocket communication.

**Solution:** Create agent SDK documentation:

```markdown
# Colony Agent SDK

## Installation

```bash
pip install colonyai
```

## Quick Start

```python
from colony.agent import AgentClient

# Connect to Colony
client = AgentClient(config_path="~/.colony/agent.json")

# Set up initiative handler
@client.on_initiative
async def handle_initiative(initiative):
    print(f"Received: {initiative['description']}")
    
    # Process initiative...
    
    # Acknowledge
    await client.acknowledge(initiative["id"])
    
    # Complete
    await client.complete(initiative["id"], result="Done!")

# Start client
await client.start()
```

## API Reference

### AgentClient

#### `__init__(config_path: str)`
Load agent config from file.

#### `start() -> None`
Connect to Colony and start message loop.

#### `stop() -> None`
Disconnect from Colony.

#### `acknowledge(initiative_id: str) -> bool`
Acknowledge initiative receipt.

#### `complete(initiative_id: str, result: str) -> bool`
Mark initiative as completed.

#### `fail(initiative_id: str, reason: str, retry: bool = True) -> bool`
Mark initiative as failed.

#### `delegate(initiative_id: str, reason: str) -> bool`
Delegate initiative to another agent.

#### `update_status(status: str, load: float) -> bool`
Update agent status.

### Events

#### `@client.on_initiative`
Decorator for initiative handler.

#### `@client.on_config`
Decorator for config update handler.

#### `@client.on_disconnect`
Decorator for disconnect handler.
```

---

### Gap 19: No Migration Compatibility Matrix

**Problem:** What versions can coexist?

**Solution:**

```markdown
# Compatibility Matrix

## Colony Version Compatibility

| Colony | Plugin | MCP Client | Node.js |
|--------|--------|------------|---------|
| 0.6.x | 0.6.x | 0.6.x | 18+ |
| 0.7.0 | 0.7.0 | 0.7.0 | 22+ |

## Upgrade Path

### 0.6.x → 0.7.0

1. Stop Colony: `colony stop`
2. Update: `pip install colonyai>=0.7.0`
3. Run migration: `colony migrate`
4. Update plugin: `openclaw plugins update @openclaw/plugin-colony`
5. Start Colony: `colony start`

### Breaking Changes

- Node.js 22+ required (was 18+)
- Plugin must be 0.7.0+ (API changes)
- MCP client must be 0.7.0+ (new auth flow)

## Rollback

If issues occur:

1. Stop Colony
2. Downgrade: `pip install colonyai==0.6.29`
3. Restore backup: `colony restore ~/.colony/backup-0.6.x/`
4. Downgrade plugin: `openclaw plugins install @openclaw/plugin-colony@0.6.29`
5. Start Colony
```

---

## Part 7: Summary

### Gaps Found (19)

| # | Gap | Severity | Category |
|---|-----|----------|----------|
| 1 | WebSocket reconnection logic missing | Critical | Protocol |
| 2 | Ping/pong timeout handling missing | Critical | Protocol |
| 3 | Message ordering not guaranteed | Moderate | Protocol |
| 4 | No binary message support | Minor | Protocol |
| 5 | Agent config schema not defined | Critical | Agent-Side |
| 6 | No agent status tracking | Moderate | Agent-Side |
| 7 | Agent message handler not defined | Critical | Agent-Side |
| 8 | No local agent HTTP client | Moderate | Agent-Side |
| 9 | No dead letter queue | Moderate | Error Handling |
| 10 | No circuit breaker | Moderate | Error Handling |
| 11 | No graceful degradation | Moderate | Error Handling |
| 12 | No transaction rollback | Critical | Error Handling |
| 13 | No metrics export | Moderate | Observability |
| 14 | No structured logging | Minor | Observability |
| 15 | No detailed health check | Minor | Observability |
| 16 | No integration test scenarios | Moderate | Testing |
| 17 | No load testing guidance | Minor | Testing |
| 18 | No Agent SDK documentation | Minor | Documentation |
| 19 | No compatibility matrix | Minor | Documentation |

---

## Part 8: Recommended Spec Amendments

### Add to Part 4: WebSocket Protocol

```markdown
### 4.4 Reconnection

Agents should implement exponential backoff reconnection:

- Initial retry: 1 second
- Max retry: 60 seconds
- Multiplier: 2x

### 4.5 Ping/Pong Timeout

- Colony sends ping every 30 seconds
- Agent must respond with pong within 10 seconds
- No response → disconnect

### 4.6 Message Sequencing

All messages include `seq` field for ordering:

```json
{"type": "initiative", "seq": 42, "initiative": {...}}
```
```

### Add to Part 8.5: Error Standards

```markdown
### 8.5.5 Circuit Breaker

Colony implements circuit breaker for agent delivery:

- 5 consecutive failures → circuit open
- 5 minute recovery timeout
- Half-open state allows one retry
```

### Add to Part 23: Error Recovery

```markdown
### 23.6 Dead Letter Queue

Failed initiatives are logged to `~/.colony/dead-letter-queue.jsonl`:

```json
{"initiative_id": "init-123", "reason": "...", "failed_at": "..."}
```

### 23.7 Transaction Safety

Multi-step operations use SQLite transactions with rollback:

- Initiative assignment
- Agent status updates
- Initiative state transitions
```

### Add New Part 27: Agent SDK

```markdown
## Part 27: Agent SDK

Remote agents use the Colony Agent SDK for WebSocket communication.

### Installation

```bash
pip install colonyai
```

### Usage

```python
from colony.agent import AgentClient

client = AgentClient("~/.colony/agent.json")

@client.on_initiative
async def handle(initiative):
    await client.acknowledge(initiative["id"])
    # Process...
    await client.complete(initiative["id"], result="Done!")

await client.start()
```
```

---

## Part 9: Updated Effort Estimate

| Component | Hours | Added |
|-----------|-------|-------|
| Agent Store + Invites + Audit | 5h | — |
| WebSocket Server + Auth | 5h | +1h |
| Initiative Store + Models | 3h | — |
| InitiativeEngine modification | 2h | — |
| Assignment Engine | 2h | — |
| Bridge extension | 1h | — |
| AutonomyLoop phases | 3h | — |
| API endpoints + Metrics | 4h | +1h |
| CLI commands | 3h | — |
| Remote MCP client | 2h | — |
| Agent SDK (NEW) | 3h | +3h |
| Tailscale integration | 2h | — |
| Plugin WebSocket | 2h | — |
| Security hardening | 3h | — |
| Error recovery | 3h | +1h |
| Testing | 6h | +2h |
| Documentation | 2h | +2h |
| **Total** | **51h** | **+10h** |

---

**Analysis Complete.**

# Multi-Agent Colony v0.7.0 — Deep Analysis #2

> **Analysis Date:** 2026-04-25 (Second Pass)
> **Analyst:** DevAgent
> **Goal:** Find ANYTHING missed in first analysis

---

## Executive Summary

After thorough review of the spec and existing codebase, **17 additional gaps** were found:

| Category | Critical | Moderate | Minor |
|----------|----------|----------|-------|
| Data Model | 2 | 1 | 1 |
| Integration | 1 | 2 | 1 |
| Security | 1 | 2 | 2 |
| Automation | 0 | 2 | 1 |
| Edge Cases | 0 | 1 | 1 |

**Overall Risk Assessment:** MEDIUM-HIGH

---

## Part 1: Data Model Gaps

### Gap 1: Initiative Dataclass Missing Fields

**Problem:** The existing `Initiative` dataclass in `initiative_engine.py` does NOT have fields required by the spec:

**Existing Dataclass (lines 46-67):**
```python
@dataclass
class Initiative:
    id: str
    type: InitiativeType
    description: str
    priority: float
    rationale: str
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
```

**Spec Schema Requires:**
```sql
-- initiatives table
id TEXT PRIMARY KEY,
dedup_key TEXT UNIQUE,                 -- MISSING
type TEXT NOT NULL,
description TEXT NOT NULL,
priority REAL DEFAULT 0.5,
rationale TEXT,
action_hint TEXT,
entity_id TEXT,
source_type TEXT,                      -- MISSING
source_id TEXT,                        -- MISSING
created_by TEXT,                       -- MISSING
status TEXT DEFAULT 'pending',         -- MISSING
assigned_agent_id TEXT,                -- MISSING
assigned_agent_name TEXT,              -- MISSING
assigned_at TIMESTAMP,                 -- MISSING
acknowledged_at TIMESTAMP,             -- MISSING
completed_at TIMESTAMP,                -- MISSING
cancelled_at TIMESTAMP,                -- MISSING
cancelled_by TEXT,                     -- MISSING
cancelled_reason TEXT,                 -- MISSING
failed_at TIMESTAMP,                   -- MISSING
failed_reason TEXT,                    -- MISSING
attempt_count INTEGER DEFAULT 0,       -- MISSING
max_attempts INTEGER DEFAULT 3,        -- MISSING
timeout_seconds INTEGER DEFAULT 300,   -- MISSING
last_attempt_at TIMESTAMP,             -- MISSING
expires_at TIMESTAMP,
created_at TIMESTAMP,
delivery_mode TEXT DEFAULT 'websocket', -- MISSING
delivery_attempts INTEGER DEFAULT 0,   -- MISSING
last_delivery_at TIMESTAMP,            -- MISSING
preferred_agent_id TEXT,               -- MISSING
```

**Solution:** Extend the Initiative dataclass OR create a separate `StoredInitiative` dataclass:

```python
# initiatives/store.py

@dataclass
class StoredInitiative:
    """Persisted initiative with assignment tracking."""
    
    # From Initiative
    id: str
    type: str  # InitiativeType value
    description: str
    priority: float
    rationale: str
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    # NEW: Deduplication
    dedup_key: Optional[str] = None
    
    # NEW: Source tracking
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    created_by: Optional[str] = None
    
    # NEW: Assignment
    status: str = "pending"
    assigned_agent_id: Optional[str] = None
    assigned_agent_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancelled_reason: Optional[str] = None
    failed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    
    # NEW: Retry
    attempt_count: int = 0
    max_attempts: int = 3
    timeout_seconds: int = 300
    last_attempt_at: Optional[datetime] = None
    
    # NEW: Delivery
    delivery_mode: str = "websocket"
    delivery_attempts: int = 0
    last_delivery_at: Optional[datetime] = None
    preferred_agent_id: Optional[str] = None
    
    @classmethod
    def from_initiative(cls, init: Initiative, **kwargs) -> "StoredInitiative":
        """Convert Initiative to StoredInitiative."""
        return cls(
            id=init.id,
            type=init.type.value,
            description=init.description,
            priority=init.priority,
            rationale=init.rationale,
            action_hint=init.action_hint,
            entity_id=init.entity_id,
            expires_at=init.expires_at,
            created_at=init.created_at,
            **kwargs,
        )
```

---

### Gap 2: No Migration for Existing Initiatives

**Problem:** Existing Colony installations may have initiatives in memory. No migration plan.

**Solution:** Add migration in `cli.py migrate`:

```python
# cli.py

def _cmd_migrate(args) -> int:
    # ... existing migration code ...
    
    # Migrate in-memory initiatives to SQLite
    engine = registry.initiative_engine
    if engine and hasattr(engine, '_initiatives'):
        pending = [i for i in engine._initiatives if i.id not in store.list_ids()]
        
        for init in pending:
            stored = StoredInitiative.from_initiative(
                init,
                dedup_key=f"{init.type.value}:{init.entity_id}",
                created_by="migration",
                status="pending",
            )
            await store.create(stored)
        
        print(f"✓ Migrated {len(pending)} initiatives to SQLite")
```

---

### Gap 3: agents.status Enum Not Defined

**Problem:** `status` field uses strings but no enum for valid values.

**Solution:**

```python
# agents/store.py

from enum import Enum

class AgentStatus(str, Enum):
    """Agent status values."""
    
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    
    def is_active(self) -> bool:
        """Can this agent receive assignments?"""
        return self in (AgentStatus.ONLINE, AgentStatus.BUSY)
```

---

### Gap 4: No JSON Schema Validation for Metadata

**Problem:** `metadata TEXT DEFAULT '{}'` is unstructured. Invalid JSON could break queries.

**Solution:**

```python
# agents/store.py

import json

class AgentStore:
    def _validate_metadata(self, metadata: Any) -> dict:
        """Validate and normalize metadata."""
        if metadata is None:
            return {}
        
        if isinstance(metadata, str):
            try:
                return json.loads(metadata)
            except json.JSONDecodeError:
                logger.warning("Invalid metadata JSON, using empty dict")
                return {}
        
        if isinstance(metadata, dict):
            return metadata
        
        return {}
```

---

## Part 2: Integration Gaps

### Gap 5: InitiativeEngine.generate() Returns Raw Initiatives

**Problem:** `engine.generate()` returns `List[Initiative]` without `dedup_key`. The spec says to check dedup before persisting, but who sets `dedup_key`?

**Solution:** Generate dedup_key in `_phase_initiative`:

```python
# autonomy/loop.py

async def _phase_initiative(self) -> None:
    engine = self._registry.initiative_engine
    store = self._registry.initiative_store
    
    # ... existing context feeding ...
    
    initiatives = await engine.generate(
        min_priority=self.config.initiative_confidence_threshold,
    )
    
    # NEW: Add dedup_key and source tracking
    for init in initiatives:
        if not hasattr(init, 'dedup_key'):
            # Generate dedup_key from type + entity_id
            init.dedup_key = f"{init.type.value}:{init.entity_id or 'unknown'}"
        
        if not hasattr(init, 'source_type'):
            init.source_type = "autonomy_loop"
        
        if not hasattr(init, 'created_by'):
            init.created_by = "autonomy_loop"
    
    # NEW: Persist to SQLite
    if store:
        for init in initiatives:
            await store.create(StoredInitiative.from_initiative(init))
    
    self._pending_initiatives = initiatives
```

---

### Gap 6: SubsystemRegistry Has Both `initiative` and `initiative_engine`

**Problem:** Confusion between two properties:
- `initiative` → returns `_metalearner` (cognition)
- `initiative_engine` → returns `InitiativeEngine`

**Current Code (registry.py):**
```python
@property
def initiative(self) -> Any:
    from colony_sidecar.api.routers.host import _metalearner
    return _metalearner  # InitiativeEngine is part of cognition

@property
def initiative_engine(self) -> Any:
    # ... returns InitiativeEngine ...
```

**Solution:** Document clearly or rename:

```python
@property
def metalearner(self) -> Any:
    """MetaLearner for cognition (was 'initiative')."""
    from colony_sidecar.api.routers.host import _metalearner
    return _metalearner

@property
def initiative_engine(self) -> Optional["InitiativeEngine"]:
    """InitiativeEngine for generating proactive suggestions."""
    # ... existing code ...
```

---

### Gap 7: No Clear Handoff from InitiativeEngine to InitiativeStore

**Problem:** Spec shows InitiativeEngine taking `store` param, but existing code doesn't support this.

**Current (initiative_engine.py line 76):**
```python
def __init__(self, graph_client: Any, event_bus: Any, mind_model: Any) -> None:
```

**Spec says:**
```python
def __init__(self, graph_client, event_bus, mind_model, store=None) -> None:
```

**Solution:** Modify InitiativeEngine constructor:

```python
# intelligence/components/initiative_engine.py

class InitiativeEngine:
    def __init__(
        self,
        graph_client: Any,
        event_bus: Any,
        mind_model: Any,
        store: Optional["InitiativeStore"] = None,  # NEW
    ) -> None:
        self.graph = graph_client
        self.events = event_bus
        self.mind_model = mind_model
        self._store = store  # NEW
        self._initiatives: List[Initiative] = []
        self._context: Dict[str, List[Dict[str, Any]]] = {}
```

---

### Gap 8: `create_node_certificate` Generates Its Own node_id

**Problem:** The existing `create_node_certificate()` function generates a new `node_id` internally, but remote agents need to send their OWN `node_id` to Colony for signing.

**Current (chain/node.py):**
```python
def create_node_certificate(
    state_dir: str | Path,
    colony_key_manager: Optional["LocalKeyManager"] = None,
) -> dict:
    # ...
    node_id = get_or_create_node_id(state_dir)  # Uses LOCAL state_dir
```

**Solution:** Add `node_id` parameter:

```python
def create_node_certificate(
    state_dir: str | Path,
    colony_key_manager: Optional["LocalKeyManager"] = None,
    node_id: Optional[str] = None,  # NEW: for remote agents
    node_public_key: Optional[str] = None,  # NEW: for remote agents
) -> dict:
    """Create a node certificate signed by the Colony's private key.
    
    Args:
        state_dir: Colony state directory
        colony_key_manager: Colony key manager for signing
        node_id: Optional node_id (uses local if not provided)
        node_public_key: Optional public key hex (uses local if not provided)
    """
    # ...
    
    if node_id is None:
        node_id = get_or_create_node_id(state_dir)
    
    if node_public_key is None:
        node_km = ensure_node_keypair(state_dir)
        node_public_key = node_km.public_key_hex()
    
    # Continue with cert creation...
```

---

## Part 3: Security Gaps

### Gap 9: WebSocket Connection Doesn't Verify Client IP

**Problem:** Challenge-response auth verifies identity but doesn't log/verify client IP.

**Risk:** Can't detect suspicious connection patterns from different IPs.

**Solution:**

```python
# agents/websocket.py

class WebSocketManager:
    async def handle_connection(
        self,
        websocket: WebSocket,
        agent_id: str,
        client_ip: str,  # NEW parameter
    ) -> None:
        # ... existing auth ...
        
        # Check for IP change
        agent = await self._agent_store.get(agent_id)
        last_ip = agent.get("metadata", {}).get("last_connection_ip")
        
        if last_ip and last_ip != client_ip:
            logger.warning(
                "Agent %s IP changed: %s → %s",
                agent_id,
                last_ip,
                client_ip,
            )
            # Could require re-authentication or notify user
        
        # Update IP in metadata
        await self._agent_store.update(
            agent_id,
            metadata={"last_connection_ip": client_ip},
        )
```

---

### Gap 10: No Maximum WebSocket Message Size

**Problem:** WebSocket has no message size limit. Malicious agent could send huge messages.

**Solution:**

```python
# agents/websocket.py

class WebSocketManager:
    MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB
    
    async def _message_loop(self, agent_id: str, websocket: WebSocket):
        """Handle incoming messages with size limit."""
        async for message in websocket.iter_messages():
            # Check size
            if len(message) > self.MAX_MESSAGE_SIZE:
                logger.warning(
                    "Agent %s sent oversized message (%d bytes)",
                    agent_id,
                    len(message),
                )
                await websocket.close(code=4002, reason="Message too large")
                return
            
            # Process message
            # ...
```

---

### Gap 11: No Session Timeout for WebSocket

**Problem:** WebSocket can stay connected indefinitely. No re-authentication required.

**Solution:**

```python
# agents/websocket.py

class WebSocketManager:
    SESSION_TIMEOUT = timedelta(hours=24)  # Re-auth after 24h
    
    async def _message_loop(self, agent_id: str, websocket: WebSocket):
        """Handle messages with session timeout."""
        connected_at = datetime.now(timezone.utc)
        
        async for message in websocket.iter_messages():
            # Check session timeout
            if datetime.now(timezone.utc) - connected_at > self.SESSION_TIMEOUT:
                logger.info("Session timeout for agent %s, requesting re-auth", agent_id)
                await websocket.send_json({
                    "type": "reauth_required",
                    "reason": "session_timeout",
                })
                # Wait for new auth
                # ...
                connected_at = datetime.now(timezone.utc)
            
            # Process message
            # ...
```

---

### Gap 12: Setup Code Not Hashed

**Problem:** Setup codes stored in plaintext. Database leak exposes all valid codes.

**Solution:** Hash setup codes like passwords:

```python
# agents/store.py

import hashlib
import secrets

def hash_setup_code(code: str) -> str:
    """Hash setup code for storage."""
    # Use SHA-256 with pepper
    pepper = os.environ.get("COLONY_CODE_PEPPER", "default-pepper-change-in-prod")
    return hashlib.sha256(f"{code}{pepper}".encode()).hexdigest()

async def create_invite(self, ...) -> dict:
    code = generate_setup_code()
    code_hash = hash_setup_code(code)
    
    # Store hash, not plaintext
    self._db.execute(
        "INSERT INTO agent_invites (code, code_hash, ...) VALUES (?, ?, ...)",
        [code, code_hash, ...],
    )
    
    return {"code": code, ...}  # Return plaintext once

async def validate_invite(self, code: str) -> dict:
    code_hash = hash_setup_code(code)
    
    # Look up by hash
    invite = self._db.execute(
        "SELECT * FROM agent_invites WHERE code_hash = ?",
        [code_hash],
    ).fetchone()
    
    # ...
```

---

### Gap 13: No Certificate Revocation List (CRL) Implementation

**Problem:** Spec mentions CRL but doesn't show implementation.

**Solution:**

```python
# agents/store.py

class AgentStore:
    _revoked_node_ids: set[str] = set()
    _crl_loaded: bool = False
    
    def _load_crl(self) -> None:
        """Load CRL from database into memory."""
        if self._crl_loaded:
            return
        
        cursor = self._db.execute(
            "SELECT node_id FROM agents WHERE status = 'revoked'"
        )
        self._revoked_node_ids = {row["node_id"] for row in cursor.fetchall()}
        self._crl_loaded = True
    
    def is_node_revoked(self, node_id: str) -> bool:
        """Check if node_id is revoked (fast, in-memory check)."""
        self._load_crl()
        return node_id in self._revoked_node_ids
    
    async def revoke(self, agent_id: str, reason: str) -> None:
        """Revoke agent and add to CRL."""
        agent = await self.get(agent_id)
        if not agent:
            return
        
        await self.update(agent_id, status="revoked")
        
        # Add to in-memory CRL
        self._revoked_node_ids.add(agent["node_id"])
        
        # Log audit
        await self.log_audit("agent_revoke", "api", agent_id, {"reason": reason})
```

---

## Part 4: Automation Gaps

### Gap 14: No Auto-Detection of Tailscale on colony agent connect

**Problem:** `colony agent connect` should detect Tailscale IP automatically, but spec doesn't show how.

**Solution:**

```python
# cli.py

async def _cmd_agent_connect(args) -> int:
    # ... existing code ...
    
    # Auto-detect Tailscale if --colony-url not provided
    if not args.colony_url:
        # Check if Tailscale is connected
        ts = TailscaleManager()
        if ts.is_connected():
            # Try to discover Colony via Tailscale
            colony_ip = await _discover_colony_on_tailnet(ts.get_ip())
            if colony_ip:
                args.colony_url = f"http://{colony_ip}:7777"
                print(f"✓ Auto-detected Colony on Tailscale: {args.colony_url}")
            else:
                print("ERROR: Could not find Colony on tailnet.")
                print("Use --colony-url to specify Colony URL.")
                return 1
        else:
            print("ERROR: --colony-url is required.")
            return 1

async def _discover_colony_on_tailnet(my_ip: str) -> Optional[str]:
    """Discover Colony on tailnet by scanning common IPs."""
    # Try to ping common Colony ports on tailnet
    # This requires Tailscale's peer discovery or manual config
    pass
```

---

### Gap 15: Harness Config Doesn't Check for Conflicts

**Problem:** `colony mcp setup --harness X` could overwrite existing config without warning.

**Solution:**

```python
# cli.py

def _cmd_mcp(args) -> None:
    harness = args.harness
    config_path = _get_harness_config_path(harness)
    
    if config_path.exists() and not args.force:
        existing = json.loads(config_path.read_text())
        if "mcpServers" in existing and "colony" in existing["mcpServers"]:
            print(f"WARNING: {harness} already configured for Colony.")
            print(f"  Config: {config_path}")
            print("\nUse --force to overwrite.")
            return 1
    
    # Write config
    # ...
```

---

## Part 5: Edge Cases & Missing Features

### Gap 16: No Bulk Operations for Agents

**Problem:** Admin can't revoke multiple agents at once.

**Solution:**

```python
# api/routers/host.py

@router.post("/agents/bulk-revoke")
async def bulk_revoke_agents(body: BulkRevokeRequest) -> BulkRevokeResponse:
    """Revoke multiple agents at once."""
    results = []
    
    for agent_id in body.agent_ids:
        try:
            await _revoke_agent(agent_id, reason=body.reason)
            results.append({"agent_id": agent_id, "ok": True})
        except Exception as e:
            results.append({"agent_id": agent_id, "ok": False, "error": str(e)})
    
    success_count = sum(1 for r in results if r["ok"])
    return BulkRevokeResponse(
        total=len(body.agent_ids),
        success=success_count,
        failed=len(body.agent_ids) - success_count,
        results=results,
    )
```

---

### Gap 17: No Initiative Prioritization API

**Problem:** Can't boost/prioritize an initiative after creation.

**Solution:**

```python
# api/routers/host.py

@router.patch("/initiatives/{initiative_id}/priority")
async def update_initiative_priority(
    initiative_id: str,
    body: UpdatePriorityRequest,
) -> InitiativeResponse:
    """Update initiative priority."""
    initiative = await _initiative_store.get(initiative_id)
    if not initiative:
        raise HTTPException(404, "Initiative not found")
    
    # Only allow priority update for pending/assigned
    if initiative["status"] not in ("pending", "assigned"):
        raise HTTPException(400, "Can only update priority for pending/assigned initiatives")
    
    await _initiative_store.update(initiative_id, priority=body.priority)
    
    # Re-sort assignment queue
    # ...
    
    return await _initiative_store.get(initiative_id)
```

---

## Part 6: Missing Documentation

### Missing 1: API Error Response Schema

**Problem:** Spec doesn't define consistent error response format.

**Solution:**

```python
# api/schemas/host.py

class ErrorResponse(BaseModel):
    """Standard error response."""
    
    error: str  # Error code: "not_found", "invalid_request", etc.
    message: str  # Human-readable message
    details: Optional[dict] = None  # Additional context
    
    class Config:
        json_schema_extra = {
            "example": {
                "error": "not_found",
                "message": "Agent not found",
                "details": {"agent_id": "agent-123"},
            }
        }
```

---

### Missing 2: WebSocket Close Codes

**Problem:** Spec doesn't define WebSocket close codes.

**Solution:**

```markdown
### WebSocket Close Codes

| Code | Reason | Description |
|------|--------|-------------|
| 1000 | Normal | Normal closure |
| 4001 | Auth Timeout | Authentication not completed in time |
| 4002 | Message Too Large | Message exceeded 1 MB limit |
| 4003 | Forbidden | Agent revoked, invalid signature, or rate limited |
| 4004 | Not Found | Agent not found |
| 4005 | Reauth Required | Session timeout, need to re-authenticate |
| 4006 | Server Shutdown | Colony is shutting down |
```

---

### Missing 3: Rate Limit Headers

**Problem:** API responses don't include rate limit info.

**Solution:**

```python
# API responses should include:
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 7
X-RateLimit-Reset: 2026-04-25T18:00:00Z
```

---

## Part 7: Performance Considerations

### Consideration 1: Initiative Store Query Performance

**Problem:** Querying initiatives by status without pagination could be slow.

**Solution:** Add pagination:

```python
# initiatives/store.py

async def list(
    self,
    status: Optional[List[str]] = None,
    assigned_agent_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "priority DESC",
) -> List[StoredInitiative]:
    """List initiatives with pagination."""
    
    query = "SELECT * FROM initiatives WHERE 1=1"
    params = []
    
    if status:
        placeholders = ",".join("?" * len(status))
        query += f" AND status IN ({placeholders})"
        params.extend(status)
    
    if assigned_agent_id:
        query += " AND assigned_agent_id = ?"
        params.append(assigned_agent_id)
    
    query += f" ORDER BY {order_by}"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor = self._db.execute(query, params)
    return [self._row_to_initiative(row) for row in cursor.fetchall()]
```

---

### Consideration 2: WebSocket Connection Memory

**Problem:** Each WebSocket connection holds memory. Many agents = high memory usage.

**Solution:** Connection limits:

```python
# agents/websocket.py

class WebSocketManager:
    MAX_CONNECTIONS = 100  # Maximum concurrent WebSocket connections
    
    async def handle_connection(self, websocket, agent_id, client_ip):
        # Check connection limit
        if len(self._active_connections) >= self.MAX_CONNECTIONS:
            logger.warning("WebSocket connection limit reached (%d)", self.MAX_CONNECTIONS)
            await websocket.close(code=4003, reason="Connection limit reached")
            return
        
        # ... rest of connection handling ...
```

---

## Part 8: Summary

### Gaps Found (17)

| # | Gap | Severity | Category |
|---|-----|----------|----------|
| 1 | Initiative dataclass missing fields | Critical | Data Model |
| 2 | No migration for existing initiatives | Moderate | Data Model |
| 3 | AgentStatus enum not defined | Minor | Data Model |
| 4 | No JSON validation for metadata | Minor | Data Model |
| 5 | dedup_key not set in _phase_initiative | Critical | Integration |
| 6 | SubsystemRegistry naming confusion | Moderate | Integration |
| 7 | InitiativeEngine constructor mismatch | Critical | Integration |
| 8 | create_node_certificate can't sign remote | Moderate | Integration |
| 9 | WebSocket doesn't verify client IP | Minor | Security |
| 10 | No max WebSocket message size | Moderate | Security |
| 11 | No session timeout for WebSocket | Moderate | Security |
| 12 | Setup codes not hashed | Critical | Security |
| 13 | CRL not implemented | Moderate | Security |
| 14 | No auto-detect Tailscale IP | Minor | Automation |
| 15 | Harness config doesn't check conflicts | Minor | Automation |
| 16 | No bulk agent operations | Minor | Edge Cases |
| 17 | No initiative prioritization API | Minor | Edge Cases |

### Missing Documentation (3)

1. API error response schema
2. WebSocket close codes
3. Rate limit headers

### Performance Considerations (2)

1. Initiative pagination
2. WebSocket connection limits

---

## Part 9: Recommended Spec Amendments

### Add to Part 1.6: Dataclass Extensions

```python
# NEW FILE: initiatives/models.py

@dataclass
class StoredInitiative:
    """Persisted initiative with full tracking."""
    # ... full definition from Gap 1 ...
```

### Add to Part 22.8: WebSocket Close Codes

| Code | Reason |
|------|--------|
| 4001 | Auth timeout |
| 4002 | Message too large |
| 4003 | Forbidden |
| 4004 | Not found |
| 4005 | Reauth required |
| 4006 | Server shutdown |

### Add to Part 22.9: Setup Code Hashing

```python
# Store setup codes hashed, not plaintext
code_hash = hashlib.sha256(f"{code}{pepper}".encode()).hexdigest()
```

### Add to Part 1.6: AgentStatus Enum

```python
class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
```

---

## Part 10: Updated Effort Estimate

| Component | Hours | Added |
|-----------|-------|-------|
| Agent Store + Invites + Audit | 5h | +1h |
| WebSocket Server + Auth | 4h | — |
| Initiative Store + Models | 3h | +1h |
| InitiativeEngine modification | 2h | +1h |
| Assignment Engine | 2h | — |
| Bridge extension | 1h | — |
| AutonomyLoop phases | 3h | — |
| API endpoints | 3h | +1h |
| CLI commands | 3h | — |
| Remote MCP client | 2h | — |
| Tailscale integration | 2h | — |
| Plugin WebSocket + config | 2h | — |
| Security hardening | 3h | +1h |
| Error recovery | 2h | — |
| Testing | 4h | +1h |
| **Total** | **41h** | **+6h** |

---

**Analysis Complete.**

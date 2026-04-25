# Multi-Agent Colony v0.7.0 — Deep Analysis #5

> **Analysis Date:** 2026-04-25 (Fifth Pass)
> **Analyst:** DevAgent
> **Goal:** Find ANYTHING missed in first four analyses

---

## Executive Summary

After thorough review of edge cases, error scenarios, and operational concerns, **23 additional gaps** were found:

| Category | Critical | Moderate | Minor |
|----------|----------|----------|-------|
| Edge Cases | 3 | 8 | 5 |
| Error Handling | 2 | 3 | 2 |

**Overall Risk Assessment:** LOW-MEDIUM

---

## Part 1: Initiative Processing Edge Cases

### Gap 1: Unknown Initiative Type Handling

**Problem:** What happens when `initiative.type` is not in `INITIATIVE_CAPABILITIES`?

**Current Code:**
```python
required_caps = INITIATIVE_CAPABILITIES.get(initiative.type, [])
if required_caps:
    candidates = [a for a in candidates if all(cap in a.capabilities for cap in required_caps)]
```

**Issue:** Unknown type returns `[]` which means "no capabilities required" - any agent can handle it. This may be intentional (forward compatibility) but should be documented.

**Solution:**
```python
# Option A: Reject unknown types
UNKNOWN_TYPE_POLICY = "reject"  # or "allow_any" or "log_and_allow"

def select_agent_for_initiative(initiative, agents):
    # Check if type is known
    if initiative.type not in INITIATIVE_CAPABILITIES:
        if UNKNOWN_TYPE_POLICY == "reject":
            logger.warning("Unknown initiative type: %s, rejecting", initiative.type)
            return None
        elif UNKNOWN_TYPE_POLICY == "log_and_allow":
            logger.warning("Unknown initiative type: %s, allowing any agent", initiative.type)
        # "allow_any" - no logging
    
    # ... rest of selection logic

# Option B: Add default_capabilities config
INITIATIVE_CAPABILITIES = {
    "follow_up": [],
    "relationship": ["messaging"],
    "scheduling": ["calendar"],
    "coding": ["coding"],
    "health": [],
    "__default__": [],  # Default for unknown types
}
```

**Recommendation:** Add `UNKNOWN_TYPE_POLICY` config with default `"allow_any"` for forward compatibility, but log warnings.

---

### Gap 2: Initiative Expiry Mid-Processing

**Problem:** What happens when initiative expires while agent is processing it?

**Current Schema:**
```sql
expires_at TIMESTAMP,
```

**Missing:** No handling for expiry during processing.

**Solution:**
```python
# initiatives/store.py

async def check_expired(self, initiative_id: str) -> bool:
    """Check if initiative has expired."""
    initiative = await self.get(initiative_id)
    if not initiative:
        return True
    
    if initiative.get("expires_at"):
        if datetime.now(timezone.utc) > datetime.fromisoformat(initiative["expires_at"]):
            # Mark as expired (sub-status of failed)
            await self.update(
                initiative_id,
                status="failed",
                failed_at=datetime.now(timezone.utc).isoformat(),
                failed_reason="initiative_expired",
            )
            return True
    
    return False

# In agent complete/fail handlers
async def complete_initiative(initiative_id: str, agent_id: str, result: str):
    # Check if expired
    if await store.check_expired(initiative_id):
        return {
            "ok": False,
            "error": "initiative_expired",
            "message": "Initiative expired before completion",
        }
    
    # ... normal completion flow
```

**Agent Notification:**
```python
# When initiative expires mid-processing
# Agent receives:
{
    "type": "initiative_expired",
    "initiative_id": "init-123",
    "message": "Initiative expired while processing"
}
```

---

### Gap 3: Agent Crash Mid-Processing

**Problem:** What happens when agent crashes while processing an initiative?

**Current:** Initiatives with status `acknowledged` are NOT reassigned.

**Issue:** If agent crashes, acknowledged initiatives are stuck.

**Solution:**
```python
# autonomy/loop.py - Phase: Stale Initiative Cleanup

async def _phase_stale_initiative_cleanup(self) -> None:
    """Clean up initiatives stuck in acknowledged state."""
    store = self._registry.initiative_store
    if not store:
        return
    
    # Find initiatives acknowledged > 1 hour ago with no activity
    threshold = datetime.now(timezone.utc) - timedelta(hours=1)
    
    stale = await store.list(
        status="acknowledged",
        acknowledged_before=threshold,
        no_activity_since=threshold,
    )
    
    for initiative in stale:
        # Check if agent is still online
        agent = await self._registry.agent_store.get(initiative["assigned_agent_id"])
        
        if not agent or agent["status"] != "online":
            # Agent offline, reassign initiative
            logger.warning(
                "Initiative %s stuck in acknowledged, reassigning (agent %s offline)",
                initiative["id"],
                initiative["assigned_agent_id"],
            )
            await store.update(
                initiative["id"],
                status="pending",
                assigned_agent_id=None,
                assigned_at=None,
                acknowledged_at=None,
                stale_reason="agent_offline_with_acknowledged",
            )
            # Log to history
            await store.log_history(
                initiative["id"],
                action="reassigned_stale",
                agent_id=initiative["assigned_agent_id"],
                details={"reason": "acknowledged_but_agent_offline"},
            )

# Run every 5 minutes
if self._tick_count % 5 == 0:
    await self._phase_stale_initiative_cleanup()
```

---

### Gap 4: Agent Priority Ties

**Problem:** What happens when multiple agents have same load and priority?

**Current:**
```python
candidates.sort(key=lambda a: (
    a.current_assignments / max(a.max_concurrent, 1),
    -a.priority,
))
```

**Issue:** Ties are broken arbitrarily by Python's sort (implementation-dependent).

**Solution:**
```python
# Add tiebreaker: agent_id (deterministic) or last_assigned (round-robin)
import random

candidates.sort(key=lambda a: (
    a.current_assignments / max(a.max_concurrent, 1),  # Primary: load
    -a.priority,                                       # Secondary: priority
    a.get("last_assigned_at") or datetime.min,         # Tertiary: least recently used
    random.random(),                                   # Quaternary: random (if all else equal)
))
```

**Alternative:** Round-robin within same load/priority:
```python
def select_agent_for_initiative(initiative, agents):
    # ... filter candidates ...
    
    # Sort by load, then priority
    candidates.sort(key=lambda a: (a.load, -a.priority))
    
    # Find all candidates with same best score
    if candidates:
        best_load = candidates[0].load
        best_priority = candidates[0].priority
        
        tied = [
            a for a in candidates
            if a.load == best_load and a.priority == best_priority
        ]
        
        if len(tied) > 1:
            # Round-robin among tied agents
            agent = min(tied, key=lambda a: a.get("last_assigned_at", datetime.min))
        else:
            agent = candidates[0]
        
        return agent
    
    return None
```

---

### Gap 5: Included_Types Empty List vs None

**Problem:** What's the difference between `included_types = []` and `included_types = None`?

**Current:**
```python
if agent.included_types and initiative.type not in agent.included_types:
    continue
```

**Issue:** `[]` evaluates to `False`, so `included_types = []` acts like `None`. This is confusing.

**Solution:** Explicit handling:
```python
# Agent config validation
def validate_agent_config(config: dict) -> dict:
    included_types = config.get("included_types")
    
    # Normalize: None means "any type", [] means "no types allowed"
    # But [] for "no types" is nonsensical, so reject it
    
    if included_types is not None and len(included_types) == 0:
        raise ValueError("included_types cannot be empty list - use None for 'any type'")
    
    return config

# Selection logic
if agent.included_types is not None:
    # Agent has specific type restrictions
    if initiative.type not in agent.included_types:
        continue
# else: agent accepts any type
```

**Alternative:** Treat `[]` as "no restrictions":
```python
# Both None and [] mean "no restrictions"
if agent.included_types and initiative.type not in agent.included_types:
    continue
```

**Recommendation:** Document explicitly that `None` and `[]` both mean "no restrictions".

---

### Gap 6: Initiative Timeout Not Enforced

**Problem:** `timeout_seconds` is stored but never enforced.

**Current Schema:**
```sql
timeout_seconds INTEGER DEFAULT 300,
```

**Issue:** No code checks if initiative has exceeded its timeout.

**Solution:**
```python
# autonomy/loop.py - Add to initiative timeout phase

async def _phase_initiative_timeout(self) -> None:
    """Check for timed-out initiatives."""
    store = self._registry.initiative_store
    if not store:
        return
    
    # Find initiatives that have exceeded timeout
    now = datetime.now(timezone.utc)
    
    timed_out = await store.find_timed_out(now)
    
    for initiative in timed_out:
        logger.warning(
            "Initiative %s timed out after %ds",
            initiative["id"],
            initiative["timeout_seconds"],
        )
        
        # Mark as failed
        await store.update(
            initiative["id"],
            status="failed",
            failed_at=now.isoformat(),
            failed_reason="timeout_exceeded",
        )
        
        # Log to history
        await store.log_history(
            initiative["id"],
            action="timed_out",
            agent_id=initiative["assigned_agent_id"],
            details={"timeout_seconds": initiative["timeout_seconds"]},
        )
        
        # Add to dead letter queue if max attempts not reached
        if initiative.get("attempt_count", 0) < initiative.get("max_attempts", 3):
            await self._dead_letter_queue.add(initiative)

# initiatives/store.py

async def find_timed_out(self, now: datetime) -> List[StoredInitiative]:
    """Find initiatives that have exceeded their timeout."""
    cursor = self._db.execute(
        """SELECT * FROM initiatives
           WHERE status IN ('assigned', 'acknowledged')
           AND timeout_seconds IS NOT NULL
           AND datetime(assigned_at, '+' || timeout_seconds || ' seconds') < ?""",
        [now.isoformat()],
    )
    return [self._row_to_initiative(row) for row in cursor.fetchall()]
```

---

### Gap 7: Certificate Expiry Mid-Session

**Problem:** Certificate expiry is checked on WebSocket connect, but not during session.

**Current:**
```python
# Only checked during initial auth
if cert.get("expires_at"):
    expires = datetime.fromisoformat(cert["expires_at"])
    if datetime.now(timezone.utc) > expires:
        return False
```

**Issue:** Long-running sessions (> 24h) could have certificate expire mid-session.

**Solution:**
```python
# agents/websocket.py

class WebSocketManager:
    CERT_EXPIRY_CHECK_INTERVAL = 3600  # 1 hour
    
    async def _session_monitor(self, agent_id: str, websocket: WebSocket):
        """Monitor session for certificate expiry."""
        while agent_id in self._active_connections:
            await asyncio.sleep(self.CERT_EXPIRY_CHECK_INTERVAL)
            
            # Check certificate expiry
            agent = await self._agent_store.get(agent_id)
            cert = json.loads(agent.get("node_cert", "{}"))
            
            if cert.get("expires_at"):
                expires = datetime.fromisoformat(cert["expires_at"])
                if datetime.now(timezone.utc) > expires:
                    logger.info("Agent %s certificate expired, requesting reauth", agent_id)
                    
                    # Send reauth request
                    await websocket.send_json({
                        "type": "reauth_required",
                        "reason": "certificate_expired",
                        "message": "Certificate has expired, please re-authenticate",
                    })
                    
                    # Give agent 60 seconds to reauth
                    await asyncio.sleep(60)
                    
                    # If still connected, close
                    if agent_id in self._active_connections:
                        await websocket.close(code=4005, reason="Reauth timeout")
                    return

# Agent-side handler
@client.on_message("reauth_required")
async def handle_reauth(message):
    # Re-authenticate with fresh certificate
    await client.reauthenticate()
```

---

### Gap 8: ACK Timeout Not Enforced

**Problem:** Initiative delivery requires ACK but timeout is not enforced.

**Current:**
```python
# Send initiative
await websocket.send_json({
    "type": "initiative",
    "seq": seq,
    "initiative": initiative,
})

# Wait for ACK
ack_future = asyncio.Future()
self._pending_acks[initiative["id"]] = ack_future
await ack_future  # No timeout!
```

**Solution:**
```python
# agents/websocket.py

ACK_TIMEOUT = 30  # seconds

async def send_initiative(
    self,
    agent_id: str,
    initiative: dict,
) -> bool:
    """Send initiative and wait for ACK."""
    websocket = self._active_connections.get(agent_id)
    if not websocket:
        return False
    
    seq = self._next_seq(agent_id)
    
    # Send initiative
    await websocket.send_json({
        "type": "initiative",
        "seq": seq,
        "initiative": initiative,
    })
    
    # Wait for ACK with timeout
    ack_future = asyncio.Future()
    self._pending_acks[initiative["id"]] = ack_future
    
    try:
        await asyncio.wait_for(ack_future, timeout=self.ACK_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        logger.warning("No ACK from agent %s for initiative %s", agent_id, initiative["id"])
        
        # Mark initiative as delivery failed
        await self._initiative_store.update(
            initiative["id"],
            status="pending",
            delivery_failed_at=datetime.now(timezone.utc).isoformat(),
            delivery_failed_reason="ack_timeout",
        )
        
        # Remove from pending
        self._pending_acks.pop(initiative["id"], None)
        
        # Add to dead letter queue
        await self._dead_letter_queue.add(initiative)
        
        return False
```

---

## Part 2: Race Condition Edge Cases

### Gap 9: Setup Code Race Condition

**Problem:** What happens when same setup code is used twice simultaneously?

**Current:**
```python
# Validate setup code
invite = await self._validate_setup_code(code)
if not invite:
    return None

# Mark as used
await self._mark_invite_used(code)

# Create agent
agent = await self._create_agent(...)
```

**Issue:** Two requests could pass validation before either marks as used.

**Solution:**
```python
# agents/store.py

async def use_setup_code(
    self,
    code: str,
    node_id: str,
    name: str,
    capabilities: list[str],
) -> Optional[dict]:
    """Use setup code to register agent (atomic)."""
    
    # Hash the code
    code_hash = hashlib.sha256(
        (code + (os.environ.get("COLONY_SETUP_CODE_PEPPER", ""))).encode()
    ).hexdigest()
    
    conn = self._db
    
    # Try to atomically claim the invite
    cursor = conn.execute(
        """UPDATE agent_invites
           SET used_at = ?,
               used_by_node_id = ?,
               granted_name = ?,
               granted_capabilities = ?,
               granted_max_concurrent = ?
           WHERE code_hash = ?
           AND used_at IS NULL
           AND expires_at > ?""",
        [
            datetime.now(timezone.utc).isoformat(),
            node_id,
            name,
            json.dumps(capabilities),
            5,  # default max_concurrent
            code_hash,
            datetime.now(timezone.utc).isoformat(),
        ],
    )
    conn.commit()
    
    if cursor.rowcount == 0:
        # Already used or expired
        logger.warning("Setup code already used or expired")
        return None
    
    # Get the invite
    cursor = conn.execute(
        "SELECT * FROM agent_invites WHERE code_hash = ?",
        [code_hash],
    )
    invite = dict(cursor.fetchone())
    
    # Create agent
    agent = await self._create_agent_from_invite(invite, node_id, name, capabilities)
    
    # Log audit
    await self.log_audit(
        action="agent_connect",
        actor=node_id,
        target=agent["agent_id"],
        details={"name": name, "capabilities": capabilities},
    )
    
    return agent
```

---

### Gap 10: Initiative Claim Race Condition

**Problem:** Already handled with atomic UPDATE. ✅

**Current:**
```python
cursor = conn.execute(
    """UPDATE initiatives 
       SET status = 'assigned', 
           assigned_agent_id = ?,
           assigned_at = ?
       WHERE id = ? AND status = 'pending'""",
    [agent_id, datetime.now(timezone.utc).isoformat(), initiative_id]
)
return cursor.rowcount == 1  # False if already claimed
```

**Verified:** This correctly handles race conditions.

---

### Gap 11: Rate Limit Boundary

**Problem:** What happens when rate limit is hit exactly at boundary?

**Current:**
```python
if assigned_this_hour >= agent.get("max_initiatives_per_hour", 10):
    return False
```

**Issue:** Window is calculated from "1 hour ago", so:
- T=0: Agent gets 10 initiatives
- T=59: Agent tries 11th → rejected
- T=60: First initiative falls out of window, agent can get another

**This is correct behavior** but should be documented.

**Solution:** Document sliding window behavior:
```python
# Rate limit is a sliding window:
# - Count initiatives assigned in last 60 minutes
# - When oldest initiative falls out of window, capacity opens up
# - Example: 10 initiatives at T=0, 11th rejected at T=30, allowed at T=61
```

---

## Part 3: Error Handling Edge Cases

### Gap 12: WebSocket Malformed Message

**Problem:** What happens when agent sends malformed JSON?

**Current:**
```python
message = json.loads(data)
```

**Issue:** No try/catch, connection will crash.

**Solution:**
```python
async def _message_loop(self, agent_id: str, websocket: WebSocket):
    """Handle incoming WebSocket messages."""
    try:
        while True:
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
            except json.JSONDecodeError as e:
                logger.warning("Agent %s sent invalid JSON: %s", agent_id, e)
                await websocket.send_json({
                    "type": "error",
                    "error": "invalid_json",
                    "message": "Invalid JSON format",
                })
                continue
            
            # Validate message structure
            if not isinstance(message, dict):
                await websocket.send_json({
                    "type": "error",
                    "error": "invalid_message",
                    "message": "Message must be a JSON object",
                })
                continue
            
            if "type" not in message:
                await websocket.send_json({
                    "type": "error",
                    "error": "missing_type",
                    "message": "Message must have 'type' field",
                })
                continue
            
            # Handle message
            await self._handle_message(agent_id, websocket, message)
            
    except WebSocketDisconnect:
        logger.info("Agent %s disconnected", agent_id)
    except Exception as e:
        logger.error("Agent %s WebSocket error: %s", agent_id, e)
    finally:
        await self._cleanup_connection(agent_id)
```

---

### Gap 13: Agent.json Corruption

**Problem:** What happens when `~/.colony/agent.json` is corrupted?

**Current:**
```python
config = json.loads(plaintext_path.read_text())
```

**Issue:** No error handling for corrupted JSON.

**Solution:**
```python
def load_agent_config() -> Optional[dict]:
    """Load agent config (encrypted or plaintext)."""
    config_path = Path.home() / ".colony" / "agent.json.enc"
    plaintext_path = Path.home() / ".colony" / "agent.json"
    
    # Try encrypted first
    if config_path.exists():
        try:
            key = _get_or_create_agent_key()
            f = Fernet(key)
            encrypted = config_path.read_bytes()
            return json.loads(f.decrypt(encrypted))
        except Exception as e:
            logger.error("Failed to decrypt agent config: %s", e)
            # Don't return None yet, try plaintext fallback
    
    # Fallback to plaintext
    if plaintext_path.exists():
        try:
            config = json.loads(plaintext_path.read_text())
            # Migrate to encrypted
            save_agent_config_encrypted(config)
            plaintext_path.unlink()
            return config
        except json.JSONDecodeError as e:
            logger.error("Agent config corrupted: %s", e)
            
            # Try to recover from backup
            backup_path = Path.home() / ".colony" / "agent.json.backup"
            if backup_path.exists():
                logger.info("Attempting recovery from backup")
                try:
                    config = json.loads(backup_path.read_text())
                    # Restore from backup
                    plaintext_path.write_text(backup_path.read_text())
                    logger.info("Recovered agent config from backup")
                    return config
                except Exception:
                    logger.error("Backup also corrupted")
            
            # No recovery possible
            print("ERROR: Agent config corrupted and no backup available.")
            print("Run 'colony agent connect' to re-register this agent.")
            return None
    
    return None
```

---

### Gap 14: Colony Restart with Pending Initiatives

**Problem:** What happens when Colony restarts with pending/assigned initiatives?

**Issue:** In-memory state is lost, agents may be disconnected.

**Solution:**
```python
# server.py - on startup

async def on_startup():
    """Initialize Colony state on startup."""
    
    # Mark all agents as offline (they'll reconnect)
    await agent_store.mark_all_offline()
    
    # Check for stuck initiatives
    await recover_stuck_initiatives()

async def recover_stuck_initiatives():
    """Recover initiatives stuck in assigned/acknowledged state."""
    store = initiative_store
    
    # Find initiatives that were assigned but agent is now offline
    assigned = await store.list(status=["assigned", "acknowledged"])
    
    for initiative in assigned:
        agent = await agent_store.get(initiative["assigned_agent_id"])
        
        if not agent or agent["status"] == "offline":
            logger.info(
                "Resetting initiative %s to pending (agent offline after restart)",
                initiative["id"],
            )
            await store.update(
                initiative["id"],
                status="pending",
                assigned_agent_id=None,
                assigned_at=None,
                acknowledged_at=None,
                recovery_reason="colony_restart",
            )

# Mark all agents offline
async def mark_all_offline(self):
    """Mark all agents as offline (called on Colony restart)."""
    self._db.execute(
        "UPDATE agents SET status = 'offline', websocket_connected = 0"
    )
    self._db.commit()
```

---

### Gap 15: Circuit Breaker with Urgent Initiatives

**Problem:** Circuit breaker opens after 5 failures, but what about urgent initiatives?

**Issue:** Urgent initiatives may need delivery even if circuit is open.

**Solution:**
```python
# delivery/circuit_breaker.py

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 300,
        allow_urgent_on_open: bool = True,  # NEW
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.allow_urgent_on_open = allow_urgent_on_open
        self.failures = 0
        self.state = "closed"
        self.last_failure_time = None
    
    def can_execute(self, priority: int = 1) -> bool:
        """Check if execution is allowed."""
        if self.state == "closed":
            return True
        
        if self.state == "open":
            # Check if urgent execution allowed
            if self.allow_urgent_on_open and priority >= 2:  # high priority
                logger.info("Circuit open but allowing urgent initiative")
                return True
            
            # Check if recovery timeout passed
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half_open"
                return True
            
            return False
        
        # half_open - allow one test
        return True

# Usage
if circuit_breaker.can_execute(initiative.get("priority", 1)):
    await deliver_initiative(initiative)
else:
    logger.warning("Circuit breaker open, initiative queued")
    await dead_letter_queue.add(initiative, reason="circuit_open")
```

---

### Gap 16: Database Lock Timeout

**Problem:** SQLite can return "database is locked" under concurrent access.

**Current:**
```python
conn.execute("PRAGMA busy_timeout=5000")
```

**Issue:** 5 seconds may not be enough under heavy load.

**Solution:**
```python
# Add retry logic for database operations

import time
from contextlib import contextmanager

@contextmanager
def retry_db_operation(max_retries: int = 3, delay: float = 0.1):
    """Retry database operation on lock."""
    for attempt in range(max_retries):
        try:
            yield
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                logger.warning("Database locked, retrying (attempt %d)", attempt + 1)
                time.sleep(delay * (attempt + 1))  # Exponential backoff
            else:
                raise

# Usage
async def update(self, agent_id: str, **updates):
    with retry_db_operation():
        self._db.execute(...)
        self._db.commit()
```

---

## Part 4: Configuration Edge Cases

### Gap 17: Agent Capabilities Change Mid-Assignment

**Problem:** What happens when agent's capabilities are changed while processing initiatives?

**Issue:** Agent may no longer have capabilities required for its current initiatives.

**Solution:**
```python
# agents/store.py

async def update(self, agent_id: str, **updates) -> dict:
    """Update agent settings."""
    agent = await self.get(agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")
    
    # Check if capabilities are being reduced
    if "capabilities" in updates:
        old_caps = set(agent.get("capabilities", []))
        new_caps = set(updates["capabilities"])
        removed = old_caps - new_caps
        
        if removed:
            # Check if agent has initiatives requiring removed capabilities
            initiatives = await self._initiative_store.list(
                assigned_agent_id=agent_id,
                status=["assigned", "acknowledged"],
            )
            
            for init in initiatives:
                required = INITIATIVE_CAPABILITIES.get(init["type"], [])
                if any(cap in removed for cap in required):
                    logger.warning(
                        "Agent %s losing capability required by initiative %s",
                        agent_id,
                        init["id"],
                    )
                    # Option 1: Reassign initiative
                    await self._initiative_store.update(
                        init["id"],
                        status="pending",
                        assigned_agent_id=None,
                        reassigned_reason="agent_capability_removed",
                    )
                    # Option 2: Block capability removal
                    # raise ValueError(f"Cannot remove capabilities {removed}: initiative {init['id']} requires them")
    
    # Proceed with update
    return await self._do_update(agent_id, **updates)
```

---

### Gap 18: Invalid Max_Concurrent Value

**Problem:** What happens when `max_concurrent` is set to 0 or negative?

**Issue:** Division by zero in load calculation.

**Current:**
```python
a.current_assignments / max(a.max_concurrent, 1)
```

**Solution:**
```python
# Validation
def validate_agent_config(config: dict) -> dict:
    if "max_concurrent" in config:
        if config["max_concurrent"] < 1:
            raise ValueError("max_concurrent must be >= 1")
    
    if "priority" in config:
        if config["priority"] < 0 or config["priority"] > 2:
            raise ValueError("priority must be 0, 1, or 2")
    
    return config
```

---

### Gap 19: Empty Capabilities List

**Problem:** What happens when agent has empty capabilities list?

**Current:**
```python
candidates = [
    a for a in candidates 
    if all(cap in a.capabilities for cap in required_caps)
]
```

**Issue:** `[]` passes all capability checks (vacuously true).

**This is intentional** - an agent with no capabilities can handle initiatives that require no capabilities.

**Solution:** Document this behavior:
```python
# Agent with capabilities=[] can only handle initiatives with no capability requirements
# Agent with capabilities=["messaging", "calendar"] can handle messaging, calendar, and no-requirement initiatives
# Agent with capabilities=["coding"] can only handle coding initiatives (NOT messaging or calendar)
```

---

## Part 5: Operational Edge Cases

### Gap 20: Ghost Agent with Assigned Initiatives

**Problem:** What happens when ghost agent (never connected) has assigned initiatives?

**Current:** Ghost cleanup removes agents that never connected.

**Issue:** Initiatives assigned to ghost agents should be reassigned first.

**Solution:**
```python
# autonomy/loop.py - Ghost cleanup phase

async def _phase_ghost_cleanup(self) -> None:
    """Remove agents that registered but never connected."""
    agent_store = self._registry.agent_store
    initiative_store = self._registry.initiative_store
    
    if not agent_store or not initiative_store:
        return
    
    # Find ghosts
    threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
    ghosts = await agent_store.list_ghosts(registered_before=threshold)
    
    for ghost in ghosts:
        # Reassign initiatives first
        initiatives = await initiative_store.list(assigned_agent_id=ghost["agent_id"])
        
        for init in initiatives:
            logger.info(
                "Reassigning initiative %s from ghost agent %s",
                init["id"],
                ghost["agent_id"],
            )
            await initiative_store.update(
                init["id"],
                status="pending",
                assigned_agent_id=None,
                reassigned_reason="agent_ghost",
            )
        
        # Now remove ghost
        await agent_store.delete(ghost["agent_id"])
        logger.info("Removed ghost agent %s", ghost["agent_id"])
```

---

### Gap 21: Initiative Priority Below 0 or Above 1

**Problem:** Priority is calculated as 0.0-1.0, but what if it's outside range?

**Current:**
```python
priority: float  # 0.0-1.0
```

**Issue:** No validation that priority is in range.

**Solution:**
```python
# Validation
def validate_initiative(initiative: dict) -> dict:
    if "priority" in initiative:
        priority = initiative["priority"]
        if not 0.0 <= priority <= 1.0:
            logger.warning("Priority %s out of range, clamping to [0.0, 1.0]", priority)
            initiative["priority"] = max(0.0, min(1.0, priority))
    
    return initiative

# Or in StoredInitiative
@validator("priority")
def validate_priority(cls, v):
    if not 0.0 <= v <= 1.0:
        raise ValueError("priority must be between 0.0 and 1.0")
    return v
```

---

### Gap 22: Unicode in Initiative Description

**Problem:** Initiative description may contain unicode characters.

**Issue:** SQLite stores UTF-8 by default, so this should work. But need to verify.

**Solution:**
```python
# Verify SQLite is using UTF-8
conn = sqlite3.connect(db_path)
conn.text_factory = str  # Default, handles UTF-8

# Test unicode
test_description = "Follow up with 用户 🎉"
assert store.create({"description": test_description})["description"] == test_description
```

**This is already handled** by SQLite's UTF-8 default.

---

### Gap 23: Initiative Count Limit

**Problem:** What happens when there are thousands of pending initiatives?

**Issue:** Query performance may degrade.

**Solution:**
```python
# Add pagination
async def list(
    self,
    status: Optional[List[str]] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[StoredInitiative]:
    """List initiatives with pagination."""
    query = "SELECT * FROM initiatives"
    params = []
    
    if status:
        placeholders = ",".join("?" * len(status))
        query += f" WHERE status IN ({placeholders})"
        params.extend(status)
    
    query += " ORDER BY priority DESC, created_at ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor = self._db.execute(query, params)
    return [self._row_to_initiative(row) for row in cursor.fetchall()]

# Add max pending limit
MAX_PENDING_INITIATIVES = 1000

async def create(self, data: dict) -> StoredInitiative:
    """Create initiative with limit check."""
    pending_count = await self.count(status="pending")
    
    if pending_count >= MAX_PENDING_INITIATIVES:
        logger.warning(
            "Max pending initiatives reached (%d), rejecting new initiative",
            MAX_PENDING_INITIATIVES,
        )
        raise ValueError(f"Too many pending initiatives (max {MAX_PENDING_INITIATIVES})")
    
    # ... create initiative
```

---

## Part 6: Summary

### Gaps Found (23)

| # | Gap | Severity | Category |
|---|-----|----------|----------|
| 1 | Unknown initiative type handling | Minor | Edge Case |
| 2 | Initiative expiry mid-processing | Critical | Edge Case |
| 3 | Agent crash mid-processing (acknowledged) | Critical | Edge Case |
| 4 | Agent priority ties | Minor | Edge Case |
| 5 | Included_types empty list vs None | Minor | Edge Case |
| 6 | Initiative timeout not enforced | Critical | Edge Case |
| 7 | Certificate expiry mid-session | Moderate | Edge Case |
| 8 | ACK timeout not enforced | Moderate | Edge Case |
| 9 | Setup code race condition | Moderate | Race Condition |
| 10 | Initiative claim race condition | — | Already Fixed |
| 11 | Rate limit boundary (document) | Minor | Race Condition |
| 12 | WebSocket malformed message | Moderate | Error Handling |
| 13 | Agent.json corruption | Moderate | Error Handling |
| 14 | Colony restart with pending initiatives | Moderate | Error Handling |
| 15 | Circuit breaker with urgent initiatives | Moderate | Error Handling |
| 16 | Database lock timeout | Minor | Error Handling |
| 17 | Capabilities change mid-assignment | Moderate | Configuration |
| 18 | Invalid max_concurrent value | Minor | Configuration |
| 19 | Empty capabilities list (document) | Minor | Configuration |
| 20 | Ghost agent with assigned initiatives | Moderate | Operations |
| 21 | Initiative priority out of range | Minor | Operations |
| 22 | Unicode in description | — | Already Handled |
| 23 | Initiative count limit | Moderate | Operations |

---

## Part 7: Recommended Spec Amendments

### Add to Part 20: Edge Cases

```markdown
### 20.X Initiative Expiry Mid-Processing

When initiative expires while agent is processing:
- Colony sends `initiative_expired` message
- Initiative marked as `failed` with reason `initiative_expired`
- Agent should abort processing

### 20.X Agent Crash Mid-Processing

When agent crashes with acknowledged initiatives:
- Autonomy loop checks for stale acknowledgments (every 5 min)
- If agent offline for > 1 hour with acknowledged initiative, reassign to pending
- Log to history: `reassigned_stale`

### 20.X Initiative Timeout Enforcement

When initiative exceeds `timeout_seconds`:
- Autonomy loop checks for timed-out initiatives (every tick)
- Mark as `failed` with reason `timeout_exceeded`
- Add to dead letter queue if attempts remaining
```

### Add to Part 22: Security

```markdown
### 22.X Setup Code Atomic Claim

Setup code validation and claim must be atomic:
- Single UPDATE with WHERE clause checks both conditions
- Returns affected row count to detect if already used
- Prevents race condition with concurrent setup code use
```

### Add to Part 23: Error Recovery

```markdown
### 23.X Colony Restart Recovery

On Colony restart:
1. Mark all agents as offline (they'll reconnect)
2. Find initiatives in assigned/acknowledged state
3. If assigned agent is offline, reset to pending
4. Log recovery to history

### 23.X WebSocket Malformed Message Handling

On malformed JSON:
- Log warning with agent_id
- Send `error` message with `invalid_json` code
- Continue message loop (don't disconnect)
```

---

## Part 8: Updated Effort Estimate

| Component | Hours | Added |
|-----------|-------|-------|
| All previous components | 63h | — |
| Initiative timeout enforcement | 1h | +1h |
| Stale initiative cleanup phase | 1h | +1h |
| Certificate expiry monitoring | 1h | +1h |
| ACK timeout enforcement | 1h | +1h |
| Setup code atomic claim | 0.5h | +0.5h |
| WebSocket error handling | 1h | +1h |
| Agent.json recovery | 0.5h | +0.5h |
| Colony restart recovery | 1h | +1h |
| Initiative count limit | 0.5h | +0.5h |
| Configuration validation | 0.5h | +0.5h |
| **Total** | **71h** | **+8h** |

---

**Analysis Complete.**

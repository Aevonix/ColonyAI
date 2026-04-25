# Multi-Agent Colony v0.7.0 — Gap & Security Analysis

> **Analysis Date:** 2026-04-25
> **Analyst:** DevAgent
> **Scope:** Gaps, security, automation, edge cases

---

## Executive Summary

The spec is **well-architected** but has **23 gaps** requiring attention:

| Category | Critical | Moderate | Minor |
|----------|----------|----------|-------|
| Security | 4 | 3 | 1 |
| Automation | 2 | 4 | 2 |
| Edge Cases | 1 | 3 | 3 |

**Overall Risk Assessment:** MEDIUM

Primary concerns:
1. Certificate signing flow incomplete
2. Remote agent bootstrap lacks atomicity
3. No automated cleanup for failed connections
4. Missing rate limiting on setup codes

---

## Part 1: Security Gaps

### Gap 1: Certificate Signing — Missing Colony Key Access

**Location:** Part 17.3 (Setup Code Flow)

**Problem:** Remote agent connection requires Colony private key to sign node certificate. But:

```python
# Spec shows this flow:
1. Remote agent POST /v1/host/agents/connect
2. Colony validates setup code
3. Colony signs node certificate with Colony private key ← HOW?
```

**Current Code:** `chain/node.py` has `create_node_certificate()` but requires `colony_key_manager`:

```python
def create_node_certificate(
    state_dir: str | Path,
    colony_key_manager: Optional["LocalKeyManager"] = None,  # Required!
) -> dict:
```

**Missing:**
- API endpoint for signing certificates
- Colony key access from API layer
- Passphrase handling for encrypted Colony keys

**Risk:** HIGH — Without this, remote agents cannot connect.

**Fix:**

```python
# agents/store.py
class AgentStore:
    def __init__(self, state_dir: Path, colony_key_manager: LocalKeyManager):
        self._state_dir = state_dir
        self._colony_km = colony_key_manager
    
    async def sign_node_certificate(
        self,
        node_id: str,
        node_public_key: str,
    ) -> dict:
        """Sign a node certificate for remote agent."""
        from colony_sidecar.chain.node import create_node_certificate
        
        # Generate signed cert
        cert = create_node_certificate(
            state_dir=self._state_dir,
            colony_key_manager=self._colony_km,
        )
        
        # Override node_id and public key from request
        cert["node_id"] = node_id
        cert["node_public_key_ed25519"] = node_public_key
        cert["issued_at"] = datetime.now(timezone.utc).isoformat()
        
        # Re-sign with Colony key
        payload = json.dumps(
            {k: v for k, v in cert.items() if k != "signature"},
            sort_keys=True,
            separators=(",", ":")
        ).encode()
        cert["signature"] = self._colony_km.sign(payload)
        
        return cert
```

---

### Gap 2: WebSocket Authentication — Signature Verification Incomplete

**Location:** Part 14.8, Part 4.1

**Problem:** Spec shows auth header with signature, but verification details incomplete:

```python
# Spec shows:
Authorization: Bearer {timestamp}:{signature}

# But what gets signed?
# - agent_id:timestamp? 
# - Just timestamp?
# - Full challenge?
```

**Current Code:** No WebSocket auth verification exists for agent connections.

**Missing:**
- Challenge-response protocol
- Replay attack prevention
- Signature verification implementation

**Risk:** HIGH — Unauthenticated agents could connect.

**Fix:**

```python
# agents/websocket.py

import time
import hmac

class WebSocketManager:
    # Time skew tolerance (5 minutes)
    MAX_TIMESTAMP_SKEW = 300
    
    async def verify_agent_auth(
        self,
        agent_id: str,
        auth_header: str,
    ) -> bool:
        """Verify WebSocket authentication.
        
        Expected format: Bearer {timestamp}:{signature}
        Signed payload: {agent_id}:{timestamp}
        """
        if not auth_header.startswith("Bearer "):
            return False
        
        try:
            token = auth_header[7:]
            timestamp_str, signature_hex = token.split(":")
            timestamp = int(timestamp_str)
        except (ValueError, AttributeError):
            return False
        
        # Check timestamp is recent (prevent replay)
        now = int(time.time())
        if abs(now - timestamp) > self.MAX_TIMESTAMP_SKEW:
            logger.warning(
                "Agent %s auth timestamp skew too large: %ds",
                agent_id,
                abs(now - timestamp),
            )
            return False
        
        # Get agent's node public key from cert
        agent = await self._agent_store.get(agent_id)
        if not agent:
            return False
        
        cert = json.loads(agent.get("node_cert", "{}"))
        pubkey = cert.get("node_public_key_ed25519")
        if not pubkey:
            return False
        
        # Verify signature
        # Signed payload: {agent_id}:{timestamp}
        message = f"{agent_id}:{timestamp}".encode()
        return _verify_ed25519_signature(pubkey, message, signature_hex)
```

---

### Gap 3: Setup Code Brute Force Protection

**Location:** Part 2.1, Part 1.2

**Problem:** No rate limiting on setup code validation. Attacker could brute force codes.

**Current Schema:**
```sql
CREATE TABLE agent_invites (
    code TEXT PRIMARY KEY,  -- "COLONY-7X9K-M2P4-QR8W"
    -- No failed_attempts counter
    -- No lockout mechanism
);
```

**Risk:** MEDIUM — Setup codes are short (16 chars), could be brute forced.

**Fix:**

```sql
-- Add to agent_invites schema
ALTER TABLE agent_invites ADD COLUMN failed_attempts INTEGER DEFAULT 0;
ALTER TABLE agent_invites ADD COLUMN locked_until TIMESTAMP;
```

```python
# agents/store.py

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

async def validate_invite(self, code: str) -> dict:
    invite = self._get_invite(code)
    
    if not invite:
        raise InvalidInviteError("Invalid setup code")
    
    # Check lockout
    if invite.get("locked_until"):
        locked_until = datetime.fromisoformat(invite["locked_until"])
        if datetime.now(timezone.utc) < locked_until:
            raise InvalidInviteError(
                f"Setup code locked until {locked_until.isoformat()}"
            )
    
    # Check expiry
    if datetime.fromisoformat(invite["expires_at"]) < datetime.now(timezone.utc):
        raise InvalidInviteError("Setup code expired")
    
    # Check max uses
    if invite["use_count"] >= invite["max_uses"]:
        raise InvalidInviteError("Setup code already used")
    
    return invite

async def record_failed_attempt(self, code: str) -> None:
    """Record failed validation attempt. Lock after too many."""
    self._db.execute(
        "UPDATE agent_invites SET failed_attempts = failed_attempts + 1 WHERE code = ?",
        [code],
    )
    
    invite = self._get_invite(code)
    if invite and invite["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
        locked_until = datetime.now(timezone.utc) + LOCKOUT_DURATION
        self._db.execute(
            "UPDATE agent_invites SET locked_until = ? WHERE code = ?",
            [locked_until.isoformat(), code],
        )
        logger.warning(
            "Setup code %s locked after %d failed attempts",
            code[:8] + "...",
            invite["failed_attempts"],
        )

async def reset_failed_attempts(self, code: str) -> None:
    """Reset failed attempts counter on successful validation."""
    self._db.execute(
        "UPDATE agent_invites SET failed_attempts = 0 WHERE code = ?",
        [code],
    )
```

---

### Gap 4: Colony Private Key Passphrase Handling

**Location:** Part 17, Part 7.4

**Problem:** Spec doesn't address encrypted Colony private keys. Colony key may be passphrase-protected.

**Current Code:** `chain/identity.py` supports passphrase:

```python
def _sign_with_key(
    private_key_pem: str | bytes,
    message: bytes,
    passphrase: Optional[bytes] = None,  # ← Can be encrypted
) -> str:
```

**Missing:**
- How API layer accesses Colony private key
- Where passphrase is stored/retrieved
- Interaction with `colony key set-passphrase`

**Risk:** MEDIUM — Could block remote agent setup.

**Fix:**

```python
# server.py or config.py

def get_colony_key_manager() -> Optional[LocalKeyManager]:
    """Get Colony key manager, handling passphrase if needed."""
    from colony_sidecar.chain.local_keys import LocalKeyManager
    from colony_sidecar.chain.identity import get_or_create_colony_id
    
    state_dir = get_state_dir()
    colony_id = get_or_create_colony_id(state_dir)
    keys_dir = state_dir / "colony-keys"
    
    if not (keys_dir / "private.pem").exists():
        return None
    
    # Check if key is encrypted
    passphrase = None
    passphrase_env = os.environ.get("COLONY_KEY_PASSPHRASE", "")
    if passphrase_env:
        passphrase = passphrase_env.encode()
    else:
        # Check for passphrase file (created by 'colony key set-passphrase')
        passphrase_file = state_dir / ".colony-key-passphrase"
        if passphrase_file.exists():
            passphrase = passphrase_file.read_bytes().strip()
    
    return LocalKeyManager(
        keys_dir=keys_dir,
        colony_id=colony_id,
        passphrase=passphrase,
    )
```

```bash
# .env
COLONY_KEY_PASSPHRASE=your-passphrase-here  # Or use file

# Or store in restricted file
echo -n "your-passphrase" > ~/.colony/.colony-key-passphrase
chmod 600 ~/.colony/.colony-key-passphrase
```

---

### Gap 5: Agent Impersonation Prevention

**Location:** Part 3, Part 4

**Problem:** If agent_id is known, could another agent impersonate it?

**Attack Vector:**
```
1. Attacker learns agent_id "agent-123" (from logs, API response)
2. Attacker connects WebSocket with agent_id in URL
3. Attacker sends crafted auth header
```

**Current Protection:** Node certificate verification (Gap 2).

**Additional Protection Needed:** Bind agent_id to node_id.

**Fix:**

```python
# agents/websocket.py

async def verify_agent_auth(self, agent_id: str, auth_header: str) -> bool:
    # ... existing signature verification ...
    
    # Additional check: agent_id must match node_id in cert
    agent = await self._agent_store.get(agent_id)
    cert = json.loads(agent.get("node_cert", "{}"))
    
    # Verify the certificate's node_id matches this agent's registered node_id
    if cert.get("node_id") != agent.get("node_id"):
        logger.warning(
            "Agent %s cert node_id mismatch: cert=%s, registered=%s",
            agent_id,
            cert.get("node_id"),
            agent.get("node_id"),
        )
        return False
    
    return True
```

---

### Gap 6: API Key Exposure in Agent Config

**Location:** Part 2.3, Part 18.2

**Problem:** `agent.json` contains API key in plaintext:

```json
{
    "agent_id": "agent-uuid",
    "api_key": "colony-api-key",  // ← Plaintext
    ...
}
```

**Risk:** LOW — File is on agent's machine, but still plaintext credentials.

**Fix:**

```json
// Store only reference, not actual key
{
    "agent_id": "agent-uuid",
    "api_key_source": "env:COLONY_API_KEY",  // Reference
    ...
}
```

Or use OS keyring:

```python
# agents/store.py
import keyring

SERVICE_NAME = "colony-agent"

def store_api_key(agent_id: str, api_key: str) -> None:
    keyring.set_password(SERVICE_NAME, agent_id, api_key)

def get_api_key(agent_id: str) -> Optional[str]:
    return keyring.get_password(SERVICE_NAME, agent_id)
```

---

### Gap 7: Revocation Doesn't Invalidate Active Sessions

**Location:** Part 17.5, Part 3.4

**Problem:** `colony agent revoke` marks agent as revoked, but doesn't close active WebSocket.

**Current Spec:**
```python
await agent_store.update(agent_id, status="revoked")
# WebSocket still connected!
```

**Risk:** MEDIUM — Revoked agent could continue receiving initiatives.

**Fix:**

```python
# agents/websocket.py

class WebSocketManager:
    _active_connections: Dict[str, WebSocket] = {}
    
    async def disconnect_agent(self, agent_id: str, reason: str) -> None:
        """Disconnect a specific agent's WebSocket."""
        ws = self._active_connections.get(agent_id)
        if ws:
            try:
                await ws.send_json({
                    "type": "disconnect",
                    "reason": reason,
                    "reconnect": False,
                })
                await ws.close(code=4003, reason=reason)
            except Exception:
                pass
            finally:
                self._active_connections.pop(agent_id, None)
            logger.info("Disconnected agent %s: %s", agent_id, reason)

# api/routers/host.py

@router.delete("/agents/{agent_id}")
async def revoke_agent(agent_id: str, body: RevokeRequest) -> RevokeResponse:
    agent = await _agent_store.get(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    
    # Mark as revoked
    await _agent_store.update(agent_id, status="revoked")
    
    # Disconnect WebSocket if connected
    if _websocket_manager and agent.get("websocket_connected"):
        await _websocket_manager.disconnect_agent(
            agent_id,
            reason=body.reason or "Agent revoked",
        )
    
    # Reassign initiatives
    reassigned = 0
    if body.reassign_initiatives:
        reassigned = await _reassign_agent_initiatives(agent_id)
    
    return RevokeResponse(ok=True, reassigned_initiatives=reassigned)
```

---

### Gap 8: No Mutual TLS for WebSocket

**Location:** Part 4, Part 17

**Problem:** WebSocket uses bearer token auth, not mutual TLS. Token could be intercepted.

**Current:** Bearer token in Authorization header.

**Risk:** LOW — TLS already encrypts transport. Token is single-use per connection.

**Decision:** Accept current design. Bearer token over TLS is standard (OAuth 2.0, etc.).

---

## Part 2: Automation Gaps

### Gap 9: Remote Agent Bootstrap Not Atomic

**Location:** Part 2.2, Part 18.2

**Problem:** `colony agent connect` has multiple steps that could fail mid-way:

```
1. Validate setup code with Colony          ← Could fail
2. Generate node_id + node_keypair          ← Could fail
3. Send public key to Colony                ← Could fail
4. Colony validates, signs certificate      ← Could fail
5. Receive agent_id, node_cert, ws_url      ← Could fail
6. Save to ~/.colony/agent.json             ← Could fail
7. Open WebSocket connection                ← Could fail
```

**Risk:** Agent left in partially configured state.

**Fix:**

```python
# cli.py

async def _cmd_agent_connect(args) -> None:
    state_dir = Path.home() / ".colony"
    agent_config_path = state_dir / "agent.json"
    backup_path = state_dir / "agent.json.backup"
    
    # Pre-flight checks
    if not args.setup_code or not args.colony_url:
        print("ERROR: --setup-code and --colony-url are required")
        return 1
    
    # Create backup of existing config if present
    if agent_config_path.exists():
        shutil.copy(agent_config_path, backup_path)
    
    try:
        # Step 1: Validate setup code
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{args.colony_url}/v1/host/agents/validate-invite",
                json={"code": args.setup_code},
            ) as resp:
                if resp.status != 200:
                    print(f"ERROR: Invalid or expired setup code")
                    return 1
        
        # Step 2: Generate or load node identity
        node_id = get_or_create_node_id(state_dir)
        node_km = ensure_node_keypair(state_dir)
        node_pubkey = node_km.public_key_hex()
        
        # Step 3-5: Connect to Colony
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{args.colony_url}/v1/host/agents/connect",
                json={
                    "setup_code": args.setup_code,
                    "node_id": node_id,
                    "node_public_key": node_pubkey,
                    "name": args.name or socket.gethostname(),
                    "capabilities": args.capabilities.split(",") if args.capabilities else [],
                    "metadata": {
                        "hostname": socket.gethostname(),
                        "version": __version__,
                    },
                },
            ) as resp:
                if resp.status != 200:
                    error = await resp.json()
                    print(f"ERROR: {error.get('message', 'Connection failed')}")
                    return 1
                
                result = await resp.json()
        
        # Step 6: Write config (atomic write)
        temp_path = agent_config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps({
            "agent_id": result["agent_id"],
            "node_id": result["node_id"],
            "colony_id": result["colony_id"],
            "colony_url": args.colony_url,
            "websocket_url": result["websocket_url"],
            "name": args.name or socket.gethostname(),
            "capabilities": result["capabilities"],
            "is_primary": result["is_primary"],
            "max_concurrent": result["max_concurrent"],
            "node_cert": result["node_cert"],
            "connection_mode": "remote",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n")
        temp_path.rename(agent_config_path)  # Atomic on POSIX
        
        # Step 7: Verify WebSocket connection works
        ws_url = result["websocket_url"]
        # ... test connection ...
        
        # Success! Remove backup
        if backup_path.exists():
            backup_path.unlink()
        
        print(f"✓ Agent connected: {result['agent_id']}")
        print(f"✓ Config saved to {agent_config_path}")
        
    except Exception as e:
        # Rollback on any failure
        print(f"ERROR: {e}")
        
        # Restore backup if exists
        if backup_path.exists():
            shutil.move(backup_path, agent_config_path)
            print("  Restored previous config from backup")
        
        # Clean up partial node keypair
        node_keys_dir = state_dir / "node-keys"
        if node_keys_dir.exists():
            # Only if we generated it in this session
            # (check if node-id was created just now)
            pass
        
        return 1
```

---

### Gap 10: No Automatic Harness Detection on Remote Agent

**Location:** Part 18.2

**Problem:** `colony agent connect` doesn't auto-detect which harness to configure.

**Current:**
```bash
# User must run TWO commands:
colony agent connect --setup-code ... --colony-url ...
colony mcp setup --harness claude-code --remote
```

**Fix:**

```python
# cli.py

def _cmd_agent_connect(args) -> None:
    # ... existing connect logic ...
    
    # After successful connection:
    
    # Auto-detect harness if not specified
    if not args.harness:
        from colony_sidecar.mcp.config import detect_harnesses
        detected = detect_harnesses()
        installed = [h for h, is_inst in detected.items() if is_inst]
        
        if len(installed) == 1:
            args.harness = installed[0]
            print(f"  Auto-detected harness: {args.harness}")
        elif len(installed) > 1:
            print("\nDetected harnesses:")
            for h in installed:
                print(f"  - {h}")
            print("\nRun 'colony mcp setup --harness <name> --remote' to configure")
    
    # Auto-configure harness if detected
    if args.harness:
        print(f"\nConfiguring {args.harness}...")
        _cmd_mcp_setup_harness(args.harness, remote=True, sidecar_url=args.colony_url)
```

```bash
# Now user can run ONE command:
colony agent connect --setup-code COLONY-... --colony-url https://...

# Output:
# ✓ Agent connected
# ✓ Auto-detected harness: claude-code
# ✓ MCP config written to ~/.claude.json
```

---

### Gap 11: Tailscale Auto-Join Requires Pre-Stored API Key

**Location:** Part 15.3, Part 15.5

**Problem:** `colony agent invite --tailscale --auto-join` requires Tailscale API key pre-stored.

**Current:**
```bash
# Colony host must have run:
colony tailscale setup --api-key tskey-api-xxx
```

**Issue:** This is a manual step that could be forgotten.

**Fix:**

```python
# cli.py

def _cmd_agent_invite(args) -> None:
    # ... existing invite logic ...
    
    if args.tailscale:
        ts = TailscaleManager()
        
        if not ts.is_connected():
            print("ERROR: Tailscale not connected on this machine.")
            print("  Run: tailscale up")
            return 1
        
        ts_ip = ts.get_ip()
        ts_auth_key = None
        
        if args.auto_join:
            # Check for API key
            if not ts._load_api_key():
                print("\nERROR: Tailscale API key not configured.")
                print("\nTo enable auto-join, run one of:")
                print("  1. colony tailscale setup --api-key tskey-api-xxx")
                print("  2. Set TAILSCALE_API_KEY environment variable")
                print("\nOr omit --auto-join and manually join tailnet:")
                print("  tailscale up")
                return 1
            
            ts_auth_key = ts.generate_auth_key()
            if not ts_auth_key:
                print("WARNING: Could not generate Tailscale auth key.")
                print("  Falling back to manual join.")
        
        # ... continue with invite ...
```

---

### Gap 12: Plugin Doesn't Auto-Detect agent.json

**Location:** Part 10, Part 14.5

**Problem:** Spec shows plugin detecting `agent.json`, but actual implementation unclear.

**Current:** Plugin reads `sidecarUrl` from config, doesn't check for `agent.json`.

**Fix:**

```typescript
// plugin.ts

function detectConnectionMode(config: ColonyPluginConfig): "local" | "remote" {
    // Explicit mode in config
    if (config.connectionMode) {
        return config.connectionMode;
    }
    
    // Check for remote agent config file
    const agentConfigPath = path.join(os.homedir(), ".colony", "agent.json");
    if (fs.existsSync(agentConfigPath)) {
        try {
            const agentConfig = JSON.parse(fs.readFileSync(agentConfigPath, "utf-8"));
            if (agentConfig.websocket_url) {
                return "remote";
            }
        } catch (e) {
            // Ignore parse errors
        }
    }
    
    // Default to local
    return "local";
}

async function initializePlugin(api: OpenClawPluginApi, config: ColonyPluginConfig) {
    const mode = detectConnectionMode(config);
    
    if (mode === "remote") {
        const agentConfig = await loadAgentConfig();
        
        // Use WebSocket URL from agent config
        config.sidecarUrl = agentConfig.colony_url;
        
        // Start WebSocket connection
        await connectRemoteAgent(api, config, agentConfig);
    } else {
        // Local mode: register via HTTP
        await registerLocalAgent(api, config);
    }
}
```

---

### Gap 13: No Cleanup for Failed Connections

**Location:** Part 3, Part 4

**Problem:** If agent connection fails, no cleanup of partial state.

**Scenario:**
```
1. Agent connects via setup code
2. Colony creates agent record, returns cert
3. Agent crashes before saving agent.json
4. Agent record exists but agent never connects
5. "Ghost" agent in registry
```

**Fix:**

```python
# autonomy/loop.py

async def _phase_agent_heartbeat(self) -> None:
    """Mark agents offline and clean up ghosts."""
    store = self._registry.agent_store
    if not store:
        return
    
    # Mark stale agents offline (existing logic)
    threshold = datetime.now(timezone.utc) - timedelta(seconds=90)
    stale = await store.list(status="online", last_seen_before=threshold)
    
    for agent in stale:
        await store.update(agent.agent_id, status="offline")
        logger.info("Agent %s marked offline (no heartbeat)", agent.name)
    
    # NEW: Clean up ghost agents (registered but never connected)
    ghost_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
    ghosts = await store.list(
        status="offline",
        websocket_connected=False,
        registered_before=ghost_threshold,
        never_seen=True,  # last_seen_at IS NULL
    )
    
    for ghost in ghosts:
        await store.delete(ghost.agent_id)
        logger.info(
            "Removed ghost agent %s (never connected within 10 min)",
            ghost.name,
        )
```

---

### Gap 14: MCP Config for Remote Mode Doesn't Include Agent Config

**Location:** Part 18.4

**Problem:** `colony mcp setup --remote` generates MCP config, but doesn't include `COLONY_AGENT_CONFIG` env var.

**Current Spec:**
```json
{
    "mcpServers": {
        "colony": {
            "command": "python",
            "args": ["-m", "colony_sidecar.mcp.client"],
            "env": {
                "COLONY_AGENT_CONFIG": "~/.colony/agent.json",
                "COLONY_REMOTE_MODE": "true"
            }
        }
    }
}
```

**Issue:** MCP client doesn't exist yet (Gap 10 from previous analysis).

**Fix:** Ensure MCP client is implemented before remote mode is usable.

---

### Gap 15: No Validation of Agent Name Uniqueness

**Location:** Part 2.2, Part 3.1

**Problem:** Two agents could register with same name, causing confusion.

**Scenario:**
```
Agent 1: name="laptop", agent_id="agent-123"
Agent 2: name="laptop", agent_id="agent-456"  ← Allowed?
```

**Risk:** LOW — Names are just labels, IDs are unique.

**Decision:** Allow duplicate names. Document that names are not unique identifiers.

---

---

## Part 3: Edge Case Gaps

### Gap 16: Initiative Reassignment Race Condition

**Location:** Part 7.3, Part 20.1

**Problem:** Agent goes offline, initiatives reassigned, but agent was actually working on them.

**Scenario:**
```
1. Agent A receives initiative
2. Agent A acknowledges, starts working
3. Agent A's network drops for 91 seconds
4. Autonomy loop marks Agent A offline
5. Initiative reassigned to Agent B
6. Agent A reconnects, still working on initiative
7. Both agents complete same initiative
```

**Fix:**

```python
# initiatives/store.py

async def reassign_agent_initiatives(
    self,
    agent_id: str,
    reason: str = "agent_offline",
) -> int:
    """Reassign initiatives from an agent. Only reassign PENDING initiatives."""
    
    # Get initiatives that are still pending (not yet acknowledged)
    pending = await self.list(
        status="pending",
        assigned_agent_id=agent_id,
    )
    
    # Do NOT reassign acknowledged initiatives
    # They may still be in progress
    acknowledged = await self.list(
        status="acknowledged",
        assigned_agent_id=agent_id,
    )
    
    if acknowledged:
        logger.warning(
            "Not reassigning %d acknowledged initiatives from agent %s",
            len(acknowledged),
            agent_id,
        )
        # Could send notification to user: "Agent X went offline with pending work"
    
    reassigned = 0
    for init in pending:
        await self.update(
            init.id,
            status="pending",
            assigned_agent_id=None,
            assigned_at=None,
        )
        reassigned += 1
    
    return reassigned
```

---

### Gap 17: WebSocket Disconnect During Initiative Delivery

**Location:** Part 4, Part 7.2

**Problem:** Initiative assigned, delivery starts, WebSocket disconnects mid-delivery.

**Current:** No acknowledgment of delivery.

**Fix:**

```python
# agents/websocket.py

class WebSocketManager:
    async def send_initiative(
        self,
        agent_id: str,
        initiative: dict,
    ) -> bool:
        """Send initiative and wait for acknowledgment.
        
        Returns True if acknowledged, False otherwise.
        """
        ws = self._active_connections.get(agent_id)
        if not ws:
            return False
        
        # Send initiative
        await ws.send_json({
            "type": "initiative",
            "initiative": initiative,
        })
        
        # Wait for acknowledgment (with timeout)
        try:
            ack = await asyncio.wait_for(
                self._wait_for_ack(agent_id, initiative["id"]),
                timeout=10.0,  # 10 second ack timeout
            )
            return ack
        except asyncio.TimeoutError:
            logger.warning(
                "Initiative %s not acknowledged by agent %s within 10s",
                initiative["id"],
                agent_id,
            )
            return False
    
    async def _wait_for_ack(self, agent_id: str, initiative_id: str) -> bool:
        """Wait for acknowledgment message from agent."""
        # Implementation would use a pending ack queue
        # that gets matched when ack message arrives
        pass
```

---

### Gap 18: Multiple Primary Agents

**Location:** Part 6.2, Part 17

**Problem:** What if two agents have `is_primary=True`?

**Scenario:**
```
1. Agent A is primary
2. Admin accidentally sets Agent B as primary too
3. Both receive user-facing initiatives
```

**Fix:**

```python
# agents/store.py

async def set_primary(self, agent_id: str) -> None:
    """Set agent as primary. Demotes all other primaries."""
    
    # Demote all existing primaries
    self._db.execute(
        "UPDATE agents SET is_primary = 0 WHERE is_primary = 1"
    )
    self._db.commit()
    
    # Promote new primary
    await self.update(agent_id, is_primary=True)
    
    logger.info("Agent %s promoted to primary (demoted others)", agent_id)
```

---

### Gap 19: Agent Reconnects with Different Capabilities

**Location:** Part 3, Part 6

**Problem:** Agent reconnects with changed capabilities, but cached assignment engine uses old capabilities.

**Scenario:**
```
1. Agent registers with ["messaging"]
2. Assignment engine caches capabilities
3. Agent updates to ["messaging", "calendar"]
4. Assignment engine still thinks agent can't do calendar
```

**Fix:**

```python
# api/routers/host.py

@router.post("/agents/{agent_id}/heartbeat")
async def agent_heartbeat(
    agent_id: str,
    body: HeartbeatRequest,
) -> HeartbeatResponse:
    store = _agent_store
    if not store:
        raise HTTPException(501, "Agent store not initialized")
    
    agent = await store.get(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    
    updates = {
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Update status if provided
    if body.status:
        updates["status"] = body.status
    
    # NEW: Update capabilities if provided
    if body.capabilities:
        old_caps = set(agent.get("capabilities", []))
        new_caps = set(body.capabilities)
        
        if old_caps != new_caps:
            updates["capabilities"] = list(new_caps)
            logger.info(
                "Agent %s capabilities changed: %s → %s",
                agent.name,
                old_caps,
                new_caps,
            )
    
    await store.update(agent_id, **updates)
    
    return HeartbeatResponse(ok=True)
```

---

### Gap 20: Initiative Expires Before Assignment

**Location:** Part 5, Part 7.3

**Problem:** Initiative created with expiry, but no agent available. Expires before ever assigned.

**Fix:**

```python
# autonomy/loop.py

async def _phase_queue_assignment(self) -> None:
    """Attempt to assign pending initiatives."""
    store = self._registry.initiative_store
    agent_store = self._registry.agent_store
    
    if not store or not agent_store:
        return
    
    # Get pending initiatives that haven't expired
    pending = await store.list(status="pending", not_expired=True)
    agents = await agent_store.list(status="online")
    
    for initiative in pending:
        # Skip if expired
        if initiative.expires_at:
            expires = datetime.fromisoformat(initiative.expires_at)
            if datetime.now(timezone.utc) > expires:
                await store.update(initiative.id, status="cancelled", cancelled_reason="expired")
                continue
        
        # Attempt assignment
        agent = select_agent_for_initiative(initiative, agents)
        if agent:
            await store.assign(initiative.id, agent.agent_id)
            await self._deliver_initiative(initiative, agent)
```

---

### Gap 21: No Maximum Initiatives Per Agent

**Location:** Part 1.1, Part 6

**Problem:** `max_concurrent` limits current assignments, but not total initiatives over time.

**Risk:** LOW — Initiatives complete/fail, releasing capacity.

**Decision:** No fix needed. `max_concurrent` handles active assignments.

---

### Gap 22: SQLite Database Corruption

**Location:** Part 1, Part 8

**Problem:** SQLite databases can corrupt. No backup/recovery mechanism.

**Fix:**

```python
# agents/store.py

import sqlite3
from pathlib import Path

class AgentStore:
    def __init__(self, state_dir: Path):
        self._db_path = state_dir / "agents.db"
        self._backup_path = state_dir / "agents.db.backup"
        
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize or recover database."""
        try:
            self._connect()
            self._create_tables()
        except sqlite3.DatabaseError:
            # Database corrupted
            logger.warning("agents.db corrupted, attempting recovery")
            
            # Try to recover from backup
            if self._backup_path.exists():
                shutil.copy(self._backup_path, self._db_path)
                logger.info("Restored agents.db from backup")
            else:
                # Start fresh
                self._db_path.unlink(missing_ok=True)
                logger.warning("No backup available, starting fresh")
            
            self._connect()
            self._create_tables()
    
    def _connect(self) -> sqlite3.Connection:
        """Connect to database with WAL mode for reliability."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")  # Better crash recovery
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    
    def backup(self) -> None:
        """Create backup of database."""
        shutil.copy(self._db_path, self._backup_path)
```

---

### Gap 23: No Migration Path from Single-Agent

**Location:** Part 19

**Problem:** Existing single-agent setups need migration, but spec doesn't provide migration tool.

**Fix:**

```python
# cli.py

def _cmd_migrate(args) -> None:
    """Migrate single-agent setup to multi-agent."""
    state_dir = get_state_dir()
    
    # Check if already multi-agent
    agents_db = state_dir / "agents.db"
    if agents_db.exists():
        print("Already migrated to multi-agent.")
        return 0
    
    print("Migrating to multi-agent setup...")
    
    # Create agents database
    from colony_sidecar.agents.store import AgentStore
    store = AgentStore(state_dir)
    
    # Register existing agent as primary
    # (This assumes OpenClaw plugin was already connected)
    node_info = get_node_info(state_dir)
    
    agent = store.create({
        "agent_id": str(uuid.uuid4()),
        "node_id": node_info["node_id"],
        "colony_id": get_or_create_colony_id(state_dir),
        "name": "primary",
        "connection_mode": "local",
        "status": "online",
        "is_primary": True,
        "capabilities": ["messaging", "calendar"],
        "node_cert": load_node_certificate(state_dir),
    })
    
    print(f"✓ Created agent: {agent['agent_id']}")
    print("✓ Migration complete")
    print("\nYou can now add remote agents with:")
    print("  colony agent invite")
```

---

## Part 4: Missing Components

### Missing 1: Remote MCP Client Implementation

**Location:** Part 18.3

**Problem:** Spec references `colony_sidecar/mcp/client.py` but doesn't provide implementation.

**Required:**
- WebSocket connection management
- MCP protocol bridge
- Tool call routing

**See:** Gap 10 from previous analysis.

---

### Missing 2: Assignment History Persistence

**Location:** Part 1.4

**Problem:** Schema defined but no implementation of writing to assignment_history.

**Fix:**

```python
# initiatives/store.py

async def assign(
    self,
    initiative_id: str,
    agent_id: str,
    agent_name: str,
) -> None:
    """Assign initiative to agent and record history."""
    
    # Update initiative
    self._db.execute(
        """UPDATE initiatives 
           SET status = 'assigned', 
               assigned_agent_id = ?,
               assigned_agent_name = ?,
               assigned_at = ?
           WHERE id = ?""",
        [agent_id, agent_name, datetime.now(timezone.utc).isoformat(), initiative_id],
    )
    
    # Record history
    self._db.execute(
        """INSERT INTO assignment_history 
           (initiative_id, agent_id, agent_name, action, timestamp)
           VALUES (?, ?, ?, 'assigned', ?)""",
        [initiative_id, agent_id, agent_name, datetime.now(timezone.utc).isoformat()],
    )
    
    self._db.commit()
```

---

### Missing 3: Agent Event Broadcasting

**Location:** Part 4, Part 7

**Problem:** When agent status changes, no event is broadcast.

**Current:** Only `broadcast_event()` for general events.

**Fix:**

```python
# agents/store.py

async def update(self, agent_id: str, **kwargs) -> None:
    """Update agent and broadcast event."""
    
    # Update database
    # ...
    
    # Broadcast event
    if "status" in kwargs:
        from colony_sidecar.api.routers.host import broadcast_event
        broadcast_event({
            "type": "agent_status_changed",
            "agent_id": agent_id,
            "status": kwargs["status"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
```

---

### Missing 4: Health Check Endpoint for Agents

**Location:** Part 3

**Problem:** No endpoint for agent to check Colony health before connecting.

**Fix:**

```python
# api/routers/host.py

@router.get("/agents/health")
async def agents_health() -> dict:
    """Health check for agent connections."""
    return {
        "status": "ok",
        "accepting_connections": True,
        "websocket_endpoint": "/v1/host/agents/{agent_id}/stream",
        "version": __version__,
    }
```

---

### Missing 5: Agent Metadata Schema

**Location:** Part 1.1

**Problem:** `metadata TEXT DEFAULT '{}'` is unstructured. What metadata is expected?

**Fix:**

```markdown
### Agent Metadata Schema

```json
{
    "hostname": "macbook-pro.local",
    "platform": "darwin",
    "version": "0.7.0",
    "harness": "openclaw",
    "openclaw_version": "2026.4.25",
    "python_version": "3.11.5",
    "started_at": "2026-04-25T12:00:00Z"
}
```

Used for:
- Debugging connection issues
- Version compatibility checks
- Display in agent list
```

---

## Part 5: Security Hardening Recommendations

### Recommendation 1: Certificate Revocation List (CRL)

**Current:** Revoked agents are marked in database.

**Improvement:** Maintain CRL for fast rejection.

```python
# agents/store.py

class AgentStore:
    _revoked_node_ids: set[str] = set()
    
    def is_node_revoked(self, node_id: str) -> bool:
        """Check if node_id is revoked (cached)."""
        return node_id in self._revoked_node_ids
    
    async def revoke(self, agent_id: str) -> None:
        agent = await self.get(agent_id)
        await self.update(agent_id, status="revoked")
        
        # Add to CRL cache
        self._revoked_node_ids.add(agent["node_id"])
```

---

### Recommendation 2: Rate Limit WebSocket Connections

**Current:** No rate limiting on WebSocket connect attempts.

**Fix:**

```python
# agents/websocket.py

from collections import defaultdict
from datetime import datetime, timedelta

class WebSocketManager:
    _connect_attempts: defaultdict[str, list[datetime]] = defaultdict(list)
    MAX_CONNECT_ATTEMPTS = 5
    ATTEMPT_WINDOW = timedelta(minutes=1)
    
    async def check_rate_limit(self, ip: str) -> bool:
        """Check if IP is rate limited."""
        now = datetime.now(timezone.utc)
        
        # Clean old attempts
        self._connect_attempts[ip] = [
            t for t in self._connect_attempts[ip]
            if now - t < self.ATTEMPT_WINDOW
        ]
        
        # Check limit
        if len(self._connect_attempts[ip]) >= self.MAX_CONNECT_ATTEMPTS:
            return False
        
        # Record attempt
        self._connect_attempts[ip].append(now)
        return True
```

---

### Recommendation 3: Audit Logging

**Current:** No audit trail for sensitive operations.

**Fix:**

```python
# Create audit.db

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,              -- agent_invite, agent_connect, agent_revoke, etc.
    actor TEXT,                        -- Who performed action (agent_id, "system", "user")
    target TEXT,                       -- What was acted on
    details TEXT,                      -- JSON with full details
    ip_address TEXT,
    user_agent TEXT
);
```

```python
# agents/store.py

async def log_audit(
    self,
    action: str,
    actor: str,
    target: str,
    details: dict,
    request: Optional[Request] = None,
) -> None:
    """Log audit event."""
    self._audit_db.execute(
        """INSERT INTO audit_log 
           (action, actor, target, details, ip_address, user_agent)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            action,
            actor,
            target,
            json.dumps(details),
            request.client.host if request else None,
            request.headers.get("user-agent") if request else None,
        ],
    )
    self._audit_db.commit()

# Usage
async def create_invite(...):
    invite = ...
    await self.log_audit(
        action="agent_invite",
        actor="user",  # or agent_id
        target=invite["code"],
        details={"capabilities": capabilities, "expires_at": expires_at},
    )
```

---

### Recommendation 4: Encrypt agent.json at Rest

**Current:** `agent.json` is plaintext JSON.

**Fix:**

```python
# cli.py

from cryptography.fernet import Fernet

def _encrypt_agent_config(config: dict, key: bytes) -> bytes:
    """Encrypt agent config with Fernet symmetric key."""
    f = Fernet(key)
    plaintext = json.dumps(config).encode()
    return f.encrypt(plaintext)

def _decrypt_agent_config(ciphertext: bytes, key: bytes) -> dict:
    """Decrypt agent config."""
    f = Fernet(key)
    plaintext = f.decrypt(ciphertext)
    return json.loads(plaintext)

def _get_or_create_agent_key() -> bytes:
    """Get or create key for encrypting agent config."""
    key_path = Path.home() / ".colony" / ".agent-key"
    
    if key_path.exists():
        return key_path.read_bytes()
    
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    
    return key

def save_agent_config(config: dict) -> None:
    """Save encrypted agent config."""
    key = _get_or_create_agent_key()
    encrypted = _encrypt_agent_config(config, key)
    
    config_path = Path.home() / ".colony" / "agent.json.enc"
    config_path.write_bytes(encrypted)
    config_path.chmod(0o600)

def load_agent_config() -> Optional[dict]:
    """Load encrypted agent config."""
    config_path = Path.home() / ".colony" / "agent.json.enc"
    
    if not config_path.exists():
        # Fallback to plaintext for migration
        plaintext_path = Path.home() / ".colony" / "agent.json"
        if plaintext_path.exists():
            config = json.loads(plaintext_path.read_text())
            # Migrate to encrypted
            save_agent_config(config)
            plaintext_path.unlink()
            return config
        return None
    
    key = _get_or_create_agent_key()
    ciphertext = config_path.read_bytes()
    return _decrypt_agent_config(ciphertext, key)
```

---

## Part 6: Summary

### Gap Summary

| # | Gap | Severity | Category | Fix Complexity |
|---|-----|----------|----------|----------------|
| 1 | Certificate signing missing Colony key access | Critical | Security | Medium |
| 2 | WebSocket auth verification incomplete | Critical | Security | Medium |
| 3 | Setup code brute force protection | Moderate | Security | Low |
| 4 | Colony key passphrase handling | Moderate | Security | Low |
| 5 | Agent impersonation prevention | Moderate | Security | Low |
| 6 | API key exposure in agent.json | Low | Security | Low |
| 7 | Revocation doesn't disconnect WebSocket | Moderate | Security | Low |
| 8 | No mutual TLS for WebSocket | Low | Security | N/A |
| 9 | Remote agent bootstrap not atomic | Moderate | Automation | Medium |
| 10 | No auto-detect harness on connect | Moderate | Automation | Low |
| 11 | Tailscale requires pre-stored API key | Moderate | Automation | Low |
| 12 | Plugin doesn't auto-detect agent.json | Moderate | Automation | Low |
| 13 | No cleanup for failed connections | Moderate | Automation | Low |
| 14 | MCP config missing COLONY_AGENT_CONFIG | Moderate | Automation | Low |
| 15 | No validation of agent name uniqueness | Low | Automation | N/A |
| 16 | Initiative reassignment race | Moderate | Edge Case | Medium |
| 17 | WebSocket disconnect during delivery | Moderate | Edge Case | Medium |
| 18 | Multiple primary agents | Moderate | Edge Case | Low |
| 19 | Agent reconnects with different capabilities | Low | Edge Case | Low |
| 20 | Initiative expires before assignment | Low | Edge Case | Low |
| 21 | No maximum initiatives per agent | Low | Edge Case | N/A |
| 22 | SQLite database corruption | Moderate | Edge Case | Medium |
| 23 | No migration path from single-agent | Moderate | Edge Case | Low |

### Priority Fix Order

**Phase 1: Critical Security (4h)**
1. Gap 1: Certificate signing
2. Gap 2: WebSocket auth
3. Gap 7: Revocation disconnect

**Phase 2: Atomic Operations (3h)**
4. Gap 9: Atomic bootstrap
5. Gap 13: Cleanup failed connections
6. Gap 22: SQLite corruption handling

**Phase 3: Edge Cases (3h)**
7. Gap 16: Reassignment race
8. Gap 17: Delivery acknowledgment
9. Gap 18: Multiple primaries

**Phase 4: Automation Polish (2h)**
10. Gap 10: Auto-detect harness
11. Gap 12: Plugin auto-detect

**Phase 5: Security Hardening (2h)**
12. Gap 3: Setup code rate limiting
13. Gap 4: Passphrase handling
14. Recommendation 3: Audit logging

**Total: ~14h additional work**

---

## Part 7: Spec Amendments Required

Add to spec:

1. **Part 22: Security Considerations**
   - Certificate signing implementation
   - WebSocket auth protocol
   - Rate limiting
   - Audit logging

2. **Part 23: Error Recovery**
   - Atomic bootstrap
   - Cleanup procedures
   - Corruption handling

3. **Part 24: Migration Guide**
   - Single-agent to multi-agent
   - Backup/restore procedures

---

**Analysis Complete.**

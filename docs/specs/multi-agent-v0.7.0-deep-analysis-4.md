# Multi-Agent Colony v0.7.0 — Deep Analysis #4

> **Analysis Date:** 2026-04-25 (Fourth Pass)
> **Analyst:** DevAgent
> **Goal:** Find ANYTHING missed in first three analyses

---

## Executive Summary

After thorough review of the spec and existing codebase, **21 additional gaps** were found:

| Category | Critical | Moderate | Minor |
|----------|----------|----------|-------|
| CLI Commands | 0 | 4 | 3 |
| API Endpoints | 1 | 2 | 2 |
| Database | 1 | 2 | 1 |
| Operations | 0 | 3 | 2 |

**Overall Risk Assessment:** LOW-MEDIUM

---

## Part 1: Missing CLI Commands

### Gap 1: No `agent rename` Command

**Problem:** Agent names are set at registration but can't be changed later.

**Solution:**

```python
# cli.py

def _cmd_agent_rename(args) -> int:
    """Rename an agent."""
    store = _get_agent_store()
    
    agent = store.get(args.agent_id)
    if not agent:
        print(f"ERROR: Agent {args.agent_id} not found")
        return 1
    
    old_name = agent["name"]
    store.update(args.agent_id, name=args.name)
    
    print(f"✓ Renamed agent: {old_name} → {args.name}")
    return 0

# CLI
sub = parser.add_parser("agent", help="Agent management")
sub.add_argument("agent_action", choices=[..., "rename", ...])
sub.add_argument("--name", help="New name for agent")
```

---

### Gap 2: No `agent update` Command

**Problem:** Can't update agent capabilities, priority, or settings after registration.

**Solution:**

```python
# cli.py

def _cmd_agent_update(args) -> int:
    """Update agent settings."""
    store = _get_agent_store()
    
    agent = store.get(args.agent_id)
    if not agent:
        print(f"ERROR: Agent {args.agent_id} not found")
        return 1
    
    updates = {}
    
    if args.capabilities:
        updates["capabilities"] = args.capabilities.split(",")
    
    if args.priority is not None:
        updates["priority"] = args.priority
    
    if args.max_concurrent is not None:
        updates["max_concurrent"] = args.max_concurrent
    
    if args.excluded_types:
        updates["excluded_types"] = args.excluded_types.split(",")
    
    if args.included_types:
        updates["included_types"] = args.included_types.split(",")
    
    if not updates:
        print("No updates specified")
        return 0
    
    store.update(args.agent_id, **updates)
    
    print(f"✓ Updated agent {args.agent_id}:")
    for key, value in updates.items():
        print(f"  {key}: {value}")
    
    return 0

# CLI
colony agent update <agent_id> --capabilities messaging,calendar
colony agent update <agent_id> --priority 2
colony agent update <agent_id> --max-concurrent 10
colony agent update <agent_id> --excluded-types health
```

---

### Gap 3: No `initiative cancel` Command

**Problem:** Can cancel initiatives via API but no CLI command.

**Solution:**

```python
# cli.py

def _cmd_initiative_cancel(args) -> int:
    """Cancel an initiative."""
    store = _get_initiative_store()
    
    initiative = store.get(args.initiative_id)
    if not initiative:
        print(f"ERROR: Initiative {args.initiative_id} not found")
        return 1
    
    if initiative["status"] in ("completed", "cancelled", "failed"):
        print(f"ERROR: Cannot cancel initiative with status '{initiative['status']}'")
        return 1
    
    store.update(
        args.initiative_id,
        status="cancelled",
        cancelled_by=args.reason and "cli",
        cancelled_reason=args.reason or "Cancelled via CLI",
    )
    
    print(f"✓ Cancelled initiative {args.initiative_id}")
    return 0

# CLI
colony initiative cancel <initiative_id>
colony initiative cancel <initiative_id> --reason "No longer needed"
```

---

### Gap 4: No `initiative list` Command

**Problem:** No CLI command to list initiatives.

**Solution:**

```python
# cli.py

def _cmd_initiative_list(args) -> int:
    """List initiatives."""
    store = _get_initiative_store()
    
    filters = {}
    
    if args.status:
        filters["status"] = args.status.split(",")
    
    if args.type:
        filters["type"] = args.type
    
    if args.agent:
        filters["assigned_agent_id"] = args.agent
    
    initiatives = store.list(**filters, limit=args.limit)
    
    if not initiatives:
        print("No initiatives found")
        return 0
    
    # Table output
    print(f"{'ID':<12} {'Type':<12} {'Status':<12} {'Agent':<16} {'Description'}")
    print("-" * 80)
    
    for init in initiatives:
        agent = init.get("assigned_agent_name", "-") or "-"
        desc = init["description"][:40] + "..." if len(init["description"]) > 40 else init["description"]
        print(f"{init['id'][:12]:<12} {init['type']:<12} {init['status']:<12} {agent:<16} {desc}")
    
    return 0

# CLI
colony initiative list
colony initiative list --status pending,assigned
colony initiative list --type follow_up
colony initiative list --agent spark1
colony initiative list --limit 50
```

---

### Gap 5: No `initiative show` Command

**Problem:** No CLI command to show initiative details.

**Solution:**

```python
# cli.py

def _cmd_initiative_show(args) -> int:
    """Show initiative details."""
    store = _get_initiative_store()
    
    initiative = store.get(args.initiative_id)
    if not initiative:
        print(f"ERROR: Initiative {args.initiative_id} not found")
        return 1
    
    # Pretty print
    print(f"Initiative: {initiative['id']}")
    print(f"  Type: {initiative['type']}")
    print(f"  Status: {initiative['status']}")
    print(f"  Priority: {initiative['priority']:.2f}")
    print(f"  Description: {initiative['description']}")
    print(f"  Rationale: {initiative.get('rationale', '-')}")
    print(f"  Action Hint: {initiative.get('action_hint', '-')}")
    print(f"  Entity: {initiative.get('entity_id', '-')}")
    print(f"  Dedup Key: {initiative.get('dedup_key', '-')}")
    print(f"  Created: {initiative['created_at']}")
    print(f"  Expires: {initiative.get('expires_at', '-')}")
    
    if initiative.get("assigned_agent_id"):
        print(f"  Assigned To: {initiative.get('assigned_agent_name', initiative['assigned_agent_id'])}")
        print(f"  Assigned At: {initiative.get('assigned_at', '-')}")
    
    if initiative.get("attempt_count", 0) > 0:
        print(f"  Attempts: {initiative['attempt_count']}/{initiative.get('max_attempts', 3)}")
        print(f"  Last Attempt: {initiative.get('last_attempt_at', '-')}")
    
    # Show history if requested
    if args.history:
        history = store.get_history(initiative['id'])
        if history:
            print("\n  History:")
            for entry in history:
                ts = entry['timestamp'][:19]  # Strip microseconds
                print(f"    {ts} {entry['action']} by {entry.get('agent_name', entry['agent_id'])}")
    
    return 0

# CLI
colony initiative show <initiative_id>
colony initiative show <initiative_id> --history
```

---

### Gap 6: No `agent show` Command

**Problem:** No CLI command to show agent details.

**Solution:**

```python
# cli.py

def _cmd_agent_show(args) -> int:
    """Show agent details."""
    store = _get_agent_store()
    
    agent = store.get(args.agent_id)
    if not agent:
        print(f"ERROR: Agent {args.agent_id} not found")
        return 1
    
    # Pretty print
    print(f"Agent: {agent['name']} ({agent['agent_id']})")
    print(f"  Node ID: {agent['node_id']}")
    print(f"  Colony ID: {agent['colony_id']}")
    print(f"  Status: {agent['status']}")
    print(f"  Connection Mode: {agent['connection_mode']}")
    print(f"  Primary: {'Yes' if agent.get('is_primary') else 'No'}")
    print(f"  Priority: {agent.get('priority', 1)}")
    print(f"  Capabilities: {', '.join(agent.get('capabilities', []))}")
    print(f"  Max Concurrent: {agent.get('max_concurrent', 5)}")
    print(f"  Current Assignments: {agent.get('current_assignments', 0)}")
    print(f"  Max Initiatives/Hour: {agent.get('max_initiatives_per_hour', 10)}")
    
    if agent.get('excluded_types'):
        print(f"  Excluded Types: {', '.join(agent['excluded_types'])}")
    
    if agent.get('included_types'):
        print(f"  Included Types: {', '.join(agent['included_types'])}")
    
    print(f"  Registered: {agent['registered_at']}")
    print(f"  Last Seen: {agent.get('last_seen_at', '-')}")
    
    metadata = agent.get('metadata', {})
    if metadata:
        print(f"  Hostname: {metadata.get('hostname', '-')}")
        print(f"  Platform: {metadata.get('platform', '-')}")
        print(f"  Harness: {metadata.get('harness', '-')}")
    
    # Show current initiatives if requested
    if args.initiatives:
        init_store = _get_initiative_store()
        initiatives = init_store.list(assigned_agent_id=agent['agent_id'])
        if initiatives:
            print(f"\n  Current Initiatives ({len(initiatives)}):")
            for init in initiatives:
                print(f"    - {init['type']}: {init['description'][:50]}")
    
    return 0

# CLI
colony agent show <agent_id>
colony agent show <agent_id> --initiatives
```

---

### Gap 7: No `colony status` Command for Multi-Agent

**Problem:** No quick way to see overall system health.

**Solution:**

```python
# cli.py

def _cmd_status(args) -> int:
    """Show Colony status including multi-agent overview."""
    from colony_sidecar.autonomy.registry import SubsystemRegistry
    
    registry = SubsystemRegistry()
    
    print("Colony Status")
    print("=" * 40)
    
    # Colony info
    print(f"Version: {__version__}")
    print(f"Uptime: {get_uptime()}")
    print(f"Mode: {'multi-agent' if registry.agent_store else 'single-agent'}")
    
    # Agent stats
    if registry.agent_store:
        agents = registry.agent_store.list()
        online = [a for a in agents if a["status"] == "online"]
        
        print(f"\nAgents: {len(online)}/{len(agents)} online")
        
        for agent in online:
            assignments = agent.get("current_assignments", 0)
            max_concurrent = agent.get("max_concurrent", 5)
            primary = " (primary)" if agent.get("is_primary") else ""
            print(f"  - {agent['name']}: {assignments}/{max_concurrent} assignments{primary}")
    
    # Initiative stats
    if registry.initiative_store:
        pending = registry.initiative_store.count(status="pending")
        assigned = registry.initiative_store.count(status=["assigned", "acknowledged"])
        
        print(f"\nInitiatives:")
        print(f"  Pending: {pending}")
        print(f"  In Progress: {assigned}")
    
    # Autonomy stats
    if hasattr(registry, "autonomy_loop") and registry.autonomy_loop:
        stats = registry.autonomy_loop.stats
        print(f"\nAutonomy:")
        print(f"  Ticks: {stats.ticks}")
        print(f"  Initiatives Generated: {stats.initiatives_generated}")
        print(f"  Actions Executed: {stats.actions_executed}")
        print(f"  Errors: {stats.errors}")
    
    return 0

# CLI
colony status
```

---

## Part 2: Missing API Endpoints

### Gap 8: No `PATCH /agents/{agent_id}` Endpoint

**Problem:** Can't update agent settings via API.

**Solution:**

```python
# api/routers/host.py

@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
) -> AgentResponse:
    """Update agent settings."""
    agent = await _agent_store.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(
                error="not_found",
                message="Agent not found",
            ).dict(),
        )
    
    # Build updates
    updates = {}
    
    if body.name is not None:
        updates["name"] = body.name
    
    if body.capabilities is not None:
        updates["capabilities"] = body.capabilities
    
    if body.priority is not None:
        updates["priority"] = body.priority
    
    if body.max_concurrent is not None:
        updates["max_concurrent"] = body.max_concurrent
    
    if body.excluded_types is not None:
        updates["excluded_types"] = body.excluded_types
    
    if body.included_types is not None:
        updates["included_types"] = body.included_types
    
    if not updates:
        return agent
    
    updated = await _agent_store.update(agent_id, **updates)
    return updated

# Schema
class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    capabilities: Optional[List[str]] = None
    priority: Optional[int] = None
    max_concurrent: Optional[int] = None
    excluded_types: Optional[List[str]] = None
    included_types: Optional[List[str]] = None
```

---

### Gap 9: No `POST /initiatives/{id}/delegate` Endpoint

**Problem:** Agent can delegate but no explicit endpoint.

**Solution:**

```python
# api/routers/host.py

@router.post("/initiatives/{initiative_id}/delegate")
async def delegate_initiative(
    initiative_id: str,
    body: DelegateRequest,
) -> InitiativeResponse:
    """Delegate initiative to another agent or back to queue."""
    store = _registry.initiative_store
    
    initiative = await store.get(initiative_id)
    if not initiative:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(
                error="not_found",
                message="Initiative not found",
            ).dict(),
        )
    
    # Can only delegate assigned/acknowledged initiatives
    if initiative["status"] not in ("assigned", "acknowledged"):
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error="invalid_request",
                message="Can only delegate assigned or acknowledged initiatives",
            ).dict(),
        )
    
    # Clear assignment
    await store.update(
        initiative_id,
        status="pending",
        assigned_agent_id=None,
        assigned_agent_name=None,
        assigned_at=None,
    )
    
    # Log to history
    await store.log_history(
        initiative_id,
        action="delegated",
        agent_id=body.from_agent_id,
        details={"reason": body.reason, "to_agent": body.to_agent},
    )
    
    # If specific agent requested, assign directly
    if body.to_agent and body.to_agent != "any":
        agent = await _agent_store.get(body.to_agent)
        if agent and agent["status"] == "online":
            await store.assign(initiative_id, body.to_agent)
    
    return await store.get(initiative_id)

# Schema
class DelegateRequest(BaseModel):
    from_agent_id: str
    reason: str
    to_agent: str = "any"  # or specific agent_id
```

---

### Gap 10: No `POST /initiatives/{id}/retry` Endpoint

**Problem:** Failed initiatives need manual intervention to retry.

**Solution:**

```python
# api/routers/host.py

@router.post("/initiatives/{initiative_id}/retry")
async def retry_initiative(
    initiative_id: str,
) -> InitiativeResponse:
    """Retry a failed initiative."""
    store = _registry.initiative_store
    
    initiative = await store.get(initiative_id)
    if not initiative:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(
                error="not_found",
                message="Initiative not found",
            ).dict(),
        )
    
    # Can only retry failed initiatives
    if initiative["status"] != "failed":
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error="invalid_request",
                message="Can only retry failed initiatives",
            ).dict(),
        )
    
    # Reset to pending
    await store.update(
        initiative_id,
        status="pending",
        attempt_count=0,
        failed_at=None,
        failed_reason=None,
    )
    
    # Remove from dead letter queue
    await store.remove_from_dlq(initiative_id)
    
    return await store.get(initiative_id)
```

---

### Gap 11: No Agent History Endpoint

**Problem:** No way to see agent's initiative history via API.

**Solution:**

```python
# api/routers/host.py

@router.get("/agents/{agent_id}/history")
async def get_agent_history(
    agent_id: str,
    limit: int = 50,
    offset: int = 0,
) -> AgentHistoryResponse:
    """Get agent's initiative history."""
    agent = await _agent_store.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(
                error="not_found",
                message="Agent not found",
            ).dict(),
        )
    
    # Get from assignment_history table
    history = await _initiative_store.get_agent_history(
        agent_id,
        limit=limit,
        offset=offset,
    )
    
    return {
        "agent_id": agent_id,
        "total": len(history),
        "history": history,
    }
```

---

## Part 3: Database Gaps

### Gap 12: No Index on `initiatives.assigned_agent_id`

**Problem:** Frequent queries by `assigned_agent_id` but no index defined.

**Current Schema (Part 1.3):**
```sql
CREATE INDEX idx_initiatives_status ON initiatives(status);
CREATE INDEX idx_initiatives_assigned ON initiatives(assigned_agent_id);
```

**Actually, there IS an index.** Let me check for other missing indexes...

**Verified:** Index exists. ✅

---

### Gap 13: No Vacuum/Pruning for Old Initiatives

**Problem:** Initiatives accumulate indefinitely. No cleanup.

**Solution:**

```python
# autonomy/loop.py

async def _phase_initiative_cleanup(self) -> None:
    """Clean up old completed/cancelled/failed initiatives."""
    store = self._registry.initiative_store
    if not store:
        return
    
    # Delete initiatives older than retention period
    retention_days = self.config.initiative_retention_days  # Default: 30
    
    threshold = datetime.now(timezone.utc) - timedelta(days=retention_days)
    
    deleted = await store.delete_old(
        status=["completed", "cancelled", "failed"],
        before=threshold,
    )
    
    if deleted > 0:
        logger.info("Cleaned up %d old initiatives", deleted)

# Add to _tick():
# Phase 24: Initiative cleanup (daily)
if self._tick_count % 1440 == 0:  # Once per day (1440 ticks at 60s interval)
    await self._phase_initiative_cleanup()
```

**CLI:**

```python
# cli.py

def _cmd_initiative_prune(args) -> int:
    """Prune old initiatives."""
    store = _get_initiative_store()
    
    days = args.days or 30
    threshold = datetime.now(timezone.utc) - timedelta(days=days)
    
    if args.dry_run:
        count = store.count(
            status=["completed", "cancelled", "failed"],
            before=threshold,
        )
        print(f"Would delete {count} initiatives older than {days} days")
        return 0
    
    deleted = store.delete_old(
        status=["completed", "cancelled", "failed"],
        before=threshold,
    )
    
    print(f"✓ Deleted {deleted} initiatives older than {days} days")
    return 0

# CLI
colony initiative prune
colony initiative prune --days 7
colony initiative prune --dry-run
```

---

### Gap 14: No Database Migration System

**Problem:** Schema changes require manual migration.

**Solution:**

```python
# db/migrations.py

MIGRATIONS = [
    # v0.6.0 -> v0.7.0
    {
        "version": "0.7.0",
        "up": [
            """CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                ...
            )""",
            """CREATE TABLE IF NOT EXISTS agent_invites (...)""",
            """CREATE TABLE IF NOT EXISTS initiatives (...)""",
            """CREATE TABLE IF NOT EXISTS assignment_history (...)""",
        ],
        "down": [
            "DROP TABLE IF EXISTS assignment_history",
            "DROP TABLE IF EXISTS initiatives",
            "DROP TABLE IF EXISTS agent_invites",
            "DROP TABLE IF EXISTS agents",
        ],
    },
]

def run_migrations(db_path: Path, target_version: str) -> None:
    """Run database migrations."""
    conn = sqlite3.connect(db_path)
    
    # Get current version
    cursor = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    )
    row = cursor.fetchone()
    current = row[0] if row else "0.0.0"
    
    # Run migrations
    for migration in MIGRATIONS:
        if version_gt(migration["version"], current):
            logger.info("Running migration: %s", migration["version"])
            
            for sql in migration["up"]:
                conn.execute(sql)
            
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
                [migration["version"]],
            )
            conn.commit()
    
    conn.close()

# CLI
colony migrate
colony migrate --version 0.7.0
colony migrate --rollback  # Rollback last migration
```

---

## Part 4: Operational Gaps

### Gap 15: No Alert Configuration

**Problem:** No way to configure alerts for specific events.

**Solution:**

```python
# alerts/config.py

from pydantic import BaseModel
from typing import List, Optional
from enum import Enum

class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

class AlertRule(BaseModel):
    """Alert configuration rule."""
    
    name: str
    event: str  # agent_offline, initiative_failed, etc.
    severity: AlertSeverity = AlertSeverity.WARNING
    threshold: int = 1  # Trigger after N occurrences
    window_minutes: int = 60  # Within this time window
    channels: List[str] = ["system"]  # system, email, webhook
    enabled: bool = True
    
    # Optional filters
    agent_filter: Optional[str] = None
    type_filter: Optional[str] = None

# Configuration
ALERT_RULES = [
    AlertRule(
        name="agent_offline",
        event="agent_status_change",
        severity=AlertSeverity.WARNING,
        threshold=1,
        channels=["system"],
    ),
    AlertRule(
        name="initiative_failures",
        event="initiative_failed",
        severity=AlertSeverity.WARNING,
        threshold=3,
        window_minutes=60,
        channels=["system"],
    ),
    AlertRule(
        name="all_agents_offline",
        event="agents_offline",
        severity=AlertSeverity.CRITICAL,
        threshold=1,
        channels=["system", "webhook"],
    ),
]

# CLI
colony alert list
colony alert enable <rule_name>
colony alert disable <rule_name>
colony alert test <rule_name>
```

---

### Gap 16: No Webhook Configuration

**Problem:** No way to send alerts to external services.

**Solution:**

```python
# webhooks/config.py

from pydantic import BaseModel
from typing import Optional
import aiohttp

class WebhookConfig(BaseModel):
    """Webhook configuration."""
    
    name: str
    url: str
    method: str = "POST"
    headers: dict = {}
    secret: Optional[str] = None  # For HMAC signing
    
    # Filters
    events: list[str] = []  # Only these events (empty = all)
    severity_min: str = "warning"  # Minimum severity to send

class WebhookManager:
    def __init__(self, configs: list[WebhookConfig]):
        self._configs = configs
    
    async def send(self, event: str, payload: dict, severity: str) -> None:
        """Send webhook notification."""
        for config in self._configs:
            # Check filters
            if config.events and event not in config.events:
                continue
            
            if self._severity_lt(severity, config.severity_min):
                continue
            
            # Send
            try:
                async with aiohttp.ClientSession() as session:
                    headers = dict(config.headers)
                    
                    # Add HMAC signature if secret configured
                    if config.secret:
                        import hmac
                        import hashlib
                        body = json.dumps(payload)
                        sig = hmac.new(
                            config.secret.encode(),
                            body.encode(),
                            hashlib.sha256,
                        ).hexdigest()
                        headers["X-Signature"] = f"sha256={sig}"
                    
                    async with session.request(
                        config.method,
                        config.url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status >= 400:
                            logger.warning("Webhook %s failed: %d", config.name, resp.status)
            except Exception as e:
                logger.error("Webhook %s error: %s", config.name, e)

# Configuration file: ~/.colony/webhooks.json
[
    {
        "name": "slack-alerts",
        "url": "https://hooks.slack.com/services/XXX/YYY/ZZZ",
        "events": ["agent_offline", "initiative_failed"],
        "severity_min": "warning"
    }
]

# CLI
colony webhook list
colony webhook add --name slack --url https://hooks.slack.com/...
colony webhook test slack
colony webhook remove slack
```

---

### Gap 17: No Log Rotation Configuration

**Problem:** Logs grow indefinitely.

**Solution:**

```python
# logging_config.py

import logging
from logging.handlers import RotatingFileHandler

def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Set up rotating file logging."""
    
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Root logger
    root = logging.getLogger()
    root.setLevel(level)
    
    # Rotating file handler
    file_handler = RotatingFileHandler(
        log_dir / "colony.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setFormatter(JSONFormatter())
    root.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root.addHandler(console_handler)

# Environment variables
COLONY_LOG_LEVEL=INFO
COLONY_LOG_MAX_BYTES=10485760
COLONY_LOG_BACKUP_COUNT=5
```

---

### Gap 18: No Audit Log Export

**Problem:** Audit logs are in SQLite but can't be exported.

**Solution:**

```python
# cli.py

def _cmd_audit_export(args) -> int:
    """Export audit logs."""
    store = _get_agent_store()
    
    # Filters
    filters = {}
    
    if args.action:
        filters["action"] = args.action.split(",")
    
    if args.actor:
        filters["actor"] = args.actor
    
    if args.since:
        filters["since"] = datetime.fromisoformat(args.since)
    
    if args.until:
        filters["until"] = datetime.fromisoformat(args.until)
    
    # Get logs
    logs = store.get_audit_logs(**filters, limit=args.limit)
    
    # Export
    if args.format == "json":
        output = json.dumps(logs, indent=2, default=str)
    elif args.format == "csv":
        import csv
        from io import StringIO
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=["timestamp", "action", "actor", "target", "details", "ip_address"])
        writer.writeheader()
        for log in logs:
            writer.writerow(log)
        output = output.getvalue()
    else:
        output = "\n".join(f"{log['timestamp']} {log['action']} {log['actor']} {log['target']}" for log in logs)
    
    if args.output:
        Path(args.output).write_text(output)
        print(f"✓ Exported {len(logs)} audit logs to {args.output}")
    else:
        print(output)
    
    return 0

# CLI
colony audit export --format json --output audit.json
colony audit export --action agent_connect,agent_revoke
colony audit export --since 2026-04-01 --until 2026-04-25
```

---

### Gap 19: No Backup Verification

**Problem:** Backups are created but never verified.

**Solution:**

```python
# cli.py

def _cmd_backup_verify(args) -> int:
    """Verify a Colony backup."""
    import tarfile
    import tempfile
    
    backup_path = Path(args.backup)
    if not backup_path.exists():
        print(f"ERROR: Backup not found: {backup_path}")
        return 1
    
    print(f"Verifying backup: {backup_path}")
    
    # Check it's a valid tarball
    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            members = tar.getnames()
    except Exception as e:
        print(f"ERROR: Invalid backup file: {e}")
        return 1
    
    # Check required files
    required = ["colony-id", "agents.db", "initiatives.db"]
    missing = [f for f in required if f not in members]
    
    if missing:
        print(f"ERROR: Missing required files: {missing}")
        return 1
    
    # Verify databases
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(tmpdir)
        
        # Check agents.db
        try:
            conn = sqlite3.connect(Path(tmpdir) / "agents.db")
            conn.execute("SELECT COUNT(*) FROM agents")
            conn.close()
            print("  ✓ agents.db valid")
        except Exception as e:
            print(f"  ✗ agents.db corrupted: {e}")
            return 1
        
        # Check initiatives.db
        try:
            conn = sqlite3.connect(Path(tmpdir) / "initiatives.db")
            conn.execute("SELECT COUNT(*) FROM initiatives")
            conn.close()
            print("  ✓ initiatives.db valid")
        except Exception as e:
            print(f"  ✗ initiatives.db corrupted: {e}")
            return 1
    
    print("✓ Backup verified successfully")
    return 0

# CLI
colony backup verify colony-backup-2026-04-25.tar.gz
```

---

### Gap 20: No Restore Dry-Run

**Problem:** Restore overwrites data without preview.

**Solution:**

```python
# cli.py

def _cmd_restore(args) -> int:
    """Restore Colony from backup."""
    backup_path = Path(args.backup)
    state_dir = get_state_dir()
    
    # Dry run
    if args.dry_run:
        print("DRY RUN - No changes will be made")
        print(f"Would restore from: {backup_path}")
        print(f"Would restore to: {state_dir}")
        
        # Show what would be restored
        with tarfile.open(backup_path, "r:gz") as tar:
            for member in tar.getmembers():
                existing = state_dir / member.name
                status = "OVERWRITE" if existing.exists() else "CREATE"
                print(f"  {status}: {member.name}")
        
        return 0
    
    # Real restore
    # ... existing restore code ...
    
    return 0

# CLI
colony restore colony-backup.tar.gz --dry-run
colony restore colony-backup.tar.gz --force  # Skip confirmation
```

---

### Gap 21: No System Diagnostics Command

**Problem:** No way to diagnose issues with multi-agent setup.

**Solution:**

```python
# cli.py

def _cmd_doctor(args) -> int:
    """Run system diagnostics."""
    issues = []
    
    print("Colony Diagnostics")
    print("=" * 40)
    
    # Check Colony keys
    state_dir = get_state_dir()
    keys_dir = state_dir / "colony-keys"
    
    if not (keys_dir / "private.pem").exists():
        issues.append("Colony private key not found")
        print("✗ Colony keys: MISSING")
    else:
        print("✓ Colony keys: OK")
    
    # Check databases
    for db_name in ["agents.db", "initiatives.db", "audit.db"]:
        db_path = state_dir / db_name
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA integrity_check")
                conn.close()
                print(f"✓ {db_name}: OK")
            except Exception as e:
                issues.append(f"{db_name} corrupted: {e}")
                print(f"✗ {db_name}: CORRUPTED")
        else:
            print(f"⚠ {db_name}: NOT FOUND (will be created on first use)")
    
    # Check Neo4j
    try:
        from colony_sidecar.api.routers.host import _graph
        if _graph and hasattr(_graph, "driver"):
            _graph.driver.verify_connectivity()
            print("✓ Neo4j: CONNECTED")
        else:
            print("⚠ Neo4j: NOT CONFIGURED")
    except Exception as e:
        issues.append(f"Neo4j connection failed: {e}")
        print(f"✗ Neo4j: FAILED ({e})")
    
    # Check WebSocket server
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://127.0.0.1:7777/v1/host/agents/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    print("✓ WebSocket server: RUNNING")
                else:
                    issues.append(f"WebSocket server unhealthy: {resp.status}")
                    print(f"✗ WebSocket server: UNHEALTHY ({resp.status})")
    except Exception as e:
        issues.append(f"WebSocket server not responding: {e}")
        print(f"✗ WebSocket server: NOT RUNNING")
    
    # Check agents
    try:
        store = _get_agent_store()
        agents = store.list()
        online = [a for a in agents if a["status"] == "online"]
        
        if not agents:
            print("⚠ Agents: NONE REGISTERED")
        elif not online:
            issues.append("No online agents")
            print(f"⚠ Agents: {len(agents)} registered, 0 online")
        else:
            print(f"✓ Agents: {len(online)}/{len(agents)} online")
    except Exception as e:
        issues.append(f"Agent store error: {e}")
        print(f"✗ Agent store: ERROR ({e})")
    
    # Check initiatives
    try:
        store = _get_initiative_store()
        pending = store.count(status="pending")
        stuck = store.count(status="assigned", assigned_before=datetime.now(timezone.utc) - timedelta(hours=1))
        
        if stuck > 0:
            issues.append(f"{stuck} initiatives stuck in assigned state")
            print(f"⚠ Initiatives: {pending} pending, {stuck} STUCK")
        else:
            print(f"✓ Initiatives: {pending} pending")
    except Exception as e:
        issues.append(f"Initiative store error: {e}")
        print(f"✗ Initiative store: ERROR ({e})")
    
    # Summary
    print("")
    if issues:
        print(f"Found {len(issues)} issue(s):")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        return 1
    else:
        print("✓ All checks passed")
        return 0

# CLI
colony doctor
colony doctor --fix  # Attempt automatic fixes
```

---

## Part 5: Summary

### Gaps Found (21)

| # | Gap | Severity | Category |
|---|-----|----------|----------|
| 1 | No `agent rename` command | Moderate | CLI |
| 2 | No `agent update` command | Moderate | CLI |
| 3 | No `initiative cancel` command | Moderate | CLI |
| 4 | No `initiative list` command | Moderate | CLI |
| 5 | No `initiative show` command | Minor | CLI |
| 6 | No `agent show` command | Minor | CLI |
| 7 | No `colony status` command | Minor | CLI |
| 8 | No `PATCH /agents/{id}` endpoint | Critical | API |
| 9 | No `POST /initiatives/{id}/delegate` endpoint | Moderate | API |
| 10 | No `POST /initiatives/{id}/retry` endpoint | Moderate | API |
| 11 | No agent history endpoint | Minor | API |
| 12 | (No gap - index exists) | — | — |
| 13 | No vacuum/pruning for old initiatives | Moderate | Database |
| 14 | No database migration system | Critical | Database |
| 15 | No alert configuration | Moderate | Operations |
| 16 | No webhook configuration | Moderate | Operations |
| 17 | No log rotation configuration | Minor | Operations |
| 18 | No audit log export | Minor | Operations |
| 19 | No backup verification | Minor | Operations |
| 20 | No restore dry-run | Minor | Operations |
| 21 | No system diagnostics command | Moderate | Operations |

---

## Part 6: Recommended Spec Amendments

### Add to Part 9: CLI Commands

```markdown
### 9.X Additional CLI Commands

#### Agent Management

```bash
colony agent rename <agent_id> --name "new-name"
colony agent update <agent_id> [--capabilities CAPS] [--priority N] [--max-concurrent N]
colony agent show <agent_id> [--initiatives]
```

#### Initiative Management

```bash
colony initiative list [--status STATUS] [--type TYPE] [--agent AGENT]
colony initiative show <id> [--history]
colony initiative cancel <id> [--reason "reason"]
colony initiative retry <id>
```

#### Operations

```bash
colony status
colony doctor [--fix]
colony audit export [--format json|csv] [--output FILE]
colony backup verify <backup.tar.gz>
colony restore <backup.tar.gz> [--dry-run]
```
```

### Add to Part 8: API Endpoints

```markdown
### 8.X Additional API Endpoints

```python
# Agent management
PATCH /v1/host/agents/{agent_id}     # Update agent settings
GET /v1/host/agents/{agent_id}/history  # Get initiative history

# Initiative management
POST /v1/host/initiatives/{id}/delegate  # Delegate to another agent
POST /v1/host/initiatives/{id}/retry     # Retry failed initiative

# System
GET /v1/host/status               # Quick status overview
GET /v1/host/diagnostics          # Full diagnostics
```
```

### Add New Part 30: Operational Tooling

```markdown
## Part 30: Operational Tooling

### 30.1 Alerts

- Configurable alert rules for events
- Per-event thresholds and severity
- Channel routing (system, webhook, email)

### 30.2 Webhooks

- External notification support
- HMAC signature verification
- Event and severity filtering

### 30.3 Log Management

- Rotating file logs with size limits
- Structured JSON format option
- Configurable retention

### 30.4 Database Maintenance

- Automatic old initiative pruning (configurable retention)
- Schema migration system with rollback support
- Integrity check on startup

### 30.5 Backup/Restore

- Encrypted backups with passphrase
- Integrity verification before restore
- Dry-run mode for preview
```

---

## Part 7: Updated Effort Estimate

| Component | Hours | Added |
|-----------|-------|-------|
| All previous components | 51h | — |
| Additional CLI commands | 3h | +3h |
| Additional API endpoints | 2h | +2h |
| Alert/webhook system | 3h | +3h |
| Database migration system | 2h | +2h |
| Operational tooling (doctor, backup verify) | 2h | +2h |
| **Total** | **63h** | **+12h** |

---

**Analysis Complete.**

# Spec: Temporal Awareness, Sync Health & Auto-Restart Architecture

**Date:** 2026-05-15
**Author:** Aeva (Hermes agent)
**Status:** Draft — awaiting Marc review
**Version:** Targets ColonyAI v0.7.25
**Replaces:** `2026-05-15-temporal-awareness-and-sync-health.md`

---

## 1. Executive Summary

The `turns_sync` endpoint is **not broken**. Verified live — it returns `200 OK` with `continuity_updated: true` when called with a valid payload.

The **real** problems are systemic:

1. **Silent failure** — when the sidecar crashes or stops, Hermes continues calling `/turns/sync` into a dead port. The Colony memory provider swallows all exceptions at `DEBUG` level. No one notices until Marc asks "is the sidecar running?"
2. **No temporal ground truth** — neither Colony nor Hermes tracks *when* things last happened. We cannot answer "how long since the last successful sync?" or "how stale is this initiative?"
3. **Agent time blindness** — I have no concept of elapsed time between messages, initiatives, or my own actions. I treat every state snapshot as "right now" regardless of whether it is 5 minutes or 5 days old.
4. **No auto-recovery** — when the sidecar dies, nothing restarts it. There is no launchd service, no watchdog, no health-check loop.

This spec fixes all four by building a **three-layer resilience architecture**:

- **Layer 1 (OS):** macOS `launchd` plist with `KeepAlive` — restarts the sidecar automatically on crash
- **Layer 2 (Application):** Hermes poller acts as a heartbeat monitor — detects stale data, attempts restart, alerts
- **Layer 3 (Self-monitoring):** Sidecar telemetry endpoint — exposes temporal metrics so callers know how healthy it is

---

## 2. Current State Audit

### 2.1 turns_sync endpoint

```python
# colony_sidecar/api/schemas/host.py
class TurnSyncRequest(BaseModel):
    identity: HostIdentity          # requires host_id: str
    context: HostTurnContext        # requires session_id: str, contact_id: str
    user_message: Optional[HostMessage] = None   # requires role, content
    assistant_message: Optional[HostMessage] = None
```

The Colony memory provider (`plugins/colony-memory/provider.py:420-430`) already constructs a valid payload. Manual 422s occurred because test requests omitted `host_id` or message `role`. **No code change needed on the endpoint itself.**

### 2.2 Silent sidecar death

The provider's `sync_turn()` catches **all** exceptions and logs at `DEBUG`:

```python
except Exception as exc:
    logger.debug("Colony turn sync failed: %s", exc)
```

If the sidecar is down, this fires every turn and is invisible unless `LOG_LEVEL=debug`. There is no retry, no backoff, and no health alerting.

### 2.3 Missing temporal metadata

Colony's `GET /health` returns:
```json
{"status": "ok", "api_version": "1.0.0", "capabilities": [...], "notes": {}}
```

There is no `started_at`, `last_sync_at`, or `last_initiative_at`. The poller fetches initiatives but cannot tell if data is fresh or 3 days stale.

### 2.4 No service management

There are **no launchd plists** for the Colony sidecar. Old ClawColony plists exist (`ai.clawcolony.gateway.plist`, `ai.clawcolony.config-guard.plist`) but nothing for ColonyAI. When the sidecar crashes, it stays down until someone manually runs `colony start`.

The `colony start --force` command in `cli.py` does work (time is imported at line 11), but:
- It only kills the first PID found on a port (ignores IPv6 duplicates)
- It does not validate that the sidecar actually came up after starting
- It does not have a daemon mode that survives terminal closure (the `--detach` flag exists but is not robust)

---

## 3. Architecture: Three-Layer Resilience

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           MARC (USER)                                    │
│                    WhatsApp DM ←→ Hermes Gateway                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — APPLICATION WATCHDOG (Hermes poller cron, every 5m)           │
│                                                                          │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────────────────┐  │
│  │ Health Check │ → │ Detect Stale │ → │ Attempt Restart (optional)  │  │
│  │  GET /health │   │  sync > 2h?  │   │  `colony start --force`     │  │
│  └──────────────┘   └──────────────┘   └─────────────────────────────┘  │
│         │                      │                      │                 │
│         ▼                      ▼                      ▼                 │
│   ┌────────────┐      ┌─────────────┐      ┌─────────────────┐         │
│   │ Fire Alert │      │ Log channel │      │ Alert on failure │        │
│   │  payload   │      │    only     │      │  (do not spam)   │        │
│   └────────────┘      └─────────────┘      └─────────────────┘         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — OS SERVICE (macOS launchd)                                    │
│                                                                          │
│  Label: ai.aevonix.colony-sidecar                                        │
│  KeepAlive: true      ← restarts automatically on crash                  │
│  RunAtLoad: true      ← starts on boot/login                             │
│  ThrottleInterval: 5  ← waits 5s between restart attempts                │
│                                                                          │
│  Program: ~/.colony-venv/bin/uvicorn colony_sidecar.server:app           │
│  WorkingDirectory: ~/colony-work/sidecar                                 │
│  Environment: COLONY_STATE_DIR, NEO4J_URI, COLONY_API_KEY, etc.         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — SELF-MONITORING (inside sidecar process)                      │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Telemetry Store (in-memory)                    │    │
│  │  started_at | last_sync_at | last_tick_at | last_initiative_at  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  GET /v1/host/health → returns TemporalMetrics + stale_flags    │    │
│  │  status = "degraded" if any silence exceeds threshold           │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Autonomy Loop → touches last_tick_at every tick                │    │
│  │  Turns Sync    → touches last_sync_at on every POST             │    │
│  │  Initiatives   → touches last_initiative_at on mutation         │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Layer 1 — OS Service (macOS launchd)

### 4.1 Why launchd

- **Native macOS** — no third-party dependencies
- **Automatic restart** — `KeepAlive` restarts the process if it exits for any reason (crash, OOM, SIGKILL)
- **Boot persistence** — `RunAtLoad` starts it on login (or boot if system-level)
- **Log management** — `StandardOutPath` / `StandardErrorPath` capture all output
- **Throttle protection** — `ThrottleInterval` prevents restart loops from consuming CPU

### 4.2 New plist: `~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.aevonix.colony-sidecar</string>
    
    <key>ProgramArguments</key>
    <array>
        <string>/Users/kurcide/.colony-venv/bin/uvicorn</string>
        <string>colony_sidecar.server:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>7777</string>
        <string>--log-level</string>
        <string>info</string>
    </array>
    
    <key>WorkingDirectory</key>
    <string>/Users/kurcide/colony-work/sidecar</string>
    
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/kurcide</string>
        <key>PATH</key>
        <string>/Users/kurcide/.colony-venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>COLONY_STATE_DIR</key>
        <string>/Users/kurcide/.colony</string>
        <key>COLONY_SIDECAR_HOST</key>
        <string>127.0.0.1</string>
        <key>COLONY_SIDECAR_PORT</key>
        <string>7777</string>
        <key>NEO4J_URI</key>
        <string>bolt://localhost:7687</string>
        <key>NEO4J_USER</key>
        <string>neo4j</string>
        <key>NEO4J_PASSWORD</key>
        <string></string>
        <key>COLONY_API_KEY</key>
        <string>dev-mode-no-key</string>
        <key>PYTHONPATH</key>
        <string>/Users/kurcide/colony-work/sidecar</string>
    </dict>
    
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    
    <key>StandardOutPath</key>
    <string>/Users/kurcide/.colony/logs/sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/kurcide/.colony/logs/sidecar.log</string>
</dict>
</plist>
```

### 4.3 CLI integration

Add `colony service` subcommand:

```bash
# Install the launchd service
colony service install

# Uninstall
colony service uninstall

# Start via launchd (loads plist)
colony service start

# Stop via launchd (unloads plist)
colony service stop

# Show service status
colony service status
```

**Implementation:**
- `colony service install` writes the plist from a template, substituting real paths from env/config
- `colony service start` runs `launchctl load ~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist`
- `colony service stop` runs `launchctl unload ...`
- `colony service status` runs `launchctl list ai.aevonix.colony-sidecar` and parses PID / LastExitStatus

**Files:**
- `sidecar/colony_sidecar/cli.py` — add `service` subcommand
- `sidecar/colony_sidecar/service_template.plist` — plist template with Jinja2-style placeholders

---

## 5. Layer 2 — Application Watchdog (Hermes Poller)

### 5.1 Enhanced poller responsibilities

The existing `~/.hermes/scripts/colony-initiative-poller.py` runs every 5 minutes. We expand it to:

1. **Health check first** — `GET /v1/host/health` before fetching initiatives
2. **Detect sidecar down** — if health check fails (connection refused, timeout)
3. **Attempt auto-restart** — run `colony start --force` (or `launchctl start` if service is installed)
4. **Alert on persistent failure** — if restart fails or sidecar remains stale after restart
5. **Inject temporal context** — add `colony_state` to every payload

### 5.2 Restart logic

```python
# In poller, when health check fails:

def attempt_restart():
    """Try to restart the sidecar. Returns True if health passes after restart."""
    # Option A: launchctl restart (preferred if service is installed)
    result = subprocess.run(
        ["launchctl", "start", "ai.aevonix.colony-sidecar"],
        capture_output=True, timeout=10,
    )
    if result.returncode == 0:
        time.sleep(5)  # Wait for startup
        return health_check_passes()
    
    # Option B: colony CLI fallback
    result = subprocess.run(
        ["~/.colony-venv/bin/colony", "start", "--force"],
        capture_output=True, timeout=30,
    )
    if result.returncode == 0:
        time.sleep(5)
        return health_check_passes()
    
    return False
```

### 5.3 Alert payload

When the sidecar is down and restart fails:

```json
{
  "type": "alert",
  "payload": {
    "alert_type": "colony_sidecar_down",
    "severity": "critical",
    "message": "Colony sidecar is down and auto-restart failed. Manual intervention required.",
    "last_seen_at": "2026-05-15T05:00:00Z",
    "restart_attempts": 2,
    "suggested_action": "Run: colony start --force  or  launchctl start ai.aevonix.colony-sidecar"
  },
  "delivery_context": {
    "user_chat": "whatsapp:122037267910729@lid",
    "log_channel": "whatsapp:120363425135486141@g.us",
    "platform": "whatsapp"
  }
}
```

The webhook route prompt is updated to handle `alert` type differently from `initiative`:
- `initiative` → act autonomously, max one DM
- `alert` → route to log channel only, do not DM unless `severity == "critical"` and Marc is currently active

### 5.4 Temporal context injection

Every initiative payload now includes:

```json
{
  "type": "initiative",
  "payload": { ...initiative... },
  "occurred_at": "2026-05-15T04:00:00Z",
  "colony_state": {
    "sidecar_status": "ok",
    "sidecar_started_at": "2026-05-15T05:00:00Z",
    "last_sync_at": "2026-05-15T05:30:00Z",
    "last_tick_at": "2026-05-15T05:35:00Z",
    "last_initiative_at": "2026-05-15T05:20:00Z",
    "sync_silence_hours": 0.0,
    "tick_silence_hours": 0.08,
    "initiative_silence_hours": 0.25,
    "stale_flags": []
  },
  "delivery_context": { ... }
}
```

### 5.5 State files for temporal tracking

The poller maintains three state files:

| File | Purpose | Updated by |
|------|---------|------------|
| `~/.hermes/.colony_seen_initiatives` | Dedup by initiative ID | Poller |
| `~/.hermes/.colony_seen_dedup` | Dedup by dedup_key | Poller |
| `~/.hermes/.colony_last_health` | Last health response JSON | Poller |
| `~/.hermes/.colony_last_user_message` | Timestamp of last user message | Provider (optional) |

---

## 6. Layer 3 — Self-Monitoring (Sidecar Telemetry)

### 6.1 Telemetry store

New module: `colony_sidecar/telemetry.py`

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

@dataclass
class TelemetryStore:
    started_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_initiative_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    
    def touch(self, key: str) -> None:
        setattr(self, key, datetime.now(timezone.utc))
    
    def silence_hours(self, key: str) -> float:
        ts = getattr(self, key)
        if ts is None:
            return float('inf')
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    
    def stale_flags(self, thresholds: Dict[str, float]) -> List[str]:
        return [
            key for key, threshold in thresholds.items()
            if self.silence_hours(key) > threshold
        ]
```

Singleton instance injected into the FastAPI app state:

```python
# server.py lifespan
telemetry = TelemetryStore()
telemetry.started_at = datetime.now(timezone.utc)
app.state.telemetry = telemetry
```

### 6.2 Health endpoint update

`GET /v1/host/health` now returns:

```json
{
  "status": "ok",
  "api_version": "1.0.0",
  "capabilities": ["memory", "goals", "autonomy", ...],
  "notes": {"memory": "ColonyGraph wired"},
  "temporal": {
    "started_at": "2026-05-15T05:00:00Z",
    "last_sync_at": "2026-05-15T05:30:00Z",
    "last_tick_at": "2026-05-15T05:35:00Z",
    "last_initiative_at": "2026-05-15T05:20:00Z",
    "silence_hours": {
      "sync": 0.0,
      "tick": 0.08,
      "initiative": 0.25
    },
    "stale_flags": []
  }
}
```

Thresholds:
- `COLONY_STALE_SYNC_HOURS` = 2.0 (if no turn sync in 2h, mark degraded)
- `COLONY_STALE_TICK_HOURS` = 24.0 (if no autonomy tick in 24h, mark degraded)
- `COLONY_STALE_INITIATIVE_HOURS` = 72.0 (if no new initiatives in 72h, mark degraded)

If any threshold is exceeded, `status` becomes `"degraded"`.

### 6.3 Touch points

| Event | Location | Code |
|-------|----------|------|
| Server startup | `server.py` lifespan | `telemetry.started_at = now` |
| Turn sync | `host.py` `/turns/sync` handler | `telemetry.touch("last_sync_at")` |
| Autonomy tick | `autonomy/loop.py` `_tick()` | `telemetry.touch("last_tick_at")` |
| Initiative created | `host.py` initiative POST handlers | `telemetry.touch("last_initiative_at")` |
| Context prefetch | `host.py` `/context/assemble` | `telemetry.touch("last_prefetch_at")` |

### 6.4 Persistence to Neo4j (optional but recommended)

In addition to in-memory telemetry, `record_turn` should persist a `:Turn` node:

```cypher
CREATE (t:Turn {
  session_id: $session_id,
  contact_id: $contact_id,
  synced_at: datetime(),
  topics: $topics,
  entities: $entities,
  summary: $summary
})
```

This lets us query "last sync time" from the graph for analytics, while telemetry is the fast path for health checks.

---

## 7. Provider Resilience (Hermes Plugin)

### 7.1 Visibility

Change `logger.debug` to `logger.warning` for connection-level failures:

```python
except (httpx.ConnectError, OSError) as exc:
    logger.warning("Colony sidecar unreachable: %s", exc)
except httpx.HTTPStatusError as exc:
    if exc.response.status_code in (401, 403):
        logger.warning("Colony turn sync auth failed (HTTP %d)", exc.response.status_code)
    else:
        logger.debug("Colony turn sync HTTP error: %s", exc)
```

### 7.2 Retry with backoff

```python
def _sync():
    for attempt in range(3):
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.post(f"{url}/v1/host/turns/sync", ...)
                resp.raise_for_status()
                self._last_sync_success = datetime.now(timezone.utc)
                self._consecutive_failures = 0
                return
        except (httpx.ConnectError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
        except Exception:
            break  # Don't retry on non-connection errors
    self._consecutive_failures += 1
```

### 7.3 Circuit breaker

```python
self._circuit_open_until: Optional[datetime] = None

def _is_circuit_open(self) -> bool:
    if self._circuit_open_until is None:
        return False
    if datetime.now(timezone.utc) > self._circuit_open_until:
        self._circuit_open_until = None
        return False
    return True

# In _sync(), after all retries fail:
if self._consecutive_failures >= 3:
    self._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=60)
    logger.warning("Colony circuit breaker OPEN for 60s")
```

### 7.4 Diagnostic exposure

```python
def get_sync_status(self) -> dict:
    return {
        "sidecar_url": self.sidecar_url,
        "last_sync_attempt": self._last_sync_attempt.isoformat() if self._last_sync_attempt else None,
        "last_sync_success": self._last_sync_success.isoformat() if self._last_sync_success else None,
        "consecutive_failures": self._consecutive_failures,
        "circuit_open": self._is_circuit_open(),
    }
```

---

## 8. Agent Time Awareness (Hermes Prompt)

### 8.1 Temporal context block

Update the `colony-initiatives` webhook route prompt to prepend:

```yaml
prompt: |
  [TEMPORAL CONTEXT — {now}]
  Colony sidecar status: {payload.colony_state.sidecar_status}
  Sidecar uptime: {sidecar_uptime_hours}h
  Last turn sync: {payload.colony_state.last_sync_at} ({payload.colony_state.sync_silence_hours}h ago)
  Last autonomy tick: {payload.colony_state.last_tick_at} ({payload.colony_state.tick_silence_hours}h ago)
  Last initiative: {payload.colony_state.last_initiative_at} ({payload.colony_state.initiative_silence_hours}h ago)

  Colony initiative received:
  Type: {payload.initiative_type}
  Title: {payload.title}
  Description: {payload.description}
  Occurred at: {occurred_at}
  Age: {initiative_age_hours}h

  DELIVERY RULES:
  - Your FULL response goes to LOGS only.
  - If you need to notify Marc, use send_message with target "{payload.delivery_context.user_chat}".
  - Send AT MOST ONE message per initiative. Concise — one or two sentences.
  - Before acting, check the initiative age. If older than 24h, evaluate relevance.
  - If older than 72h, summarize briefly in the log channel and cancel via Colony API.
```

### 8.2 Gateway template support

Hermes webhook routes currently support `{payload.field}` substitution. We need to add support for:
- `{now}` — resolved to ISO-8601 timestamp at trigger time
- `{occurred_at}` — from the webhook payload root
- `{initiative_age_hours}` — computed from `occurred_at` vs `now`
- `{sidecar_uptime_hours}` — computed from `started_at` vs `now`

If the gateway does not support computed template variables, the poller computes these and injects them into the payload as flat fields:

```json
{
  "computed": {
    "now": "2026-05-15T05:41:00Z",
    "initiative_age_hours": 2.5,
    "sidecar_uptime_hours": 0.68
  }
}
```

---

## 9. CLI Robustness Improvements

### 9.1 Port collision fix

```python
def _find_pids_on_port(port: int) -> list[int]:
    """Find ALL PIDs listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        return [int(p) for p in result.stdout.strip().splitlines() if p.isdigit()]
    except Exception:
        return []

def _kill_processes(pids: list[int], port: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    # Wait up to 5s for all to die
    for _ in range(10):
        if not _find_pids_on_port(port):
            return
        time.sleep(0.5)
    # Escalate to SIGKILL
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
```

### 9.2 Startup validation

After starting the daemon, poll `/health` for up to 10 seconds:

```python
def _wait_for_sidecar(host: str, port: int, timeout: float = 10.0) -> bool:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://{host}:{port}/v1/host/health", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False
```

If validation fails, print the tail of the log file and exit with non-zero status.

### 9.3 Service-aware CLI

Update `colony start` to detect if the launchd service is loaded:

```python
def _is_service_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "list", "ai.aevonix.colony-sidecar"],
        capture_output=True, text=True,
    )
    return result.returncode == 0
```

If loaded, warn the user:
> "The launchd service is managing this sidecar. Use `colony service stop` and `colony service start` instead, or `launchctl unload` first."

---

## 10. Implementation Plan

### Phase 1: Sidecar Telemetry (1-2 hours)
- [ ] Create `colony_sidecar/telemetry.py`
- [ ] Update `HostHealthResponse` schema
- [ ] Update `/health` endpoint in `host.py`
- [ ] Add touch points in `/turns/sync`, `_tick()`, initiative handlers
- [ ] Add `Turn` node persistence in `record_turn`
- [ ] Unit tests

**PR:** `feature/sidecar-telemetry`

### Phase 2: launchd Service (2-3 hours)
- [ ] Create plist template
- [ ] Add `colony service` subcommand (install/uninstall/start/stop/status)
- [ ] Update `colony start` to detect service conflicts
- [ ] Update `colony stop` to handle both foreground and service modes
- [ ] Documentation

**PR:** `feature/launchd-service`

### Phase 3: Poller Health & Auto-Restart (2-3 hours)
- [ ] Add health check to poller
- [ ] Add restart attempt logic (launchctl → CLI fallback)
- [ ] Add `alert` payload type
- [ ] Inject `colony_state` into every payload
- [ ] Maintain `.colony_last_health` state file
- [ ] Update webhook route prompt for alert handling

**PR:** `feature/poller-health-and-restart`

### Phase 4: Provider Resilience (1-2 hours)
- [ ] Elevate connection failure logs to WARNING
- [ ] Add retry with exponential backoff
- [ ] Add circuit breaker
- [ ] Add `get_sync_status()` diagnostic method

**PR:** `feature/provider-resilience`

### Phase 5: Agent Time Awareness (1-2 hours)
- [ ] Update webhook route prompt with temporal context block
- [ ] Add computed template variables (or poller-side computation)
- [ ] Add stale initiative handling rules

**PR:** `feature/agent-time-awareness`

### Phase 6: CLI Robustness (1-2 hours)
- [ ] Fix `_find_pid_on_port` to kill all PIDs
- [ ] Add `_wait_for_sidecar` startup validation
- [ ] Improve restart race handling

**PR:** `fix/cli-startup-race`

---

## 11. Operational Runbook

### Check sidecar status

```bash
# Via API
curl -s http://127.0.0.1:7777/v1/host/health | python3 -m json.tool

# Via CLI
colony status

# Via launchd
launchctl list ai.aevonix.colony-sidecar
```

### Manual restart

```bash
# If service is installed:
launchctl stop ai.aevonix.colony-sidecar
launchctl start ai.aevonix.colony-sidecar

# If service is NOT installed:
colony start --force

# Or direct uvicorn (bypasses CLI):
~/.colony-venv/bin/uvicorn colony_sidecar.server:app --host 127.0.0.1 --port 7777
```

### View logs

```bash
# launchd managed log
tail -f ~/.colony/logs/sidecar.log

# CLI foreground log (if not using launchd)
# Output goes to terminal
```

### Check for stale data

```bash
# The poller writes this on every run
cat ~/.hermes/.colony_last_health | python3 -m json.tool
```

### Disable auto-restart temporarily

```bash
# Stop the launchd service without uninstalling
launchctl unload ~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist

# Re-enable
launchctl load ~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist
```

---

## 12. Acceptance Criteria

### Sidecar Telemetry
- [ ] `GET /health` returns `temporal` block with all timestamps
- [ ] `status` is `"degraded"` if any silence exceeds threshold
- [ ] `POST /turns/sync` updates `last_sync_at`
- [ ] Autonomy tick updates `last_tick_at`
- [ ] `record_turn` persists `:Turn` node in Neo4j with `synced_at`

### launchd Service
- [ ] `colony service install` creates plist and loads it
- [ ] `colony service status` shows PID and running state
- [ ] Killing the sidecar process causes launchd to restart it within 5s
- [ ] Logs are captured to `~/.colony/logs/sidecar.log`

### Poller
- [ ] Poller calls `GET /health` before fetching initiatives
- [ ] When sidecar is down, poller attempts restart via `launchctl start`
- [ ] If restart fails, poller fires `alert` payload to log channel
- [ ] Every initiative payload includes `colony_state` with timestamps

### Provider
- [ ] Connection failures log at `WARNING`
- [ ] Retries up to 3 times with exponential backoff
- [ ] After 3 consecutive failures, circuit breaker opens for 60s
- [ ] `get_sync_status()` returns diagnostic dict

### Agent
- [ ] Webhook prompt includes temporal context block
- [ ] Agent evaluates initiative age before acting
- [ ] Stale initiatives (>72h) are summarized in log channel and cancelled

### CLI
- [ ] `colony start --force` kills ALL PIDs on the port
- [ ] `colony start` validates the sidecar boots before exiting
- [ ] `colony start` warns if launchd service is managing the sidecar

---

## 13. Open Decisions

1. **Auto-restart default?** Should the poller attempt auto-restart by default, or should it be opt-in via env var `COLONY_POLLER_AUTO_RESTART=true`? **Recommendation:** default ON for launchctl restart (safe), default OFF for CLI restart (may mask crashes).

2. **Critical alert DMs?** Should a `critical` alert (sidecar down + restart failed) be sent to Marc's DM, or only to the log channel? **Recommendation:** log channel only, unless Marc has sent a message within the last 15 minutes (indicating he is active).

3. **Neo4j Turn node index?** Should we add an index on `:Turn(synced_at)` for fast "last sync" queries? **Recommendation:** yes, in the same PR as the telemetry work.

4. **Hermes gateway template variables?** Do we add computed template variables (`{now}`, `{initiative_age_hours}`) to the gateway, or keep all computation in the poller? **Recommendation:** keep computation in the poller to avoid gateway changes. If Marc wants global template variables later, that's a separate Hermes feature.

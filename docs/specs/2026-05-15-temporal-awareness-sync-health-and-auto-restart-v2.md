# Spec: Temporal Awareness, Sync Health & Auto-Restart Architecture

**Date:** 2026-05-15
**Author:** Aeva (Hermes agent)
**Status:** Draft — awaiting Marc review
**Version:** Targets ColonyAI v0.7.25
**Replaces:** `2026-05-15-temporal-awareness-and-sync-health.md`

---

## 1. Executive Summary

The `turns_sync` endpoint is **not broken**. Verified live — it returns `200 OK` with `continuity_updated: true` when called with a valid payload. The 422s observed earlier were from malformed manual test payloads.

The **real** problems are systemic:

1. **Silent failure** — when the sidecar crashes or stops, Hermes continues calling `/turns/sync` into a dead port. The Colony memory provider swallows all exceptions at `DEBUG` level. No one notices until Marc asks "is the sidecar running?"
2. **No temporal ground truth** — neither Colony nor Hermes tracks *when* things last happened. We cannot answer "how long since the last successful sync?" or "how stale is this initiative?"
3. **Agent time blindness** — I have no concept of elapsed time between messages, initiatives, or my own actions. I treat every state snapshot as "right now" regardless of whether it is 5 minutes or 5 days old.
4. **No auto-recovery** — when the sidecar dies, nothing restarts it. There is no launchd service, no watchdog, no health-check loop.

This spec fixes all four by building a **three-layer resilience architecture**:

- **Layer 1 (OS):** macOS `launchd` plist with `KeepAlive` — restarts the sidecar automatically on crash
- **Layer 2 (Application):** Hermes poller acts as a heartbeat monitor — detects stale data, alerts (does NOT restart; Layer 1 handles that)
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
- It only kills the first PID found on a port (ignores IPv4 duplicates)
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
│  │ Health Check │ → │ Detect Stale │ → │          ALERT ONLY           │  │
│  │  GET /health │   │  sync > 2h?  │   │  (NO restart — Layer 1 does it) │  │
│  └──────────────┘   └──────────────┘   └─────────────────────────────┘  │
│         │                      │                                           │
│         ▼                      ▼                                           │
│   ┌────────────┐      ┌─────────────┐                                           │
│   │ Fire Alert │      │ Log channel │                                           │
│   │  payload   │      │    only     │                                           │
│   └────────────┘      └─────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — OS SERVICE (macOS launchd)                                    │
│                                                                          │
│  Label: ai.aevonix.colony-sidecar                                        │
│  KeepAlive.SuccessfulExit: false ← restart on crash, not clean exit     │
│  RunAtLoad: true      ← starts on boot/login                             │
│  ThrottleInterval: 60 ← waits 60s between restart attempts (MLX warmup) │
│                                                                          │
│  Program: ~/.colony-venv/bin/uvicorn colony_sidecar.server:app           │
│  WorkingDirectory: ~/colony-work/sidecar                                 │
│  Environment: COLONY_STATE_DIR, NEO4J_URI, COLONY_API_KEY, etc.         │
│  (NO NEO4J_PASSWORD — loaded from ~/.colony/.env at runtime)            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — SELF-MONITORING (inside sidecar process)                      │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Telemetry Store (async-safe)                    │    │
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
- **Automatic restart** — `KeepAlive` restarts the process on unexpected exit
- **Boot persistence** — `RunAtLoad` starts it on login
- **Log management** — `StandardOutPath` / `StandardErrorPath` capture all output
- **Throttle protection** — `ThrottleInterval` prevents restart loops

### 4.2 Critical: MLX warmup requires long throttle interval

The sidecar takes ~3 minutes for MLX model warmup. If `ThrottleInterval` is too short (e.g., 5s) and the process crashes during warmup, launchd restarts it immediately — creating a CPU-burning restart loop.

**Solution:** `ThrottleInterval: 60` and `KeepAlive` with `SuccessfulExit: false` so launchd only restarts on crashes, not on clean exits.

### 4.3 New plist: `~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.aevonix.colony-sidecar</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/exampleuser/.colony-venv/bin/uvicorn</string>
        <string>colony_sidecar.server:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>7777</string>
        <string>--log-level</string>
        <string>info</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/exampleuser/colony-work/sidecar</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/exampleuser</string>
        <key>PATH</key>
        <string>/Users/exampleuser/.colony-venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>COLONY_STATE_DIR</key>
        <string>/Users/exampleuser/.colony</string>
        <key>COLONY_SIDECAR_HOST</key>
        <string>127.0.0.1</string>
        <key>COLONY_SIDECAR_PORT</key>
        <string>7777</string>
        <key>COLONY_API_KEY</key>
        <string>dev-mode-no-key</string>
        <key>PYTHONPATH</key>
        <string>/Users/exampleuser/colony-work/sidecar</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <!-- Only restart on crash, not on clean exit -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- 60s throttle prevents restart loops during MLX warmup -->
    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>/Users/exampleuser/.colony/logs/sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/exampleuser/.colony/logs/sidecar.log</string>
</dict>
</plist>
```

**Note:** `NEO4J_PASSWORD` is intentionally **NOT** in the plist. The sidecar loads it from `~/.colony/.env` at runtime. Putting it in the plist would override the .env value and silently break auth if the password changes.

### 4.4 CLI integration

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
- `colony service install` writes the plist from a template, substitutes real paths from env/config, creates `~/.colony/logs/`, and runs `launchctl load`
- `colony service start` runs `launchctl load` (idempotent if already loaded)
- `colony service stop` runs `launchctl unload`
- `colony service status` runs `launchctl list` and parses PID / LastExitStatus

**Files:**
- `sidecar/colony_sidecar/cli.py` — add `service` subcommand
- `sidecar/colony_sidecar/service_template.plist` — plist template with placeholders

---

## 5. Layer 2 — Application Watchdog (Hermes Poller)

### 5.1 Design principle: poller does NOT restart

The poller's job is to **detect** and **alert**. Restart is Layer 1's responsibility (launchd `KeepAlive`). This eliminates:
- The 5-second vs 3-minute startup mismatch
- Conflicting restart attempts between poller and launchd
- Port collision races during simultaneous restarts

The poller only calls `launchctl start` as a one-time recovery attempt if Layer 1 is somehow not managing the process (e.g., service was manually unloaded).

### 5.2 Enhanced poller responsibilities

1. **Health check first** — `GET /v1/host/health` before fetching initiatives
2. **Detect sidecar down** — if health check fails (connection refused, timeout)
3. **Attempt service wake-up** — `launchctl start` if service is installed but not running
4. **Alert on persistent failure** — if sidecar remains down after wake-up attempt
5. **Inject temporal context** — add `colony_state` to every payload

### 5.3 Restart logic (service-aware only)

```python
def attempt_wake_up():
    """If launchd service is installed but not running, wake it up.
    Returns True if health passes after wake-up."""
    # Check if service is installed
    result = subprocess.run(
        ["launchctl", "list", "ai.aevonix.colony-sidecar"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Service not installed — nothing we can do. Layer 1 not configured.
        return False

    # Service exists but may be stopped — try to start it
    subprocess.run(
        ["launchctl", "start", "ai.aevonix.colony-sidecar"],
        capture_output=True, timeout=10,
    )

    # Wait up to 3 minutes for MLX warmup
    for attempt in range(18):  # 18 × 10s = 180s
        time.sleep(10)
        if health_check_passes():
            return True
    return False
```

### 5.4 Alert payload

When the sidecar is down and wake-up fails:

```json
{
  "type": "alert",
  "payload": {
    "alert_type": "colony_sidecar_down",
    "severity": "critical",
    "message": "Colony sidecar is down and could not be restarted via launchd.",
    "last_seen_at": "2026-05-15T05:00:00Z",
    "suggested_action": "Run: launchctl start ai.aevonix.colony-sidecar  or  colony service start"
  },
  "delivery_context": {
    "user_chat": "whatsapp:000000000000000@lid",
    "log_channel": "whatsapp:120363425135486141@g.us",
    "platform": "whatsapp"
  }
}
```

The webhook route prompt handles `alert` differently from `initiative`:
- `initiative` → act autonomously, max one DM
- `alert` → route to log channel only. DM only if severity == "critical" AND Marc has sent a message in the last 15 minutes (indicating he is active)

### 5.5 Temporal context injection

Every initiative payload now includes:

```json
{
  "type": "initiative",
  "payload": { ...initiative... },
  "occurred_at": "2026-05-15T04:00:00Z",
  "colony_state": {
    "status": "ok",
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
  },
  "computed": {
    "now": "2026-05-15T05:41:00Z",
    "initiative_age_hours": 2.5,
    "sidecar_uptime_hours": 0.68
  },
  "delivery_context": { ... }
}
```

### 5.6 State files and retention

| File | Purpose | Updated by | Retention |
|------|---------|------------|-----------|
| `~/.hermes/.colony_seen_initiatives` | Dedup by initiative ID | Poller | Prune entries older than 90 days |
| `~/.hermes/.colony_seen_dedup` | Dedup by dedup_key | Poller | Prune entries older than 90 days |
| `~/.hermes/.colony_last_health` | Last health response JSON | Poller | Overwrite each run |
| `~/.hermes/.colony_last_user_message` | Timestamp of last user message | Provider | Overwrite each sync |

**Pruning logic:** on poller startup, remove lines from `.colony_seen_initiatives` and `.colony_seen_dedup` where the initiative `created_at` is older than 90 days.

---

## 6. Layer 3 — Self-Monitoring (Sidecar Telemetry)

### 6.1 Telemetry store (async-safe)

New module: `colony_sidecar/telemetry.py`

```python
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

@dataclass
class TelemetryStore:
    started_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_initiative_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    _lock: asyncio.Lock = None

    def __post_init__(self):
        if self._lock is None:
            self._lock = asyncio.Lock()

    async def touch(self, key: str) -> None:
        async with self._lock:
            setattr(self, key, datetime.now(timezone.utc))

    async def silence_hours(self, key: str) -> float:
        async with self._lock:
            ts = getattr(self, key)
            if ts is None:
                return float('inf')
            return (datetime.now(timezone.utc) - ts).total_seconds() / 3600

    async def stale_flags(self, thresholds: Dict[str, float]) -> List[str]:
        flags = []
        for key, threshold in thresholds.items():
            silence = await self.silence_hours(key)
            if silence > threshold:
                flags.append(key)
        return flags

    async def to_dict(self, thresholds: Dict[str, float]) -> dict:
        started = self.started_at.isoformat() if self.started_at else None
        sync_at = self.last_sync_at.isoformat() if self.last_sync_at else None
        tick_at = self.last_tick_at.isoformat() if self.last_tick_at else None
        init_at = self.last_initiative_at.isoformat() if self.last_initiative_at else None
        prefetch_at = self.last_prefetch_at.isoformat() if self.last_prefetch_at else None
        silence = {}
        for key in thresholds:
            silence[key] = await self.silence_hours(key)
        flags = await self.stale_flags(thresholds)
        return {
            "started_at": started,
            "last_sync_at": sync_at,
            "last_tick_at": tick_at,
            "last_initiative_at": init_at,
            "last_prefetch_at": prefetch_at,
            "silence_hours": silence,
            "stale_flags": flags,
        }
```

Singleton instance wired into the FastAPI app state:

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
    "last_prefetch_at": "2026-05-15T05:32:00Z",
    "silence_hours": {
      "sync": 0.0,
      "tick": 0.08,
      "initiative": 0.25,
      "prefetch": 0.15
    },
    "stale_flags": []
  }
}
```

Thresholds (env-configurable):
- `COLONY_STALE_SYNC_HOURS` = 2.0
- `COLONY_STALE_TICK_HOURS` = 24.0
- `COLONY_STALE_INITIATIVE_HOURS` = 72.0
- `COLONY_STALE_PREFETCH_HOURS` = 2.0

If any silence exceeds its threshold, `status` becomes `"degraded"`.

### 6.3 Touch points

| Event | Location | Code |
|-------|----------|------|
| Server startup | `server.py` lifespan | `telemetry.started_at = now` |
| Turn sync | `host.py` `/turns/sync` handler | `await telemetry.touch("last_sync_at")` |
| Autonomy tick | `autonomy/loop.py` `_tick()` | `await telemetry.touch("last_tick_at")` |
| Initiative created | `host.py` initiative POST handlers | `await telemetry.touch("last_initiative_at")` |
| Context prefetch | `host.py` `/context/assemble` | `await telemetry.touch("last_prefetch_at")` |

### 6.4 No Neo4j Turn persistence (v1)

Creating a `:Turn` node on every sync would grow the graph unbounded (36,500 nodes/year at 100 turns/day). TelemetryStore provides sufficient temporal ground truth for health monitoring. If Marc wants historical analytics later, we can add a time-series backend or a monthly `:Turn` cleanup job. **Not in v1.**

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
                self._persist_circuit_state()  # Save to file
                return
        except (httpx.ConnectError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
        except Exception:
            break  # Don't retry on non-connection errors
    self._consecutive_failures += 1
    self._persist_circuit_state()
```

### 7.3 Circuit breaker (persists across sessions)

```python
CIRCUIT_FILE = os.path.expanduser("~/.hermes/.colony_circuit_state")

self._circuit_open_until: Optional[datetime] = None

def _load_circuit_state(self):
    try:
        with open(CIRCUIT_FILE) as f:
            data = json.load(f)
            ts = data.get("circuit_open_until")
            if ts:
                self._circuit_open_until = datetime.fromisoformat(ts)
            self._consecutive_failures = data.get("consecutive_failures", 0)
    except (FileNotFoundError, ValueError):
        pass

def _persist_circuit_state(self):
    with open(CIRCUIT_FILE, "w") as f:
        json.dump({
            "circuit_open_until": self._circuit_open_until.isoformat() if self._circuit_open_until else None,
            "consecutive_failures": self._consecutive_failures,
        }, f)

def _is_circuit_open(self) -> bool:
    if self._circuit_open_until is None:
        return False
    if datetime.now(timezone.utc) > self._circuit_open_until:
        self._circuit_open_until = None
        self._persist_circuit_state()
        return False
    return True

# In _sync(), after all retries fail:
if self._consecutive_failures >= 3:
    self._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=60)
    self._persist_circuit_state()
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
  [TEMPORAL CONTEXT — {payload.computed.now}]
  Colony sidecar status: {payload.colony_state.status}
  Sidecar uptime: {payload.computed.sidecar_uptime_hours}h
  Last turn sync: {payload.colony_state.last_sync_at} ({payload.colony_state.silence_hours.sync}h ago)
  Last autonomy tick: {payload.colony_state.last_tick_at} ({payload.colony_state.silence_hours.tick}h ago)
  Last initiative: {payload.colony_state.last_initiative_at} ({payload.colony_state.silence_hours.initiative}h ago)

  Colony initiative received:
  Type: {payload.initiative_type}
  Title: {payload.title}
  Description: {payload.description}
  Occurred at: {occurred_at}
  Age: {payload.computed.initiative_age_hours}h

  DELIVERY RULES:
  - Your FULL response goes to LOGS only.
  - If you need to notify Marc, use send_message with target "{payload.delivery_context.user_chat}".
  - Send AT MOST ONE message per initiative. Concise — one or two sentences.
  - Before acting, check the initiative age. If older than 24h, evaluate relevance.
  - If older than 72h, summarize briefly in the log channel.
  - Do NOT attempt to cancel initiatives during webhook handling — you lack Colony tools in this context.
```

### 8.2 Computed values in poller

The poller computes these and injects them as flat fields:

```json
{
  "computed": {
    "now": "2026-05-15T05:41:00Z",
    "initiative_age_hours": 2.5,
    "sidecar_uptime_hours": 0.68
  }
}
```

This avoids modifying the Hermes gateway template engine.

---

## 9. CLI Robustness Improvements

### 9.1 Port collision fix (filter by LISTEN state)

```python
def _find_pids_on_port(port: int) -> list[int]:
    """Find ALL PIDs in LISTEN state on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
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

## 10. Implementation Plan (Reordered)

### Phase 1: launchd Service (2-3 hours)
- [ ] Create plist template (`service_template.plist`)
- [ ] Add `colony service` subcommand (install/uninstall/start/stop/status)
- [ ] `colony service install` creates `~/.colony/logs/` directory
- [ ] Update `colony start` to detect service conflicts and warn
- [ ] Update `colony stop` to handle both foreground and service modes
- [ ] Documentation

**PR:** `feature/launchd-service`

### Phase 2: Sidecar Telemetry (2-4 hours)
- [ ] Create `colony_sidecar/telemetry.py` (async-safe)
- [ ] Update `HostHealthResponse` schema with `TemporalMetrics`
- [ ] Update `/health` endpoint to compute silence hours and stale flags
- [ ] Add async touch points in `/turns/sync`, `_tick()`, initiative handlers, `/context/assemble`
- [ ] Unit tests

**PR:** `feature/sidecar-telemetry`

### Phase 3: Poller Health & Alerting (2-3 hours)
- [ ] Add `GET /health` pre-flight check to poller
- [ ] Add `launchctl start` wake-up logic (service-aware, no CLI restart)
- [ ] Add 3-minute wait for MLX warmup after wake-up
- [ ] Add `alert` payload type with severity routing rules
- [ ] Inject `colony_state` + `computed` into every payload
- [ ] Add state file retention pruning (90 days)
- [ ] Maintain `.colony_last_health` state file

**PR:** `feature/poller-health-and-alerts`

### Phase 4: Provider Resilience (1-2 hours)
- [ ] Elevate connection failure logs to `WARNING`
- [ ] Add retry with exponential backoff (max 3 attempts)
- [ ] Add circuit breaker with file-persisted state
- [ ] Add `get_sync_status()` diagnostic method

**PR:** `feature/provider-resilience`

### Phase 5: Agent Time Awareness (1-2 hours)
- [ ] Update webhook route prompt with temporal context block
- [ ] Reference `payload.computed.*` fields (poller provides them)
- [ ] Add stale initiative handling rules (>72h → log summary, do not act)
- [ ] Add explicit rule: do NOT cancel initiatives during webhook handling

**PR:** `feature/agent-time-awareness`

### Phase 6: CLI Robustness (1-2 hours)
- [ ] Fix `_find_pid_on_port` to use `lsof -sTCP:LISTEN`
- [ ] Kill ALL listener PIDs, not just the first
- [ ] Add `_wait_for_sidecar` startup validation
- [ ] Print log tail on startup failure

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

### launchd Service
- [ ] `colony service install` creates plist, loads it, and creates `~/.colony/logs/`
- [ ] `colony service status` shows PID and running state
- [ ] Killing the sidecar process causes launchd to restart it within 60s (not 5s)
- [ ] Logs are captured to `~/.colony/logs/sidecar.log`
- [ ] `colony start` warns if launchd service is managing the sidecar

### Sidecar Telemetry
- [ ] `GET /health` returns `temporal` block with all timestamps and `silence_hours`
- [ ] `status` is `"degraded"` if any silence exceeds threshold
- [ ] `POST /turns/sync` updates `last_sync_at`
- [ ] Autonomy tick updates `last_tick_at`
- [ ] TelemetryStore is thread-safe under concurrent sync calls

### Poller
- [ ] Poller calls `GET /health` before fetching initiatives
- [ ] When sidecar is down, poller attempts `launchctl start` (not CLI restart)
- [ ] Poller waits up to 3 minutes for sidecar to become healthy after wake-up
- [ ] If sidecar remains down, poller fires `alert` payload to log channel
- [ ] Every initiative payload includes `colony_state` + `computed` timestamps
- [ ] State files are pruned of entries older than 90 days

### Provider
- [ ] Connection failures log at `WARNING`
- [ ] Retries up to 3 times with exponential backoff
- [ ] After 3 consecutive failures, circuit breaker opens for 60s
- [ ] Circuit breaker state persists across Hermes sessions
- [ ] `get_sync_status()` returns diagnostic dict

### Agent
- [ ] Webhook prompt includes temporal context block referencing `payload.computed.*`
- [ ] Agent evaluates initiative age before acting
- [ ] Stale initiatives (>72h) are summarized in log channel only
- [ ] Agent does NOT attempt to cancel initiatives during webhook handling

### CLI
- [ ] `colony start --force` kills ALL listener PIDs on the port (not clients)
- [ ] `colony start` validates the sidecar boots before exiting
- [ ] `colony start` prints log tail and exits non-zero on startup failure

---

## 13. Decisions Made

1. **Auto-restart:** The poller does NOT restart the sidecar. Layer 1 (launchd `KeepAlive`) handles all restarts. The poller only calls `launchctl start` as a wake-up if the service exists but is not running.

2. **Critical alert DMs:** `critical` alerts go to the log channel only. They DM Marc only if he has sent a message in the last 15 minutes (indicating he is active and might want immediate awareness).

3. **No Neo4j Turn nodes (v1):** TelemetryStore is sufficient for temporal health. Adding `:Turn` nodes would grow the graph unbounded without a cleanup job. Deferred to v2 if Marc wants historical analytics.

4. **Computed values in poller:** All time computations (`now`, `initiative_age_hours`, `sidecar_uptime_hours`) happen in the poller, not the gateway. This avoids modifying Hermes.

5. **Circuit breaker persists to file:** `~/.hermes/.colony_circuit_state` survives Hermes restarts, making the circuit breaker actually useful.

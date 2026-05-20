# Spec: Temporal Awareness & Turn Sync Health

**Date:** 2026-05-15  
**Author:** Aeva (Hermes agent)  
**Status:** Draft — awaiting owner review  
**Version:** Targets ColonyAI v0.7.25

---

## 1. Executive Summary

The `turns_sync` endpoint is **not broken**. The 422 errors observed during manual testing were caused by malformed test payloads (missing `host_id` inside `identity`, or missing `role` inside `user_message`). The endpoint returns `200 OK` with `continuity_updated: true` when called correctly.

The **real** problems are:

1. **Silent failure** — when the sidecar crashes or stops, Hermes continues calling `/turns/sync` into a dead port. The Colony memory provider swallows all exceptions at `DEBUG` level. No one notices.
2. **No temporal ground truth** — neither Colony nor Hermes tracks *when* things last happened. We cannot answer "how long since the last successful sync?" or "how stale is this initiative?"
3. **Agent time blindness** — I have no concept of elapsed time between messages, initiatives, or my own actions. I treat every state snapshot as "right now" regardless of whether it is 5 minutes or 5 days old.

This spec fixes all three by adding **time as a first-class citizen** across the stack.

---

## 2. Root Cause Analysis

### 2.1 turns_sync endpoint

```python
# colony_sidecar/api/schemas/host.py
class TurnSyncRequest(BaseModel):
    identity: HostIdentity          # requires host_id: str
    context: HostTurnContext        # requires session_id: str, contact_id: str
    user_message: Optional[HostMessage] = None   # requires role, content
    assistant_message: Optional[HostMessage] = None
    ...
```

The Colony memory provider (`plugins/colony-memory/provider.py:420-430`) already constructs a valid payload. Manual 422s occurred because test requests omitted `host_id` or message `role`. **No code change needed on the endpoint itself.**

### 2.2 Silent sidecar death

The provider's `sync_turn()` catches **all** exceptions and logs at `DEBUG`:

```python
except Exception as exc:
    logger.debug("Colony turn sync failed: %s", exc)
```

If the sidecar is down, this line fires every turn and is invisible unless `LOG_LEVEL=debug`. There is no retry, no backoff, and no health alerting.

### 2.3 Missing temporal metadata

Colony's `GET /health` returns:
```json
{"status": "ok", "api_version": "1.0.0", "capabilities": [...], "notes": {}}
```

There is no `started_at`, `last_sync_at`, or `last_initiative_at`. The poller fetches initiatives but has no way to know whether the data is fresh or 3 days stale.

### 2.4 Agent time blindness

My system prompt and webhook prompts contain no timestamps. When Colony sends an initiative created 72 hours ago, I process it as if it were created right now. The poller injects `occurred_at` but the prompt does not surface it prominently.

---

## 3. Proposed Changes

### 3.1 Sidecar Temporal Health (Colony)

**New module:** `colony_sidecar/telemetry.py` — an in-memory telemetry store (no external DB needed, just a singleton dataclass).

**Fields tracked:**
| Field | Set by | Meaning |
|-------|--------|---------|
| `started_at` | Server lifespan startup | When the sidecar process began |
| `last_sync_at` | `POST /turns/sync` handler | Last successful turn sync |
| `last_initiative_at` | `POST /initiatives` (any mutation) | Last initiative created/updated |
| `last_tick_at` | Autonomy loop | Last autonomy tick |
| `last_prefetch_at` | `POST /context/assemble` | Last context prefetch |

**Schema update:** `HostHealthResponse` gains:
```python
class TemporalMetrics(BaseModel):
    started_at: Optional[str] = None          # ISO-8601
    last_sync_at: Optional[str] = None
    last_initiative_at: Optional[str] = None
    last_tick_at: Optional[str] = None
    silence_hours: Dict[str, float] = {}      # e.g. {"sync": 2.5, "tick": 48.0}
    stale_flags: List[str] = []               # e.g. ["sync", "tick"]

class HostHealthResponse(BaseModel):
    status: Literal["ok", "degraded", "starting", "stopping"]
    api_version: str
    capabilities: List[str]
    notes: Optional[Dict[str, str]]
    temporal: Optional[TemporalMetrics] = None   # NEW
```

**Thresholds (configurable via env):**
- `COLONY_STALE_SYNC_HOURS` = 2.0
- `COLONY_STALE_TICK_HOURS` = 24.0
- `COLONY_STALE_INITIATIVE_HOURS` = 72.0

If any silence exceeds its threshold, `status` becomes `"degraded"` and the flag is added to `stale_flags`.

**Files:**
- `sidecar/colony_sidecar/telemetry.py` (new)
- `sidecar/colony_sidecar/api/schemas/host.py`
- `sidecar/colony_sidecar/api/routers/host.py` — update `/health` and `/turns/sync`
- `sidecar/colony_sidecar/server.py` — init telemetry at lifespan start
- `sidecar/colony_sidecar/autonomy/loop.py` — touch `last_tick_at` each tick

---

### 3.2 Provider Resilience (Hermes plugin)

**Changes to `plugins/colony-memory/provider.py`:**

1. **Elevate visibility:** change `logger.debug` to `logger.warning` for connection failures (httpx.ConnectError, OSError). Keep `logger.debug` for HTTP 4xx/5xx to avoid log spam.

2. **Add retry:** wrap the `POST /turns/sync` in a loop with exponential backoff (1s, 2s, 4s — max 3 attempts).

3. **Circuit breaker:** if the sidecar fails 3 times in a row, mark it `unavailable` for 60 seconds. Skip sync attempts during this window. Reset on next successful health check.

4. **Track local timestamps:**
   ```python
   self._last_sync_attempt: Optional[datetime] = None
   self._last_sync_success: Optional[datetime] = None
   self._consecutive_failures: int = 0
   ```

5. **Expose state for diagnostics:** add a `get_sync_status()` method returning the above fields. This can be called by Hermes internal tools if needed.

---

### 3.3 Poller Health Checks & Time Injection (Hermes cron)

**Changes to `~/.hermes/scripts/colony-initiative-poller.py`:**

1. **Pre-flight health check:** before `GET /v1/host/initiatives`, call `GET /v1/host/health`.

2. **Alert on stale data:** if `temporal.silence_hours.sync > 2` or `status == "degraded"`, fire a special payload:
   ```json
   {
     "type": "alert",
     "payload": {
       "alert_type": "colony_stale",
       "message": "Colony sidecar has not synced turns in 2.5h",
       "temporal": { ... }
     },
     "delivery_context": {
       "user_chat": "whatsapp:USER_LID@lid",
       "log_channel": "whatsapp:GROUP_CHAT_ID@g.us",
       "platform": "whatsapp"
     }
   }
   ```
   The webhook route prompt should instruct the agent to send a brief alert to the **log channel only** (no DM spam).

3. **Inject colony state into every initiative payload:**
   ```json
   {
     "type": "initiative",
     "payload": { ...initiative... },
     "occurred_at": "2026-05-15T04:00:00Z",
     "colony_state": {
       "sidecar_status": "ok",
       "last_sync_at": "2026-05-15T05:00:00Z",
       "last_initiative_at": "2026-05-15T04:30:00Z",
       "sync_silence_hours": 0.0,
       "tick_silence_hours": 1.5
     },
     "delivery_context": { ... }
   }
   ```

4. **Track `last_user_message_at`:** maintain a small state file `~/.hermes/.colony_last_user_message` updated by the webhook route handler (or by the provider's `sync_turn` if we can hook into it). For now, the poller can approximate it by noting when the last initiative was *user-initiated* vs *system-initiated*.

   **Better approach:** add a lightweight `POST /webhooks/colony-ping` endpoint in Hermes that the provider calls on every user message, just to timestamp it. But that's more invasive. For v1, approximate via initiative metadata.

---

### 3.4 Agent Time Awareness (Hermes prompt)

**Update the `colony-initiatives` webhook route prompt in `~/.hermes/config.yaml`:**

Prepend a temporal context block that the gateway resolves at runtime:

```yaml
prompt: |
  Current time: {now}
  Time since last user message: {hours_since_last_message}h
  Colony sidecar status: {colony_status}
  Last successful turn sync: {last_sync_at} ({sync_silence_hours}h ago)

  Colony initiative received:
  ...
```

**Implementation note:** Hermes webhook routes already support template variables like `{payload.initiative_type}`. We need to add **global template variables** resolved at trigger time:
- `{now}` — ISO-8601 timestamp
- `{hours_since_last_message}` — from a lightweight state file
- `{colony_status}`, `{last_sync_at}`, `{sync_silence_hours}` — from the poller's last health check

If Hermes does not support global webhook template variables, we instead enrich the payload in the poller (which we already control) and reference them as `{payload.colony_state.last_sync_at}`.

**Additional rule in prompt:**
> "Before acting on any initiative, check `occurred_at`. If the initiative is older than 24 hours, evaluate whether it is still relevant. Stale initiatives (older than 72 hours) should be summarized briefly in the log channel and cancelled in Colony via the API — do not act on them unless the owner explicitly confirms."

---

### 3.5 CLI Robustness (Sidecar startup)

The `colony start --force` command works correctly in the current `cli.py` (`time` is imported at line 11). However, two robustness issues exist:

1. **Port collision ambiguity:** `_find_pid_on_port` uses `lsof -ti :{port}`. If multiple processes are bound to the same port (e.g., IPv4 + IPv6), it only kills the first PID. Add a loop to kill all PIDs found.

2. **Restart race:** after `os.kill(pid, 15)`, the code sleeps 2s then checks if the port is free. On slow systems the process may still be shutting down. Add a retry loop (max 5s total) before escalating to SIGKILL.

3. **Startup validation:** after starting the daemon, poll `/health` for up to 10s to confirm the sidecar actually came up. If not, print the tail of `sidecar.log` and exit with error code.

**Files:**
- `sidecar/colony_sidecar/cli.py`

---

## 4. Implementation Order

| Phase | Scope | Est. Complexity | PR Target |
|-------|-------|-----------------|-----------|
| 1 | Sidecar telemetry + health schema | Low | `feature/sidecar-temporal-health` |
| 2 | Provider resilience + circuit breaker | Low | `feature/provider-resilience` |
| 3 | Poller health checks + payload enrichment | Low | `feature/poller-health-injection` |
| 4 | Webhook prompt updates + stale initiative rules | Low | `feature/agent-time-awareness` |
| 5 | CLI startup robustness | Low | `fix/cli-startup-race` |

All five are independent and can be reviewed in parallel. Phase 3 depends on Phase 1 being deployed (to read the new health fields), but the poller can gracefully degrade if `temporal` is missing from the health response.

---

## 5. Acceptance Criteria

- [ ] `GET /health` returns `temporal.started_at` and `temporal.silence_hours`
- [ ] After a successful `POST /turns/sync`, `temporal.last_sync_at` is updated
- [ ] If the sidecar has not synced in >2h, `status` is `"degraded"`
- [ ] Provider logs connection failures at `WARNING`, retries 3x, and enters 60s circuit-breaker
- [ ] Poller fires `alert` payloads to the log channel when sidecar is stale or down
- [ ] Every initiative payload includes `colony_state` with sync/initiative timestamps
- [ ] Agent prompt includes temporal context and stale-initiative handling rules
- [ ] `colony start` validates the sidecar actually boots before exiting

---

## 6. Open Questions

1. **Should the poller attempt auto-restart?** If the sidecar is down, the poller could run `colony start --force` or `~/.colony-venv/bin/uvicorn ...` directly. However, auto-restart might mask the root cause of crashes (OOM, Neo4j disconnect). **Recommendation:** alert only; let the owner decide to restart. We can add auto-restart as a configurable opt-in later.

2. **How does Hermes inject `{now}` into webhook prompts?** If the gateway does not support global template variables, we rely entirely on poller payload enrichment. Need to verify `gateway/run.py` template resolution.

3. **Should `record_turn` persist the turn timestamp in Neo4j?** Currently `record_turn` stores topics/entities/summary but not `synced_at`. Adding a `Turn` node with `:SYNCED_AT` property would let us query "last sync time" from the graph instead of an in-memory telemetry store. **Recommendation:** do both. Telemetry is fast for health checks; Neo4j is the source of truth for analytics.

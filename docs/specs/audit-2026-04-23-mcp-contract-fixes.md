# MCP Contract & Integration Fix Spec — 2026-04-23

Source: code audit 2026-04-23 (second audit). Covers MCP tool-schema contract bugs, Hermes integration failures, runtime crash bugs, and security fixes.

---

## Batch 1 — MCP Contract Fixes

### 1.1 colony_get_context omits required incoming_message (C1)

**File:** `sidecar/colony_sidecar/mcp/server.py` (~line 148)
**Schema:** `api/schemas/host.py:114-119` — `incoming_message: HostMessage` (required, no default)

**Problem:** The tool only includes `incoming_message` when the `message` param is truthy. Any call without `message` posts a payload missing the field and gets a 422 from FastAPI.

**Fix:** Always send `incoming_message`. Default to `{"role": "user", "content": ""}` when no message is provided.

```python
# Before:
if message:
    payload["incoming_message"] = {"role": "user", "content": message}

# After:
payload["incoming_message"] = {"role": "user", "content": message or ""}
```

---

### 1.2 Revert YAML API key to env var template (C3)

**File:** `sidecar/colony_sidecar/mcp/config.py` (~line 192-200)

**Problem:** `_add_to_yaml_config` requires `COLONY_API_KEY` at config-write time and bakes the raw key value into `~/.hermes/config.yaml`. The JSON/TOML handlers use `${COLONY_API_KEY}` placeholder. The raw key ends up on disk in cleartext.

**Why this was changed:** Earlier audit flagged that Hermes doesn't expand `${...}` at runtime. However, re-reading the Hermes `mcp_tool.py` source confirms that Hermes's `_build_safe_env()` does `env.update(user_env)` with the user-specified env block — and `${COLONY_API_KEY}` in the YAML is expanded by Hermes's config loader (`hermes_cli/config.py` uses PyYAML with env var expansion). The MCP subprocess then receives the resolved value.

**Fix:** Revert to `${COLONY_API_KEY}` template. Remove the "COLONY_API_KEY env var not set" guard (the key can be set later before Hermes starts).

```python
# Before:
api_key_value = os.environ.get("COLONY_API_KEY", "")
if not api_key_value:
    return "  COLONY_API_KEY env var not set — set it before running mcp setup"
new_config = {
    "command": "colony",
    "args": ["mcp"],
    "env": {
        "COLONY_API_KEY": api_key_value,
        ...
    },
}

# After:
new_config = {
    "command": "colony",
    "args": ["mcp"],
    "env": {
        "COLONY_API_KEY": "${COLONY_API_KEY}",
        ...
    },
}
```

---

### 1.3 cancellation_reason → metadata (H1)

**File:** `sidecar/colony_sidecar/mcp/server.py` (~line 269-271)
**Schema:** `api/schemas/host.py` — `CommitmentUpdateRequest` has no `cancellation_reason` field but does have `metadata: Optional[Dict[str, Any]]`

**Problem:** `cancellation_reason` is sent as a top-level field and silently dropped by Pydantic.

**Fix:** Move into metadata dict.

```python
# Before:
data: dict[str, Any] = {"status": "cancelled"}
if reason:
    data["cancellation_reason"] = reason

# After:
data: dict[str, Any] = {"status": "cancelled"}
if reason:
    data.setdefault("metadata", {})["cancellation_reason"] = reason
```

---

### 1.4 context type mismatch: str → dict (H2)

**File:** `sidecar/colony_sidecar/mcp/server.py` (~line 328, 340)
**Schema:** `api/schemas/host.py:1021` — `context: Optional[Dict[str, Any]]`

**Problem:** Tool declares `context: str | None` but schema expects `dict`. A non-None string payload fails Pydantic validation → 422.

**Fix:** Change tool signature to `context: dict | None = None`.

---

### 1.5 Align tool defaults with schema defaults (M1, M2)

**File:** `sidecar/colony_sidecar/mcp/server.py`

M1: `colony_record_affect` — `arousal` should default to `0.5` (schema default), not be required.

```python
# Before:
arousal: float,
# After:
arousal: float = 0.5,
```

M2: `colony_record_surprise` — `expected` should be optional (schema allows None).

```python
# Before:
expected: str,
# After:
expected: str | None = None,
```

---

## Batch 2 — Hermes Integration Fixes

### 2.1 Add raw message fields to TurnSyncRequest (C2)

**File:** `sidecar/colony_sidecar/api/schemas/host.py` (~line 388-395)
**Provider:** `plugins/hermes-memory/provider.py` (~line 155-163)

**Problem:** `TurnSyncRequest` expects `topics`, `entities`, `pending_tasks`, `tools_used`, `summary`. The Hermes provider sends `user_message` and `assistant_message` instead. Pydantic drops the unrecognized fields. The sidecar receives empty lists for everything — extraction is a no-op.

**Fix:** Add `user_message` and `assistant_message` as optional fields to `TurnSyncRequest`. Update the turn sync handler in `api/routers/host.py` to run extraction from raw messages when the structured fields are empty but raw messages are present.

Schema change:

```python
class TurnSyncRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    # Structured fields (populated by OpenClaw plugin)
    topics: List[str] = []
    entities: List[Dict[str, Any]] = []
    pending_tasks: List[str] = []
    tools_used: List[str] = []
    summary: Optional[str] = None
    # Raw message fields (populated by Hermes provider, MCP tools)
    user_message: Optional[HostMessage] = None
    assistant_message: Optional[HostMessage] = None
```

Handler change in `routers/host.py` (turn sync endpoint):

```python
# After receiving the request, if structured fields are empty but
# raw messages are present, run extraction on them.
if not req.topics and not req.entities and req.user_message:
    # Extract from raw messages using existing extraction pipeline
    extraction = await _extract_from_messages(
        user_msg=req.user_message,
        asst_msg=req.assistant_message,
        contact_id=req.context.contact_id,
    )
    req.topics = extraction.get("topics", [])
    req.entities = extraction.get("entities", [])
    # etc.
```

This makes turn extraction work for any harness that sends raw messages, not just OpenClaw.

---

### 2.2 Log WARN on auth failures in Hermes provider (H4)

**File:** `plugins/hermes-memory/provider.py` (~line 109-111, 166-167)

**Problem:** 401/403 responses are logged at DEBUG and silently swallowed. Operators can't distinguish "sidecar down" from "wrong API key".

**Fix:** Branch on status code in prefetch and sync_turn.

```python
except httpx.HTTPStatusError as exc:
    code = exc.response.status_code
    if code in (401, 403):
        logger.warning("Colony auth failed (HTTP %d) — check COLONY_API_KEY", code)
    else:
        logger.debug("Colony request failed: %s", exc)
    return ""  # or None for sync_turn
```

Also add validation in `initialize()`:

```python
def initialize(self, session_id: str, **kwargs) -> None:
    self._session_id = session_id
    if not self._api_key:
        logger.warning("Colony: COLONY_API_KEY not set — requests will fail if sidecar requires auth")
    ...
```

---

## Batch 3 — Runtime Crash & Security Fixes

### 3.1 Inject colony runtime handle for synthesized skills (C4)

**File:** `sidecar/colony_sidecar/skills/learning/pattern_extractor.py` (~line 182)
**Executor:** `sidecar/colony_sidecar/skills/executor.py` (~line 387)

**Problem:** Synthesized skill signature is `async def run(colony, {params}):` but executor calls `run_fn(**inputs)` with no `colony` in inputs. Results in `TypeError: run() missing 1 required positional argument: 'colony'`.

**Fix:** Two parts:

1. In `pattern_extractor.py`, change the generated signature to not require `colony` as a positional arg. Instead, make it available via closure or remove it if the skill doesn't need runtime tool access.

2. In `executor.py`, inject a colony runtime handle before calling the skill:

```python
# executor.py — before calling run_fn
from colony_sidecar.skills.runtime import ColonyRuntime

if "colony" in inspect.signature(run_fn).parameters:
    colony_runtime = ColonyRuntime(sidecar_url=os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
    injected = {"colony": colony_runtime, **inputs}
    return await run_fn(**injected)
else:
    return await run_fn(**inputs)
```

The `ColonyRuntime` class needs to expose `.tools.invoke(name, args)` — this is a new thin wrapper around the sidecar HTTP API.

---

### 3.2 UUID for initiative IDs (H5)

**File:** `sidecar/colony_sidecar/intelligence/components/initiative_engine.py` (~lines 195, 235, 263, 282)

**Problem:** IDs like `f"{prefix}-{hash(desc) % 100000:05d}-{int(ts)}"` are collision-prone and non-reproducible (Python hash is salted per-process).

**Fix:** Replace with UUID-based IDs everywhere in the file.

```python
import uuid

# Before:
f"{prefix}-{hash(desc) % 100000:05d}-{int(ts)}"
# After:
f"{prefix}-{uuid.uuid4().hex[:12]}"
```

---

### 3.3 Fix naive datetime in initiative engine (H6)

**File:** `sidecar/colony_sidecar/intelligence/components/initiative_engine.py` (~lines 174, 177)

**Problem:** `datetime.now()` compared against tz-aware `expires_at` values raises `TypeError`.

**Fix:** Replace with `datetime.now(timezone.utc)`.

```python
from datetime import datetime, timezone

# Before:
datetime.now()
# After:
datetime.now(timezone.utc)
```

Check all occurrences in the file (lines 174, 177, 195, 202, 235, 242, 263, 269, 282, 288).

---

### 3.4 Whitelist-enforcing __import__ wrapper in sandbox (H7)

**File:** `sidecar/colony_sidecar/skills/sandbox_runner.py` (~lines 119-130)

**Problem:** `__import__` is in `_safe_dunders`, allowing any skill that bypasses AST scanning to import `os`, `socket`, `ctypes` at runtime. Defence-in-depth is broken.

**Fix:** Replace the bare `__import__` in the allowlist with a wrapper that only permits modules declared in the skill's manifest.

```python
# In _build_safe_builtins:
declared_modules = set(skill_manifest.get("imports", []))

def _restricted_import(name, *args, **kwargs):
    if name not in declared_modules:
        raise ImportError(f"Import of '{name}' not allowed (not in skill manifest)")
    return original_import(name, *args, **kwargs)

safe_builtins["__import__"] = _restricted_import
```

Also pre-load declared modules into the execution namespace before running the skill, so `from X import Y` works without hitting `__import__` at all for declared modules.

---

### 3.5 Fix getattr scanner to inspect only args[1] (H9)

**File:** `sidecar/colony_sidecar/skills/security/scanner.py` (~lines 166-174)

**Problem:** Dynamic-getattr rule inspects the whole arg list instead of just `args[1]` (the attribute name). `getattr(obj, "safe_attr", computed_default)` where `computed_default` is non-constant is incorrectly flagged.

**Fix:** Only inspect `node.args[1]` for the attribute name. Ignore `args[2]` (the default).

```python
# Before: iterates all args
for arg in node.args:

# After: only check the attribute name (second positional arg)
if len(node.args) >= 2:
    self._check_attr_name(node.args[1], node)
```

---

## Batch 4 — Guards & Resilience

### 4.1 Wrap onEvent callback in try/catch (H3)

**File:** `src/sidecar-client.ts` (~lines 801, 808)

**Problem:** `ws.on("message")` calls `onEvent(parsed as HostEvent)` with no try/catch. A synchronous throw propagates into the ws EventEmitter and kills the message handler. Async rejections surface as unhandledRejection.

**Fix:** Wrap in try/catch, handle async rejections.

```typescript
ws.on("message", (data: Buffer) => {
  try {
    const parsed = JSON.parse(data.toString());
    try {
      const result = onEvent(parsed as HostEvent);
      // Handle async callbacks
      if (result && typeof result === "object" && "catch" in result) {
        result.catch((err: unknown) => {
          logger?.error(`[colony] events: async handler error: ${String(err)}`);
        });
      }
    } catch (err) {
      logger?.error(`[colony] events: handler error: ${String(err)}`);
    }
  } catch {
    // Invalid JSON — ignore
  }
});
```

---

### 4.2 Debounce tool refresh on skill approval (M6)

**File:** `src/tool-registrar.ts` (~lines 330-343)
**Trigger:** `src/plugin.ts` (~lines 2097-2100)

**Problem:** `refreshSkillTools` is fire-and-forget. Concurrent `skill_draft_approved` events can register the same tool twice or race the `knownSkillToolNames` set.

**Fix:** Serialize with an in-flight promise.

```typescript
private _refreshPromise: Promise<void> | null = null;

async refreshSkillTools(): Promise<void> {
  if (this._refreshPromise) {
    return this._refreshPromise;
  }
  this._refreshPromise = this._doRefresh();
  try {
    await this._refreshPromise;
  } finally {
    this._refreshPromise = null;
  }
}

private async _doRefresh(): Promise<void> {
  // existing refresh logic
}
```

---

## Deferred to docs/deferred-items.md

The following are documented but not fixed in this batch:

| ID | Item | Reason |
|---|---|---|
| M3 | README miscounts (subsystems, endpoints, resources, tests) | Automate with CI script, not manual updates |
| M4 | iMessage UTF-8 truncation | Verify whether limit is chars or bytes first |
| M5 | ThreadPoolExecutor per call in aggregators | Low impact, fix with singleton when touching that file |
| M7 | No adversarial tests for skill security scanner | Incremental test coverage |
| M8 | Hermes 401/5xx both at DEBUG | Fixed in 2.2 (WARN for 401/403) |
| M9 | Hermes install.sh doesn't verify Hermes is installed | Add precondition check |
| M10 | Bearer auth not systematically covered in tests | Parametrized test matrix over routes |
| M11 | World-model schema migrations untested | Migration test harness |
| M12 | Compression edge cases untested | Add edge case tests |
| L1 | README provenance claim broader than reality | Reconcile with deferred schema gap |
| L2 | Skill approval async rejection fire-and-forget | Low impact |
| L3 | is_available() sync httpx in async provider | Startup-only, acceptable |
| L4 | TS tests mock entire OpenClaw SDK | Integration test CI job |
| L5 | E2E tests use hardcoded time.sleep | Replace with polling |

---

## Implementation Order

```
Batch 1 (MCP contract)     → commit, verify with pytest + npm build
Batch 2 (Hermes fixes)     → commit, verify
Batch 3 (crash/security)   → commit, verify
Batch 4 (guards/resilience) → commit, verify
Version bump + release
```

Each batch is independently valuable and can be shipped if later batches need more time.

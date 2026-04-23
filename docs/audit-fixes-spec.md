# Audit Fix Spec — Batch 1

Source: code audit 2026-04-23. Covers critical, high-priority, and easy defensive fixes.

---

## 1. WebSocket Error/Close Handlers

**File:** `src/sidecar-client.ts:747-819`
**Severity:** Critical
**Problem:** `openEvents()` only listens for `open` and `message`. If the socket closes (server 4001 rejection, network failure, sidecar restart), no event fires and the `{close}` handle becomes a no-op. Proactive delivery silently dies until the gateway restarts.

**Fix:**
- Add `ws.on("error", ...)` and `ws.on("close", ...)` handlers.
- On close/error, call a new `onDisconnect` callback so the plugin knows the stream is dead.
- Update `openEvents` signature to accept `onDisconnect?: (code?: number, reason?: string) => void`.
- In `src/plugin.ts`, the events subscription block should log the disconnect and optionally attempt a reconnect with backoff (or at minimum surface it so operators know).

**TypeScript changes:**
```
// sidecar-client.ts — openEvents signature
openEvents(
  onEvent: (event: HostEvent) => void,
  lastEventId?: string,
  onDisconnect?: (code?: number, reason?: string) => void,
): { close: () => void }

// Inside openEvents, after ws.on("message", ...):
ws.on("error", (err) => {
  onDisconnect?.(undefined, err.message);
});
ws.on("close", (code, reason) => {
  onDisconnect?.(code, reason.toString());
});
```

**Plugin.ts changes:**
```
// In the events subscription block, pass onDisconnect:
subscription = ctx.client.openEvents(
  (event) => { /* existing handler */ },
  lastEventTimestamp,
  (code, reason) => {
    logger?.warn(`[colony] events: WebSocket closed (code=${code} reason=${reason}) — proactive deliveries disabled until reconnect`);
    subscription = null;
    // Optional: schedule reconnect with backoff
  },
);
```

---

## 2. asyncio.create_task Fire-and-Forget

**File:** `sidecar/colony_sidecar/api/routers/host.py` (lines 865, 960, 1007, 1480, 1496)
**Severity:** High
**Problem:** `asyncio.create_task(_run())` with no reference retained. Per Python docs, the task can be garbage-collected mid-flight if no reference exists. Cognition triggers and ToM extraction are affected.

**Fix:**
- Add a module-level `set()` to hold task references.
- Wrap creation in a helper that adds the task and removes it on completion.
- `asyncio.create_task` at line 3149 (autonomy loop) already assigns to `_autonomy_task` so it's safe, but the others need fixing.

```python
# At module level in host.py:
_background_tasks: set[asyncio.Task] = set()

def _spawn_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
```

- Replace all 5 bare `asyncio.create_task(...)` calls with `_spawn_task(...)`.
- Replace `_aio.create_task(trigger_cognition(...))` at line 1438 with `_spawn_task(trigger_cognition(...))`.

---

## 3. Flatten relationships/ Directory

**Files:** `sidecar/colony_sidecar/intelligence/relationships/`
**Severity:** High (structural debt)
**Problem:**
- `relationships/relationships/` is a nested duplicate directory.
- `trust_tiers.py` exists in both locations and is byte-identical.
- Outer `__init__.py` is just a comment.
- `scorer.py`, `anomaly_detector.py`, `permissions.py`, `federation_scorer.py` only exist in the nested dir.
- `PermissionsManager`, `FederationScorer`, nested `AnomalyDetector` have no importers — dead code.
- The real `AnomalyDetector` lives at `intelligence/components/anomaly_detector.py`.
- `autonomy/loop.py:586` imports from the nested path (`relationships.relationships.scorer`).

**Fix:**
1. Move `relationships/relationships/scorer.py` → `relationships/scorer.py` (it's the only nested module with a live importer).
2. Update `autonomy/loop.py:586` import: `from colony_sidecar.intelligence.relationships.scorer import RelationshipScorer`
3. Delete the entire `relationships/relationships/` directory.
4. Update `relationships/__init__.py` to export `TrustTier` and `RelationshipScorer`.
5. Delete the duplicate `relationships/trust_tiers.py` (identical to the nested one, but the nested one is the one getting deleted — keep the outer one which is already the canonical import path).

**Imports affected (all already use the outer path):**
- `sessions/store.py`, `sessions/isolated_session.py`, `sessions/context_loader.py`
- `gate/layers/l4_trust_tier.py`, `gate/models.py`
- `api/routers/host.py:1519`
- `task_queue/handlers/inference.py:355`

All of these import from `colony_sidecar.intelligence.relationships.trust_tiers` — no change needed.

**Dead code to remove (no live importers):**
- `relationships/relationships/anomaly_detector.py` (duplicate of `components/anomaly_detector.py`)
- `relationships/relationships/permissions.py`
- `relationships/relationships/federation_scorer.py`

---

## 4. dispatchHostEvent Outside try/catch

**File:** `src/plugin.ts:2080-2101`
**Severity:** High
**Problem:** `dispatchHostEvent()` is called outside the try/catch that wraps `summarizeHostEvent()`. If dispatch throws, the exception propagates to the WS message handler which has no guard, potentially terminating the event callback.

**Fix:**
- Wrap the `dispatchHostEvent()` call in its own try/catch.
- Log the error and continue processing events (don't let one bad dispatch kill the stream).

```typescript
try {
  dispatchHostEvent(event, { cache, logger, onSkillApproved });
} catch (dispatchErr) {
  logger?.warn(`[colony] events: dispatch error on ${event.type} (${String(dispatchErr)})`);
}
```

---

## 5. WorldModelStore Silent Failure

**File:** `sidecar/colony_sidecar/server.py:458`
**Severity:** Medium
**Problem:** `except Exception: pass` on WorldModelStore fallback init. If both attempts fail, `world_store=None` and every world model endpoint silently returns empty results. No log, no indication anything is wrong.

**Fix:**
- Replace bare `pass` with a `logger.error(...)` call.
- Same pattern in `setup.py:928, 983` — log the failure instead of swallowing it.

```python
except Exception as exc:
    logger.error(f"WorldModelStore init failed (fallback): {exc}")
    world_store = None
```

---

## 6. Identity Bootstrap .then() Race

**File:** `src/plugin.ts:2219-2238`
**Severity:** Medium
**Problem:** `ctx.refreshIdentity()` and `ctx.verifyChain()` use `.then()` chains on a shared snapshot object. Concurrent `buildContext` calls during startup can interleave.

**Fix:**
- Convert to `async/await` so each call completes before the next starts.
- Add a guard boolean so the bootstrap only runs once.

```typescript
let identityBootstrapped = false;

async function bootstrapIdentity() {
  if (identityBootstrapped) return;
  identityBootstrapped = true;
  try {
    const snap = await ctx.refreshIdentity();
    if (snap.colony_id || snap.node_id) {
      logger?.info(`[colony] identity resolved colony_id=${snap.colony_id ?? "?"} node_id=${snap.node_id ?? "?"} tier=${snap.trust_tier ?? "unset"}`);
    }
    const verified = await ctx.verifyChain();
    if (verified.chain_valid) {
      logger?.info(`[colony] identity chain verified (depth=${verified.depth})`);
    }
  } catch (err) {
    logger?.warn(`[colony] identity bootstrap failed: ${String(err)}`);
  }
}
```

---

## 7. res.entries[0] No Guard

**File:** `src/plugin.ts:587`
**Severity:** Medium
**Problem:** `res.entries[0]` accessed without checking `res.entries` is non-empty. If the sidecar returns a payload without entries, this throws instead of degrading.

**Fix:**
```typescript
const entry = res.entries?.[0];
if (!entry?.content) {
  // degrade gracefully
  return;
}
```

---

## Not in this batch

These are deferred per discussion:
- **Proactive delivery subagent** — known architectural issue, needs design work
- **Naive datetime.now()** — worst offenders are in dead code getting removed in fix #3
- **empty-trace NotImplementedError** — approval-gated, low hit rate
- **501 decorator consolidation** — cosmetic/boilerplate
- **Stub aggregator renaming** — cosmetic
- **Topic sort tiebreaker** — practically irrelevant
- **Duplicate set_session_store(None)** — zero impact

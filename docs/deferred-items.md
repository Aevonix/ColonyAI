# Deferred Items

Items we've consciously chosen to defer. If you're running an audit, these are known and should not be re-reported without new information.

Last updated: 2026-04-23

---

## MCP Provenance Not Stored in Schemas

- **Location:** `sidecar/colony_sidecar/api/schemas/host.py` (all create-request schemas)
- **Issue:** `COLONY_MCP_SOURCE` provenance is injected into request data by the MCP server but no Pydantic model has a `provenance` field. The value is silently dropped by Pydantic validation. Per-harness attribution doesn't work through MCP.
- **Reason deferred:** Partial fix applied — provenance now goes into `metadata` dict instead of top-level, which survives Pydantic's `extra="ignore"` for models that have a `metadata` field. Not all schemas have `metadata`. Adding `provenance` to every create-request schema is a larger change that needs careful schema migration.
- **Will unblock when:** We add a `provenance` field to the base schema or add `metadata` to all create-request schemas.

---

## MCP Session ID Hardcoded to "mcp"

- **Location:** `sidecar/colony_sidecar/mcp/server.py:148, 355`
- **Issue:** Every MCP-initiated call uses `session_id="mcp"`, mixing all MCP invocations into one logical session in the sidecar's session store. Ruins session-scoped analytics.
- **Reason deferred:** Needs design decision — generate per-process UUID at server start, or read `COLONY_MCP_SESSION_ID` from env, or generate per-request. Also affects world model entity queries and context assembly.
- **Will unblock when:** We need session-scoped analytics or multi-session MCP usage.

---

## Port Baked at Config-Write Time

- **Location:** `sidecar/colony_sidecar/mcp/config.py:88, 143, 176`
- **Issue:** `COLONY_SIDECAR_PORT` is baked into the harness config when `colony mcp setup` runs. If the user later changes the port, every harness keeps calling the stale URL until setup is re-run.
- **Reason deferred:** Acceptable tradeoff. TOML/JSON configs for Claude Code/Codex/OpenCode support `${COLONY_URL}` expansion, but not all harnesses do. Documenting the limitation is sufficient for now.
- **Will unblock when:** We standardize on env var expansion across all harnesses, or add a `colony mcp refresh` command.

---

## Hermes MemoryProvider No-Op Property Indirection

- **Location:** `plugins/hermes-memory/provider.py` (removed in rewrite)
- **Issue:** `_contact_id` used a property/setter pair with name mangling that added no value over a plain attribute.
- **Reason deferred:** Fixed in async rewrite — `_contact_id` is now a plain attribute.
- **Will unblock when:** N/A (resolved)

---

## Prefetch Cache Race in Hermes Provider

- **Location:** `plugins/hermes-memory/provider.py:111-119`
- **Issue:** `queue_prefetch` clears `_cached_context` and fires an async task. If `prefetch` is called before the task completes, it sees empty and issues a duplicate HTTP call.
- **Reason deferred:** Partially addressed in async rewrite with `asyncio.Event`. Full solution would use a `Future` or `Lock`. The race is narrow — Hermes's MemoryManager calls `queue_prefetch` then `prefetch` sequentially, so the overlap window is small.
- **Will unblock when:** Hermes's MemoryManager behavior changes, or we see duplicate calls in practice.

---

## MCP SDK No Upper Bound

- **Location:** `sidecar/pyproject.toml:25`
- **Issue:** `mcp[cli]>=1.0` has no upper bound. A future 2.x could break the call shape or transport enums.
- **Reason deferred:** Premature. We don't know what 2.0 breaks yet. Pinning now could prevent adopting compatible minor updates.
- **Will unblock when:** MCP SDK 2.0 is announced or breaks our usage.

---

## `mcp setup --harness all` Inconsistency

- **Location:** `sidecar/colony_sidecar/cli.py`
- **Issue:** Interactive mode iterates detected harnesses; explicit `all` goes through all defs. Minor mismatch with the remove path.
- **Reason deferred:** Edge case. Both paths work; the difference is that `--harness all` attempts config for harnesses that aren't installed, while interactive only shows detected ones.
- **Will unblock when:** We unify the logic or add a `--detected-only` flag.

---

## Proactive Delivery Spawns Full Subagent Turn

- **Location:** `src/plugin.ts:237-283 / 2082`
- **Issue:** Proactive message delivery spawns a full subagent turn just to echo a notification. Burns tokens and adds latency.
- **Reason deferred:** Architectural change needed — not a quick fix. Documented in `open-items-plan.md` item 2.
- **Open since:** v0.5.x
- **Will unblock when:** A lighter delivery path is designed (e.g. direct message dispatch without agent turn).

---

## Naive datetime.now() Mixed with tz-aware Comparisons

- **Location:** Widespread (46 naive vs 205 aware). Riskiest spots: `intelligence/components/initiative_engine.py`, `briefings/delivery.py`.
- **Issue:** `datetime.now()` compared against tz-aware persisted values will TypeError. `delivery.py` also assumes server local time matches user timezone.
- **Reason deferred:** Worst offenders were in `relationships/relationships/` dead code (removed in v0.6.3 flatten). Remaining instances are low-hit-rate paths. Initiative engine is the one that will bite eventually.
- **Will unblock when:** Initiative engine gets a proper refactor, or we adopt `datetime.now(timezone.utc)` project-wide as a lint rule.

---

## Empty-trace Skills Raise NotImplementedError

- **Location:** `sidecar/colony_sidecar/skills/learning/pattern_extractor.py:159`
- **Issue:** Auto-synthesized skill body is `raise NotImplementedError`. Caller crashes if run without a captured trace.
- **Reason deferred:** Approval-gated now — the empty-trace path shouldn't be reached in practice. The scaffold exists for future auto-learning.
- **Will unblock when:** We implement trace capture for auto-synthesized skills, or add a runtime guard that returns a "skill not ready" error instead of NotImplementedError.

---

## 501 "Not Wired" Endpoint Boilerplate

- **Location:** `sidecar/colony_sidecar/api/routers/host.py` (~50 endpoints)
- **Issue:** Endpoints return `HTTPException(501, "not wired")` individually. Could be consolidated with a `@requires(_store)` decorator.
- **Reason deferred:** Cosmetic/boilerplate. No runtime impact. Graceful degradation works as intended.
- **Will unblock when:** We do a router refactor pass, or the endpoint count grows enough that the boilerplate becomes a maintenance burden.

---

## Stub*Aggregator Naming

- **Location:** `sidecar/colony_sidecar/briefings/aggregators.py:149+`
- **Issue:** Fallback aggregator classes named `Stub*Aggregator` look like unfinished code but are actually intentional no-op fallbacks.
- **Reason deferred:** Cosmetic. Functional behavior is correct.
- **Will unblock when:** Rename pass (e.g. `NoopRelationshipAggregator`) happens alongside other briefings work.

---

## Topic Sort Non-determinism

- **Location:** `src/extraction/pipeline.ts:128-131`
- **Issue:** Topics sorted by frequency with no tiebreaker. Output order varies across runs when frequencies tie.
- **Reason deferred:** Practically irrelevant — consumers don't depend on order among equal-frequency topics.
- **Will unblock when:** Something depends on deterministic ordering, or we add a secondary sort key as a one-liner during related work.

---

## Duplicate set_session_store(None) / set_task_queue(None) on Shutdown

- **Location:** `sidecar/colony_sidecar/server.py:828+855, 846+856`
- **Issue:** Each called twice on shutdown. No runtime impact.
- **Reason deferred:** Zero functional effect. Dead statements.
- **Will unblock when:** Touched during a shutdown-path refactor.

---

## Reasoning Capability Gated on Sidecar Self-advertisement

- **Location:** `src/plugin.ts:1155 TODO`
- **Issue:** Capability probe returns empty set on any error. Transient network blip flips reasoning off until a retry succeeds.
- **Reason deferred:** Works correctly when sidecar is healthy. Only affects the first few turns during startup if sidecar is slow.
- **Will unblock when:** We add retry logic to capability probing or cache the last known capabilities.

---

## Extraction Phase 7+ TODO

- **Location:** `src/plugin.ts:1930`
- **Issue:** `outgoing_message` and `channel_id` extraction is documented as incomplete in the plugin's post-turn path.
- **Reason deferred:** Feature incomplete, not a bug. Lower-priority extraction targets.
- **Will unblock when:** These extraction fields become useful downstream (e.g. per-channel context assembly).

---

## LLM Interpretation Pass Not Wired in Goals

- **Location:** `sidecar/colony_sidecar/goals/inference.py:195`
- **Issue:** Extension point documented as "not implemented here — hook provided via override" is never wired in production initialization.
- **Reason deferred:** Goals subsystem works without LLM interpretation. The hook exists for future use.
- **Will unblock when:** Goals need smarter inference beyond rule-based decomposition.

# Changelog

## 0.6.0 (2026-04-22)

Colony MCP Server: shared intelligence across agent and coding harnesses.

### New
- MCP server exposing 14 tools, 4+ resources, 3 prompts to Claude Code, Codex, and Crush
- `colony mcp` CLI: run (stdio/HTTP), detect, setup (selective, --dry-run), remove (--dry-run)
- `/mcp` HTTP endpoint on sidecar for streamable HTTP transport
- Harness auto-detection (claude, codex, crush CLIs)
- Selective harness setup: choose which harnesses connect, not all-or-nothing
- Source tracking via COLONY_MCP_SOURCE env var, auto-injected by MCP server
- Provenance field on all MCP writes (separate from sidecar's source enum)
- Contact ID required during setup, set via COLONY_MCP_CONTACT_ID
- Host framework selection in setup wizard (OpenClaw, Claude Code, Codex, Crush, Standalone)
- `mcp[cli]>=1.0` as optional dependency
- 51 MCP unit tests (27 server + 24 config)
- 14/14 MCP tools E2E validated against live sidecar

### Fixed
- World model search endpoint: POST /v1/host/world/entities/query (not GET /search)
- Provenance vs source: MCP provenance writes to `provenance` field, not `source`, to avoid enum conflicts with sidecar schemas

## 0.5.8 (2026-04-22)

New CLI commands for lifecycle management and E2E validation.

- **Added:** `colony start -d` — daemon mode with PID tracking, port conflict detection, auto-kill stale processes
- **Added:** `colony stop` — clean shutdown (SIGTERM → SIGKILL fallback)
- **Added:** `colony status` — health check + E2E validation status
- **Added:** `colony validate` — full pipeline test (seeds data, checks context assembly, optional LLM test)
- **Added:** E2E validation stamp (`.colony-e2e-validated`) — persists across restarts
- **Added:** `colony doctor` check #34: E2E pipeline validated
- **Added:** Validation warnings in `colony status` and `colony start` until E2E is run
- **Added:** Setup wizard prompts for `colony validate` after setup
- **Fixed:** EOFError on `colony start -d` when stdin unavailable

## 0.5.7 (2026-04-22)

Setup wizard bug fixes and gateway restart flow.

- **Fixed:** Neo4j connectivity test in wizard (uses raw driver, not ColonyGraph)
- **Fixed:** Sidecar auto-start in wizard (writes to log file, start_new_session)
- **Fixed:** TIER_TABLE → TIERS import (correct export name)
- **Fixed:** Skip multimodal check when embeddings disabled
- **Added:** Gateway restart verification — waits for restart, checks plugin loaded
- **Added:** Warning that Colony won't receive messages until gateway restart

## 0.5.6 (2026-04-22)

Setup wizard fixes for fresh install experience.

- **Added:** "Skip embeddings" option in tier selection (option 3) — Colony runs without vector search
- **Fixed:** Sidecar auto-start in wizard now uses uvicorn instead of bare module
- **Fixed:** ContactsStore init uses correct `sqlite_path` parameter
- **Fixed:** Neo4j connectivity test uses driver session directly (bypasses query allowlist)
- **Fixed:** `COLONY_EMBED_PROVIDER=skip` no longer crashes EmbeddingPipeline
- **Validated:** Full `pip install colonyai` → `colony init` → sidecar start → health=ok flow

## 0.5.5 (2026-04-22)

Critical packaging and startup fixes.

- **Fixed:** SQL schema files (goals, contacts, task_queue, world_model) missing from pip wheel
- **Fixed:** `COLONY_EMBED_PROVIDER=skip` now gracefully disables embeddings instead of crashing
- **Fixed:** Affect in context assembly reads `current_valence`/`current_arousal` (AffectStore API)
- **Fixed:** `build/` directory accidentally committed to git (removed, added to .gitignore)
- **Added:** `package_data` in pyproject.toml to include SQL/JSON/YAML files in wheel
- **Added:** E2E test scripts for live environment validation (full cycle + turn sync extraction)

## 0.5.4 (2026-04-22)

Fixes for autonomy loop, startup errors, and graph traversal.

### Autonomy
- Autonomy loop now auto-starts on sidecar startup (was manual only)
- AnomalyDetector now receives graph_client + EventBus (was missing required args)
- Fixed import path for RelationshipScorer (nested directory structure)
- MetaLearner returns default CPI when PerformanceIndexComputer not wired (was RuntimeError)

### Goals
- All enum `.value` accesses now use hasattr guards (str vs enum crash)

### Neo4j Backend
- `get_neighbors()` now bidirectional — follows both outgoing and incoming relationships
- Fixes neighborhood traversal and path finding for directed edges

### Health & Setup
- Health endpoint shows autonomy running state + tick count
- Setup wizard adds ownContextEngine + ownMemoryCapability to OpenClaw plugin config
- Setup wizard auto-selects WORLD_MODEL_BACKEND based on Neo4j password
- Setup wizard verifies data flow (creates test commitment, checks context assembly)
- Added asyncio + timezone imports where missing

## 0.5.3 (2026-04-22)

Neo4j graph database backend for the World Model, plus full CRUD API endpoints.

### Neo4j Backend
- Full `Neo4jBackend` implementing the same interface as SQLiteBackend
- Entity and relationship CRUD with Cypher MERGE/SET
- Native graph traversal via `get_neighbors()`
- Full-text search via Neo4j index
- Observations, merge proposals, entity resolution, stats
- Auto-schema on connect: indexes, constraints, full-text index
- Compatible with Neo4j driver v6 async API
- Backend selection via `WORLD_MODEL_BACKEND` env var (sqlite/neo4j)
- Env vars: `NEO4J_URI`, `NEO4J_DATABASE`, `NEO4J_USER`, `NEO4J_PASSWORD`
- Automatic fallback to SQLite if driver missing or Neo4j unavailable

### World Model API Endpoints (12 new)
- `POST /world/entities` — create entity
- `GET /world/entities/{id}` — get entity
- `PATCH /world/entities/{id}` — update entity
- `DELETE /world/entities/{id}` — delete entity
- `POST /world/relationships` — create relationship
- `GET /world/relationships` — list/query relationships with filters
- `GET /world/relationships/{id}` — get relationship
- `PATCH /world/relationships/{id}` — update/close relationship
- `DELETE /world/relationships/{id}` — close relationship
- `GET /world/entities/{id}/neighborhood` — BFS graph traversal
- `GET /world/entities/{src}/path/{tgt}` — shortest path
- `GET /world/stats` — world model statistics

### Fixes
- Default `neo4j_database` changed from `colony` to `neo4j` (Community edition compatibility)
- Stats query filters NULL entity_types from legacy data

## 0.5.2 (2026-04-22)

Security and dependency maintenance.

### Security
- Updated openclaw dependency to 2026.4.21 (resolves 9 Dependabot alerts: protobufjs critical, tar high, axios moderate)
- All transitive vulnerabilities now resolved (0 npm audit findings)

### Bug Fixes
- Fixed `_llm_router` reference in ToM extractor init log (would crash when LLM router is wired)

## 0.5.1 (2026-04-22)

ToM Layer 3: LLM extraction for affect and shared facts.

### ToM LLM Extraction
- TomExtractor: async LLM-backed extraction from conversation turns
- Affect extraction: valence/arousal/trigger from conversation text (neutral readings skipped)
- Fact extraction: knowledge items with source classification
- Per-contact throttle (5 min default, configurable via COLONY_TOM_EXTRACTION_THROTTLE_MINUTES)
- Auto-fires on turn_sync when LLM router is wired
- POST /v1/host/tom/extract for manual extraction
- 21 new unit tests

## 0.5.0 (2026-04-22)

Pattern Extraction + Surprise Engine: pattern detection and anomaly scoring.

### Pattern Extraction
- PatternStore: SQLite-backed CRUD with upsert (frequency increment on duplicate pattern_key)
- Deactivate stale patterns, list with filters (type, min frequency, source, active)
- 4 pattern types: entity_cooccurrence, relation_frequency, temporal_sequence, attribute_cluster
- Extraction logic: entity cooccurrence, relation frequency, attribute clusters
- Extraction workers graceful no-op when world model not wired
- `POST /v1/host/patterns/extract` trigger endpoint

### Surprise Engine
- SurpriseStore: SQLite-backed CRUD, resolve, count_unresolved, get_unresolved
- Scorer: pattern-matching scoring (no match=0.7, violated=0.5+conf×0.5, low freq=0.2, high freq=0.0)
- Auto-score via `auto_score` flag on create (routes through scorer with pattern store)
- `GET /v1/host/surprises/unresolved` for high-score unresolved surprises

### Integration
- Context assembly: surprises section at priority 75 (between affect 80 and shared facts 70)
- Autonomy: `surprise_accumulation` condition (30min interval, fires when 5+ unresolved in 1h)
- Events: `pattern.created`, `pattern.extracted`, `surprise.high`, `surprise.accumulation`
- TypeScript client + types + config + cache channels
- 38 new unit tests (158 total)

## 0.4.0 (2026-04-22)

Theory of Mind v0.1: affect tracking and shared facts.

### Affect Tracking
- AffectStore: per-contact emotional valence (-1.0 to 1.0) and arousal (0.0 to 1.0)
- Exponential decay toward neutral (5% per hour, configurable)
- Trend detection: improving, declining, stable
- Negative spike detection (valence <= -0.5)
- Sustained decline detection (3+ events with declining trend)
- Context assembly injection: Emotional Context section (priority 80)
- Autonomy: affect_decline check every 30min
- Events: affect.event_created, affect.negative_spike, affect.sustained_decline
- API: POST /affect/events, GET /affect/state/{id}, GET /affect/history/{id}, DELETE /affect/events/{id}

### Shared Facts
- SharedFactsStore: what the agent believes each contact knows
- Fact categories: told_by_contact, told_to_contact, shared_context, inferred
- Confidence scores (0.0-1.0), TTL expiry, expired fact purging
- Context assembly injection: Shared Knowledge section (priority 70)
- Event: mind.fact_created
- API: POST /mind/facts, GET /mind/facts, GET /mind/facts/{id}, PATCH /mind/facts/{id}, DELETE /mind/facts/{id}

## 0.3.0 (2026-04-21)

Cognition substrate, commitment tracking, LLM compression tier 3, and native tool fixes.

### Cognition Substrate
- Commitment Store: SQLite-backed CRUD, status transitions (pending/fulfilled/cancelled/broken), overdue detection, delete guard (cancel first)
- Context Assembly: Pending Commitments section (priority 72) injected per contact
- Cognition Prompt + Trigger: `POST /v1/host/cognition/trigger`, throttle, `cognition.requested` event
- Trigger Pipeline: turn sync + signal ingest auto-fire cognition triggers (non-blocking)
- Config: `COLONY_COGNITION_ENABLED` (default false), `COLONY_COGNITION_MODEL`, `COLONY_COGNITION_THROTTLE_SECONDS` (default 30)
- Config: `COLONY_COMMITMENTS_ENABLED` (default true), `COLONY_COMMITMENT_CHECK_INTERVAL_MINUTES` (default 30)

### LLM Compression Tier 3
- `compress_sections_with_llm()` async wrapper for aggressive mode
- Falls back to sync tight-truncation on any LLM error
- Automatically used when `_llm_router` is wired and mode is aggressive

### DIGEST Delivery Channel
- `build_digest_bundle()`, `consume_digest()`, `flush_digests_to_gateway()`
- Scheduled daily via autonomy scheduler (configurable interval)
- Config: `COLONY_DIGEST_HEADER`, `COLONY_DIGEST_INTERVAL_SECONDS` (default 86400)

### LLM Entity Extractor
- Fallback for ExtractionPipeline when format extractors return nothing
- Bounded input (12K chars) and output (1024 tokens), JSON-only parsing
- Graceful degradation: returns empty list on any failure

### Fixes
- Native tool registration: register `.execute` methods instead of class instances (critical bug, tools were uncallable)
- ToolExecutor.get_definitions: only advertise tools with registered handlers
- execute_batch: JSON-serialize dict/list results instead of str()
- list_research endpoint: call pipeline instead of returning empty list
- Duplicate `set_commitment_store` import removed
- `.gitignore`: added `sidecar/events/`

## 0.2.0 (2026-04-21)

Security hardening, event journal, and adaptive context compression.

### Security
- Auth: `hmac.compare_digest` for API key checks (timing-attack resistant)
- `/v1/host/configure` blocked in dev mode (no COLONY_API_KEY)
- Body size limit middleware (10MB default, configurable)
- WebSocket frame size cap (1MB default)
- Subprocess-isolated skill sandbox with `setrlimit` guards (mem/CPU/fds/nproc)
- AST scanner: ESC001 (dunder escape chains), ESC002 (dynamic getattr/setattr)
- Rate limiter: SQLite-persisted delivery counts, crashloop-safe
- PII: hashed contact data in logs, no raw PII in error messages
- Neo4j: property allowlist on `update_person`, generated per-install password
- Docker Compose: requires `NEO4J_PASSWORD`, no default fallback

### Event Journal + Replay
- Append-only file-per-event journal with atomic writes and SHA-256 checksums
- `GET /v1/host/events/replay?since=&limit=&types=` endpoint
- WebSocket reconnect with `lastEventId` for replaying missed events
- Plugin tracks `lastEventTimestamp` across reconnects
- Bounded retention (default 500 events, configurable)

### Adaptive Context Compression
- Three modes: off (default), conservative, balanced, aggressive
- Tier 1: Drop low-relevance sections (query-aware F1 scoring)
- Tier 2: Sentence-boundary-aware truncation
- Tier 3: Tight truncation (LLM summarization placeholder for future)
- Per-request override via API field, plugin config setting

## 0.1.0 (2026-04-16)

First release with all adapter shapes matching the real OpenClaw SDK contracts.

### Adapters

- **MemoryPluginCapability** — `promptBuilder` (auto-inject instructions with citation hints) + `MemoryPluginRuntime` (`ColonyMemorySearchManager` backed by `/v1/host/memory/*` endpoints; `search`, `readFile`, `status`, `sync`, `probeEmbeddingAvailability`, `probeVectorAvailability`). Gated by `ownMemoryCapability` config flag (exclusive slot).
- **MemoryEmbeddingProviderAdapter** — `create()` factory with explicit `{provider: null}` when sidecar has no embedder. `embedQuery`/`embedBatch` delegate to `/v1/host/memory/embed` with 64-input chunking. Errors propagate (no silent zero-vectors).
- **ContextEngine** — `info` + `ingest` (no-op) + `assemble` (calls `/v1/host/context/assemble`, folds sections into `systemPromptAddition`) + `compact` (delegated to OpenClaw runtime via `delegateCompactionToRuntime`). `ownsCompaction: false`.
- **AgentHarness** — `supports()` with 3-layer gate (config flag + runtime match + capability probe), `runAttempt()` that never throws (always returns shaped `EmbeddedRunAttemptResult` with `promptError` on failure for harness-fallback routing), `reset` + `dispose` hooks. Currently 501 because reasoning endpoint isn't wired yet (Stage B).
- **Safety hook** (`message_sending`) — fail-closed by default (`failSafetyClosed: true`). Per-chunk safety with capability gating. Never throws out of the handler.
- **Post-turn hook** (`reply_dispatch`) — fire-and-forget observer via `Promise.allSettled`. Sends `signals/ingest` + `turns/sync` concurrently. Never takes over dispatch.
- **Events lifecycle service** — WebSocket subscriber with first-message auth, diagnostic `summarizeHostEvent` logging, error boundary per frame.

### Infrastructure

- `ColonySidecarClient` — typed HTTP/WS client with one method per `/v1/host/*` endpoint, AbortSignal support on `reasoningTurn`, first-message WebSocket auth.
- `capabilityProbe` — single-flight lazy probe with `has()`, `hasProbedSuccessfully()`, `snapshot()` (synchronous accessor for `supports()`), auto-reset on failure.
- `withDegradation` — shared error taxonomy (501/5xx → fallback, 4xx → re-throw, network → fallback).
- `ColonyApiError`, `ColonyEmbedUnavailableError` — typed errors.
- `summarizeHostEvent` — diagnostic log formatter with safe default (no payload leak on unknown types).
- Zod-validated config schema with `failSafetyClosed`, `ownMemoryCapability`, `ownReasoningLoop` flags.

### Tests

- 116 unit tests across 8 test files.
- 13 integration tests against a live colony-core sidecar (gated by `COLONY_SMOKE_URL` env var).

### Type safety

- Real `OpenClawPluginApi` imported from `openclaw/plugin-sdk/plugin-entry` (not a structural stub).
- Zero `@ts-expect-error` markers — every `register*` / `on` call type-checks against the SDK.
- SDK types derived via `Parameters<...>` / `Awaited<ReturnType<...>>` where public exports aren't available.

## 0.0.1 (2026-04-14)

Initial scaffold. Adapter shapes did not match the real SDK contracts. Superseded by 0.1.0.

## 0.1.1 (2026-04-21)

### Added
- Context engine slot: `plugins.slots.contextEngine = "colony"` auto-configured by setup wizard and documented in README
- Setup wizard now starts the sidecar, verifies health, checks LLM credentials, offers gateway restart, and runs `colony doctor`
- Node.js/npm guard in wizard — warns if missing before attempting plugin build
- Full identity awareness through Colony's context engine (colony_id, node_id, trust_tier, Genesis status)
- 22 E2E integration tests covering all subsystems (30 passed, 1 skipped in latest run)

### Changed
- Naming cleanup: "safety" → "response gate" / "content classifier" across codebase
- Setup wizard renumbered: 11 steps (was 10), now includes start + verify + doctor
- README updated to reflect hardened wizard flow — one `colony init` and you're done

### Fixed
- 25+ API router bugfixes (method names, constructors, type coercion, sync/await mismatches)
- VectorStore wiring: explicit `connect(dimensions)` + `ensure_collections()` after graph init
- GoalStore persistence: `:memory:` → `colony-goals.db`
- ResponseGate L1: passes when no session context (direct API calls)
- API key auth middleware added (was completely missing)

## 0.1.2 (2026-04-21)

### Changed
- Renamed PyPI package from `colony-sidecar` to `colonyai` for naming consistency across all registries
- Updated README and CONTRIBUTING to reflect `pip install colonyai`

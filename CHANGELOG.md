# Changelog

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

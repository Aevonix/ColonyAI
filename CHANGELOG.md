# Changelog

## 0.14.0 (2026-05-22)

Session context architecture — cross-session state bridge for agent continuity.

### Added
- **Session Report API** (`/v1/host/session-report`):
  - `POST /session-report` — store session summary from agent (start, end, message counts, sentiment, context notes, token usage)
  - `SessionReportStore` — in-memory FIFO (default 20 reports/contact), timezone-aware `get_recent(hours)` filtering
- **Context Digest API** (`/v1/host/context-digest`):
  - `GET /context-digest` — unified state snapshot combining pending initiatives, system health, and recent session history
  - `AgentSnapshotSystemState` schema — autonomy mode, running status, last tick age, stale flags
  - `ContextDigestSessionReport` schema — lightweight session summaries for agent context window
- **`proactive_delivery_enabled` config flag** — gates all `push_initiative()` calls; defaults to `False` for backward compatibility
  - Env var: `COLONY_PROACTIVE_DELIVERY_ENABLED`
- **`last_agent_outreach_at` telemetry field** — renamed from `last_aeva_outreach_at` for generic agent support

### Changed
- **De-personalized codebase** — all owner references changed to generic "owner"; agent references changed to generic "agent"
- **DRY initiative mapping** — extracted `_map_initiative_to_schema()` module-level helper shared by agent-snapshot and context-digest endpoints

### Fixed
- Timezone safety in session report ingestion — `_parse_iso()` helper forces UTC on naive ISO strings
- `SessionReportStore.get_recent()` defensive tzinfo fallback before cutoff comparison
- Query parameter bounds on context-digest: `hours` (1–168), `initiative_limit` (1–100)

## 0.13.0 (2026-05-21)

Aeva heartbeat and agent snapshot endpoints. Colony exposes state; Aeva decides when to communicate.

### Added
- **Agent Snapshot API** (`/v1/host/agent-snapshot`):
  - `GET /agent-snapshot` — comprehensive Colony state snapshot for Aeva evaluation
    - Telemetry with silence hours and stale flags
    - Pending initiatives (top 20), recently completed (top 10), failed (top 10)
    - Autonomy mode, running status, last tick age
    - Computed flags: `high_priority_pending`, `failed_initiatives`, `long_initiative_silence`, `stale_autonomy_loop`
  - `POST /agent-snapshot/record-outreach` — record Aeva proactive outreach timestamp
- **TelemetryStore** — `last_aeva_outreach_at` field with `to_dict()` serialization
- **Pydantic schemas** — `AgentSnapshotInitiative`, `AgentSnapshotResponse`, `RecordOutreachRequest`, `RecordOutreachResponse`
- **Tests** — `tests/test_agent_snapshot.py` with 6 unit tests (empty state, initiatives, stale tick, silence flags, outreach recording, round-trip)

### Changed
- Replaces `OwnerCheckInTask` (removed in v0.12.1) with a state-exposure model
- Colony never messages the owner directly; Aeva evaluates snapshot and decides on outreach

### Fixed
- `silence_hours` null handling in flag computation (`None > 4` TypeError on fresh telemetry)
- `record_outreach` returns actual touched timestamp instead of stale `now.isoformat()`

## 0.12.0 (2026-05-21)

Agent work queue v0.13.0 — distributed job scheduling for autonomous agent execution.

### Added
- **Task Queue API** (`/v1/host/queue`) — 14 endpoints for job lifecycle:
  - Worker registration, heartbeat, deregistration
  - Job post, claim, start, complete, fail, release, heartbeat
  - Pending/completed listing, queue stats, digest generation
- **SQLite-backed persistent queue** with WAL mode, atomic claims, retry logic, job audit trail
- **Aeva cron worker** (`scripts/aeva_worker.py`) — claims and executes `AGENT_ACTION` jobs every 5 min
  - Safety: skips destructive actions when owner is in active session
  - Handles `agent_check_repo_status`, `agent_investigate_subsystem`, `agent_cleanup_orphans`
  - Graceful shutdown with SIGTERM/SIGINT deregistration
- **Digest script** (`scripts/digest.py`) — generates Colony Digest initiative from completed/failed jobs
- **Loop integration** — `_post_agent_action_to_queue()` routes `AGENT_ACTION` initiatives to task queue instead of delivery bridge
  - Destructive actions posted as `BLOCKED` with approval fallback to delivery bridge
  - Non-destructive actions posted as `QUEUED` for immediate worker claiming
- **Initiative engine** — `_generate_agent_action_initiatives()` with 4h cooldown and dedup keys
- **Provider schema** — `colony_claim_task` tool exposed to LLM with `worker_id` and `capabilities` params
- **`job_id` field** on Initiative model + store updatable column for task-queue linkage

### Fixed
- **11 runtime bugs** from spec review:
  - Digest payload used invalid `initiative_type` and float priority
  - Worker registration sent dict for capabilities instead of `List[str]`
  - Worker claim used `"worker_id"` key instead of `"node_id"`
  - Worker crashed on empty queue (null JSON response)
  - `worker_heartbeat` called non-existent `queue.worker_heartbeat()`
  - `queue_stats` called non-existent `queue.stats()`
  - `get_digest_jobs` referenced non-existent `completed_at` SQL column
  - Missing `/start` and `/release` endpoints
  - `colony_claim_task` tool schema had empty properties
  - Skipped jobs stayed `CLAIMED` instead of returning to `QUEUED`
  - `repo_status` initiative generated every tick (no cooldown)

## 0.11.1 (2026-05-17)

Self-initiative execution fix and new initiative types.

### Fixed
- **Critical (4 bugs shipped in v0.11.0):**
  - `execute_initiative()` skill name mapping was a no-op (`.replace("_", "_")`) — now uses proper `_SKILL_NAME_MAP`
  - `execute_initiative()` never passed `entity_type` to `InitiativeExecutionContext` — now extracted from `trigger_data`
  - `AutonomyLoop._phase_execute()` bypassed execution entirely — now auto-executes self-initiatives via `engine.execute_initiative()` before pushing to delivery; auto-fixed initiatives skip delivery
  - `data_quality` and `operational_hygiene` skills called non-existent `.publish()` on EventBus — rewritten to use `.emit()`
- **Dependency injection** — `ToolExecutor` now accepts `graph_client` via constructor, eliminating circular imports between `reasoning/` and `api/routers/`

### Added
- **New graph schema types**: `Concept` and `Preference` nodes with full fields
- **Extended schema**: `Capability` gets `status`, `failure_count`, `last_failure_at`; `Pattern` gets `pattern_type`, `trigger`, `action`, `recurrence_count`, `last_triggered_at`, `is_active`
- **New context loaders**: `_load_capability_gaps()`, `_load_knowledge_gaps()`, `_load_behavioral_patterns()` — wired into `InitiativeEngine._load_graph_context()`
- **New initiative generators**: `_generate_capability_gap_initiatives()`, `_generate_knowledge_acquisition_initiatives()`, `_generate_behavioral_correction_initiatives()`
- **New executor skills**: `CapabilityGapSkill`, `KnowledgeAcquisitionSkill` (proposal-only), `BehavioralCorrectionSkill` (proposal-only, stores preferences to graph)
- **`trigger_data` field** added to `Initiative` dataclass so context flows end-to-end from generator → execution
- **`_build_initiative_context()`** extended for `capability_gap`, `knowledge_acquisition`, `behavioral_correction` types

## 0.8.3 (2026-05-15)

Silence-triggered owner check-in and telemetry fix.

### Added
- **Owner Check-In scheduler task** — detects when the autonomy loop has not generated an initiative for a configurable period (default: 1 hour), then emits a `proactive_message` initiative asking the owner if anything is needed
  - `OwnerCheckInTask` with file-backed persistence (`~/.colony/data/autonomy_checkin.json`) so state survives restarts
  - Owner resolution: config override (`COLONY_OWNER_CONTACT_ID`) → identity manager → highest-scored non-stranger contact
  - Safety guards: quiet hours (23:00–07:00), cooldown (default 4h), disabled if telemetry missing
  - Configurable via `COLONY_OWNER_CHECK_IN_ENABLED`, `COLONY_OWNER_CHECK_IN_SILENT_HOURS`, `COLONY_OWNER_CHECK_IN_COOLDOWN_HOURS`
  - Runs as a scheduler task independent of the autonomy loop tick interval

### Fixed
- **`AutonomyLoop._phase_execute()`** now touches `TelemetryStore.last_initiative_at` when initiatives are successfully pushed, fixing `silence_hours("initiative")` which previously reported infinite silence even during active loop operation

## 0.7.24 (2026-05-15)

Anti-spam initiative delivery and autonomy pipeline spec.

### Fixed
- **Initiative dedup reactivation** — only FAILED initiatives are reactivated on duplicate `dedup_key`. Completed and cancelled initiatives stay terminal, eliminating infinite-loop follow-up spam.

### Added
- **`docs/autonomy-pipeline-spec.md`** — full spec for Colony-Hermes autonomy without Hermes source changes.
- **`plugins/hermes-plugin/examples/colony-initiative-poller.py`** — production poller with `dedup_key` tracking, `delivery_context` injection, and env-var configuration.
- **`plugins/hermes-plugin/examples/hook-handler.py`** — example Hermes hook handler with nested payload support.


# Changelog

## 0.7.22 (2026-05-14)

Initiative dedup fix and Hermes turns/sync telemetry.

### Fixed
- **InitiativeStore.create()** now reactivates failed/completed/cancelled initiatives when a duplicate `dedup_key` is submitted, instead of crashing on the SQLite UNIQUE constraint.
- **Hermes plugin websockets 15 compat** — `extra_headers` → `additional_headers`.
- **Autonomy follow-up generator** now handles `priority=None` gracefully.

### Added
- **ColonyClient.sync_turn()** — POSTs session summaries to `/v1/host/turns/sync`.
- **`agent:start` hook** in the Hermes plugin captures `session_id` for cross-turn state.
- **`on_session_end` hook** extracts last user/assistant messages and syncs them to Colony automatically.
- Zero Hermes core patches required — all telemetry is plugin-side.

## 0.7.21 (2026-05-12)

Bug-fix release: initiative delivery via WebSocket and plugin apiKey compatibility.

### Fixed
- **Autonomy loop** now broadcasts `initiative` events via WebSocket in addition to HTTP push, ensuring Hermes subscribers receive them in real time.
- **ProactiveDeliveryBridge** falls back to WebSocket broadcast when the gateway's `/internal/initiative` endpoint is unreachable or returns an error.
- **OpenClaw plugin** auth check now accepts both `apiKey` (manifest) and `api_key` (normalized) for compatibility across OpenClaw versions.


## 0.7.20 (2026-05-11)

Qwen3 reranker token config fallback.

### Fixed
- **NativeMLXRerankerProvider** now falls back to `1_LogitScore/config.json` when token IDs are not found in `config_sentence_transformers.json`
  - Qwen3 rerankers (e.g. `Qwen/Qwen3-Reranker-0.6B`, `Qwen/Qwen3-Reranker-8B`) store `true_token_id` and `false_token_id` in a separate config file
  - Without this fallback the provider silently used raw max-logit scoring, producing incorrect relevance scores

## 0.7.19 (2026-05-11)

Native MLX embedding and reranker providers for Apple Silicon.

### Added
- **NativeMLXEmbeddingProvider** — true Apple Silicon MLX support via `mlx-embeddings`
  - Loads models directly into MLX arrays, bypassing PyTorch MPS overhead
  - Supports original HuggingFace models (on-the-fly conversion) and pre-converted `mlx-community` checkpoints
  - Recommended models: `Qwen/Qwen3-Embedding-8B` (4096 dims), `BAAI/bge-m3` (1024 dims)
- **NativeMLXRerankerProvider** — native MLX CrossEncoder via `mlx-lm`
  - Extracts true/false token logits for relevance scoring
  - Supports `Qwen/Qwen3-Reranker-8B` and `BAAI/bge-reranker-v2-m3`
- **Hardware tier matrix** — new `native-mlx-high` (64GB+) and `native-mlx-balanced` (32GB+) tiers
- **Auto-detection** — server and wizard now prefer `native_mlx` when `mlx-embeddings` is installed
- **Setup wizard** — supports `native_mlx` provider selection and model pre-download via MLX loaders

### Changed
- Legacy `MLXEmbeddingProvider` and `MLXRerankerProvider` docstrings clarified: they use PyTorch MPS, not true MLX

## 0.7.18 (2026-05-11)

Hermes integration suite, autonomy bridge, and initiative engine hardening.

### Added
- **Colony-Hermes integration suite** — full plugin for Hermes agent context injection
  - Graph memory MCP server with Neo4j-backed entity/relationship queries
  - Contact reminders via Hermes todo system with neglected-contact detection
  - Context assembly endpoint for agent prompt enrichment
  - Setup wizard (`colony setup --hermes`) for one-command installation
- **Colony autonomy bridge** — Hermes-native initiative tools
  - `initiatives_list` tool for querying pending initiatives
  - `initiative_acknowledge` / `initiative_complete` / `initiative_snooze` lifecycle tools
  - `autonomy_cycle` tool for triggering reactive autonomy ticks
- **Initiative engine hardening**
  - Name deduplication for Person nodes (first + last → canonical full name)
  - Junk contact filtering — skips nodes missing both name and email
  - Graph cleanup — removes stale `:HAS_CONTACT` relationships for non-Person nodes
  - `entity_id` and `dedup_key` fields on health/scheduling initiatives

### Fixed
- **`/autonomy/cycle` reactive mode** — now runs `_tick()` directly instead of scheduling a background task (#23)
- **Schema-adaptive graph queries** — handles `lastCommunication` vs `last_interaction` property variants gracefully

## 0.7.17 (2026-05-10)

MLX reranker fix and new `/memory/rerank` endpoint.

### Added
- **POST /v1/host/memory/rerank** — rerank documents by relevance to a query
  - Returns ranked results with `index`, `score`, `text`
  - Supports up to 256 documents per request
  - Returns 501 if reranker not initialized

### Fixed
- **MLX reranker** — same PyTorch MPS deadlock fix as embeddings (Issue #17)
  - `MLXRerankerProvider.warmup()` now runs synchronously in main thread
- **Reranker initialization** — `make_reranker_provider(spec=None)` returned None
  - Now directly instantiates the correct provider class based on `hw.gpu_type`
- **Reranker warmup** — was never called during lifespan startup, now explicitly awaited
- **Health endpoint** — now exposes `rerank` capability when reranker is wired

## 0.7.16 (2026-05-10)

MLX embedding provider fix and harrier model deprecation.

### Fixed
- **Critical**: MLX embedding provider no longer hangs on Apple Silicon during startup (#17)
  - Root cause: PyTorch MPS backend deadlocked when model initialization ran in `asyncio.run_in_executor()` during FastAPI lifespan startup
  - Fix: `MLXEmbeddingProvider.warmup()` now loads models synchronously in the main thread
  - Startup is not serving requests yet, so brief blocking is acceptable

### Changed
- **Deprecated**: Removed non-functional `microsoft/harrier-oss-v1-27b` model from tiers 5 and 6
  - Replaced with validated `Qwen/Qwen3-Embedding-8B` (8B params, 4096 dims)
  - Affects 128GB+ RAM systems (Mac Studio Ultra, DGX Spark, servers)
- Setup wizard now warns Apple Silicon users about MLX provider and directs them to CPU provider for stability

## 0.7.14 (2026-05-07)

Initiative engine graph context loading and comprehensive bug fixes.

### Added
- `InitiativeEngine._load_graph_context()` — automatic graph + mind model queries before generation
- Graph loaders: `_load_blocked_goals()`, `_load_neglected_contacts()`, `_load_health_trends()`, `_load_scheduling_opportunities()`, `_load_pending_signals()`, `_load_pending_research_tasks()`
- `InitiativeConfig` dataclass with env var loading (`COLONY_INITIATIVE_*`)
- 10-second graph context cache to avoid redundant queries within same tick
- `clear_context()` resets `_last_graph_load` (Bug 37)
- Priority blending: graph priority (40%) + time-based priority (60%) for follow-ups (Bug 20)
- `entity_id` and `dedup_key` for health/scheduling initiatives (Bugs 44, 45)
- `max_initiatives` parameter to limit output (default 20, Bug 43)
- In-memory initiative list with 1000-item cap (Bug 36)
- 38 comprehensive unit tests for initiative generation
- Environment variables: `COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS`, `COLONY_INITIATIVE_GOAL_BLOCK_DAYS`, `COLONY_INITIATIVE_HEALTH_THRESHOLD`, `COLONY_INITIATIVE_GAP_THRESHOLD`, `COLONY_INITIATIVE_RESEARCH_AGE_DAYS`, `COLONY_INITIATIVE_SIGNAL_THRESHOLD`

### Fixed
- **Critical**: `mark_initiative_generated()` now called for ALL initiatives inside persistence loop (Bug 11)
- **Critical**: Research tasks use actual age from `created_at`, not threshold days (Bug 12)
- **Critical**: `complete()` uses `entity_id` (goal ID) not initiative ID for goal store (Bug 47)
- **Critical**: Neo4j `DateTime` objects handled via `_parse_neo4j_datetime()` (Bugs 50, 51)
- **Critical**: `created_at` uses timezone-aware `datetime.now(timezone.utc)` (Bug 38)
- Negative days prevented with `max(0, ...)` (Bug 13)
- NULL `last_interaction` gets 2× threshold days (Bug 14)
- `acknowledge()` removes from in-memory list (Bug 22)
- Generators run in parallel with `asyncio.gather()` (Bug 33)
- Signal loading separated from scheduling check (Bug 40)
- `get_active()` only falls back on exception, not empty result (Bug 54)
- Store validates priority range [0, 1] (Bug 26)
- `SubsystemRegistry.anomalies` uses shared event bus (Bug 41)
- Parameter validation in `generate()` (Bugs 57, 58)
- Env var parsing with fallback on invalid values (Bug 59)

### Changed
- Exception handling: specific types for connection vs validation vs unexpected errors
- `clear_context(context_type)` preserves `_last_graph_load` (only full clear resets)

## 0.7.10 (2026-04-27)

Initiative deduplication and LLM feedback loop.

### Added
- `last_initiative_at`, `snoozed_until`, `snooze_count`, `dismissal_reason` fields on Goal model
- `GoalStore.complete_task()`, `snooze_task()`, `dismiss_task()`, `get_active_tasks()`, `mark_initiative_generated()`
- Snooze fatigue: auto-dismiss after 3 snoozes
- Initiative engine dedup via GoalStore cooldown (no in-memory state, persists across restarts)
- MCP tools: `colony_task_complete`, `colony_task_snooze`, `colony_task_dismiss`, `colony_initiative_feedback`
- API endpoints: `/tasks/{id}/complete`, `/tasks/{id}/snooze`, `/tasks/{id}/dismiss`, `/initiatives/{id}/respond`
- Native tool definitions for task management in `tools/definitions.py`
- `InitiativeConfig` dataclass with configurable cooldowns
- `entity_type` field in initiative payload
- Action hints in `formatInitiativeText()` for LLM task management
- Environment variables: `COLONY_INITIATIVE_COOLDOWN_TASKS` (default 12h), `COLONY_INITIATIVE_COOLDOWN_CONTACTS` (default 72h)

### Changed
- `_feed_pending_tasks()` now uses `get_active_tasks()` with cooldown awareness
- Initiative generation accepts `cooldown_tasks` and `cooldown_contacts` parameters

## 0.6.21 (2026-04-24)

Fixed port conflict handling in foreground mode.

### Fixed
- Foreground mode (`colony start`) now checks if port is in use before starting
- Exits with error if port occupied, with helpful message
- `--force` flag works in both foreground and daemon modes to kill existing process

## 0.6.20 (2026-04-24)

Harness integration refactor with new CLI flags.

### Changed
- New CLI flags for harness configuration:
  - `--mcp-harnesses` for coding harnesses (claude-code, codex, crush, opencode)
  - `--agent-harness` for agent harnesses (openclaw, hermes)
  - `--no-harness` for standalone mode
- `--host-framework` deprecated but still works for backward compatibility
- Step 3 renamed from "Host framework" to "Harness integration"
- Separate detection and setup for coding harnesses vs agent harnesses
- Standalone mode is now explicit and first-class
- Shows install instructions when OpenClaw is requested but not installed
- Detects Node.js stability (version manager vs system install)

### Added
- `_detect_coding_harnesses()` - detect MCP-capable coding harnesses
- `_detect_agent_harnesses()` - detect agent harnesses (OpenClaw, Hermes)
- `_check_nodejs_stability()` - check if Node.js is system-wide or version manager
- `_setup_mcp_harnesses()` - configure multiple MCP harnesses
- `_setup_agent_harness()` - configure agent harness plugin
- `_show_openclaw_install_instructions()` - platform-specific install guide

### Fixed
- Colony can now run completely standalone with no harness
- Better guidance when harness not installed

## 0.6.19 (2026-04-23)

Fixed crash in wizard plugin setup.

### Fixed
- `non_interactive` parameter now passed to `_configure_openclaw_plugin()`
- Prevents `NameError: name 'non_interactive' is not defined` in non-interactive mode

## 0.6.18 (2026-04-23)

Wizard now uses `openclaw plugins install` for proper plugin registration.

### Changed
- Use `openclaw plugins install @aevonix/colonyai` instead of `npm install -g`
- Check if plugin is already installed before reinstalling
- Prompt to restart gateway after plugin install
- Better error messages for permission/network failures

## 0.6.17 (2026-04-23)

Fixed missing npm dependency.

### Fixed
- Added `@sinclair/typebox` dependency (used by tool-registrar)

## 0.6.16 (2026-04-23)

Added OpenClaw plugin manifest for native plugin installation.

### Added
- `openclaw.plugin.json` manifest for `openclaw plugins install` support
- Declares contextEngine and memory contracts
- Config schema with sidecarUrl, apiKey, hostId settings

## 0.6.15 (2026-04-23)

Improved OpenClaw plugin installation with better error handling.

### Fixed
- Check Node.js version before npm install (requires v22.16+)
- Retry npm install with `sudo` on EACCES permission errors
- Clear guidance when Node version is too old
- Better error messages and next steps

## 0.6.14 (2026-04-23)

Fix: OpenClaw plugin auto-install via npm.

### Fixed
- Wizard now checks if `@aevonix/colonyai` is installed globally
- Auto-installs via `npm install -g @aevonix/colonyai` if missing
- Better error messages when config settings fail

## 0.6.13 (2026-04-23)

Neo4j health check system with auto-recovery.

### New
- `_neo4j_health_check()`: Connect + auth + query verification
- `_neo4j_poll_health()`: Poll with timeout and progress messages
- `COLONY_NEO4J_STARTUP_TIMEOUT` env var (default 30s)

### Fixed
- Neo4j now verified healthy before sidecar accepts requests
- Auto-restart on failed health check for running containers
- Clear error messages with actionable steps on failure
- No more arbitrary `sleep(3)` — waits exactly as long as needed

### Behavior
- Cold start → create container → poll with timeout
- Warm start (stopped) → start → poll with timeout
- Hot start (running) → quick check → restart + poll on failure
- All paths degrade gracefully with clear user guidance

## 0.6.12 (2026-04-23)

Fix: Neo4j auto-start now works in foreground mode.

### Fixed
- Neo4j check moved before detach decision (was only in daemon mode)
- Works for both `colony start` and `colony start -d`

## 0.6.11 (2026-04-23)

Fix: Neo4j auto-start and validate EOF.

### Fixed
- `colony start` now checks and starts Neo4j container if needed
- `colony validate` handles EOF gracefully with helpful message
- Neo4j container persisted across sidecar restarts

## 0.6.10 (2026-04-23)

Fix: `--tier` CLI arg now correctly skips interactive tier selection.

### Fixed
- Tier CLI arg in non-interactive mode now properly skips the tier selection UI
- Tier selection UI is now inside the else block for interactive mode

## 0.6.9 (2026-04-23)

Wizard fixes: Neo4j docker-run and multimodal EOF.

### Fixed
- Neo4j startup now uses `docker run` instead of docker-compose.yml
- Multimodal prompt now uses `_prompt()` for EOF handling
- OpenClaw config set error handling improved

## 0.6.8 (2026-04-23)

Colony init wizard: non-interactive mode and all piped input issues fixed.

### New
- `--non-interactive` mode for headless/automated setup
- `--host-framework` CLI arg (openclaw, hermes, claude-code, codex, crush, standalone)
- `--contact-name` CLI arg
- `--bind` and `--port` CLI args for network configuration
- `--tier` CLI arg for embedding tier selection (0-7)
- `--neo4j-password` CLI arg
- `--skip-model-download` to defer embedding model download
- `--start` flag to start sidecar after init
- `_check_neo4j_auth()` to detect if Neo4j requires authentication
- `_write_config_yaml()` writes `~/.colony/config.yaml` alongside `.env`

### Fixed
- Issue 1: `_prompt()` returns default on EOF instead of crashing
- Issue 2: Tier selection no longer skipped silently when stdin exhausted
- Issue 3: Config YAML now written to `~/.colony/config.yaml`
- Issue 4: Bind address prompt added (interactive) + CLI args
- Issue 5: Neo4j auth detection skips password prompt when auth disabled
- Issue 6: SQLite DBs now stored in `~/.colony/data/` instead of `~/`
- Issue 8: `--skip-model-download` defers model download to first start

### Example
```bash
colony init --non-interactive \\
  --host-framework claude-code \\
  --contact-name owner \\
  --bind 0.0.0.0 \\
  --port 7777 \\
  --tier 6 \\
  --start
```

## 0.6.7 (2026-04-23)

Second code audit: MCP contract, Hermes integration, runtime crash, and security fixes.

### Fixed
- C1: `colony_get_context` now always sends `incoming_message` (was conditionally omitted → 422)
- C2: `TurnSyncRequest` has `user_message`/`assistant_message` fields; sidecar extracts from raw messages
- C3: YAML harness config uses `${COLONY_API_KEY}` template (was baking raw key to disk)
- C4: Synthesized skills get `ColonyRuntime` handle injected (was crashing with missing arg)
- H1: `cancellation_reason` goes into `metadata` dict (was dropped by Pydantic)
- H2: `context` param type changed to `dict` (was str → 422)
- H3: WebSocket `onEvent` wrapped in try/catch (was unguarded → crash propagation)
- H4: Hermes provider logs WARN on 401/403 (was DEBUG, silent degradation)
- H5: Initiative IDs use UUID (was collision-prone `hash() % 100000`)
- H6: `datetime.now(timezone.utc)` everywhere (was mixing naive/aware → TypeError)
- H7: Sandbox `__import__` wrapped with manifest whitelist enforcement
- M1: `arousal` param defaults to 0.5 in `colony_record_affect`
- M2: `expected` param is optional in `colony_record_surprise`
- M6: `refreshSkillTools` debounced with in-flight promise (was racy)

### New
- `sidecar/colony_sidecar/skills/runtime.py` — `ColonyRuntime` class for synthesized skill tool access
- `allowed_imports` field on `SkillPermissions` for manifest-declared module whitelist

## 0.6.2 (2026-04-23)

Code audit fixes from Claude Code security scan.

### Fixed
- H1: Added missing `set_reranker` import to server.py (was silently failing on reranker config)
- H2: Fixed tautological test assertions in test_sidecar.py
- H3: Implemented `probeVectorAvailability` — now checks embed capability instead of hardcoded false
- H4: Removed dead `lastBoundary` variable in pipeline.ts
- H5: Added metrics tracking for invalid blocks (`blocks_rejected`, `blocks_accepted`, `invalid_signatures`)
- H6: Comprehensive block validation: merkle root check, future timestamp rejection, detailed NACK reasons
- H7: 12 Byzantine-fault tests for Raft consensus
- Medium: Error logging for Raft fire-and-forget message sends via `_spawn_send()` helper

### Tests
- 12 new Byzantine-fault tests in `test_consensus_byzantine.py`

## 0.6.1 (2026-04-22)

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

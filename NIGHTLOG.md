# Overnight Hardening Log — 2026-06-12

**Agent:** Claude (Aeva build agent) · **Branch:** `hardening/overnight-2026-06-12`
**Mandate (Marc, before bed):** "Continue scanning the code and features with deep thorough
evaluation, defining and implementing improvements and system hardening until the morning.
Make everything running in Colony today production-ready so it turns any Hermes deployment
into a useful, functioning, helpful autonomous agent."

**Ground rules I'm holding myself to overnight:**
- All dev work on this branch; commit per logical change; never force-push `main`.
- Live Mac services (gateway/sidecar) only get **verified, reversible** changes; verify health after each.
- No messages to real contacts. "Testing with Aeva" = sidecar API / vLLM endpoint / isolated harnesses.
- Every change gets a test or a concrete verification. No green-by-assumption.
- Faithful reporting: failures recorded with output; skipped steps named.

---

## Timeline

### 02:55 — Provider timeout fix (the proper-fix analysis Marc asked for)
**Question:** do the timeouts I added restrict genuinely long context work?
**Finding (closely analyzed):** Aeva runs **non-streaming** vs vLLM (`streaming.enabled: false` —
deliberate, because the vLLM `Qwen3XMLToolParser` bug only fires on the incremental *streaming*
path; non-streaming parses complete XML in one pass = always clean). Confirmed at
`agent/chat_completion_helpers.py:453`: the non-streaming stale detector compares total
wall-clock since call start (never reset mid-generation) against `stale_timeout`. So
**`stale_timeout` is the total generation budget, not an idle gap.** My interim `stale: 120`
would have killed any turn whose generation exceeded 120s (240s for >100k ctx).
**Measured throughput:** 39 tok/s on the main MiMo-V2.5 endpoint.
**Corrected (grounded in throughput):** `stale_timeout_seconds: 1200` (covers ~47k output
tokens + large-context prefill; catches a true hang in 20 min vs the original 47-min hang),
`request_timeout_seconds: 2400` (httpx backstop). Applied to spark9-v25 + spark1-pro.
Config cache is mtime-keyed → live without a restart. Backup: `config.yaml.bak-fixtimeouts`.
**Deeper conclusion:** the *architecturally* proper fix is streaming (per-chunk idle detection
removes the budget tradeoff). Gated on the vLLM streaming parser bug being truly resolved by
the enhanced chat-template. **TODO tonight: rigorously test streaming cleanliness; flip only if
provably clean + reversible.**

### 03:37 — Baseline established
Test env: fresh venv `/tmp/colony-venv`, `pip install -e ".[dev,mcp,extraction]"`.
Full suite (`pytest tests/ colony_sidecar/`): **1192 passed, 116 skipped, 4 warnings in 18.6s** — green.
Warnings worth tracking: (1) `RuntimeWarning: coroutine never awaited` at
`initiative_engine.py:799` — possible real un-awaited async bug; (2) Pydantic-v2
class-based config deprecation (`agent/models.py:20`); (3) Starlette/httpx testclient deprecation.

### 03:38 — Launched 4 parallel deep audits (read-only)
1. Relationship/scoring subsystem (root-cause the "scoring starved" bug).
2. Initiative engine + datetime correctness (un-awaited coroutine @799, naive/aware mixing).
3. Sidecar robustness (ThreadPoolExecutor-per-call, prefetch race, graceful degradation).
4. Hermes plugin + security baseline (reasoning-probe flap, bearer-auth coverage).

### 03:40–04:05 — Tier-1 fixes (committed, each with tests, suite stays green)
- **`0aa915d` datetime tz-normalization** (`initiative_engine._parse_neo4j_datetime`):
  naive timestamps → `TypeError` at :438/:721 → swallowed → silently dropped ALL
  blocked-goal & pending-research initiatives. Root fix + 7 tests.
- **`1e21682` inferred-deadline/provenance preserved** (`goals/engine.on_message`):
  candidate deadline + confidence + signals were dropped; now carried in goal.context
  (deadline as a hint phrase, not a fake datetime). + 2 tests.
- **`55de040` scoring-starvation fix** (`autonomy/loop._phase_relationships`): scheduled
  phase only wrote dead Neo4j path; now also recomputes SQLite closeness for ALL contacts
  via compute_relationship_score so recency-decay stays live without a turn. Idempotent. + 2 tests.

### 04:05 — Launched streaming-cleanliness probe (background, Aeva's main vLLM 5003)
60 iterations, half with enable_thinking (the parser-bug trigger), tool-heavy prompts.
Detects empty-name / bad-JSON tool-call deltas client-side. Decides whether streaming is
a safe architectural fix for the timeout tradeoff.

### 04:05 — Streaming probe result (decides the timeout architecture)
**60/60 clean, 0 anomalies, 0 errors** (119s) on Aeva's main endpoint, half with
enable_thinking (the parser-bug trigger), tool-heavy prompts. Strong evidence the
enhanced chat-template resolved the vLLM streaming tool-call bug client-side.
**Decision: do NOT flip Aeva to streaming tonight.** It reverses a deliberate
architecture choice on a live system; 60 short-context iters ≠ production load with
MTP + 1M context. Recommend (for Marc's go/no-go): (1) confirm zero "not well-formed"
in the vLLM container log over a load window, (2) large-context + Pro-endpoint probe,
(3) flip streaming.enabled=true with easy rollback + monitor. Streaming would make the
non-streaming total-budget timeout tradeoff disappear (per-chunk idle detection).

### 04:20 — Auth/security hardening (committed `2af46fe`)
- Live deployed memory provider is `plugins/colony-memory/provider.py` (NOT the
  `hermes-memory` copy the audit flagged). The live one already avoids the prefetch
  race (sync, joins the bg thread before reading cache). The audit's race is in a
  NON-deployed copy. **Debt noted:** 3 divergent provider.py copies — consolidate.
- `_guard_bind_auth`: fail closed on non-loopback bind without COLONY_API_KEY.
- `test_auth_coverage`: every HTTP route (>50) rejects unauthenticated requests.

### 04:25 — Full suite after all fixes: **1220 passed, 116 skipped** (was 1192). 0 regressions.
Branch `hardening/overnight-2026-06-12`, 5 commits. NOT yet deployed to the live Mac
sidecar — see deploy assessment below.

### 04:35 — DEPLOYED to live sidecar (verified, reversible)
Live sidecar = editable install from `/Users/kurcide/colony-work/sidecar` @ b387c0a (clean,
== my branch base). Applied the 4 commits via `git am` onto Mac branch
`hardening/overnight-2026-06-12`. Verified in the Mac runtime venv (.colony-venv,
pytest 9.0.3): **89 affected tests pass**. Restarted sidecar (launchctl kickstart;
new pid 4424), health 200, all capabilities intact.
**Live verification:**
- `POST /v1/host/context/assemble` (turn hot path) → **200 OK**, full context assembled.
- New autonomy code running live: log shows `Phase relationships: 42 behavioral score
  updates` at **loop.py:1694** (the new line; old code logged at :1681).
- Scores correct: never-interacted acquaintances at exactly **0.226** =
  0.30·0.42(acquaintance) + 0.20·0.5(neutral) — the compute_relationship_score baseline,
  a value only the new periodic layer can set for last=None contacts. Closeness layer now
  no-ops (already current) = correct idempotent behavior. Marc 0.8646 (inner_circle, active).
- No post-restart errors/tracebacks.
**Rollback:** `git -C ~/colony-work checkout main` + kickstart sidecar.

### 04:40 — Noted live recurring error to chase: `Unclosed client session` (graph client.py:465)
aiohttp/neo4j session not closed — resource leak smell, recurring pre-restart. Investigating.

### 04:55 — Final sync: 5th commit deployed; live turn path 200; live == branch
Briefings shared-pool + docstring commit deployed. Sidecar restarted, health 200,
context/assemble 200, contacts 200, imports OK, no errors. The live sidecar now runs
the full reviewed branch.

---

## ═══ MORNING SUMMARY (read me first) ═══

**Branch:** `hardening/overnight-2026-06-12` (build box `~/repos/ColonyAI` AND Mac
`~/colony-work`, identical). 6 commits on top of `b387c0a`. **All deployed live and
verified.** Full suite **1220 passed / 116 skipped** (was 1192; +28 tests, 0 regressions).

**What changed & why (each tested, each live):**
1. `fix(initiative)` — neo4j datetimes normalized to tz-aware UTC. Naive timestamps
   raised TypeError that was swallowed, silently dropping ALL blocked-goal &
   pending-research initiatives. **This was making whole classes of proactive
   initiative vanish.**
2. `fix(goals)` — inferred-goal deadline + provenance (confidence/signals) preserved in
   goal.context instead of being dropped.
3. `fix(autonomy)` — relationship scoring de-starved: the scheduled phase now recomputes
   the SQLite closeness score (the one everything reads) for ALL contacts, so recency
   decay stays live for contacts without a recent turn. Verified live (loop.py:1694).
4. `harden(security)` — sidecar fails closed on non-loopback bind without COLONY_API_KEY
   (was serving open with only a warning); + auth test over the WHOLE route table.
5. `perf(briefings)` + `docs(goals)` — shared async-bridge pool (was an executor per call);
   corrected docstrings that advertised a non-existent LLM inference pass.

**Provider timeouts (the question you asked):** Aeva is non-streaming vs vLLM, so
`stale_timeout` is the TOTAL generation budget, not an idle gap. Corrected to
`stale_timeout_seconds: 1200` / `request_timeout_seconds: 2400` (grounded in measured
39 tok/s) on spark9-v25 + spark1-pro. The earlier 120/600 would have truncated long turns.

**DECISION FOR YOU — streaming flip (would make the timeout tradeoff disappear):**
60/60 streaming tool-call iterations were clean (half with reasoning, the bug's trigger).
Strong evidence the enhanced template fixed the vLLM streaming parser bug. I did NOT flip
it — it reverses a deliberate choice on the live system. To proceed: confirm zero
"not well-formed" in the vLLM container log over a load window + a large-context/Pro probe,
then set `streaming.enabled: true` with rollback ready.

**Known follow-ups (noted, not done):** `Unclosed client session` recurring warning
(aiohttp, GC'd, errors=0 — needs deeper trace); 3 divergent memory `provider.py` copies to
consolidate (live one is `plugins/colony-memory`); the un-awaited-coroutine test warning
(`initiative_engine.py` data-quality mock — cosmetic).

**To merge:** review the branch, `git checkout main && git merge hardening/overnight-2026-06-12`.
**To roll back live:** `git -C ~/colony-work checkout main` + `launchctl kickstart -k gui/$(id -u)/ai.aevonix.colony-sidecar`.

---

## ═══ DEEP-AUDIT BACKLOG (round 2 — found, triaged, mostly NOT auto-fixed) ═══

A second parallel audit (memory / cognition / task-queue+commitments) surfaced these.
I fixed only the clean, safe, testable one (commitments H6, committed `00cf909`/live).
The rest are **architectural or need integration testing — left for your call** with my
assessment. Verified against the live system where noted.

### Fixed tonight from this round
- **Commitments overdue (H6)** ✅ — due_at stored raw (mixed naive/aware) → string-compare
  overdue detection unreliable (promises surfaced late/never). Now normalized to UTC. Live.

### HIGH — recommend, but YOUR architectural call (I did NOT change these)
- **Task-queue: the reclaim/maintenance Scheduler is never started** (`server.py` runs only
  `WorkerNode`). Consequences: BLOCKED jobs whose dep failed never unblock; owner-approval
  BLOCKED jobs never expire (72h `expire_blocked_approvals` has no caller); stuck CLAIMED
  jobs (agent webhook 200'd but never completed) never reclaimed; jobs table grows unbounded.
  **CAUTION — do not just start it:** `abandon_silent_jobs` uses a 60s heartbeat timeout, but
  the cron worker fires-and-exits without heartbeating, so starting the scheduler would
  reclaim *actively-running* agent turns and **double-dispatch** them. Fix the agent_action
  heartbeat/timeout model FIRST (use `timeout_secs`/deadline, not heartbeat, for that job
  type), then start a periodic maintenance tick. `task_queue/queue_manager.py` 652/728/780.
- **Proactive deliveries are in-memory, lost on restart** (`delivery/bridge.py:76`). Queued
  PUSH/IN_SESSION/DIGEST messages the gateway hasn't polled vanish on a sidecar restart
  (and I restarted it several times tonight). Recommend backing `_pending`/`_sent` with SQLite
  (the rate_limiter in the same module already shows the pattern).
- **Memory: distillation & weak-memory pruning never run** (`autonomy/loop.py` 1546/1559/1583
  gate on method names — `consolidate_memories`/`prune_memories`/`distill_memories` — that
  don't exist on `ColonyGraph`; the real names are `prune_weak_memories`/`decay_memories`;
  `MemoryDistiller` is never instantiated). Consolidation still runs via the hourly scheduler,
  but pruning/distillation are dead → unbounded memory growth, no episodic→semantic promotion.
  **CAUTION:** enabling pruning DELETES memories — wire + test carefully, not overnight.
- **Memory: `store_memory` Neo4j+LanceDB write is non-atomic** (`graph/client.py:512`). If the
  LanceDB vector write fails, the memory lives in Neo4j but is unsearchable by vector recall
  (only keyword fallback finds it). No reconciliation/backfill is scheduled. Recommend fail-
  closed (raise, like the embed-failure guard) or a periodic Neo4j→LanceDB backfill.
- **Cognition: `_phase_cognition` discards `MetaLearner.run_cycle()` errors** (`loop.py:1512`).
  Every internal cognition step (CPI, gap detection, strategy adjustment) can fail each cycle
  and the loop logs "cycle complete" at DEBUG. The self-improvement loop can be fully dead
  while reporting healthy. Recommend inspecting `CycleResult.errors` → WARNING + `stats.errors`.

### MEDIUM
- **`Unclosed client session`** (the recurring live warning) = litellm creates an aiohttp
  ClientSession per `acompletion` and never closes it (`router/router.py:259`; no
  `litellm.aclient_session`/close anywhere). Benign (GC'd, errors=0) but leaky. Fix: set a
  shared `litellm.aclient_session` at startup, close on lifespan shutdown.
- **Memory dedup SET hits ALL duplicates** (`graph/client.py:405`) — Cypher applies SET to
  every matched row before ORDER/LIMIT, so corroboration_count/strength (→ effective_confidence)
  inflate across legacy duplicate clusters. Fix: `WITH m ORDER BY created_at ASC LIMIT 1` before
  SET. (Needs Neo4j integration test — didn't change a live query blind.)
- **Memory consolidator merge loses edges** (`consolidator.py`) — re-points only MENTIONS/ABOUT;
  DERIVED_FROM / SUPERSEDES / causal edges dangle after `REMOVE :Memory`.
- **Cognition: weekly jobs retry every tick on failure** (`loop.py` self_reflection/bootstrap
  set their `_last_*` timestamp only AFTER the await succeeds → a failing weekly job hammers the
  LLM every tick) + in-memory timestamps cause a first-tick stampede on every restart.

### LOW / observability honesty
- **Autonomy default is REACTIVE** (`autonomy/config.py:37`) → a fresh deployment's whole
  think-act loop is dormant with only an INFO log. **Live Aeva is fine** (verified: phases run
  → it's proactive), but for "any Hermes deployment" recommend defaulting proactive or a loud
  WARNING + surfacing `mode` in health. **Most relevant to your "turn any deployment into a
  useful agent" goal.**
- **4 scheduler tasks are no-op lambdas** (`server.py` signal_ingest / briefing_generate /
  cpi_track / world_model_prune) — report "ok" forever, do nothing. signal_ingest being a no-op
  may explain part of the behavioral-signal sparsity behind the original scoring complaint.
- `stats.errors` is an unreliable health signal (many failure paths never touch it).
- Consolidation always logs `merged: 0` (`server.py:931` reads `merged_count`; attr is
  `pairs_merged`).

**These are documented, not lost.** The agent IDs for each audit are in the session if you
want me to expand any into a full diff in the morning.

### 04:52 (monitoring tick 1) — no regressions; 1 more safe fix
Regression check: sidecar 200/200, pid stable, 0 new errors, relationship phase clean
(loop.py:1694), gateway alive, 0 new tool errors. Tonight's 6 fixes holding.
Assessed the litellm shared-session fix (Unclosed-session leak) → **declined to deploy
unattended**: touches every LLM call, version-dependent (`litellm.__version__` not even
introspectable), and a wrong move takes Aeva dark while you sleep — bad risk for a benign
(GC'd) warning. Documented instead.
Shipped a safe observability batch instead (commit `1205adf`, deployed → Mac `b2877c4`,
**1227 passed**): (a) `_phase_cognition` now surfaces MetaLearner cycle errors instead of
discarding them (a fully-degraded cognition cycle no longer reports a clean tick);
(b) memory_consolidate scheduler stat reads `pairs_merged` not the non-existent
`merged_count` (was always 0). Both observability-only, no behavior change (stats.errors
gates nothing). Live verify: health/turn 200, consolidation actively merging (merged=18/14/10).

### 05:23 (monitoring tick 2) — stable, no regressions
sidecar 200/200 (pid 5811), 0 new errors, gateway pid 2555 alive, 0 new tool errors, Aeva
quiet. All 7 deployed fixes holding. Remaining backlog items are architectural or hot-path
(task-queue scheduler, memory pruning, litellm session, recall create_task race, weekly-job
timing) — deliberately leaving those for Marc's review rather than changing a live system
unattended. Defaulting to monitoring.

### 05:55 (monitoring tick 3) — stable
sidecar 200/200 (pid 5811, no restart since tick 2), 0 new errors, relationship phase clean
(loop.py:1708 — line moved by the observability commit, confirms latest code live), gateway
pid 2555, 0 new tool errors, Aeva quiet. No action — monitoring.

### 06:27 (monitoring tick 4) — stable
sidecar 200/200 (pid 5811), 0 new errors, gateway pid 2555, 0 new tool errors, Aeva quiet
(Marc still asleep). No action — monitoring.

### 06:58 (monitoring tick 5) — stable; 2 gateway errors are the KNOWN Pro residual
sidecar 200/200 (pid 5811), 0 sidecar errors. Gateway showed 2 new tool errors — confirmed
they are `Job 'Aeva Colony Worker' failed: Model generated invalid tool call`, the known
Pro-parser bug on the cron path (spark1-pro), tracked by the existing toolcall-watch.
NOT a regression from tonight's sidecar work (Main endpoint is clean; only Pro/cron affected).
Aeva quiet (Marc still asleep). No action — monitoring.

### 07:30 (monitoring tick 6) — stable
sidecar 200/200 (pid 5811), 0 sidecar errors, gateway pid 2555, Aeva quiet (Marc still
asleep). No action — monitoring.

### 08:01 (monitoring tick 7) — stable
sidecar 200/200 (pid 5811), 0 sidecar errors, gateway pid 2555, Aeva quiet (Marc still
asleep). No action — monitoring.

### 08:32 (monitoring tick 8) — stable
sidecar 200/200 (pid 5811), 0 sidecar errors, gateway pid 2555, Aeva quiet (Marc still
asleep). No action — monitoring.

### 09:03 — LOOP WOUND DOWN (Marc awake)
Marc is awake and actively in conversation (streaming question). Monitoring mandate
("until morning") fulfilled — no further wakeups scheduled. Final state: sidecar 200/200
(pid 5811), gateway pid 2555, 0 real sidecar errors across the whole night. All 7 fixes
held stable through ~8 monitoring ticks with zero regressions. Branch ready for review/merge.

CORRECTION logged this morning: Aeva almost certainly ALREADY API-streams from vLLM
(agent defaults `_use_streaming=True`; `streaming.enabled:false` only gates *display*
streaming). The non-streaming path is a per-session FALLBACK triggered when a stream call
errors (`_disable_streaming=True` @ chat_completion_helpers.py:2262) — i.e. the vLLM parser
bug can flip Aeva into the blunt-budget non-streaming mode, the likely origin of the long
hangs. Upshot: keeping the streaming path clean (enhanced template) is the real lever; the
1200/2400 timeouts remain a valid backstop for the fallback path.

# ROADMAP: Cognition Program (Seven Capabilities)

Status: PLAN COMMITTED, build not started. This document is the durable source
of truth for the seven-capability cognition program. It is written so a
successor agent can resume from this file alone. Update the Program State
section as phases land.

All capability code is GENERIC and public-repo-safe: env-driven, no identities,
no deployment endpoints. Anything instance-specific (credentials, hosts, plists,
connector endpoints, worker placement, LLM endpoints) is listed in the
"Private deployment layer" section per item and written to the live Mac
locations for the aeva private-repo sync, never committed here.

Every new action path is gated the same way the existing stack is:
DirectiveGuard boundary check (capability-aware: ACT vs OBSERVE), then approval
tiering, behind a shadow/dry-run flag first. Do not disturb the now-live
delivery path or the held directed-action dry_run.

## What already exists (build on these, do not duplicate)

- `directives/` DirectiveManager + DirectiveGuard (tiered ACT/OBSERVE, entity
  scoped, recent-blocks, critical-flag), `Action(kind,...)` contract. Every new
  action calls `registry.directives.check(Action(...))`.
- `proposals/` Proposal artifact + store + `proposal_to_payload` (dedicated
  "proposal" delivery type) routed via `loop._route_reachout_delivery` through
  the guarded (sanitize + staleness + rate + shadow) path.
- `feedback/` TypeFeedbackStore (per-type multiplier, `record`, `multiplier`).
- `directed/` ScopedTask + intake + service (gates -> dispatch(dry_run) ->
  audit -> feedback -> owner report) + `directed/audit.py`.
- `repos/` RepoMirrorManager (read-only mirrors, boundary-gated, repo_* tools,
  registers repos as Project entities).
- `world_model/populator.py` shadow-first entity population; `world_model/`
  store + entities (Person/Company/Project/Product/Location/Event/Concept) +
  EntityResolver + relationships (WM_* types).
- `intelligence/components/initiative_engine.py` ~20 generators + `generate()`
  with dedup/cooldown/cap; `self_directed_thinker.py` (mode off/shadow/live).
- `intelligence/graph/` ColonyGraph (recall, store_memory, decay, distiller),
  epistemic_state on memories; `intelligence/graph/distiller.py` MemoryDistiller.
- `services/initiative_executor.py` InitiativeExecutorService (needs_tool loop,
  boundary gate, repeat-work suppression, resilient run_turn).
- `task_queue/` Job/JobStatus/queue_manager/scheduler; `services/agent_bridge.py`
  + `workers/` (currently FORWARD-only to an external webhook; no local handler).
- `autonomy/registry.py` SubsystemRegistry accessors; `autonomy/loop.py` phased
  tick (proactive), `_phase_execute`, `_route_reachout_delivery`, `_phase_thinking`.
- `api/routers/host.py` global set_*/get_* + REST; `server.py` boot wiring.
- Config precedent: each subsystem gets `COLONY_<X>_MODE` off|shadow|live and
  a state-dir SQLite db (`colony-<x>.db`).

## Dependency-ordered phases

- Phase A (core cognition, sidecar-internal, lowest risk): items 1 + 3 + 4 + 7.
- Phase B (riskiest, server-side enforced): items 5 + 6.
- Phase C (external inputs): item 2, then wire populator + belief maintenance to it.

Within each phase: build -> unit tests green -> deploy to live Mac -> shadow/
gated verify -> per-commit leak self-scan -> push -> live git parity -> update
Program State here. Refresh bundles at each push.

---

## Amendment 1 (2026-07-04, owner-approved): graduated autonomy, the
## self-model as trust engine

Static capability gating and manually-decided readiness are NOT the goal.
The owner wants an agent that acts when its track record supports it, asks
when unsure, and course-corrects automatically, with comprehensive logging as
the accountability layer. Item 4 (self-model) is therefore promoted from
advisory measurement to the CENTRAL TRUST ENGINE. Design principle baked into
every item: default toward action-with-journaling; blocking is reserved for
the immutable floor, owner boundaries, and genuine below-threshold
uncertainty (which becomes ask-first, never silent inaction).

1. Confidence-gated action. Every autonomous action class carries a
   calibrated confidence from real outcomes: CompetenceStore success/failure/
   timeout history, audit verdicts, and owner reactions (TypeFeedbackStore).
   Above threshold: act + journal + report. Below: ASK FIRST, and the
   approval request carries the reasoning and the confidence ("I want to do
   X, confidence 0.62, because Y"). The approval machinery is something the
   agent INVOKES when unsure, not a static wall.
2. Auto-graduation. COLONY_<X>_MODE shadow phases are CALIBRATION phases,
   not waiting rooms. A capability class auto-graduates shadow -> ask_first
   -> act_first as its track record crosses thresholds
   (COLONY_TRUST_* envs, sensible defaults), with an owner NOTIFICATION on
   each graduation ("I am now doing X autonomously; say stop if not"), not a
   permission request. The env mode remains an owner override: off stays
   off; an env set to live is live; shadow means "start in calibration".
3. Circuit breakers. N failures (COLONY_TRUST_BREAKER_FAILURES, default 3)
   within a window (COLONY_TRUST_BREAKER_WINDOW_HOURS, default 24) or ANY
   audit violation auto-demotes that class to ask_first and journals why.
   Course correction is automatic, not post-mortem.
4. Unified action journal. Every autonomous action is logged with reasoning,
   confidence, reversibility class, decision (acted/asked/held) and outcome;
   introspectable via a tool ("what did you do today and why") and the API.
5. One-command pause. An owner utterance like "stop acting" / "pause
   autonomy" becomes an immediate GLOBAL ACT-level boundary through the
   directive system (the kill switch). Lifting it uses the existing staged
   confirmation. Verified end to end.
6. Immutable floor (small and principled: irreversibility x blast radius).
   Owner directives, plus a short hard list that is never self-decidable
   regardless of confidence: money movement, non-recoverable deletion,
   credential/security changes, bulk messaging of third parties. Everything
   else, including individual third-party messages, directed repo actions,
   and delivery volume (the daily cap becomes ADAPTIVE, earned upward with
   track record), moves to the earned model.
7. Directed retrofit. Directed action runs LIVE now: read-only scopes
   auto-approve with journaling; mutating scopes ask with reasoning +
   confidence; act-first is earned per scope class as the audit record
   accumulates.

Category note: engineering rollout of hot-path infrastructure (e.g. the
context.engine activation) is deployment hygiene, stays staged, and is NOT
subject to trust-engine graduation.

---

## Item 1 - GOAL PERSISTENCE (Projects) [Phase A, centerpiece]

Turn one-shot initiatives into sustained multi-tick pursuit.

Design:
- New `projects/` package.
  - `models.Project`: id, title, objective, source (owner/thinker/directive),
    status (planning|active|blocked|completed|abandoned), created/updated,
    entity_ids (world-model links), abandon/complete reason, next_review_at.
  - `models.Step`: id, project_id, ordinal, description, depends_on[step_ids],
    status (pending|active|done|failed|skipped), attempts, result, action_kind
    (analyze|research|directed|deliver|internal), boundary_subject (for gating).
  - `store.ProjectStore` (SQLite `colony-projects.db`): CRUD, list by status,
    steps CRUD, due-for-review query. Survives restarts.
- `planner.py`: `plan_project(objective, context) -> [Step]` via the reasoning
  loop (one LLM planning pass) returning a STRICT JSON step list
  (ordinal, description, depends_on, action_kind). Deterministic re-validation:
  drop steps with unknown action_kind; topological-sort deps; cap N steps.
  LLM proposes, code validates (same discipline as directed intake).
- `engine.ProjectEngine`: pursued by a new autonomy phase `_phase_projects`
  (after `_phase_execute`). Each tick, for active projects: pick the next
  ready step (deps done), BOUNDARY-CHECK it (`Action(kind=step.action_kind,
  text=step.description, target=project subject)`); if blocked -> project
  blocked + boundary flag path. Dispatch the step by kind:
  analyze/research -> InitiativeExecutorService-style reasoning turn (reuse
  the executor's resilient run_turn + tool subset); directed -> create a
  ScopedTask via DirectedActionService (dry_run/approval honored); deliver ->
  proposal via guarded path. Record step result; on failure -> replan the
  remaining steps (bounded replan count). Milestone reports (on step-group or
  status change) via guarded delivery (Proposal type). Outcome -> TypeFeedback
  ("project"). Uses self-model (item 4) to decide pursue vs defer.
- Skill hook (item 3): on non-trivial project/step completion, distill a Skill;
  retrieve relevant skills into the planner/executor prompt.
- Tools: `list_projects`, `project_status(project_id)`, `create_project`
  (owner-directed; boundary + approval gated), `abandon_project(reason)`.
  Registered in tools/definitions + handlers, `registry.project_engine`.
- API: GET /projects, GET /projects/{id}, POST /projects (owner), POST
  /projects/{id}/abandon.

Config flags: `COLONY_PROJECTS_MODE` off|shadow|live (default shadow: plan +
log intended step actions, take no outward/mutating action); step dispatch
still routes through each sub-path's own gate (directed dry_run, delivery
shadow/live). `COLONY_PROJECTS_MAX_STEPS` (default 12), `COLONY_PROJECTS_
REVIEW_SECS`, `COLONY_PROJECTS_MAX_REPLANS` (default 3).

Rollout gate (amended): shadow is the CALIBRATION stage; a sample project
must plan + log a clean step sequence with boundary checks and a
milestone-proposal in shadow, after which the trust engine auto-graduates
project pursuit per Amendment 1 (owner notified on each graduation). Step
sub-paths keep their own gates either way.

Tests: planner validation (unknown kind dropped, dep cycle broken, cap), engine
step selection + dep ordering, boundary-blocked step -> project blocked, replan
on failure bounded, store roundtrip/persistence, milestone-report shadow.

Private deployment layer: none (fully generic).

## Item 3 - COMPOUNDING LEARNING (Skills) [Phase A]

Distill reusable procedure memory from non-trivial successes; post-mortems on
failure.

Design:
- `skills_memory/` package (name avoids clash with existing `skills/` executor
  registry).
  - `models.Skill`: id, title, situation_signature (normalized terms +
    embedding optional), steps[], gotchas[], domain (initiative_type/
    project/directed), source_ref, uses, wins, losses, confidence, created/
    last_used, decayed. `store.SkillStore` (SQLite `colony-skills.db`).
  - `distill.py`: `distill_from_completion(context, transcript) -> Skill|None`.
    Trigger conditions: success AFTER >=1 retry, or a novel diagnosis (executor
    result contains a resolution not seen in recent skills). One reasoning-loop
    pass -> STRICT JSON {title, situation, steps, gotchas}; validate; dedup by
    situation-signature similarity (drop if >0.8 overlap with an existing skill,
    bump its uses instead). Cap total skills (`COLONY_SKILLS_MAX`, default 200);
    evict lowest score = f(confidence, recency, wins-losses).
  - `retrieve.py`: `relevant_skills(situation, k=3)` by signature/keyword (and
    embedding if available) -> compact bullet block for prompts.
  - Failure post-mortem: on terminal failure, `record_failure` updates a
    per-domain strategy note (short text, capped) surfaced in prompts.
- Wiring: InitiativeExecutorService and ProjectEngine prepend a "Relevant past
  procedures" section (retrieve) to the system prompt, and call distill on
  completion. Purely additive to prompts; no new action path (safe, can go
  live directly - it only informs reasoning, never acts).
- Tools: `recall_skills(situation)` (read), optional. Registry:
  `registry.skill_store`.
- API: GET /skills (observability).

Config: `COLONY_SKILLS_ENABLED` (default true - read/inform only),
`COLONY_SKILLS_DISTILL` (default shadow: log the distilled skill without
storing, then live). Distillation costs one LLM call per qualifying completion;
gate cadence so M3 load stays modest (only on retry-success/novel).

Tests: trigger conditions (retry-success yes, first-try no), dedup by
signature, cap/evict, retrieval block format, post-mortem note update.

Private deployment layer: none.

## Item 4 - SELF-MODEL / TRUST ENGINE [Phase A, amended: the centerpiece of
## Amendment 1]

Live competence/calibration per capability domain from real outcomes,
promoted to the central trust engine that grants, gates, and revokes
autonomy per action class.

Design:
- `self_model/` package.
  - `store.CompetenceStore` (SQLite `colony-self-model.db`) keyed by domain
    (initiative_type | project | directed[:scope-class] | research |
    delivery | worker-job-type): counts success/failure/timeout, ewma
    latency, last_outcome_at, PLUS a per-event log (domain, outcome, ts) for
    windowed circuit-breaker queries. Load = active executor initiatives +
    active projects + queued jobs (read live).
  - `brief.py`: `self_brief()` -> compact text: "You reliably do X (n, p=..),
    you often time out on Y, current load L." Injected into thinker +
    executor + project-planner prompts so she routes/declines/escalates.
  - `journal.py`: unified ActionJournal (SQLite `colony-action-journal.db`):
    record(domain, description, reasoning, confidence, reversibility,
    decision acted|asked|held|blocked, outcome, ref). Read APIs: today(),
    recent(). Every autonomous action chokepoint writes here.
  - `trust.py`: TrustEngine.
    - `confidence(domain)`: Laplace-smoothed success rate from the
      CompetenceStore, scaled by the TypeFeedbackStore multiplier, penalized
      by audit violations.
    - Stage per domain (shadow -> ask_first -> act_first) persisted in the
      store; `gate(domain, description, reasoning, reversibility,
      floor_class)` -> decision act|ask|hold, journaled automatically.
    - Auto-graduation on threshold crossings (COLONY_TRUST_ASK_THRESHOLD
      default 0.45/n>=3, COLONY_TRUST_ACT_THRESHOLD default 0.8/n>=5), owner
      notification (not a request) through guarded delivery on each
      graduation; COLONY_TRUST_AUTOGRADUATE=false disables.
    - Circuit breaker: failures in window / any audit violation ->
      auto-demote to ask_first + journal + owner note.
    - Immutable floor: `is_floor(...)`: money movement, non-recoverable
      deletion, credential/security changes, bulk third-party messaging.
      Never self-decidable; always ask.
- Recording: executor completion/fail, project step outcome, directed audit
  verdict, delivery pushes, and worker job results (item 5) all feed
  CompetenceStore.record + the journal.
- Adaptive delivery cap: the per-recipient daily cap scales with the
  "delivery" domain's earned confidence (base COLONY_MAX_DAILY, earned
  upward, bounded by COLONY_TRUST_DELIVERY_CAP_MAX).
- Tools: `self_status()` (read) -> domains, rates, load, stages;
  `action_journal(day?)` (read). Registry: `registry.self_model`.
- API: GET /self, GET /self/journal.

Config: `COLONY_SELF_MODEL_ENABLED` (default true), COLONY_TRUST_* thresholds
(above). Measurement/journal live directly; gating decisions apply wherever a
capability consults `gate()`.

Tests: record math (ewma latency, rates), brief text thresholds, load count,
confidence calibration, graduation + demotion (breaker) transitions, floor
never graduates, journal write/read, adaptive cap bounds, self_status tool.

Private deployment layer: none.

## Item 7 - BELIEF MAINTENANCE [Phase A]

Drive the graph's epistemic scaffolding.

Design:
- `beliefs/` package operating over ColonyGraph + world-model.
  - `contradictions.py`: detect same subject+predicate conflicting value across
    facts/world-model properties. For world-model: `update_entity_property`
    already keeps higher confidence; extend to record superseded values with an
    audit row. For graph memories: query Memory/Fact nodes sharing
    subject+predicate; conflicting object -> conflict record.
  - `resolve.py`: pick winner by (recency, confidence, source-trust); mark loser
    epistemic_state = "superseded" (existing state machine on memories) with an
    audit trail (who/when/why). Genuinely unresolvable (equal recency+confidence,
    different trusted sources) -> emit an internal review INITIATIVE
    (type "data_quality" or new "belief_conflict") surfaced to the owner via
    the internal path (NOT a reach-out unless the owner wants).
  - `decay.py`: expire/decay stale world-state (last_seen older than TTL ->
    lower confidence / archive), reusing graph decay_memories +
    world-model observation timestamps.
- Wiring: new `_phase_belief_maintenance` (periodic, daily-ish) in autonomy loop
  next to memory phases; also invoked when the populator upserts a conflicting
  property (inline, cheap detection; heavy resolve deferred to the phase).
- Tools: `belief_conflicts()` (read). Registry: `registry.belief_engine`.

Config: `COLONY_BELIEFS_MODE` off|shadow|live (shadow: detect + log + surface
review initiatives, do NOT mutate epistemic_state; live: resolve/decay).
Default shadow.

Tests: contradiction detection (same s+p, diff value), resolution ordering
(recency > confidence > source), superseded-audit written, unresolvable ->
review initiative, decay lowers confidence past TTL.

Private deployment layer: source-trust ranking may reference deployment
sources (owner vs connector vs inference); ranking table is generic + env
override `COLONY_SOURCE_TRUST`.

---

## Item 5 - COLONY WORKERS [Phase B, riskiest]

Make the multi-agent scaffolding real: a generic worker daemon that claims and
executes typed jobs, with SERVER-SIDE enforcement (never trust the worker).

Design:
- Server side (public, ColonyAI):
  - Local `agent_action` handling is currently forward-only. Add a
    server-authoritative job lifecycle already present in `task_queue/`
    (claim/complete/fail, BLOCKED for approval). Ensure every job carries its
    ScopedTask/boundary context and that BOTH claim-eligibility and completion
    are re-checked server-side against DirectiveGuard + approval tiering
    (the worker's report is audited exactly like directed action item; never
    trust client-reported scope).
  - Capability-typed registration: workers register `{worker_id, capabilities:
    [research, analyst, ...]}`; the queue only hands a job to a worker whose
    capabilities cover the job's required_capability. Add `required_capability`
    to Job; a registration table + `POST /queue/workers/register`, claim
    filtered by capability. Heartbeat + lease so a dead worker's job requeues.
  - Post-completion audit (reuse `directed/audit.py` pattern) + feedback + self
    model + skill distill.
- Worker daemon (public, ColonyAI, installable): `workers/colony_worker.py`
  (extend existing `workers/`): authenticates to the sidecar (COLONY_API_KEY),
  registers capabilities, polls claim endpoint, executes with an LLM
  (OpenAI-compatible endpoint from env) + the SAME tool registry subset
  (read/internal tools + repo_* + web_search; NEVER mutation tools client-side),
  posts a structured report to the job complete endpoint. Ships with an install
  script + a launchd/systemd unit template (generic placeholders).

Config flags: `COLONY_WORKER_ENABLED`, `COLONY_WORKER_CAPABILITIES`,
`COLONY_WORKER_LLM_BASE_URL`/`_MODEL`/`_API_KEY`, `COLONY_WORKER_POLL_SECS`,
server-side `COLONY_WORKERS_MODE` off|shadow|live (shadow: accept registration
+ claims but execute in dry-run reporting only). Default shadow.

Rollout gate (amended): shadow is calibration; after one worker registers,
claims a read-only job, reports, and the server audit passes, worker job
classes graduate via the trust engine (read/internal classes first; each
worker-job-type is its own trust domain feeding item 4).

Tests: capability-filtered claim, server-side boundary re-check on claim AND
completion (a worker claiming a boundaried job is refused server-side), lease
requeue on missed heartbeat, audit of worker report, never-trust (worker
reporting a mutation on a read-only job -> violation).

Private deployment layer (aeva repo + Mac): which hosts run workers, their LLM
endpoints, capabilities per host, launchd plists, COLONY_API_KEY provisioning.
Document in the handoff list.

## Item 6 - EXPLORATION SANDBOX [Phase B, riskiest]

Sandboxed code execution for safe curiosity.

Design:
- `sandbox/` package with a pluggable backend interface `SandboxBackend.run(
  script, lang, limits) -> {stdout, stderr, exit, artifacts, timed_out}`.
  Reference backend: Docker (`DockerSandbox`): ephemeral container, `--network
  none` (or allowlist via a proxy), read-only rootfs + tmpfs workdir, no
  credentials/env mounted, `--cpus`/`--memory`/`--pids-limit`, wall-clock
  timeout via `timeout`, artifacts read back from the workdir mount (size
  capped). A `DisabledSandbox` default when Docker absent.
- Tools: `sandbox_run(script, lang, purpose)`, `sandbox_status()`. Gated by
  approval tiering: AUTO for an owner-directed experiment within default limits,
  FLAGGED (owner approval) otherwise; DirectiveGuard checked on the purpose/
  script text (a boundaried subject blocks it). Never mounts secrets.
- Server-side enforcement: limits + no-egress enforced by the backend, not the
  caller; the tool cannot widen limits.

Config: `COLONY_SANDBOX_MODE` off|dry_run|live (dry_run: validate + log the
would-run command, execute nothing), `COLONY_SANDBOX_IMAGE`,
`COLONY_SANDBOX_CPUS`/`_MEMORY`/`_TIMEOUT`/`_EGRESS` (none|allowlist),
`COLONY_SANDBOX_MAX_ARTIFACT_BYTES`. Default off (Docker may be absent on the
Mac; enable per deployment).

Rollout gate: dry_run until a sample script validates + logs; live only where
Docker is present and limits verified (no network, no creds, limits enforced).

Tests (mock backend): limit passthrough, no-egress flag set, approval tiering
(auto vs flagged), boundary block on purpose, artifact size cap, dry_run
executes nothing.

Private deployment layer: Docker availability + image on the Mac, egress
allowlist, resource ceilings. Document; likely stays `off`/`dry_run` on the
Mac unless the owner wants a sandbox host.

---

## Item 2 - SENSES / CONNECTOR FRAMEWORK [Phase C]

Generic read-only connectors: poll -> normalize -> observations + world-model
entities + initiative-engine context.

Design:
- `connectors/` package.
  - `base.Connector` interface: `poll() -> [Observation]` where Observation =
    {domain, external_id, ts, payload, entities:[{kind,name,external_ids}]}.
    `ConnectorConfig` from env only. `manager.ConnectorManager`: registered
    connectors, a periodic `_phase_connectors` poll (rate per connector),
    normalize -> (a) observation store (existing `/observations` path feeds the
    initiative engine domains), (b) world-model populator (upsert entities +
    relationships, boundary-gated, shadow-first), (c) belief maintenance
    (item 7) on changed facts.
  - Reference connectors (all read-only, token/config based):
    - `imap_email.py`: IMAP (host/user/app-password from env), read headers +
      snippets -> people/company entities + "email" observation domain.
    - `caldav_calendar.py`: CalDAV/Google-calendar via CalDAV or an API token
      -> events -> "calendar" observation domain + Event entities. If OAuth
      consent is required, implement token-based (refresh token in env) and
      DOCUMENT the one-time consent step in the handoff, do not stall.
    - `fs_documents.py`: a folder path (env) -> new/changed files -> document
      entities + "document" observations (reuse world_model extraction).
    - `webhook_pull.py`: generic JSON-pull for business metrics (URL + auth
      header from env, JSONPath-ish field map) -> "metrics" observations +
      Project/Product entity metrics.
  - Credentials/instances: ONLY env/private layer. Public code ships the
    framework + connector logic + a documented env schema.
- Boundary: every connector poll and every entity upsert is boundary-gated
  (an OBSERVE boundary on a subject suppresses ingest of it).

Config: `COLONY_CONNECTORS_ENABLED`, per-connector `COLONY_CONNECTOR_<NAME>_*`
(enabled, endpoint, token, poll_secs), `COLONY_CONNECTORS_MODE` off|shadow|live
(shadow: poll + log normalized output + shadow-populate, no world-model writes).
Default off.

Rollout gate: shadow per connector until its normalized output + would-populate
entities look clean (same discipline as the world-model populator shadow),
then live per connector.

Tests: base normalize contract, each connector against a canned fixture
(no live creds in tests), boundary suppression, populator/observation wiring,
poll cadence.

Private deployment layer (aeva repo + Mac): actual endpoints, mailboxes,
calendar accounts + OAuth refresh tokens, metric URLs + auth, folder paths,
poll cadences, launchd if a connector runs out-of-process. Document + write the
env to the Mac plist; list for aeva-repo sync. Also document the one-time
calendar OAuth consent step for the owner.

---

## Cross-cutting

Registry accessors to add: project_engine, skill_store, self_model,
belief_engine, connector_manager, sandbox, worker registry.
Server boot: init each behind its mode flag, same pattern as directives/
proposals/feedback/directed/repos.
Tools to add (all boundary + approval gated where they act): list_projects,
project_status, create_project, abandon_project, recall_skills, self_status,
belief_conflicts, sandbox_run, sandbox_status. Read-only ones can be live;
acting ones shadow/dry-run first.
Delivery: milestone/report/proposal outputs all reuse the existing guarded
`_route_reachout_delivery` (now live) with the "proposal" type; keep them
subject to the rate limiter + boundary + quiet hours.

Prompt architecture (charter): every internal LLM role composes its system
prompt via `cognition/charter.py` `build_system_prompt(role=...)`: shared
<charter> doctrine + <role> block + budget-capped injection slots
(<self_model> from the trust engine's brief, <boundaries> from
DirectiveGuard.context_brief(), <skills> from skills_memory retrieval,
<corrections> as avoid-lines from failure post-mortems, <context>) + a
confidence-mandatory <output> contract. Rules: doctrine changes go ONLY in
the charter (never per-role); PROMPT_VERSION is journaled with every action
so behavior shifts are attributable; the golden-set eval harness
(tests/test_prompt_evals.py: composition contracts + canned-output decision
goldens) runs on any prompt change. Stated confidences in thinker/planner/
executor outputs are trust-engine calibration inputs (stated-vs-realized
recorded per event; CompetenceStore.calibration()). Migrated so far:
executor, thinker (schema now requires confidence + evidence; ungrounded
items dropped), project planner + project step runner. Remaining, adopted
as each is touched in its phase: observer (cognition/prompt.py, keep its
positive/negative examples via extra=), synthesis, worker (Phase B),
directed_intake (when LLM-assisted intake lands), hermes-plugin
memory-context injection (confidence-sorted facts, token budget,
avoid-prefix; Phase C).

Public/private split (principle): capability code + schemas + flags = this
repo (generic, env-driven). Instance specifics (creds, hosts, plists, persona
glue, connector endpoints, worker placement, sandbox host) = documented here
per item and written to the Mac live locations for aeva-repo sync.

Safety invariants (amended, do not regress): DirectiveGuard checked before
every act; the immutable floor (Amendment 1.6) is never self-decidable;
below-threshold confidence asks first with reasoning + confidence; every
autonomous action is journaled; new action paths start in calibration
(shadow) and graduate via the trust engine rather than manual flips;
server-side (never client-side) enforcement for workers + sandbox; do not
disturb the live delivery path. Directed action is LIVE per Amendment 1.7
(read-only auto + journal, mutating ask-first).

Hygiene per push: per-commit leak self-scan (the pattern set used at program
start, ground-truthed against live config), refresh bundles both locations,
live git parity. Independent verification once at program end. At program end:
verify parity, then delete stale local branches (live-overlay-20260704,
backup-pre-scrub, refs/original/*) and confirm no stale branches on either repo.

---

## Hermes integration (capability map at Hermes v0.18.0 / tag v2026.7.1)

Surveyed 2026-07-04 against the v2026.7.1 release checkout. This section is the
authoritative map of what the host framework now offers, what this integration
uses, and which roadmap items must NOT duplicate native Hermes capability.
Hook conventions were re-verified at 0.18: hooks are still invoked SYNC as
cb(**kwargs), pre_llm_call still carries sender_id (plus new kwargs task_id,
turn_id, conversation_history, is_first_turn, model, platform), and a returned
{"context": str} still injects into the user message. The doctor's dynamic
checks (VALID_HOOKS, sync, **kwargs) remain the right validation and pass
unchanged at 0.18.

### Extension surfaces (what Hermes 0.18 exposes without code changes)

- Plugin hooks (23 VALID_HOOKS): the 0.15-era set plus pre_verify (keep-going
  gate after code edits, bounded by agent.max_verify_nudges) and the kanban
  lifecycle trio (kanban_task_claimed in the dispatcher process,
  kanban_task_completed / kanban_task_blocked in the worker subprocess).
- Middleware (hermes_cli/middleware.py, distinct from observer hooks):
  llm_request / tool_request rewrite the outgoing payload, llm_execution /
  tool_execution wrap the actual call onion-style. This is the sanctioned way
  to alter sampling parameters, request shape, or tool args per model or per
  policy WITHOUT a core patch.
- PluginContext beyond register_tool/register_hook/register_command:
  ctx.llm (PluginLlm facade running on the host's model with fail-closed
  per-plugin trust gates), register_auxiliary_task (declare an LLM side-job
  with its own auxiliary.<key> model config), register_context_engine,
  register_skill, register_platform, register_middleware, provider
  registration for tts/stt/web-search/browser/image/video, inject_message.
  New trust gate: register_tool(override=True) on a built-in requires
  plugins.entries.<plugin_id>.allow_tool_override: true (we do not override,
  so no action needed).
- HTTP surfaces: the "api" platform (gateway/platforms/api_server.py) gives an
  authenticated OpenAI-compatible endpoint PLUS session CRUD/chat/fork, cron
  job CRUD over HTTP, and structured runs (POST /v1/runs, SSE events, approval
  and stop endpoints). The generic inbound webhook adapter
  (gateway/platforms/webhook.py, config platforms.webhook.extra.routes) takes
  external POSTs with HMAC secrets, prompt templates, and deliver/deliver_only
  targets (deliver_only sends the rendered message to a channel with NO agent
  turn). hermes send / send_message_tool is the native outbound path.
- Relay connector contract (docs/relay-connector-contract.md, EXPERIMENTAL):
  front an arbitrary messaging platform from an external process over a
  WebSocket wire contract; the gateway never learns the platform. Relevant to
  the channel-framework work as the native alternative to custom platform
  plugins for out-of-process channels.
- Background work: cron with pluggable scheduler providers (cron.provider) and
  per-job deliver targets; kanban board with dispatcher plus worker-subprocess
  execution and the lifecycle hooks above; gateway file-hooks
  (~/.hermes/hooks/<name>/HOOK.yaml + handler.py) for gateway/session/agent
  lifecycle events, living OUTSIDE the framework checkout so they survive
  updates.
- Memory/context: MemoryProvider ABC (exclusive, selected by memory.provider;
  our provider plugin is one of these), ContextEngine ABC selected by
  context.engine (NOTE: top-level context_engine: <name> is a DEAD key at
  0.18; only context.engine is read. Audit found a deployment carrying the
  dead key with the engine plugin present but inactive; decide deliberately
  whether to activate via context.engine or drop the plugin).
- Model routing: Mixture-of-Agents as a selectable model (moa: presets),
  Vertex AI provider, per-task auxiliary.<key> model config, fallback chains.
- Ops: scale-to-zero gateway (env HERMES_SCALE_TO_ZERO + config
  scale_to_zero.idle_timeout_minutes). Keep it OFF for an always-on messaging
  spine. Config migrations to _config_version 32 flip agent.verify_on_stop to
  false (good default for a chat-first deployment; pre_verify remains
  available for policy).

### What the integration uses today

register_tool (colony toolset), register_command with register_slash_command
fallback, hooks pre_llm_call / post_llm_call / on_session_end / pre_tool_call,
the memory provider plugin, a WebSocket subscription to the sidecar for
proactive events, sidecar-webhook delivery plus a deployment deliver-shim, and
the ops layer (doctor, patch runner, restart runner, activity monitor).

### Adoption recommendations (ordered by leverage)

1. Delivery via the native webhook adapter: the sidecar's proactive delivery
   POST should target a platforms.webhook.extra.routes route (HMAC secret,
   deliver_only for pure notification delivery, or a prompt template when the
   agent should compose). This resolves the KNOWN DEPLOYMENT BLOCKER below
   natively and retires the deployment deliver-shim (which bypasses the
   gateway and re-implements transport).
2. Sampling/model tweaks as llm_request middleware, not core patches: a
   middleware can set temperature per model family, which retires the
   mimo-temperature class of core patch entirely. Policy: before authoring any
   new core patch, check middleware and hooks first (see
   plugins/hermes-plugin/ops/PATCHES.md).
3. pre_gateway_dispatch for inbound gating: the response-gate/guard work has a
   native pre-auth flow-control point (skip / rewrite / allow) and
   transform_llm_output is the native outbound seam for provenance/persona
   checks on the reply text.
4. on_session_reset / on_session_finalize for session-handoff briefs instead
   of external glue scripts watching session state.
5. kanban_task_* hooks as a self-model/journaling feed for agent work items
   (zero-cost observability into dispatcher/worker activity).
6. register_auxiliary_task for integration LLM side-jobs so operators tune
   their model/provider under auxiliary.<key> like every built-in side-task.
7. ctx.llm for plugin-side quick classification instead of shipping separate
   LLM client config in the plugin.
8. Structured runs API (/v1/runs + SSE + approval) as the programmatic drive
   surface for external orchestrators and workers, replacing bespoke
   subprocess or chat-simulation glue.

### Impact on the seven items (do not duplicate native capability)

- Item 1 (Projects): keep project/goal persistence in the sidecar (cognition
  state, Hermes has no equivalent). For step EXECUTION, prefer dispatching
  into Hermes kanban (board + worker subprocess + lifecycle hooks) or the
  /v1/runs API over building a private execution runner; consume
  kanban_task_* hooks for step outcome tracking.
- Item 2 (Connectors): Hermes's generic webhook adapter already handles
  push-style ingress (HMAC, templates, rate limits, idempotency); build only
  the pull-style connectors (IMAP, CalDAV, fs, metrics-pull) and normalize
  into observations. Do not build a push webhook receiver.
- Item 3 (Skills): Hermes 0.18 has a full agent-facing skills system plus
  /learn and /journey. Colony's skills_memory remains sidecar-internal
  procedure memory for its own loops; where a distilled skill is useful to
  the AGENT, export it to a directory listed in skills.external_dirs (or
  ctx.register_skill) instead of inventing an import path. Never rebuild
  /learn.
- Item 4 (Self-model): feed it from post_tool_call / post_api_request /
  kanban_task_* hooks (latency, status, error_type are provided as kwargs);
  no polling needed.
- Item 5 (Workers): Hermes kanban's dispatcher/worker model and the delegation
  subsystem (subagent_start/stop, delegation.* config) already provide typed
  work execution with process isolation. Colony's value-add is SERVER-SIDE
  enforcement (boundary/approval re-check on claim and completion), the
  capability registry, and the audit trail; implement those in the sidecar
  queue as planned, but strongly consider the worker daemon CLAIMING work as
  a Hermes kanban worker or /v1/runs driver instead of a fully bespoke
  executor.
- Item 6 (Sandbox): no native isolated-execution backend confirmed in 0.18
  (terminal tool policy is approval-based, not containment); proceed as
  planned, keep server-side enforcement.
- Item 7 (Beliefs): no native equivalent; proceed as planned.

### Core-patch policy at 0.18

The only sanctioned mechanism for altering Hermes behavior beyond
config/plugins/hooks/middleware is the guarded patch registry
(plugins/hermes-plugin/ops/hermes-patch-runner.py + PATCHES.md; the doctor
heals the registry every run and fails loudly on anchor drift). Current
deployment patches that still lack a native seam: rerouting the background
review and long-running-heartbeat notifications to a home channel (no
notification-routing hook exists at 0.18; re-check each release). Patches
whose behavior 0.18 can express natively (sampling overrides via
llm_request middleware) should migrate off the registry when next touched.

## Program State (update as phases land)

- 2026-07-04: Plan committed. Build not started.
- 2026-07-04 (later): Hermes v0.18.0 (v2026.7.1) capability survey + integration
  audit landed (see "Hermes integration" section above). Generic guarded-patch
  mechanism (ops/hermes-patch-runner.py + PATCHES.md + doctor wiring) shipped;
  deployment patch definitions migrated to the private deployment repo and the
  live registry. Items 1 to 5 have native-capability notes that constrain their
  designs; read that section before building each item.
- Pre-req landed earlier this session (already pushed at tip 2726698):
  directives (tiered), proposals, feedback, directed-action (dry_run),
  read-only repo mirrors, world-model populator (shadow), thinker (shadow),
  delivery go-LIVE flip (Colony side).
- RESOLVED (was: KNOWN DEPLOYMENT BLOCKER): proactive delivery now runs
  LIVE via COLONY_DELIVERY_TRANSPORT=gateway to the deployment's message
  gateway (rate caps + cooldown bind and were observed doing so); the old
  Hermes webhook-route 404 path is moot for this deployment.
- 2026-07-04 (Amendment 1): owner-approved graduated-autonomy amendment
  landed (section above). Item 4 promoted to trust engine; per-item designs
  touched; safety invariants rewritten to action-with-journaling.
- 2026-07-04 (ops flips, owner-ordered, deployment layer): COLONY_DIRECTED_MODE
  dry_run -> live (with a deployment delegate shim bridging the dispatch
  contract to the local agent gateway's async task surface; E2E verified:
  intake -> read-only auto-approval -> real dispatch -> agent execution ->
  structured report -> audit verdict clean -> feedback recorded; the owner
  report was correctly gated by the delivery cooldown). COLONY_THINKING_MODE
  shadow -> live (semantic note: shadow ALREADY delivered thinker proposals
  through guarded delivery; live additionally executes the thought-up items
  as internal work). COLONY_WORLD_POPULATE_MODE shadow -> live (first real
  entity writes observed same day). COLONY_DIRECTIVE_LLM_ASSIST off -> on
  (verified against the deployment's fast classifier endpoint). The former
  Colony-side delivery blocker is resolved at the deployment layer
  (COLONY_DELIVERY_TRANSPORT=gateway); delivery is LIVE with rate caps
  binding. context.engine deliberately NOT flipped (engineering-rollout
  category).
- Phase A: COMPLETE (2026-07-04). Landed:
  - `self_model/` (item 4 as trust engine): CompetenceStore + event log,
    self-brief, ActionJournal (`colony-action-journal.db`), TrustEngine
    (confidence, stages shadow/ask_first/act_first, auto-graduation with
    owner notices, circuit breakers, immutable floor, adaptive delivery
    cap). Wired into: initiative executor (outcomes + prompt brief),
    directed action (trust-graduated approval tiering + ask-first proposals
    carrying reasoning+confidence + audit-fed track record), delivery
    (outcome recording + adaptive cap), projects, beliefs. Flags:
    COLONY_SELF_MODEL_ENABLED (true), COLONY_TRUST_* thresholds.
  - `skills_memory/` (item 3): SkillStore (`colony-skills.db`), distillation
    (retry-success / novel-diagnosis triggers, strict-JSON validation,
    signature dedup, cap+evict), retrieval blocks + per-domain failure
    notes; wired into executor + project planner/engine. Flags:
    COLONY_SKILLS_ENABLED (true), COLONY_SKILLS_DISTILL (shadow default;
    live on the reference deployment), COLONY_SKILLS_MAX.
  - `projects/` (item 1): Project/Step models + ProjectStore
    (`colony-projects.db`), planner (one LLM pass, deterministic
    validation: kind whitelist, cycle-breaking, cap), ProjectEngine
    (autonomy phase `_phase_projects`: adoption of project-type
    initiatives, boundary-checked step dispatch through existing sub-gates,
    bounded replans, milestone proposals, self-model defer + trust
    graduation of the shadow calibration stage). Tools list_projects /
    project_status / create_project / abandon_project; API /projects.
    Flags: COLONY_PROJECTS_MODE (shadow default), _MAX_STEPS,
    _REVIEW_SECS, _MAX_REPLANS, _MAX_CONCURRENT, _DEFER_LOAD.
  - `beliefs/` (item 7): claim extraction (conservative), conflict
    detection, resolution (recency > confidence > source-trust; env
    COLONY_SOURCE_TRUST), supersession audit (`colony-beliefs.db`), inline
    property-audit hook on world-model updates, stale-entity decay,
    unresolvable -> data_quality review initiative; daily phase
    `_phase_belief_maintenance`. Live resolution requires the earned
    act_first stage (env live = owner override). Tool belief_conflicts;
    API /beliefs.
  - Amendment extras: one-command global pause (extractor + guard +
    manager ack; "stop acting" binds instantly, staged lift), world-model
    LLM-assist extraction (`world_model/llm_extract.py`, daily batch phase,
    journaled writes, COLONY_WORLD_LLM_EXTRACT), action_journal +
    self_status tools, GET /self + /self/journal + /skills-memory +
    /world/llm-extract/status.
  - Tests: test_self_model, test_skills_memory, test_projects,
    test_beliefs, test_global_pause, test_directed_trust,
    test_world_llm_extract; full unit suite green.
- 2026-07-04 (charter adoption, Phase A modules): executor, thinker,
  project planner and project step runner now compose through the shared
  cognition charter (see "Prompt architecture" above); thinker schema
  requires confidence + grounding evidence; per-step planner confidence is
  persisted and stated-vs-realized calibration is recorded in the
  self-model; PROMPT_VERSION journaled per action; golden-set prompt eval
  harness added (tests/test_prompt_evals.py).
- Phase B: NOT STARTED.
- Phase C: NOT STARTED (world-model LLM extraction pulled forward from the
  item 2 wiring as an Amendment-era deliverable; connectors proper remain).

## Resumption note for a successor agent

Read this doc top to bottom. The codebase sections under "What already exists"
are real and pushed. Start Phase A item 1 (Projects) unless Program State says
otherwise. Follow the per-phase loop (build -> tests -> deploy Mac -> gated
verify -> leak scan -> push -> parity -> update this doc). Keep everything
generic + env-driven; write deployment specifics to the Mac and list them for
the aeva private repo. Never edit ~/.hermes/hermes-agent (read-only); use local
config only. Do not disturb the live delivery path or the held directed dry_run.

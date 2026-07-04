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

Rollout gate: shadow until a sample project plans + logs a clean step sequence
with boundary checks and a milestone-proposal in shadow; live only per the owner.

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

## Item 4 - SELF-MODEL [Phase A]

Live competence/calibration per capability domain from real outcomes.

Design:
- Reuse/extend `feedback/`: add `self_model/` with `store.CompetenceStore`
  (SQLite `colony-self-model.db`) keyed by domain (initiative_type | project |
  directed | research | delivery | worker-job-type): counts success/failure/
  timeout, ewma latency, last_outcome_at. Load = active executor initiatives +
  active projects + queued jobs (read live).
- `brief.py`: `self_brief()` -> compact text: "You reliably do X (n, p=..),
  you often time out on Y, current load L." Injected into thinker + executor +
  project-planner prompts so she routes/declines/escalates.
- Recording: hook the executor completion/fail, project step outcome, directed
  audit verdict, and worker job results (item 5) into CompetenceStore.record.
- Tool: `self_status()` (read) -> domains, rates, load. Registry:
  `registry.self_model`.
- API: GET /self.

Config: `COLONY_SELF_MODEL_ENABLED` (default true - measurement + prompt brief
only, no action path). Safe to live directly.

Tests: record math (ewma latency, rates), brief text thresholds, load count,
self_status tool.

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

Rollout gate: shadow until one worker registers, claims a read-only job,
reports, and the server audit passes; live per the owner, and only read/internal
job types first.

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

Public/private split (principle): capability code + schemas + flags = this
repo (generic, env-driven). Instance specifics (creds, hosts, plists, persona
glue, connector endpoints, worker placement, sandbox host) = documented here
per item and written to the Mac live locations for aeva-repo sync.

Safety invariants (do not regress): DirectiveGuard checked before every act;
approval tiering for mutating/outbound/directed/sandbox/worker; shadow/dry-run
default for every new action path; server-side (never client-side) enforcement
for workers + sandbox; do not disturb the live delivery path or held directed
dry_run.

Hygiene per push: per-commit leak self-scan (the pattern set used at program
start, ground-truthed against live config), refresh bundles both locations,
live git parity. Independent verification once at program end. At program end:
verify parity, then delete stale local branches (live-overlay-20260704,
backup-pre-scrub, refs/original/*) and confirm no stale branches on either repo.

---

## Program State (update as phases land)

- 2026-07-04: Plan committed. Build not started.
- Pre-req landed earlier this session (already pushed at tip 2726698):
  directives (tiered), proposals, feedback, directed-action (dry_run),
  read-only repo mirrors, world-model populator (shadow), thinker (shadow),
  delivery go-LIVE flip (Colony side).
- KNOWN DEPLOYMENT BLOCKER (not code): the live Hermes gateway (:8644) has no
  `colony-initiatives` webhook route in ~/.hermes/config.yaml (deliberately
  removed per a prior Hermes architecture plan), so proactive delivery POSTs
  return 404 and no message reaches the owner (rate bucket correctly unconsumed).
  Resolve at the Hermes deployment layer (restore the route OR point
  COLONY_HERMES_WEBHOOK_URL at the correct current endpoint) before real
  delivery works. Colony delivery code is correct.
- Phase A: NOT STARTED.
- Phase B: NOT STARTED.
- Phase C: NOT STARTED.

## Resumption note for a successor agent

Read this doc top to bottom. The codebase sections under "What already exists"
are real and pushed. Start Phase A item 1 (Projects) unless Program State says
otherwise. Follow the per-phase loop (build -> tests -> deploy Mac -> gated
verify -> leak scan -> push -> parity -> update this doc). Keep everything
generic + env-driven; write deployment specifics to the Mac and list them for
the aeva private repo. Never edit ~/.hermes/hermes-agent (read-only); use local
config only. Do not disturb the live delivery path or the held directed dry_run.

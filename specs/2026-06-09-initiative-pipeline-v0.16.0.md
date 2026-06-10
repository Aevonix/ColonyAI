# Initiative Pipeline v0.16.0 — From Relationship Reminders to Autonomous Work Engine

**Status:** Phase 1 + Phase 2 foundations IMPLEMENTED; remaining phases SPECIFIED
**Date:** 2026-06-09
**Supersedes (partially):** the INITIATIVEPIPELINEFIXES brief; builds on `2026-05-20-agent-work-queue-v0.13.0-revised.md`

---

## 1. Goal

Turn any Hermes agent connected to Colony into an autonomous pseudo-AGI
agent: constantly self-evaluating and evaluating the owner's needs,
generating goals, internally organizing, taking action or making
requests — while still allowing direct interaction with the user.

**Framing that governs everything here:** initiatives are directed at
the *agent*, not the human. They are the agent's autonomous task queue.
Colony = brain (generates initiatives). The host agent = decision layer:
it polls, decides what to execute, when to communicate, and when to stay
silent. Colony never sends messages directly; the agent is the sole
decision-maker for outbound communication.

Colony is agent-name-agnostic: it is a public project, and every
deployment names its own agent ("Aeva" is the reference deployment's).
Agent identity always comes from configuration — `COLONY_AGENT_NAME`,
`COLONY_WORKER_NODE_ID` — never from code, defaults, or prompts.

The relationship tracking that currently dominates the engine was only
ever meant to be one domain among many — the agent tracking *its own*
relationships built on the owner's behalf. v0.16.0 re-centers the
pipeline as a general-purpose autonomous work engine.

---

## 2. What Shipped in This Change

### 2.1 Phase 1 — pipeline bug fixes (all four)

| Bug | Fix | Files |
|-----|-----|-------|
| Title used `rationale` (the reason) instead of `description` (the action) | `title = description[:100]`; rationale moved into the context dict | `api/routers/host.py` |
| `InitiativeResponse` missing `entity_id`; `target_agent_id` hardcoded `None` | Schema + serializer return `entity_id` (the SUBJECT) and `target_agent_id` (the EXECUTOR, from `assigned_agent_id` falling back to `preferred_agent_id`) | `api/schemas/host.py`, `api/routers/host.py` |
| Serializer hardcoded `context={}` | `context` column (JSON TEXT) added to the initiatives table with an idempotent migration; the autonomy loop persists the per-initiative context snapshot at creation; serializer returns `initiative.context or {}` (old NULL rows → `{}`) | `initiatives/models.py`, `initiatives/store.py`, `autonomy/loop.py`, `api/routers/host.py` |
| Owner self-initiative filter broken (two env vars, `"owner"` default, format mismatch) | IdentityResolver (below); `COLONY_OWNER_CONTACT_ID` canonical with deprecated `COLONY_HOST_CONTACT_ID` shim; filter scoped to relationship generators only; fail-closed semantics | `identity/resolver.py` (new), `autonomy/loop.py`, `intelligence/components/initiative_engine.py` |

Also fixed while in there:
- `InitiativeResponse.status` Literal was missing `"assigned"` — a real
  store status the loop sets when linking initiatives to queue jobs;
  serializing an assigned initiative raised a validation error.
- `POST /initiatives` passed the request's 0–100 priority into the
  store's 0.0–1.0 scale (everything ≥1 clamped to 100). Now divided.
- `POST /initiatives` silently dropped the request's `context` and had
  no `entity_id`; both now persist.

### 2.2 Step 0 finding — identity layer (gates Task 4)

**Question:** does a single contact record carry every identifier form?
**Answer: yes, with caveats — the contact store is the source of truth
and the resolver is an index over it.** Per-store findings:

| Store | Holds | Cross-link |
|-------|-------|-----------|
| Contact store (SQLite) | CID (PK), display/given/family names, platform handles (`contact_handles` table, normalized), `person_node_id` FK → Neo4j | `get()`, `resolve_handle()`, `find_by_name()`, `find_by_person_node_id()` |
| Affect store (SQLite) | `contact_id` only | none — must resolve through the contact store |
| Neo4j graph | `Person.id` (UUID — **not a slug**; the "marc-andrew-seal" slug format does not exist in this codebase), `Person.name` | **no back-reference to CID**; reverse lookup goes through `Contact.person_node_id` |

Caveats the resolver absorbs: `person_node_id` is optional (not
guaranteed populated); Neo4j has no reverse link (the contact-store
index IS the reverse link); display names are not unique.

### 2.3 IdentityResolver (`identity/resolver.py`, new)

- `resolve(any_id) → set[str]`: accepts CID, Neo4j Person UUID, display
  name, email, or phone; returns every known form (CID, names, node id,
  handle addresses, case-folded variants). Ambiguous names → empty set
  (never merge two people).
- `is_owner(any_id) → bool`: membership against the owner's cached
  identity set, with cross-format resolution fallback.
- **Owner rules:** resolved once from `COLONY_OWNER_CONTACT_ID`
  (deprecated alias honored with a warning). Configured-but-unresolvable
  → `OwnerIdentityError`. No default-string fallback — the silent
  `"owner"` default was the bug.
- **Fail-closed deviation from the brief:** the brief said "raise at
  startup." Killing the whole sidecar (57+ endpoints) over one domain
  policy is worse than the disease, so instead: the loop logs CRITICAL
  at startup when the owner is unresolvable, and the relationship/affect
  generators *generate nothing* until it is fixed. Fail-open (the old
  behavior) is gone either way.
- **Scope:** the owner filter lives only in `_feed_neglected_contacts`,
  `_load_neglected_contacts`, and `_generate_relationship_suggestions`.
  It is a relationship-domain policy, not a loop-level gate — the owner
  is a legitimate subject for COMMITMENT ("follow up on what you
  promised Marc"), CALENDAR ("prep Marc's 3pm meeting"), and
  AGENT_ACTION work, and tests pin that both directions.

### 2.4 Phase 2 foundations

**New initiative types** (enum members in `initiative_engine.py`):
`COMMITMENT`, `CALENDAR`, `RESEARCH`, `TASK`, `PROJECT`, `SYSTEM`
(`CODING` already existed). `REPO_MONITOR`/`WORK_MAINTENANCE` are
deliberately NOT types — they are `AGENT_ACTION` generators. The
type axis is *what domain is this about*, not *detect vs auto-fix*.

**Action registry** (`initiatives/action_registry.py`, new):
`action_hint` for agent-executable work is a named capability in an
allow-listed registry — never a raw command string (initiatives are
built from graph data that can include untrusted content; free-form
commands are an injection-to-execution path). Risk tiers:

- `read_only` → auto-execute.
- `mutating` / `outbound` → requires **human owner** approval. The agent
  cannot approve its own mutations — the same actor on both sides of a
  gate is a log line, not a boundary. `COLONY_AGENT_AUTO_APPROVE=true`
  collapses the gate for trusted deployments (default false).

The dispatch path (`loop._post_agent_action_to_queue`) now consults the
registry: unregistered hints are **never queued** (the initiative stays
stored as information); gated actions post as BLOCKED awaiting owner
approval, reusing the v0.13.0 task-queue approval flow. The v0.13.0
`DESTRUCTIVE_HINTS` set is preserved as registered mutating actions.
All actions from the brief's per-type tables (task/coding/project/
system/calendar/commitment/research) are registered.

**Context durability** (`initiatives/context_freshness.py`, new):
every type declares `durable` (snapshot at creation stays true:
relationship, commitment, research, task, project) or `volatile` (can
go false in the queue: calendar, coding, system, health, agent_action),
with per-type freshness TTLs (calendar/system 300s, coding/agent 600s).
The loop stamps `context_captured_at` into every persisted context.
The API returns `context_durability` per initiative. Volatile snapshots
without a stamp are treated as stale — fail closed.

**Per-entity context refresh:** `engine.rebuild_context(type, entity_id)`
is the per-entity loader interface (the brief's option (a)), exposed as
`POST /v1/host/initiatives/{id}/context/refresh`. Implemented rebuilders:
`relationship` (re-queries one Person node) and `commitment` (re-reads
one commitment). Durable types return their stored snapshot; volatile
types without a registered rebuilder return 501 rather than silently
serving stale data. New volatile loaders MUST register a rebuilder.

**COMMITMENT generator (Task 5, lowest-hanging fruit):** the loop now
feeds full commitment records (`upcoming_commitments` context: id,
description, due_at, hours_until_due, overdue, person_id) instead of
flattening them into anonymous scheduling opportunities.
`_generate_commitment_initiatives()` emits durable-context initiatives
with `dedup_key=commitment:{id}`, overdue escalation (priority 0.9),
and the owner explicitly allowed as subject.

### 2.5 Verification added (`tests/test_initiative_pipeline_v016.py`)

- Context round-trip through store + serializer; pre-migration NULL rows
  return `{}` without erroring; schema migration from a v0.15 table.
- Title = action; `entity_id`/`target_agent_id`/`assigned` status.
- Resolver: all formats resolve to one identity; ambiguity → empty set;
  missing/unresolvable owner raises; no-store exact-match degradation;
  legacy env shim.
- Owner exclusion both directions: excluded from RELATIONSHIP (by node
  id AND display name), present for COMMITMENT; unresolvable owner
  generates nothing (fail closed).
- Dedup regression: two neglected contacts → two distinct subject-keyed
  dedup keys.
- Negative tests: unregistered `action_hint` never reaches the queue;
  mutating/outbound require approval; legacy destructive hints stay
  gated; injection-shaped hints are not executable.

### 2.6 Agent-as-sensor loop (IMPLEMENTED)

The full sensor loop from §3 shipped:

- **Observation store** (`observations/store.py`): latest-snapshot-per-
  entity SQLite store across six domains (coding, task, calendar,
  research, project, system) with per-domain freshness tracking.
- **Ingestion API** (`api/routers/observations.py`): `POST
  /v1/host/observations` (batch), `GET /v1/host/observations[/{domain}]`
  (sensor health/summary), `DELETE .../{domain}/{entity_id}`.
- **Sync requests** (`loop._phase_observation_sync`): when a domain's
  newest observation outlives its sync interval
  (`OBSERVATION_SYNC_INTERVALS`), the loop posts a read-only
  `agent_sync_<domain>` job to the task queue (all six registered in
  the action registry). Per-domain in-memory gating prevents re-spam
  while the agent is slow. `COLONY_SYNC_DOMAINS` env var scopes which
  domains are requested (default: all six).
- **Six observation-backed generators** in the engine: failing-CI /
  review-requested PRs, stale open tasks, events starting within 24h,
  unchecked research items, milestones due ≤7d with open work, and
  unhealthy services. All dedup on the subject's entity_id and carry
  the observation snapshot as context.
- **Per-entity rebuild + auto-close**: all six domains share one
  rebuilder (the freshest stored observation). The refresh endpoint
  now auto-closes volatile initiatives whose condition has cleared
  (CI green, service recovered, meeting over) — cancelled with
  `stale_reason="condition_cleared"` instead of surfacing stale work.
- **Hermes plugin**: `poller/colony-queue-worker.py` registers as a
  queue worker, claims one `agent_action` job per run, and fires it to
  the `colony-jobs` webhook route with explicit lifecycle URLs (report
  observations / complete / fail via curl). Example webhook config
  rewritten around the five decision verbs.

### 2.7 Framing sweep (IMPLEMENTED)

- `notify_user` defaults removed from the dispatch payloads and the
  delivery bridge — the fallback disposition is now `review_and_decide`
  (a test greps the source tree to keep it that way).
- The relationship generator's hint changed from "Send a message or
  schedule a call" to `evaluate_relationship` — the agent evaluates and
  chooses a disposition; nothing instructs it to message.
- Webhook prompt examples rewritten: "This is YOUR work item… the owner
  has not seen it and does not need to unless YOU decide they should,"
  with the five-verb decision block and the volatile-context freshness
  check up front.

---

## 3. Remaining Phase 2 — data feeds via the agent (no duplicated integrations)

The enum is done; **the real work is data feeds**. Original drafts had
Colony owning API clients for GitHub, calendars, and arXiv. That is the
wrong shape: Hermes already holds those connections (github, terminal,
web, browser toolsets), and the v0.13.0 worker registry already maps
toolsets to capabilities. Duplicating them in Colony means duplicate
credentials, duplicate clients, and two places to configure "connect to
GitHub."

### 3.1 The agent is Colony's sensor array

Colony does not reach out to the world; the agent observes the world
through its existing Hermes connections and reports back. A brain does
not have its own eyes — it processes what the body senses.

1. **Observation store (new, Colony-side).** Domain-scoped records:
   `domain`, `entity_id`, `payload` (JSON), `observed_at`,
   `reported_by`. Written via a new ingestion endpoint
   (`POST /v1/host/observations`), readable by domain + entity.
2. **Sync actions (registry, read_only).** `agent_sync_repos`,
   `agent_sync_calendar`, `agent_sync_research`, ... When a domain's
   newest observation is older than its freshness TTL, the loop
   generates a sync job. The agent claims it, looks through its own
   toolsets, and POSTs observations back. Read-only → auto-executes,
   no approval friction.
3. **Loaders read observations, not APIs.** `_load_coding_context()`
   etc. query the observation store in batch mode and per-entity mode;
   per-entity rebuild = the latest observation for that entity (or a
   sync job if there is none fresh enough).
4. **Self-priming loop.** Colony requests observation → agent observes
   → Colony generates initiatives from observations → agent executes →
   results and fresh observations flow back.
5. **Agent-assisted setup, by construction.** Enabling a feed = the
   agent registering that it can observe a domain (existing
   worker-capability registration). A new connection added in Hermes
   becomes a new domain Colony can think about, with zero Colony-side
   credentials. Local-only sources (sidecar health, logs) remain the
   one exception where Colony observes directly — it is observing
   itself.

| Task | Type | Observed via (agent toolset) | Dedup | Context |
|------|------|------------------------------|-------|---------|
| 6 | `AGENT_ACTION` expansion | terminal/git, self-health | `agent_action:{entity}:{action}` | volatile, per-entity |
| 7 | `CALENDAR` | agent calendar access | `calendar:{event_id}` | volatile, per-entity |
| 8 | `RESEARCH` | web (arXiv, HuggingFace) | `research:{paper_id}` | durable |
| 9 | `TASK` | github (issues), local lists | `task:{task_id}` | durable |
| 10 | `CODING` | github (PRs, CI) | `coding:{pr_id}` | volatile, per-entity |
| 11 | `PROJECT` | github (milestones/boards) | `project:{milestone_id}` | durable |
| 12 | `SYSTEM` | terminal, Colony self-metrics | `system:{service_id}` | volatile, per-entity |

Loader interface (both modes from the start):

```python
async def _load_coding_context(self, entity_id: str | None = None) -> dict | None:
    """Batch mode (None): populates self._context["coding"] from the
    observation store. Per-entity mode: returns the freshest stored
    observation for one PR; registered in rebuild_context()."""
```

**Volatile auto-close lifecycle (settle before Task 10/12):** when a
per-entity refresh shows the condition has cleared (CI green again,
service recovered), the initiative should retire itself (`cancelled`,
`stale_reason="condition_cleared"`) instead of surfacing stale context.
Recommended: the refresh endpoint compares the rebuilt snapshot against
a per-type "still actionable?" predicate and cancels on false.

---

## 4. The Layers Beyond the Spec (gap map)

These were the acknowledged gaps in the brief. Most have more existing
infrastructure than expected; the right move in each case is to extend,
not invent.

### 4.1 Execution engine — mostly exists (v0.13.0)
The distributed task queue (`task_queue/`) with atomic claim, worker
registry, heartbeats, retries, and the BLOCKED→approval→QUEUED state
machine is the execution engine. The host agent registers as an external worker
claiming `agent_action` jobs. v0.16.0 adds the allow-list in front of
it. **Remaining:** map registered action names to Hermes toolsets in the
worker; per-action timeout/retry policy from the `ActionSpec`.

### 4.2 Decision layer — agent-side, keep it there
Colony ranks (priority, dedup, cooldowns); the agent decides. The decision
inputs are now complete: subject (`entity_id`), situation (`context`),
staleness contract (`context_durability` + `context_captured_at`),
risk (`risk` in job payloads). **Remaining:** a decision prompt/policy
in the Hermes plugin choosing one of five agent verbs, each of which
already has an endpoint:

| Verb | Endpoint |
|------|----------|
| execute | claim → queue lifecycle → complete/fail |
| snooze (until) | `/initiatives/{id}/respond` (snoozed) |
| dismiss (reason) | `/initiatives/{id}/respond` (dismissed) |
| communicate to owner | outbound-tier action via delivery bridge |
| request approval | BLOCKED job → owner approval flow |

Communicating with the owner is *one action among five*, not the
default disposition — and as an outbound-tier action it passes the same
gate as any other outward-facing act until the generators are trusted.

### 4.3 Communication layer — exists, keep Colony out of it
Delivery bridge + rate limiter + quiet hours + channel registry already
gate outbound pushes; the v2.2 webhook spec governs payload hygiene (no
raw dumps). The rule stands: Colony never composes user-facing
messages; it pushes structured payloads and the agent decides whether/what
to say. **Remaining:** digest mode (bundle completed-job summaries,
spec'd in v0.13.0 §9) instead of per-event pings.

### 4.4 Learning layer — partial
`initiative_dedup_feedback` (v0.7.10) records respond actions
(acknowledged/dismissed/snoozed). **Remaining:** feed those outcomes
back into generator thresholds — e.g. repeated dismissals of a dedup
key family raise that generator's min_priority or extend its cooldown
(per-type multiplier persisted in the initiative store). This is a
self-contained follow-up spec; do not bolt it onto Phase 2.

### 4.5 Safety layer — now has teeth
Allow-listed registry (nothing unregistered executes), risk tiers,
human-owner approval for mutating/outbound, fail-closed owner identity,
assignment history as audit trail, `MAX_PENDING_INITIATIVES` back-
pressure. **Remaining:** rollback metadata on mutating ActionSpecs
(inverse action name where one exists), and approval expiry (BLOCKED
jobs older than N days auto-cancel with notification).

### 4.6 Monitoring layer — exists
Self-initiative types (SUBSYSTEM_HEALTH, DATA_QUALITY, OPERATIONAL,
...) plus LoopStats plus telemetry touches already make the agent
monitor itself. The SYSTEM type (Task 12) extends this to host
infrastructure. No new framework needed.

### 4.7 Integration layer — agent-supplied, not Colony-owned
SUPERSEDED BY §3.1: Colony does not own external API clients. The
agent observes through its existing Hermes connections and reports
into the observation store; Colony's loaders read observations. The
only direct integrations Colony keeps are self-observations (its own
health, stores, and graph).

### 4.8 Configuration layer — exists, consolidate as you go
`AutonomyConfig.from_env()` + `InitiativeConfig.from_env()` is the
pattern. Each new generator adds its thresholds to `InitiativeConfig`
rather than scattering `os.environ` reads (the env-var split that
caused Bug 4 is the cautionary tale).

---

## 5. Framing Guardrail — Colony Is the Agent's Brain

Colony builds **the agent's** initiatives, not the owner's. The owner
interacts with the agent; the agent thinks with Colony. Owner needs and
agent initiatives overlap exactly the way they do with any assistant —
a promise the *owner* made still becomes the *agent's* task to track
and act on — but the addressee of every initiative is the agent.
Relationship tracking exists so the agent can manage *its own*
relationships, built with whoever it interacts with on the owner's
behalf.

Where the framing is structurally enforced today:
- Initiatives are a queue the agent polls, claims, acknowledges, and
  completes — never a notification feed to the human.
- Self-initiative types (capability gaps, knowledge acquisition,
  behavioral correction, subsystem health) are the agent's
  introspection about itself.
- The MANAGES-edge gate: relationship work is generated only for
  people the agent manages, not for everyone in the graph.
- Owner exclusion (Bug 4): "build a relationship with my own operator"
  is a category error and is now impossible, fail-closed.
- Approval boundaries are about *whose authority*, not *whose work*:
  the agent initiates; the human authorizes mutations and outbound.

Known leaks to clean up (fold into the decision-layer work, not
one-offs):
- Legacy action hints phrased as notifications — `remind_user`,
  `notify_user`, "Send a message or schedule a call". These frame the
  agent as a relay. Replace with registered capabilities or drop the
  hint and let the decision policy choose the disposition.
- Any future generator that defaults to "tell the owner" as its
  suggested action should instead emit the situation and let the agent
  decide. Colony describes; the agent disposes.

**Agent-name agnosticism:** Colony is a public project; no agent name
may appear in sidecar code, schema defaults, or registry entries.
Identity comes from `COLONY_AGENT_NAME` (display name) and
`COLONY_WORKER_NODE_ID` (queue worker id). A regression test greps the
sidecar source to enforce this; plugin examples use placeholders.

**Review rule for new generators:** ask "is this a task the AGENT
should evaluate and act on?" If the only conceivable action is
"forward to the human," it belongs in the briefings system (the
owner-facing channel), not the initiative queue.

---

## 6. Deploy on the Agent's Machine & Watch It Work

### 6.1 Update Colony

```bash
cd ~/ColonyAI && git fetch origin && git checkout claude/colony-initiative-pipeline-98ye3x
pip install -e ./sidecar          # or: pip install -U colonyai once released
colony service restart            # or: launchctl kickstart -k gui/$UID/ai.aevonix.colony-sidecar
```

Set before/at restart (in the service environment):

```bash
export COLONY_OWNER_CONTACT_ID="cid-..."   # owner's CID, Person UUID, or unambiguous name
export COLONY_SYNC_DOMAINS="coding,task,system"  # start small; add domains as the agent proves them
# COLONY_HOST_CONTACT_ID still works but logs a deprecation warning
```

The `context` column migrates automatically on first start. Watch the
startup log for `ObservationStore initialized` and — if the owner env
is wrong — the CRITICAL `OWNER IDENTITY NOT RESOLVED` line (fix it;
relationship generation stays off until you do).

### 6.2 Update the Hermes plugin

```bash
cd ~/ColonyAI/plugins/hermes-plugin && ./install.sh --poller
hermes cron create --name colony-queue-worker --schedule 'every 5m' \
  --script colony-queue-worker.py --no-agent
```

Then add BOTH webhook routes from `examples/webhook-config.yaml`
(`colony-initiatives` updated with the five decision verbs;
`colony-jobs` new) to `~/.hermes/config.yaml` and restart the gateway.

### 6.3 Watch it work (smoke sequence)

```bash
H='-H "X-API-Key: $COLONY_API_KEY" -H "Content-Type: application/json"'

# 1. Sensor health — all domains empty on first boot
curl $H http://127.0.0.1:7777/v1/host/observations

# 2. After one autonomy tick: stale domains have sync jobs queued
curl $H http://127.0.0.1:7777/v1/host/queue/jobs/pending
#    → agent_sync_coding / agent_sync_task / agent_sync_system jobs

# 3. Within ~5 min the queue worker hands one to the agent; it observes and
#    reports. Verify the sensor filled in:
curl $H http://127.0.0.1:7777/v1/host/observations/coding

# 4. Next tick: initiatives generated FROM her observations
curl $H "http://127.0.0.1:7777/v1/host/initiatives?status=pending"
#    → e.g. "Investigate failing CI on <PR>", entity_id, full context

# 5. Manual end-to-end without waiting on real repos — inject a failing
#    observation, watch the initiative appear, then clear it:
curl -X POST $H http://127.0.0.1:7777/v1/host/observations -d '{
  "domain": "system", "reported_by": "manual-test",
  "observations": [{"entity_id": "test-svc",
    "payload": {"status": "degraded", "error_rate": 0.4}}]}'
#    ... after a tick: "Investigate test-svc: degraded" appears.
curl -X POST $H http://127.0.0.1:7777/v1/host/observations -d '{
  "domain": "system", "reported_by": "manual-test",
  "observations": [{"entity_id": "test-svc",
    "payload": {"status": "healthy", "error_rate": 0.0}}]}'
curl -X POST $H http://127.0.0.1:7777/v1/host/initiatives/<id>/context/refresh
#    → status "cancelled", stale_reason "condition_cleared" (auto-close)
```

Log lines that prove the loop is alive: `Requested <domain> observation
sync`, `Recorded N observation(s) for domain <d>`, `Phase initiative: N
new proposals`, `Blocked <risk> job ... awaiting owner approval` (when
the agent escalates a mutating action).

## 7. Operational Notes

- **Env:** set `COLONY_OWNER_CONTACT_ID` (CID, Neo4j Person UUID, or
  unambiguous display name). `COLONY_HOST_CONTACT_ID` still works but
  warns. If unset/unresolvable: CRITICAL log at loop start and
  relationship generation stays off — everything else runs.
- **Migration:** automatic and idempotent (`context` column added on
  first store open). Old rows return `context: {}` over the API.
- **API additions:** `entity_id`, `context_durability`, populated
  `context`/`target_agent_id` in initiative responses;
  `POST /initiatives/{id}/context/refresh`.
- **Behavior change:** commitments now surface as `commitment`-type
  initiatives instead of `scheduling`; agents filtering on
  `initiative_type` should add the new types.

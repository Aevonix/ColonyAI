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
Colony = brain (generates initiatives). Aeva = decision layer (polls,
decides what to execute, when to communicate, when to stay silent).
Colony never sends messages directly; Aeva is the sole decision-maker
for outbound communication.

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
| Neo4j graph | `Person.id` (UUID — **not a slug**; the "jane-ann-doe" slug format does not exist in this codebase), `Person.name` | **no back-reference to CID**; reverse lookup goes through `Contact.person_node_id` |

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
- `mutating` / `outbound` → requires **human owner** approval. Aeva
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

---

## 3. Remaining Phase 2 — data-source integrations

The enum is done; **the real work is context loaders**. Each remaining
type follows the same recipe: data source → `_load_<type>_context()`
(batch + per-entity modes) → `_generate_<type>_initiatives()` →
subject-keyed dedup → registered actions.

| Task | Type | Data source | Dedup | Loader mode |
|------|------|-------------|-------|-------------|
| 6 | `AGENT_ACTION` expansion | GitHub API, local git, system health | `agent_action:{entity}:{action}` | volatile, per-entity |
| 7 | `CALENDAR` | Google/Apple Calendar | `calendar:{event_id}` | volatile, per-entity |
| 8 | `RESEARCH` | arXiv, HuggingFace, notes | `research:{paper_id}` | durable |
| 9 | `TASK` | GitHub issues, local lists | `task:{task_id}` | durable |
| 10 | `CODING` | GitHub PRs, CI status | `coding:{pr_id}` | volatile, per-entity |
| 11 | `PROJECT` | GitHub milestones/boards | `project:{milestone_id}` | durable |
| 12 | `SYSTEM` | health metrics, logs | `system:{service_id}` | volatile, per-entity |

Loader interface (both modes from the start):

```python
async def _load_coding_context(self, entity_id: str | None = None) -> dict | None:
    """Batch mode (None): populates self._context["coding"].
    Per-entity mode: returns a fresh snapshot for one PR and is
    registered in rebuild_context()."""
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
machine is the execution engine. Aeva registers as an external worker
claiming `agent_action` jobs. v0.16.0 adds the allow-list in front of
it. **Remaining:** map registered action names to Hermes toolsets in the
worker; per-action timeout/retry policy from the `ActionSpec`.

### 4.2 Decision layer — Aeva-side, keep it there
Colony ranks (priority, dedup, cooldowns); Aeva decides. The decision
inputs are now complete: subject (`entity_id`), situation (`context`),
staleness contract (`context_durability` + `context_captured_at`),
risk (`risk` in job payloads). **Remaining:** a `decide` prompt/policy
in the Hermes plugin: act / snooze / dismiss / escalate, written back
via the existing `/initiatives/{id}/respond` feedback endpoint.

### 4.3 Communication layer — exists, keep Colony out of it
Delivery bridge + rate limiter + quiet hours + channel registry already
gate outbound pushes; the v2.2 webhook spec governs payload hygiene (no
raw dumps). The rule stands: Colony never composes user-facing
messages; it pushes structured payloads and Aeva decides whether/what
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

### 4.7 Integration layer — this IS remaining Phase 2
Each external integration (GitHub, calendar, arXiv/HF) should be a
thin async client owned by its context loader, configured by env vars,
absent-by-default (loader skips cleanly when unconfigured — same
defensive pattern the graph loaders already use).

### 4.8 Configuration layer — exists, consolidate as you go
`AutonomyConfig.from_env()` + `InitiativeConfig.from_env()` is the
pattern. Each new generator adds its thresholds to `InitiativeConfig`
rather than scattering `os.environ` reads (the env-var split that
caused Bug 4 is the cautionary tale).

---

## 5. Operational Notes

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

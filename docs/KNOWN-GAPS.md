# Known gaps — features that exist in code but are not (fully) wired

Honest inventory, verified by cross-referencing call sites (last full audit:
v0.28.x cycle). Everything here compiles and imports; what it does NOT do is
run in a live deployment. When one of these gets wired, delete its entry.

## Partially wired (works, with a missing half)

- **Briefing aggregators** — the composer now receives real relationship and
  goal aggregators (wired at startup when the graph / goal engine exist).
  Calendar, anomaly, mind-model, and synthesis sections still use stubs:
  `CalendarAggregator` wraps a `CalendarIntegration` class that does not
  exist in-repo, and no concrete anomaly/mind-model/synthesis aggregators
  have been written. Those sections render empty.
- **ConditionWorker** — the system-level checks (commitment overdue flip,
  affect decline, surprise accumulation) run hourly from the autonomy loop.
  The per-goal external-condition path (`handle_check_condition` for
  email_reply / deployment_health / api_response) has no producer: nothing
  enqueues `check_condition` jobs, so goals blocked on external conditions
  are not auto-unblocked.
- **`cognition.requested` event** — emitted with a full spawn spec
  (system_prompt, model, tools_allow) but no shipped consumer spawns a
  session from it; the hermes plugin only caches events as context blurbs.
  The working per-turn path is the inline introspection
  (`cognition/introspection.py`).
- **Gate Layer 6 secondary review** — fails open (always passes) unless a
  review LLM client is injected; the pipeline logs a loud boot warning when
  enabled without one. Known configuration state, not hidden.

## Unwired (feature code with zero callers)

- `gate/rejection.py` — `RejectionFeedbackLoop` (retry-with-escalation when
  the gate blocks) is never constructed.
- `autonomy/schedule_adapter.py` — `ScheduleAdapter` (MetaLearner patterns →
  cron adjustments) is never constructed.
- `chain/consensus.py` — Raft-lite consensus (`RaftNode` et al.) advertised
  by `chain/__init__` is never imported.
- `identity_bootstrap/self_query.py` — self-referential-query corpus
  grounding has no callers.
- `world_model/extraction/structured_importer.py` — `StructuredImporter`
  (calendar/contact structured import) has no callers.
- `task_queue/handlers/email_handler.py` — `job_type="send_email"` exists in
  no JobType enum and is registered nowhere; the send_email decomposition
  template routes via `custom` instead.
- Desktop/browser job handlers — `build_default_handlers` accepts
  desktop/browser configs, but the backing `colony_sidecar.desktop` /
  `colony_sidecar.browser` packages do not exist; `JobType.DESKTOP` /
  `JobType.BROWSER` jobs have no handler.
- `chain/keys_cli.py`, `chain/sentinel_cli.py`, `chain/admin_cli.py` — no
  `__main__`, no console-script entry, referenced nowhere.
- `skills/versioning.py`, `skills/marketplace.py`,
  `contacts/importers/email_contacts.py`, `gate/pending_dispatch.py` —
  no consumers.

## Missing primitives (a scheduled task or doc references work that has no implementation)

- **World-model pruning** — there is no prune/stale-removal primitive in the
  world model store. The former daily `world_model_prune` scheduler task was
  a no-op lambda reporting success and has been removed rather than left
  lying; add the primitive first, then re-register the task.
- **`COLONY_WORKER_ENABLED`** — mentioned in ROADMAP docs; no code reads it
  (workers gate on `COLONY_WORKERS_MODE`).
- **Autonomy `_phase_events` per-event routing** — matching message events
  are counted but not routed; only aggregate counts are used.

## Known mechanisms (documented so the log noise is interpretable)

- **"Unclosed client session" (aiohttp) after tick-budget cancellations** —
  when a tick exceeds `COLONY_TICK_BUDGET_SECS` the whole-tick `wait_for`
  cancels whatever await is in flight; a cancellation landing inside an
  aiohttp request can interrupt the session unwind and the GC later logs the
  unclosed session. Mitigated (v0.28.x): the world-LLM extraction timeout is
  capped under the budget, per-recall touch tasks are strongly referenced,
  and the research gatherer closes its per-call graph driver. Residual noise
  right after a budget-exceeded tick is expected and harmless.
- **ResponseGuard fails open (ALLOW) on internal error** — deliberate and
  logged (`gate/response_guard.py`); the guard runs shadow-first in live
  deployments. The L6 review layer inside the gate pipeline fails closed.

## Settlement semantics (by design, documented here so nobody "fixes" it)

- Workspace concerns raised from **commitments** settle durably on resolve
  (the source closes). Concerns raised from **anomalies / benchmark
  regressions** have no settler: resolving them suppresses the dedup key for
  `COLONY_WORKSPACE_RESOLVED_TTL_HOURS` (default 24h), after which a source
  that is STILL firing legitimately returns. That re-raise is intentional —
  a day-old still-live anomaly deserves attention again.

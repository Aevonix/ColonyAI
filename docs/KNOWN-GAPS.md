# Known gaps — features that exist in code but are not (fully) wired

Honest inventory, verified by cross-referencing call sites (last full audit:
v0.30.0 cycle). Everything here compiles and imports; what it does NOT do is
run in a live deployment. When one of these gets wired, delete its entry.

Closed in v0.30.0 (kept briefly for the record): per-goal external-condition
polling (goals now self-unblock), anomaly/synthesis/goal/relationship/calendar
briefing aggregators, world-model pruning, self-referential-query grounding.

## Partially wired (works, with a missing half)

- **Mind-model briefing section** — `HealthSnapshot` (sleep/readiness) and
  predicted-load remain a protocol + stub with NO backing data source in the
  system. Deliberately not wired: fabricating health numbers would violate
  the measurement doctrine. Wire only when a real health/wearable source
  feeds the mind model.
- **Gate Layer 6 secondary review** — fails open (always passes) unless a
  review LLM client is injected; the pipeline logs a loud boot warning when
  enabled without one. Known configuration state, not hidden.

## Deliberate no-builds (division of responsibility with the host agent)

Colony is the cognitive substrate; the host agent framework (e.g. Hermes)
owns sessions, tool execution, message transport, and cron. These stay
unbuilt HERE by design:

- **`cognition.requested` consumer** — the event carries a full spawn spec
  (system_prompt, model, tools_allow with real tool names), but spawning a
  restricted agent session is the host framework's job. A deployment that
  wants it should implement a thin host-plugin subscriber; the sidecar's
  working per-turn path is the inline introspection
  (`cognition/introspection.py`).
- **Email/desktop/browser job handlers** — outbound messaging goes through
  the host gateway (delivery bridge); Colony never sends email itself. The
  desktop/browser packages were scaffolding for host-side capabilities and
  the dead EmailHandler was removed in v0.30.0. `JobType.DESKTOP`/`BROWSER`
  remain enum values with no handler.
- **ScheduleAdapter** — removed in v0.30.0. Its contracts were
  unimplementable (the real MetaLearner has no pattern API; the
  AutonomyScheduler is interval-based, not a cron store) and mutating host
  cron jobs would cross into the host framework's domain.

## Unwired (feature code with zero callers)

- `gate/rejection.py` — `RejectionFeedbackLoop` (retry-with-escalation when
  the gate blocks) is never constructed. Pairs with the gate rebuild track;
  wire it when the gate itself runs enforcing (it is shadow-first today).
- `chain/consensus.py` — Raft-lite consensus scaffolding for multi-colony
  federation; single-colony deployments don't need it. The chain package
  docstring now labels it scaffolding.
- `world_model/extraction/structured_importer.py` — superseded in practice
  by the connector → populator ingest path, which already imports structured
  observations into the world model.
- `chain/keys_cli.py`, `chain/sentinel_cli.py`, `chain/admin_cli.py` — no
  entry points; federation-era scaffolding.
- `skills/versioning.py`, `skills/marketplace.py`,
  `contacts/importers/email_contacts.py`, `gate/pending_dispatch.py` —
  no consumers.

## Known mechanisms (documented so the log noise is interpretable)

- **"Unclosed client session" (aiohttp) after tick-budget cancellations** —
  when a tick exceeds `COLONY_TICK_BUDGET_SECS` the whole-tick `wait_for`
  cancels whatever await is in flight; a cancellation landing inside an
  aiohttp request can interrupt the session unwind and the GC later logs the
  unclosed session. Mitigated (v0.29.0): the world-LLM extraction timeout is
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

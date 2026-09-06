# Known gaps and retired scaffolding

This is a source inventory, not a production health report. The original
extended-profile audit was written during the v0.30.0 cycle. The 1.0
consolidation has qualified narrower canonical-memory and native-host paths;
their current behavior and limits live in the README and capability guides.
Enabling a profile still requires checking its actual effects.

Known current limits include complete erasure of unlinked historical data and
host transcripts, cross-database recovery, empirical retrieval quality beyond
the tested cases, and deployment-specific hardware coverage. Native skill
evaluation needs an independently supplied task oracle; it does not establish
general self-improvement simply by recording a successful review.

## Partially wired (works, with a missing half)

- **Mind-model briefing section** — `HealthSnapshot` (sleep/readiness) and
  predicted-load remain a protocol + stub with NO backing data source in the
  system. Deliberately not wired: fabricating health numbers would violate
  the measurement doctrine. Wire only when a real health/wearable source
  feeds the mind model.
- **Gate Layer 6 secondary review**: without an injected reviewer, it returns
  an unflagged result. A configured reviewer exception or malformed JSON flags
  the result as a review error. Neither the unconfigured path nor the separate
  ResponseGuard shadow mode establishes enforcement.
- **ResponseGuard applied-output receipts** — guarded candidates now carry an
  exact candidate digest, and the proactive send path honors enforce verdicts,
  but the audit store records evaluations rather than durable proof of the
  bytes a transport actually withheld or emitted. The Hermes plugin also
  remains shadow-only on current Hermes because its post-LLM hook cannot mutate
  replies. Consequently the server intentionally leaves the Tom2 enforcement
  evidence probe unset and level 2 stays capped. A future transport-owned
  mediator must persist policy-, candidate-, and applied-output-digest receipts
  before this gap can close. Do not infer enforcement from verdict row counts.

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
- **Built-in initiative executor is the no-host-agent path** — the one
  deliberate exception to the division above:
  `services/initiative_executor.py` exists specifically for same-machine
  deployments that have NO host agent, closing the autonomy loop in-process
  (ReasoningLoop + Colony tools against pending initiatives). Deployments
  that DO run a host agent with its own execution plane should leave
  `COLONY_EXECUTOR_ENABLED=false` (the default): enabling both means two
  executors competing to claim the same initiatives.

## Removed during the 1.0 consolidation

The following modules had no runtime imports, registered entry points or
configured loaders in this repository. They are retained in Git history rather
than offered as unfinished features:

- Raft consensus and its isolated unit suite, plus the unregistered chain key,
  sentinel and administrative CLIs. Existing chain identity, storage and
  validation consumers remain intact.
- The federation skill marketplace and its unused protocol, plus the unused
  skill schema-version helper. Hermes owns the supported native skill review
  path; the existing initiative executor registry remains available.
- The unused structured-world importer and email-header contact importer.
  Existing connector/populator and supported contact import paths remain.

`gate/pending_dispatch.py` remains a small compatibility re-export. Removing
that alias would not simplify the gate implementation or its stored state.

## Known mechanisms (documented so the log noise is interpretable)

- **"Unclosed client session" (aiohttp) after tick-budget cancellations** —
  when a tick exceeds `COLONY_TICK_BUDGET_SECS` the whole-tick `wait_for`
  cancels whatever await is in flight; a cancellation landing inside an
  aiohttp request can interrupt the session unwind and the GC later logs the
  unclosed session. Mitigated (v0.29.0): the world-LLM extraction timeout is
  capped under the budget, per-recall touch tasks are strongly referenced,
  and the research gatherer closes its per-call graph driver. Residual noise
  right after a budget-exceeded tick is expected and harmless.
- **ResponseGuard failure behavior is surface/mode specific** — exact
  text/artifact surfaces fail open while observing in `shadow` and fail closed
  on a configured-check outage in `enforce`; exact real-time speech surfaces
  are excluded. The static contract is documented in
  `docs/response-guard-surface-policy-v1.md`. The L6 review layer inside the
  separate gate pipeline fails closed.

## Settlement semantics (by design, documented here so nobody "fixes" it)

- Workspace concerns raised from **commitments** settle durably on resolve
  (the source closes). Concerns raised from **anomalies / benchmark
  regressions** have no settler: resolving them suppresses the dedup key for
  `COLONY_WORKSPACE_RESOLVED_TTL_HOURS` (default 24h), after which a source
  that is STILL firing legitimately returns. That re-raise is intentional —
  a day-old still-live anomaly deserves attention again.

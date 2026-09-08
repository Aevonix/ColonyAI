# Task forecasts and expected replies

Colony keeps forecasts in the existing expectation ledger and reply waits in the commitment database. Hermes owns native tasks and scheduling. The selected delivery outbox owns attempted, acknowledged and uncertain sends.

The owner work view retains the original forecast horizon and unchanged-prior
decision alongside later measured outcomes. These are shadow comparisons;
suggestions remain disabled while their value is unproven. An unknown actual
serving processor does not establish comparable conditions. Request context
omits raw shadow decisions and private outcome/configuration records.

A prospective native internal-review attachment issues a task-turnaround forecast when expectations are enabled. Independent native lifecycle readback records the outcome. The next review of the same registered action uses a bounded estimate learned from those receipts. The estimate includes queue delay; native execution time is recorded separately. Completion establishes elapsed time, not answer quality. The native ledger does not identify the actual serving processor, so that field remains unknown.

Forecast revisions preserve previous probabilities and horizons. Only the latest pending revision enters current context; original forecasts drive calibration, preventing repeated revisions from inflating its sample count. Explicit outcome corrections remain append-only. Missing coverage is unresolved, and paused, cancelled or unavailable work is censored. Historical causal-edge survival remains a self-consistency diagnostic and is excluded from predictive scores.

Reply waits start their response clock from a retained transport receipt. A provider-linked reply can precede a delayed acknowledgment. Exact contact and parent-message references resolve the wait; a reply does not fulfill its parent obligation. Quiet hours and availability use named timezones and actual UTC instants. Changes to source evidence cancel stale waits before native preparation.

Task registration requires the current parent work claim and exact canonical source versions. It grants only a local status review. Public outbound dispatch additionally requires existing exact task authority and an absent outbox operation. Private adapters may use their existing message authority after the same temporal readiness check. No per-message RCS approval is introduced.

Trusted durable adapters can use `/v1/host/transport/ingress` to admit metadata,
claim one native handoff and read its processing receipt. The existing
communications database holds metadata; the adapter owns pending payloads.
Exact verified handles resolve contacts server-side. Canonical ingestion must
match the bound source, contact and session before processing settles. Unknown
handoffs remain pending, and erasure marks linked receipts for payload cleanup.
This does not prove successful generation or a provider send.

Prepared WhatsApp followup review additionally requires a fresh connected
interval, all sequence receipts above its connection floor through its watermark,
available media and no unresolved recipient activity. Missing coverage holds the
review. Authority verification for an original send remains independent. This
proves only local intake coverage, not complete provider history; deployment
adapters must separately qualify this protocol before claiming silence.

Integration points:

- Include `api.routers.temporal_followups.router` with existing owner/scoped credentials.
- Register `NativeFollowups.reconcile` on the existing default-board Hermes dispatch tick.
- Feed actual transport receipts and `CommsLog.match_reply` results through trusted adapters.
- Recheck source bindings, current waiting state and existing effect authority immediately before sending.
- Keep recipient configuration and hardware adapters private to each deployment.

The focused native qualification uses actual HTTP routes and Hermes Kanban transitions with network and inference disabled. It covers concurrent dispatch, attachment races, cancellation, lost acknowledgments, measured completion, a changed subsequent forecast and removal of erased receipts from future estimates. Live deployment validation remains a separate release step.

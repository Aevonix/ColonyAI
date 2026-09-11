# Task forecasts and expected replies

Apsimo keeps forecasts in the existing expectation ledger and reply waits in the commitment database. Hermes owns native tasks and scheduling. The selected delivery outbox owns attempted, acknowledged and uncertain sends.

The owner work view retains the original forecast horizon and unchanged-prior
decision alongside later measured outcomes. These are shadow comparisons;
suggestions remain disabled while their value is unproven. An unknown actual
serving processor does not establish comparable conditions. Request context
omits raw shadow decisions and private outcome/configuration records.

A prospective native internal-review attachment issues a task-turnaround forecast when expectations are enabled. Independent native lifecycle readback records the outcome. The next review of the same registered action uses a bounded estimate learned from those receipts. The estimate includes queue delay; native execution time is recorded separately. Completion establishes elapsed time, not answer quality. The native task ledger alone does not identify the actual serving processor. Registered request callbacks retain provider-reported model labels; missing callbacks remain unknown.

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

Registered owner Hermes turns also contribute a separate remaining-duration
observation through the existing execution observer. It uses the actual native
`pre_llm_call`, API start/response/error, session-end and delegated-child hooks.
This includes admitted owner conversation, cron and worker turns where those
hooks and identity bindings run; it does not imply every cron, child or auxiliary
model call is observed. The same observation endpoint carries the metadata; error callbacks join the
existing lifecycle observations. No extra scheduled work is added.

The prediction origin is the server's first accepted API-start observation,
provided the turn's sequence-1 start was already seen and Hermes reports its
first, unretried request. It measures observation-to-terminal time, separately
from review attachment-to-terminal time and queue delay. It is not backdated to
the host clock. A missed first observation cannot be repaired by issuing a
prediction after the response. The unchanged 480-second fallback is a comparison
prior, not a promised latency or execution timeout.

New execution forecasts use `execution-duration-observation-v2` and a separate
`hermes-execution-v2:` cohort. Their `receipt-duration-median-prior4-v2` estimator
uses up to 50 original outcomes, retaining a four-observation prior:
`(4 * prior + n * median) / (4 + n)`. It removes the former permanent half-prior
floor and double-prior ceiling. The window and prior still limit adaptation;
the retained empirical hit fraction is explicitly not calibrated confidence.
The same issue-time samples also freeze the former clamped calculation as a
comparison, alongside the unchanged prior. These are fields on the existing
forecast, not additional forecasts or independent sample counts. Samples share platform,
root/child class, actual native runtime kind, selected profile fingerprint,
requested model/provider/API mode, observed request output-limit policy, tool count and the
first request input-token bucket (up to 4K, 16K, 64K, or above). These are
initial forecasting conditions. Later context growth or tool discovery stays
eligible when recorded routing, protocol and output-limit policy remain stable.
Terminal receipts retain the observed input-bucket and tool-count evolution as
diagnostic covariates. Missing
configuration remains incomparable; no task meaning is guessed from prose. The adapter
reads only output-limit fields from the final native request payload. A complete
payload with no limit explicitly forms a provider-default cohort, with the actual
server cap unknown. It is separate from explicit numeric limits and missing or
truncated request telemetry. The optional agent-level cap is not a substitute for
the final request. This reader supports Chat Completions, Anthropic Messages and
Codex Responses payloads. Other wire formats, conflicting or invalid limit fields
remain unknown. No prior
censored outcome is relabeled by this change, and the new policy field changes
the cohort fingerprint. The most recent
eligible historical actual response-model label selects a single training
cohort, frozen in the original forecast. The model that will serve the next
request remains unknown until its response. An alias rebinding can therefore
produce a recorded duration that is incomparable with that forecast's training
cohort; a later prediction can learn from the newly observed processor without
rewriting the old prediction. Mixed processors, missing response labels or
request pairs, gaps, failures and interruptions retain explicit uncertainty.
Provider labels are not weight attestations, and task difficulty, machine
contention and hidden auxiliary calls are not yet measured covariates. Provider
configuration changes that retain the same response-model label are also not
attested by a provider-default observation.

For long requests whose hook body is truncated, Apsimo's existing request
middleware places only the request ID, output-limit policy and optional numeric
cap in Hermes' existing middleware trace. The observer accepts this marker only
for the same API request and only as the final request-changing trace entry.
Later middleware rewrites invalidate it; an available final body takes precedence.
No request cache or provider payload field is added. A missing trace or an
unqualified body remains unknown. This carries output-limit metadata only, not
the omitted prompt, and does not claim complete coverage of other request fields.

At most 128 request pairs live in one adjunct table in the existing turn ledger,
with the same seven-day operational retention. A predecessor can still write
its original fourteen-column execution rows after rollback. Upgrade the
sidecar before the adapter; roll back the adapter before the sidecar, because
the predecessor HTTP schema rejects the new optional metadata field. Forecast start and
terminal receipts retain compact aggregate counts and actual-model provenance,
without durable per-request pairs. They use empty-content canonical source metadata, producing no
semantic or lexical recall chunks and no claim, appraisal or opinion learning.
Existing source erasure and correction checks remove invalidated receipts from
future samples. Owner work context exposes only the existing compact shadow
forecast fields. Detailed original forecasts and source receipts remain in the
existing expectation and source ledgers. No timing suggestion, notification,
retry, approval, quality claim or work progression gate is enabled.

Existing V1 forecasts and receipt digests remain unchanged and readable. An
unfinished V1 forecast settles under its original strict context/tool comparison.
V1 outcomes cannot train V2 or fill a V2 qualification gate. Model aliases stay
separate even when an operator believes the spelling change is cosmetic.

Before claiming useful timing improvement, freeze the new method and cohort
contract before collecting future useful work. Retain every failure and
exclusion; score the first ten comparable natural outcomes in the selected
actual-model cohort. Compare each untouched original horizon with both its
frozen prior and its clamped counterfactual on that same new history. Check
absolute error, premature inspection and mean inspection lateness separately
from independent output quality. Declare a useful lateness target for the
intended inspection before observing outcomes; beating a poor prior alone is
insufficient. Old experiments retain their original denominators and gates. Do not count a parent and its child as independent completed tasks, or
manufacture work to fill the sample count. Local callback qualification is not
live benefit evidence.

Committed terminal observations are reconciled on later execution callbacks and owner work reads, up to 20 pending operational records per call within the seven-day retention window. A failed settlement can retry without changing the recorded terminal time or issuing a late forecast. Replaying an already settled outcome is idempotent. No new timer, queue or service is involved.

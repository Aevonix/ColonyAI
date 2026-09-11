# Declared reply forecasts

The expected-reply observer measures whether an exact reply is observed by an
already registered wait's declared horizon. It uses the existing expectation,
communications, temporal-wait and canonical-source stores. It adds no sender,
timer, database schema, permission or relationship score.

The initial method is `accepted-dispatch-reply-by-declared-horizon-v1`. Its
probability is a fixed, explicitly uncalibrated 0.5 baseline. The horizon comes
from the registered wait, not a learned estimate of a person's responsiveness.
Latency alone is not evidence of motivation, disrespect or frustration.

## Admission and observation

The existing `COLONY_EXPECTATIONS` switch and configured owner identity apply.
A trusted outgoing transport callback can freeze a forecast when the wait's
owner task and source versions are current, the actual dispatch acknowledgment
is bound, and the horizon is still ahead. The first method requires WhatsApp and
the existing single-account contract for the authenticated transport producer.
An accepted acknowledgment remains accepted; delivery or read status is never
inferred. Historical scans do not create forecasts.

An incoming reply must match the original producer, account, recipient and
provider message reference. The receipt must join completed durable ingress to
its exact current canonical source version. A legacy receipt without this join
does not qualify. A reply timestamp before forecast issuance stays unscored.
A linked reply establishes reception, not completion of the requested work.

After the horizon, a clock alone leaves the outcome unknown. A negative outcome
requires fresh, complete original-account coverage through that horizon and no
unresolved recipient activity during the interval. Disconnection, intake gaps,
missing media, erased activity and account ambiguity leave absence unknown.
A late reply alone does not establish that no earlier reply existed.

An unresolved forecast is censored if the wait is cancelled, expires, receives
a dispatched follow-up, or its parent work closes. The retained stop is neither
a miss nor evidence about the recipient. Existing callbacks or an exact wait
read observe this change; no timer is added. Already observed reply outcomes
remain historical evidence when the task is stopped later.
If canonical admission arrives later for an exact in-horizon reply whose event
time precedes the known stop, reconciliation appends a positive observation,
preserving the original censor in history. Cancellation and follow-up times
are retained in the existing wait payload. Legacy stops with no known time
remain censored. Pre-issuance replies do not hide later qualifying replies,
and canonical settlement reconciles every receipt in a native ingress batch.

## Inspection and recovery

The owner's existing temporal-wait view includes a compact `reply_forecast`
status: `pending`, `reply_observed_in_time`, `no_reply_with_coverage`,
`deadline_elapsed_unobserved`, `retrospective_unscored`, `source_unavailable`,
`cancelled`, `expired`, `intervened`, or `no_prospective_forecast`.
`suggestion_enabled` is always false. Existing
follow-up eligibility and sender behavior do not consume these scores.

Canonical-settlement and receipt-read retries reconcile the exact inbound
lineage. Coverage callbacks consider at most 100 newest due forecasts for their
actual account and connected interval. They do not exhaustively drain older
unresolved history. Exact reply callbacks exclude unrelated message references;
an owner wait read reconciles that one wait regardless of the broad limit.

An interrupted outcome write can settle on an existing retry without another
send. An interrupted forecast write can reuse a declaration already frozen in
the canonical ledger. If that declaration was never retained, a later replay
does not invent a retrospective forecast.

Owner-task and recipient-reply dependencies retain their separate person
scopes. Corrected, erased or otherwise unavailable evidence is excluded from
current projection, calibration and duration-sample selection. Raw forecast
and outcome history remains historical evidence. Empty receipt metadata adds
no lexical recollection text or appraisal jobs.

## Qualification

`sidecar/tests/test_reply_forecasts.py` exercises authenticated HTTP callbacks,
real stores, source settlement, correction and erasure, interruption/restart,
over 100 retained unknowns, and clock-only scoring exclusion with outgoing
network connections forbidden. These tests establish local wiring. A natural
declared-horizon exchange through a deployment's exact durable producer/account
is still required before claiming observed production value or learned timing.

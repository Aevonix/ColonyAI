# Source-grounded temporal assertions

Ordinary attributed turns now retain a small factual assertion projection in the canonical source ledger. Recall can distinguish an explicit correction, a dated change and an unresolved disagreement. This is an incremental memory behavior, not a truth engine or completion of all temporal reasoning.

## Ingestion and provenance

`TurnIdempotencyLedger.record_source` stores the source and queues its projection in the same transaction. The new `source_claim_jobs` and `source_claims` tables use the existing `turn-idempotency.db`. There is no additional database or vector index. Existing sources are not automatically backfilled. Historical checkpoints remain quotations because their individual messages do not attest the speaker and occurrence time.

The background consumer uses the named extraction role and its configured local fallbacks. The role's `timeoutSeconds` bounds each candidate and `deadlineSeconds` bounds the entire routing attempt. The consumer captures that total deadline and allows five additional seconds for dispatch overhead; a longer explicit background budget is no longer clipped by a fixed 40-second wrapper. Public routing defaults remain unchanged. Older tier-only configurations use SMALL with a 20-second timeout and no escalation. Concurrency is one. It starts with the sidecar; `COLONY_SOURCE_CLAIMS=off` stops extraction while retaining sources and previously derived records. An unavailable local role leaves durable pending work. Before each message, the worker renews its existing owned lease for the captured request bound plus 30 seconds to commit. This accommodates multi-message sources and role reloads without a heartbeat process. Process loss retries incomplete work; stale consumers cannot renew, commit or finish a reclaimed job.

Extraction accepts at most six assertions per string user message of at most 12,000 characters. Each must retain an exact contiguous source quotation, its message hash and character span. Subject and value must occur in that quotation. Unsupported, uncertain or rejected extraction remains raw source evidence. No confidence number is promoted to truth. The result records the extractor model alias and extraction version; changing the model does not change stored assertions.

Version 2 also requires a useful memory category and an inspectable reason for
later recall. These are unverified model judgments. Explicit personal-disavowal
phrases are checked against the full message so an extractor cannot clip them
away and turn a self-example into a preference. This is a limited English
validation rule, not general entailment checking. Persistent consumers reject
reasoning-only and unfinished provider output. See [memory quality](MEMORY-QUALITY.md).

The ordinary-turn outbox captures `occurred_at` once when a new turn enters durable delivery, unless a lifecycle timestamp was supplied. Retries reuse that value. The fallback timestamp means turn capture, not independently verified speech or event time. Source ingestion time remains separate. An explicit event date is separately stored as `event_at`. Unknown historical occurrence times remain unknown.

## Corrections, changes and conflicts

Assertion keys are scoped to the attributed contact and normalized subject/property. A first-person assertion means that contact. Normalization preserves Unicode and compares whole normalized values; substring containment is not agreement.

| Input | Stored effect | Recall effect |
| --- | --- | --- |
| Independent assertions give different values | Both keep their source and recording time | Overlapping validity becomes an unresolved conflict |
| Explicit correction names an existing assertion | New record links to the old ID; old record is retracted | Old value cannot become the current answer |
| Explicit change has an effective time | Old interval closes; new interval starts; supersession links remain | Historical queries can recover the earlier value |
| A change happens during a queried calendar day | Both intersecting intervals remain | Temporal history, without labelling disjoint intervals a contradiction |
| Source is erased during or after extraction | Claims tied to removed message hashes are deleted | Late extraction cannot restore them |
| A correction is erased | The older retraction link remains without erased text | Erasure does not silently revive the old value |

A newer timestamp alone never supersedes a conflicting assertion. Generated property names and prior-record matching can still fail; an unmatched correction becomes an independent assertion. The model is not permitted to invent a predecessor ID. The implemented projection does not grant authority, alter relationships or execute tools.

## Recall and time

Graph candidates and direct source hits locate assertion keys. Recall expands each key into its scoped, time-appropriate evidence before the existing shared reranker and context budget. An unresolved conflict is one atomic candidate: the packet cannot retain just the winning side or truncate away its qualifications. Raw source chunks have the projected spans removed; each remaining excerpt is still a contiguous quotation, including when source chunks overlap. Recognized graph summaries are candidate locators, rather than a second route for injecting superseded assertions.

Valid time, event time and recording time are different fields. Interval comparisons use canonical UTC timestamps. Supported query dates are ISO dates/datetimes, full English month dates and anchored today/yesterday/tomorrow; event queries also support trailing hours/days and since a date. Calendar days use the resolved contact/communication timezone. A historical assertion without a known validity start is not certified for that date. Raw quotations remain labelled with unknown validity. An unprojected source captured inside an event window is labelled `source_occurrence_only`, which does not establish the event's time.

Recognized unsupported ranges, multiple dates and week/month/year relative expressions are labelled unresolved instead of silently selecting the first date. The parser does not understand all natural-language temporal questions. It does not implement historical transaction-time queries such as reconstructing exactly what the system believed before an ingestion date.

One key expands to at most eight distinct values. A larger group is withheld; repeated identical values do not crowd out a distinct conflicting value. The shared five-result/character budget still applies, and a large atomic group can be omitted. This trades answer coverage for avoiding partial conflict presentation. It is not a universal guarantee that all relevant evidence will be found. Legacy graph memories without source lineage keep their existing behavior.

The memory-provider wrapper now distinguishes persistent state from source evidence, preserves uncertainty and permits clarification when a material contradiction remains.

## Operation and qualification

`GET /v1/host/memory/sources/claims/status?contact_id=...` uses the existing request scope and reports recent job states, attempts, model/version, errors and claim counts. Unavailable extraction leaves quotations usable; it does not block ordinary turn ingestion or the next conversation.

`sidecar/tests/test_source_claim_projection.py` exercises actual source ingestion, SQLite transactions, source FTS, projection and context assembly. Controlled extractor outputs make correction, conflict, valid-time, erasure, lease and model-swap checks reproducible. The router check verifies that disabled escalation makes only one provider call.

A separate private neutral run captured eight actual local-model extractions. Replaying those responses through the actual source and context path with explicit reference dates passed five checks: correction, unresolved disagreement, historical validity, effective-date transition and a calendar-day observation. The first run's mistaken March reference for a September correction is retained as a failed assessment. These are bounded behavior checks, not a representative accuracy benchmark, proof of model equivalence, graph performance test or production acceptance. A further check replayed that extraction through current source FTS/projection and the shared selector with the actual LAN reranker: five relevant evidence packets and three unrelated-question abstentions passed. Reranker calls took 110–191 ms. It reused a private threshold from an earlier development set without fitting a new threshold or adding a public default. Before promotion, observe a real ordinary turn produce a completed projection and retrieve its grounded result through the deployed memory provider.

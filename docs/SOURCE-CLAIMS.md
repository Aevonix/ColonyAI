# Source-grounded temporal assertions

Ordinary attributed turns now retain a small factual assertion projection in the canonical source ledger. Recall can distinguish an explicit correction, a dated change and an unresolved disagreement. This is an incremental memory behavior, not a truth engine or completion of all temporal reasoning.

Machine-authored reports and other retained text quotations can receive an
[attributed source annotation](SOURCE-ANNOTATIONS.md) without manufacturing a
USER assertion or rewriting the original evidence.

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

The injected assertion cards share repeated exact quotations from the same
canonical message version. A packet-local `evidence_ref` links each assertion to
its quotation, source, message hash, speaker and report/recording time. Assertion
IDs, values, event precision, validity intervals, correction ancestry and opening
anchors remain attached to their cards. Different message versions are never
merged. Internal assertion JSON is rendered once as data; raw source text stays
quoted, including text that resembles JSON. The shared character budget measures
the complete rendered packet, so a repeated passage does not consume the budget
again for each property. This changes presentation, not retrieval ranking or
whether a claim is true. A report timestamp does not establish event order.

For a current-state query, a short message of at most 2,000 characters remains
one quoted evidence unit when partial extraction would separate its clauses.
This reuses the complete-procedure source check: every stored assertion must
still be eligible, without a superseded/retracted sibling or conflicting peer.
The quotation carries its canonical message hash and property-history opening
anchors. Its unclaimed clauses are no longer independently ranked against the
asserted clause from the same message. Changed messages retain the existing span
suppression; historical queries retain assertion/time semantics. If the complete
message cannot fit the injection budget, an opening notice replaces it instead
of presenting a convenient prefix. This preserves context but does not establish
that a model will interpret the quotation correctly. Derived media assertions
retain their structured evidence cards and exact segment/recognizer lineage;
a machine transcript's display prefix does not trigger short-message expansion.

Valid time, event time and recording time are different fields. Interval comparisons use canonical UTC timestamps. Supported query dates are ISO dates/datetimes, full English month dates and anchored today/yesterday/tomorrow; event queries also support trailing hours/days and since a date. Calendar days use the resolved contact/communication timezone. A historical assertion without a known validity start is not certified for that date. Raw quotations remain labelled with unknown validity. An unprojected source captured inside an event window is labelled `source_occurrence_only`, which does not establish the event's time.

Recognized unsupported ranges, multiple dates and week/month/year relative expressions are labelled unresolved instead of silently selecting the first date. The parser does not understand all natural-language temporal questions. It does not implement historical transaction-time queries such as reconstructing exactly what the system believed before an ingestion date.

Time constraints come from request text outside valid JSON objects/arrays,
code fences, blockquotes and quoted report passages. A timestamp inside supplied
evidence does not require the source to have been captured at that instant.
A quoted time operand, such as `recorded on "2026-03-12"` or `"last 2 hours"`,
keeps its existing interpretation, including unresolved relative ranges.
These syntax rules affect only temporal interpretation; lexical and
semantic retrieval retain the complete original query. Unmarked narrative and
malformed or truncated pasted structures can remain ambiguous.

One key expands to at most eight distinct values. A larger group supplies a compact incomplete-history marker with a source/claim anchor; it does not choose a value. The shared five-result/character budget still applies. An oversized atomic assertion bundle can similarly supply an opening anchor when the marker fits. Repeated identical values do not crowd out a distinct conflict. This remains bounded discovery, not a guarantee that every relevant source is found. Legacy graph memories without source lineage keep their existing behavior.

## Complete source opening and event precision

Extraction version 5 retains `event_time`: the exact source expression, known
precision, or explicit unresolved status. A supported optional event phrase such
as "before sending this message" does not erase an otherwise supported assertion.
It bounds the event before source occurrence, without assigning that timestamp
to the event. Calendar-day expressions retain their day interval. Invented date
text and unresolved required `valid_from_text`/`valid_to_text` still reject a
proposal. The existing semantic reviewer checks the complete source and its
conditions; this representation does not independently prove entailment. Earlier
assertions without precision metadata remain labelled legacy precision unknown.

`colony_memory_read_source` opens a source ID/version supplied to the current
participant. The existing `POST /v1/host/memory/read` accepts additive canonical
selectors alongside graph reads: `source_id`, `source_version`, `session_id`, and
optional `source_view=assertions` plus an anchored `claim_id`. Source pages contain
at most 4,096 characters of serialized canonical messages and applicable
attributed corrections. History pages contain at most eight assertions, including
superseded/retracted status. Histories above 10,000 scoped assertions return an
explicit read-limit error. `next_offset`, `offset_unit` and `read_revision` support
continuation; changed evidence requires restarting at offset zero. A partial page
is not a complete procedure. Graph IDs are not canonical source IDs.

Opening checks current scope, version, attribution and annotations. The adapter
registers exact actual tool output and source dependencies, which count as
supplied only when that output reaches a model request. Changed erasure freshness
withholds the old result. A later turn must reopen an old source-read result
instead of replaying it as fresh evidence. This uses existing native request
reconciliation. Direct API consumers must likewise apply returned source
versions/watermark before using delayed results. Actual reader completeness and
answer quality still require behavioral qualification.

The memory-provider wrapper now distinguishes persistent state from source evidence, preserves uncertainty and permits clarification when a material contradiction remains.

## Operation and qualification

`GET /v1/host/memory/sources/claims/status?contact_id=...` uses the existing request scope and reports recent job states, attempts, model/version, errors and claim counts. Unavailable extraction leaves quotations usable; it does not block ordinary turn ingestion or the next conversation.

`sidecar/tests/test_source_claim_projection.py` exercises actual source ingestion, SQLite transactions, source FTS, projection and context assembly. Controlled extractor outputs make correction, conflict, valid-time, erasure, lease and model-swap checks reproducible. The router check verifies that disabled escalation makes only one provider call.

A separate private neutral run captured eight actual local-model extractions. Replaying those responses through the actual source and context path with explicit reference dates passed five checks: correction, unresolved disagreement, historical validity, effective-date transition and a calendar-day observation. The first run's mistaken March reference for a September correction is retained as a failed assessment. These are bounded behavior checks, not a representative accuracy benchmark, proof of model equivalence, graph performance test or production acceptance. A further check replayed that extraction through current source FTS/projection and the shared selector with the actual LAN reranker: five relevant evidence packets and three unrelated-question abstentions passed. Reranker calls took 110–191 ms. It reused a private threshold from an earlier development set without fitting a new threshold or adding a public default. Before promotion, observe a real ordinary turn produce a completed projection and retrieve its grounded result through the deployed memory provider.

# Memory quality

Ordinary turns are evidence first. The canonical source ledger retains their
words, speaker, time, scope and media references. Its lexical and semantic indexes
make that evidence searchable even when no durable assertion is extracted.

There is one automatic assertion extractor. Each accepted assertion requires an
exact quotation, a supported subject/value, a useful memory category and a short
reason it could help later. Categories cover preferences, personal context,
relationships, decisions, procedures and significant events. The use judgment
is stored as `memory_quality` and explicitly labeled unverified. It is neither a
truth score nor permission to act. Corrections, changes and conflicting reports
keep the existing source and time rules.

The `source-claims-v3` extractor stores a procedure's exact evidence passage as
its value, including its conditions and subsequent steps, within the existing
500-character evidence limit. It does not ask the model to generate a second
version of those instructions. Other values retain their 160-character limit.
Existing stored claims remain readable and are not rewritten.

On a binding with verified [structured-output support](FUNCTION-ROUTING.md), a
source of at most 500 characters uses its complete message as constrained
evidence. This preserves trailing dates, reporter wording and qualifications.
Longer messages retain bounded exact-span selection. Each request owns its
schema; source text never becomes shared routing configuration. Full-message
evidence also includes neighboring clauses, and the existing sensitive-evidence
filter can reject the whole short passage. The original source remains retained.

Routine status, build/test progress, acknowledgments, boilerplate, fictional or
hypothetical examples and debugging output should remain source history. Facts
inside a narrative do not become assertions about the actual world. Real props,
project decisions, reported events and conditional procedures can still qualify
when their source supports that interpretation. Short and mutable
facts can still matter: a key location, an appointment or a reusable repair
procedure should not disappear merely because it can change.

Canonical ingestion no longer duplicates every exchange as an episodic graph
summary. Automatic ToM fact extraction is also removed: guessing what a contact
accepted from an assistant is a separate task from learning a supported assertion.
Affect and engagement updates continue. Explicit contact-knowledge APIs remain;
their model estimates are marked as automatic projections and cannot be copied
into the graph by a later backfill. Explicitly supplied facts and legacy
summary-only integrations retain their existing APIs.

In default native per-turn context assembly (`/v1/host/context/assemble`),
retained contact-knowledge estimates no longer appear in a separate unconditional
"Known Facts" section. Candidates come from the authorized legacy or projected
contact store when available; canonical-only projections still exclude these
stores. The projected view checks visibility envelopes, expiry and source erasure
before retrieval. Up to 512 current records are scanned
with the existing lexical tokenizer, and at most 25 query-matching candidates
enter the same selector and character budget as source evidence. An empty query
or one with no matching terms does not inject them, including when the reranker
is disabled or unavailable. Stored confidence does not determine relevance.
Selected estimates retain their record handle, time and recorded source type,
with an explicit unverified label; they are not exact canonical quotations.

This preserves bounded lexical access to useful retained estimates without a new
index or model. It does not guarantee paraphrase-only recall or coverage beyond
the current 512-record window. Explicit contact-knowledge listing remains
available. The explicit legacy `/v1/host/context/enriched` API remains separate
and is unchanged. No retained fact or its source history is deleted by this change.

Persistent extraction consumers use the provider's completed final answer.
Reasoning-only and truncated responses are not saved as assertions, affect,
engagement or image descriptions. Image descriptions remain fallible derived
evidence tied to the original asset; they have a 160-word limit and a separate
output budget for reasoning models. The job status exposes failed attempts so
an unavailable or unsuitable model is distinguishable from an empty result.

Repeated reports keep their distinct source identities through lexical retrieval,
so date filtering and correction expansion can select the right occurrence.
Shortened lexical and semantic excerpts are marked as incomplete. A successful
rerank must score every submitted candidate; partial or malformed output keeps
the original ordering with an unavailable status instead of mixing incompatible
scores. Selection remains bounded by the common item and character budgets.

The shared memory header preserves the distinction between actual observations,
reports, fiction and hypotheses. On qualified native Hermes, the existing request
middleware replaces the recognized outer authoritative-memory note with this
evidence contract. It preserves the person's input and the supplied source packet.
An unknown wrapper stays unchanged and requires renewed native qualification.

## Evaluation and limits

The neutral cases in `sidecar/tests/fixtures/memory_quality_cases.json` exercise
ten useful and ten unwanted inputs. Expected outcomes belong to the evaluator,
never to extraction prompts. Compare unwanted promotion and missed useful
information together, then inspect quotations and perform cross-session recall.
These cases are a small regression set, not a population estimate or a guarantee
for every model. Use deployment examples retained locally to extend evaluation
when ordinary behavior reveals a failure.

Structural validation cannot prove semantic usefulness. A model can still
misclassify an item or supply an unhelpful reason. Extraction rejection does not
delete its source, and retrieval of a source does not certify its contents.
The ordinary claim-status API exposes extraction version, errors and claim
counts; accepted claim records retain their quality judgment and provenance.

Claim-job status also exposes bounded `diagnostics` once its current attempt
finishes. It distinguishes an empty model array from candidates rejected by
validation, with candidate/accepted/rejected counts and fixed rejection reasons.
Each rejected candidate records its first failing check; these counts do not
judge whether an empty result was useful. Malformed arrays remain failed jobs.
Counts aggregate the attempt's completed text extractions. `accepted_count`
means validated candidates; the existing `claim_count` reports persisted claims,
which can differ after deduplication or partial work. `last_model_provenance`
identifies the last completed extraction response, including a response that
produced zero claims. It does not describe every response in a multi-message job.
No quotations, rejected values or raw model drafts enter this diagnostic record.
Historical jobs and attempts run by predecessor workers without these
measurements return `diagnostics: null`; an older attempt's counts are not reused.
The record lives on the existing job row, follows its lease, is replaced by the
next finished attempt and is removed with source erasure.

New claim jobs use `source-claims-v4`: after the existing quote, subject, value
and date checks, one batched `source_claim_review` request uses the configured
`judging` role to assess whether each proposal preserves the source's relation,
attribution, negation, modality and memory category. Exact required proposal
keys avoid asking the model to generate matching indices. Runtime validation
still rejects missing, duplicate or invalid decisions, including on bindings
that do not declare strict JSON schema support. There is no review call for an
empty validated proposal set. Useful reports, temporary knowledge, standing
conditional preferences and actual reusable procedures remain eligible.

Review is the default admission path for new nonempty proposals. A local
installation can assign extraction and judging to the same model; older local
tier adapters use the same local tier for both requests. No separate processor
is required. Function routing honors the existing configured judging candidates
and fallback policy. An unavailable or malformed review leaves the job pending;
a valid rejection completes without that claim. The original source remains
searchable in either case. One captured outer deadline and owned job lease cover
both requests; the router also keeps each role's deadline. Review therefore adds
background work and latency, with a 1400-token output cap per batch.

Kept records preserve the proposal's fields and add `admission_review`, containing
version `source-claim-review-v1`, a bounded reason and the actual review processor's
provenance. Its basis is `model_judgment_unverified`: a supported source assertion
is not independently verified truth, and the review reason is not a new fact.
The existing extraction provenance remains separate. Review text is excluded
from the recall assertion member projection. Historical records are unchanged.

`accepted_count` continues to mean candidates that passed the mechanical
extraction checks. `reviewed_count`, `review_kept_count`, `review_rejected_count`,
`review_response_count` and `invalid_review_count` describe the subsequent stage;
`last_review_provenance` identifies its last completed response. `claim_count`
remains the number actually persisted. A positive extraction acceptance count
does not mean review succeeded or a claim committed. Review reasons are stored
only with kept claims, not in the job diagnostic record.

This change does not relabel or erase historical memories. Audit a retained
corpus before making content changes. Keep raw private samples on the deployment,
separate exact duplicates from distinct supporting evidence, and label model
triage as tentative. Backups, native runtime transcripts and unlinked historical
derivations have their own retention limits; canonical source erasure does not
claim to erase unsupported descendants.

Original source images also require a recoverable backup, not only retained
captions. See [source memory recovery](SOURCE-MEMORY-RECOVERY.md) for the existing
backup command's source-image coverage and its remaining recovery limits.
The text-claim worker skips non-text message blocks before prior lookup; their
original media, stable capture metadata and independent media/vector jobs remain
retained. Completing that text job does not claim the image's semantics were learned.

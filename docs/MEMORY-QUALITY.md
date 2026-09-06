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

Routine status, build/test progress, acknowledgments, boilerplate, hypothetical
examples and debugging output should remain source history. Short and mutable
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

This change does not relabel or erase historical memories. Audit a retained
corpus before making content changes. Keep raw private samples on the deployment,
separate exact duplicates from distinct supporting evidence, and label model
triage as tentative. Backups, native runtime transcripts and unlinked historical
derivations have their own retention limits; canonical source erasure does not
claim to erase unsupported descendants.

Original source images also require a recoverable backup, not only retained
captions. See [source memory recovery](SOURCE-MEMORY-RECOVERY.md) for the existing
backup command's source-image coverage and its remaining recovery limits.

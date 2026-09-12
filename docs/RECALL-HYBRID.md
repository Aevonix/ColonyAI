# Hybrid recall and calibrated abstention

Automatic context and explicit memory search combine lexical source search in
SQLite with the optional Lance semantic projection. Both use the same canonical
source IDs and enforce participant/session scope before selection. New source
text and corrections remain available while semantic projection is pending.

An absent, incompatible or unavailable semantic index leaves lexical search
available and reports the semantic limitation. A working empty search is distinct
from an unavailable canonical store. Neither path uses Neo4j memory candidates.
See [source semantic recall](SOURCE-SEMANTIC-RECALL.md) for projection identity,
model swaps and the shared HTTP contract.

## Returning no useful memory

`COLONY_RECALL_RERANK_MIN_SCORE` is optional and has no global default. Scores
have model-specific meanings. When a cutoff is configured and calibrated, the
reranker also evaluates candidate sets smaller than the requested result count.
Passages below the cutoff are omitted instead of padding the context. Omitted
scores cannot satisfy a configured cutoff. A reranker error retains the existing
fallback behavior; an empty result must not be interpreted as evidence that an
unavailable backend searched successfully.

The cutoff applies only when `COLONY_RECALL_RERANK_CALIBRATION` matches the
SHA-256 configuration fingerprint supplied by the active reranker registration.
The fingerprint uses the provider, model, endpoint, prompt format, candidate
input format, optional weight revision, embedding configuration and optional index generation. Changing
those values invalidates the cutoff. Custom rerank functions can supply current
metadata through `set_rerank_fn(..., calibration_metadata=...)`.

An unmatched or absent fingerprint disables the cutoff and marks the returned
rows `mismatch` or `unverified`, with a warning. A matching configuration whose
weight revision is unavailable is explicitly marked
`configuration_verified_weights_unverified`. A configuration stamp cannot detect
an unannounced weight replacement behind the same endpoint/model alias. Operators
must invalidate calibration on such a replacement; setting a model name is not
proof of immutable weights. `COLONY_RERANKER_REVISION` and
`COLONY_RECALL_INDEX_GENERATION` accept known revisions when available.

There is no universal cosine or reranker threshold. A deployment should freeze
representative positive, paraphrased, corrected and no-answer queries, select a
cutoff on its development subset, then test its held-out subset once. Keep that
deployment's calibration values outside generic defaults.

The calibrated candidate format is
`grounded-quotation-bundles-v2-corrections-first`. Attributed corrections precede
the original evidence in ranking text; all members remain in the complete output
packet. Changing that format changes the configuration fingerprint even when the
provider and numerical cutoff stay the same. Before promotion, test the intended
cutoff against useful corrections, other useful evidence and genuine no-memory
queries, preserving any earlier failures. A configuration match alone is not
quality qualification, and irrelevant competitors inside useful queries do not
replace whole-query abstention controls.

Select the qualified code and its matching calibration metadata together.
Keeping an older stamp invokes the existing mismatch fallback, which disables
the cutoff; that is not a successful calibrated upgrade. Recovery must likewise
pair the previous representation with its previous stamp while retaining current
source and erasure state. No threshold fitting occurs automatically.

## Qualification

Required observed cases are: a new source found before its vector is available;
the old superseded source excluded; an irrelevant query producing no recalled
passages under a matching calibration; source handles visible in the assembled
context; scoped misses staying scoped; and a changed reranker configuration
invalidating its old cutoff. Existing consent and tool authority remain with
their execution owners.

An earlier development benchmark used actual LAN embedding and reranker models and a
disposable Lance store with fixture-backed graph hydration. A subsequent isolated
Neo4j Community 2026.01.4 run applied all 50 migration statements, reached an
ONLINE full-text index and passed six real-query checks: person scopes, a scoped
miss, global union, source/metadata exclusion, supersession/confidence/strength
filtering and immediate recall before vector creation. Production stores were
not used. Those graph results do not qualify the current canonical memory path.

The initial implementation replay retrieved the expected sources but scored
15 of 24 complete behavior cases against its original fixture. Review then found
that the fixture conflated a request for today's footage with a window beginning
the previous day. That aggregate is not a reliable acceptance score. Explicit
calendar, validity and contradiction cases are required. A reranker cutoff does
not replace occurrence-time filtering or canonical contradiction state. This
patch preserves recorded timestamps and existing contradiction counts; it does
not implement those remaining memory semantics.

## One selection path for turn context

`/context/assemble` combines scoped lexical and semantic source hits,
source-claim expansions, media descriptions and relevant contact estimates.
Canonical source search independently enforces contact/session scope and
source-message erasure. A partially redacted turn can retain unrelated quotations.
No graph candidate or graph recall-strength update participates in this path.

Canonical candidates feed one rank-fusion and reranking pass. The calibrated
cutoff can reject every candidate. Context contains at most five total records
in one `colony-memory` section. There is no separate conversation-evidence
injection. Quotations remain evidence of what was said, while claims and contact
estimates retain their distinct interpretation and uncertainty.

Combined recall submits at most four times the requested packet count to the
inline reranker, in fused rank order: 20 candidates for the default five-record
packet. This bounds model work as producers expand, while leaving room to select
useful passages below the first five. The existing timeout still applies. On
successful active reranking, only that submitted set competes for the packet;
unsubmitted rank-fusion scores are not compared with cross-encoder scores.
Disabled, shadow and failed reranking preserve the full original candidate set.
This can exclude relevant evidence below the submission bound. Evaluate source
retention alongside latency on the deployment's corpus; a smaller batch does
not establish a relevance cutoff or solve unrelated-memory injection.

Plain attributed quotations that exactly repeat the current request are
supplementary: selection places them after independent evidence before bounded
reranking and final packing. Original bytes and source records are retained;
qualified assertion histories, annotations and uncertain attribution are exempt.
This reduces displacement by repeated questions, including while a correction
awaits claim processing. It does not guarantee primary-source coverage when
many distinct assistant retellings compete, or recover sources missed during
candidate retrieval. The five-record limit and character budget are unchanged.

The default combined rendered budget is 6,000 characters, adjustable through
`COLONY_RECALL_CONTEXT_MAX_CHARS` up to 24,000; zero suppresses this packet.
This is a character limit, not an asserted token count. Shortened excerpts carry
`excerpt_truncated=true`, and source bytes remain intact in the source store.
Records retain `kind=belief` or `kind=source_quote`, source/turn handles, speaker
role, and occurrence/ingestion times when available. Quoted content is explicitly
evidence rather than instructions or an accepted belief. Only selected graph
records gain recall strength.

Reranker failure uses one shared bounded fallback and marks returned records
`rerank_status=unavailable`; it does not promise calibrated abstention during an
outage. Unconfigured reranking retains rank-fusion fallback. Existing graph-only
recall clients retain their confidence/strength ranking behavior. This budget
covers the mixed memory packet, not other existing context sections.

Eight integrated regressions cover mixed abstention, one budget/section,
graph-unavailable selection, visibility before model input, reinforcement after
selection, failure fallback, excerpt preservation and the shared result limit.
Six neutral checks with the current LAN reranker also passed, including three
no-answer queries; these are integration checks, not a fresh calibration fit or
a representative holdout benchmark. A deployment must still qualify quotations
and its actual corpus before treating its cutoff as measured for that corpus.

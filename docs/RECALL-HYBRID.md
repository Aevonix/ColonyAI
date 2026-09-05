# Hybrid recall and calibrated abstention

The existing memory path can return unrelated passages when no stored fact
answers the turn. It also searches vectors before sources, so an embedding
backlog can hide a newly stored correction. This change adds a native source-text
leg alongside Lance retrieval and preserves source IDs in injected context.

`COLONY_RECALL_HYBRID=on` enables full-text candidates from the existing Neo4j
memory store. The ordinary graph migrations create `memory_content_fulltext`.
Verify that this index is online before promotion. Its updates are synchronous
with source writes; no new service or second authoritative store is introduced.
Candidate filtering preserves exact person scope, excluded sources/metadata,
confidence, strength and supersession. Independent ranks are fused before the
existing reranker. Index absence or timeout degrades to the existing path.

The default remains the existing vector/keyword behavior until a deployment
qualifies and enables the hybrid path. Full-text query latency and scoped
candidate completeness must be measured on the deployment's graph. The query
timeout is bounded; this change does not claim that every scoped search can meet
that deadline.

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
The fingerprint uses the provider, model, endpoint, prompt format, optional
weight revision, embedding configuration and optional index generation. Changing
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

## Qualification

Required observed cases are: a new source found before its vector is available;
the old superseded source excluded; an irrelevant query producing no recalled
passages under a matching calibration; source handles visible in the assembled
context; scoped misses staying scoped; and a changed reranker configuration
invalidating its old cutoff. Existing consent and tool authority remain with
their execution owners.

The development benchmark used actual LAN embedding and reranker models and a
disposable Lance store with fixture-backed graph hydration. A subsequent isolated
Neo4j Community 2026.01.4 run applied all 50 migration statements, reached an
ONLINE full-text index and passed six real-query checks: person scopes, a scoped
miss, global union, source/metadata exclusion, supersession/confidence/strength
filtering and immediate recall before vector creation. Production stores were
not used. The production corpus and correction writer still require qualification.

The initial implementation replay retrieved the expected sources but scored
15 of 24 complete behavior cases against its original fixture. Review then found
that the fixture conflated a request for today's footage with a window beginning
the previous day. That aggregate is not a reliable acceptance score. Explicit
calendar, validity and contradiction cases are required. A reranker cutoff does
not replace occurrence-time filtering or canonical contradiction state. This
patch preserves recorded timestamps and existing contradiction counts; it does
not implement those remaining memory semantics.

## One selection path for turn context

`/context/assemble` retrieves up to 25 authorized graph candidates and ten
authorized conversation excerpts. Graph candidate retrieval skips reranking and
recall-strength updates. P8 visibility and projection-erasure checks precede the
combined model call. Source search independently enforces contact/session scope
and source-message erasure; a partially redacted turn can retain unrelated
quotations while its old graph summary is suppressed.

Both producers feed one rank-fusion and reranking pass. The same calibrated
cutoff can reject either kind, including when the graph is unavailable. Context
contains at most five total records in one `colony-memory` section. There is no
separate conversation-evidence injection. Confidence in a belief is not treated
as comparable to certainty that words were quoted: cross-kind selection uses
rank and semantic relevance, preserving confidence separately as metadata.

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

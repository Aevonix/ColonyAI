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
disposable Lance store, but fixture-backed graph hydration. Its results do not
qualify the live Neo4j query, production corpus, correction writer or host memory
callback. Those are explicit deployment checks before promotion.

The implementation replay retrieved every expected source but passed only
15 of 24 complete held-out behavior cases: six retained older dated evidence,
and three lacked explicit contradiction markers. A reranker cutoff does not
replace occurrence-time filtering or canonical contradiction state. This patch
preserves recorded timestamps and existing contradiction counts; it does not
implement those remaining memory semantics.

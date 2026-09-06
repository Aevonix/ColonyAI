# Semantic recollection from canonical evidence

Canonical messages and retained image descriptions now produce semantic
candidates as well as lexical candidates. Both enter the existing temporal
claim handling, reranker and context budget. There is one memory packet per
turn. A vector ranks potentially relevant evidence; it never determines truth.

Authorized retained contact-knowledge estimates also enter this shared selection
and budget, using bounded lexical candidates from their existing scoped view.
They are labeled unverified estimates, not canonical source quotations. See
[memory quality](MEMORY-QUALITY.md) for candidate limits and fallback behavior.

The normal source transaction queues projection in the existing turn ledger.
The existing source worker embeds at most 16 chunks per pass, persists its
cursor and retries failures with backoff. A restart resumes those jobs. Caption
completion requeues the linked sources. No new process, database or autonomous
action is added. Disabling claim extraction does not disable configured semantic
projection. Without embeddings the lexical path continues to work.

Source chunks use the existing Lance `conversations` collection and the selected
embedding generation. Each projection links an exact source turn and original
message hash, plus its text span or asset and description hash. Original image
bytes remain separate. The caption is fallible model output, not an image
embedding or a replacement for future visual inspection.

Contact and session eligibility is a scalar filter before nearest-neighbor
selection. Private checkpoint text remains in its original session; ordinary
attributed messages can cross that participant's sessions. Candidates are then
hydrated from current canonical source state. Cached vector text is never
injected as authority. Erased messages and changed descriptions cannot validate
old projections. A partially redacted checkpoint retains unrelated message
chunks, while the old whole-turn graph summary remains fenced.

Source erasure physically removes matching invalid projections from every
retained generation through the existing vector cleanup path. Writes check
lineage both before and after asynchronous index I/O. Identical pixels belonging
to a different retained source remain available to that source's owner.

A compatible generation is required for semantic search. Unknown legacy vectors
remain on disk and lexical recall continues until an explicit reindex. Following
generation promotion, canonical projection jobs are due again, including rows
not previously indexed. The source worker writes only replaceable projections;
it does not replay conversation actions, affect, grants or commitments.

Inspect the existing scoped endpoint:

```text
GET /v1/host/memory/sources/claims/status?contact_id=<authorized-contact>
```

Its `semantic` field reports index compatibility, active generation, projected
turns, pending turns and retrying failures. A compatible index can have pending
sources. Lexical recall remains available while those jobs catch up. Relevance
abstention uses the existing optional calibration, not a universal similarity
threshold. An absent or failed reranker retains the existing fallback behavior;
semantic proximity alone is not proof of relevance.

Atomic claim bundles are ranked by their original grounded quotations. Their
full structured record, including every conflicting member, validity and source
handle, remains the output. This prevents administrative JSON from obscuring
the evidence's topic in the reranker. The candidate input format is part of the
calibration stamp, so changing this representation invalidates an older stamp;
the public code does not lower or choose a global relevance threshold.

`test_source_vectors.py` exercises ordinary HTTP ingestion, the actual worker,
cross-session context, one rerank pass, scoped prefiltering with more than 200
foreign chunks, current-source hydration, partial and late erasure, equal-width
incompatible embedding spaces, resumable jobs, caption ownership, and existing
conflict/correction grouping. Those tests use controlled embeddings and model
responses to verify contracts. The separate fixed neutral model evaluation
measures retrieval quality; these tests alone do not establish it.

This increment does not provide audio/video embeddings, unrestricted media URL
fetching, automatic embedding-provider replacement or migration of unlinked
historical graph memories into canonical sources. Existing graph memory remains
important for those historical records. Query latency and retrieval coverage on
a large deployed corpus still require measurements with that corpus.

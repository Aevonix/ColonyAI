# Semantic recollection from canonical evidence

Canonical messages and retained image descriptions now produce semantic
candidates as well as lexical candidates. Both enter the existing temporal
claim handling, reranker and context budget. There is one memory packet per
turn. A vector ranks potentially relevant evidence; it never determines truth.

After scoped claim expansion and time filtering, repeated exact plain quotations
from the same known participant and role occupy one candidate slot. This prevents
copies of a past question from crowding useful evidence out of an undated packet.
The first candidate retains its own complete source handle and timestamps; it
does not combine occurrences into a new fact. Distinct speakers, checkpoint
text, unknown attribution, assertion/conflict bundles and time-qualified events
remain separate. Erasure can expose another surviving occurrence on later recall.
No source rows or search projections are deleted by selection.

In default native per-turn `/v1/host/context/assemble`, authorized retained
contact-knowledge estimates also enter this shared selection and budget, using
bounded lexical candidates from their existing scoped view.
They are labeled unverified estimates, not canonical source quotations. See
[memory quality](MEMORY-QUALITY.md) for candidate limits and fallback behavior.

Explicit `POST /v1/host/memory/search` uses the same canonical collector and
selector as automatic context. Supply an authenticated participant, exact
session and query, with an optional result limit from 1 to 20. The response
contains bounded `content`, `count`, exact `source_refs`, an erasure `watermark`,
retrieval status and correction-set checks. It returns excerpts rather than a
claim that a complete source was opened. Existing source-read tools can open
those exact references. Empty results are successful reads; an unavailable
canonical store or selector returns HTTP 503. Semantic failure keeps current
lexical evidence and reports the semantic degradation.

Search and automatic context use canonical evidence without a graph dependency.
Source claims, attributed
corrections, media descriptions and scoped contact estimates retain their
existing ranking, temporal interpretation and shared context budget.

`POST /v1/host/memory/read` opens an exact source ID/version for the authenticated
participant and session. It returns a `source` object with the requested evidence
view and pagination metadata. Graph IDs, audience selectors and listing limits
are not part of this request. `/health` checks canonical SQLite readability;
missing or failed optional semantic projection does not remove source memory.

The existing source-freshness POST accepts the search response's
`annotation_checks` alongside its exact source references. It returns aligned
`annotation_checks_current` booleans separately from source byte and ownership
validity. A newly appended correction invalidates the older selected excerpt
even when the source bytes did not change. Independent checks let a fresh search
recover in the same turn. No new service, store or model call is needed.
Sidecar reasoning binds this tool to the authenticated request's participant and
session, requires `memory:search`, and rejects model-provided scope selectors.
Unbound internal invocations report unavailable instead of searching globally.

Canonical turn ingestion records what participants said and what tools actually
observed. Owner corrections use the attributed source-annotation API; erasure
uses source-forget. The search and source-read APIs project that evidence.
Periodic availability checks only read these surfaces and create no memories.

The normal source transaction queues projection in the existing turn ledger.
The existing source worker embeds at most 16 chunks per pass, persists its
cursor and retries failures with backoff. A restart resumes those jobs. Caption
completion requeues the linked sources. No new process, database or autonomous
action is added. Disabling claim extraction does not disable configured semantic
projection. Without embeddings the lexical path continues to work.

Lexical recall recovers message ownership by regenerating the existing exact
2,000-character chunks with a 1,800-character stride from current canonical
messages. A containing substring does not establish ownership. Stale index rows
and duplicate projections are filtered before the result limit; identical chunks
actually produced by distinct messages retain those distinct owners. Recovery
runs only for scoped full-text matches with a bounded per-query source cache.
The existing FTS schema and writer format remain compatible across rollback and
upgrade; no startup rebuild or ownership migration is required.

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

For an attributed source-annotation packet, ranking text presents every exact
correction first, labelled as not independently verified, followed by the exact
original evidence. The output still preserves the complete original, attribution,
all corrections and their current source references. This ordering neither
selects a winning correction nor rewrites the source. The versioned format must
be qualified with useful evidence and no-memory queries before selecting its
matching calibration stamp; see [calibrated recall](RECALL-HYBRID.md).

`test_source_vectors.py` exercises ordinary HTTP ingestion, the actual worker,
cross-session context, one rerank pass, scoped prefiltering with more than 200
foreign chunks, current-source hydration, partial and late erasure, equal-width
incompatible embedding spaces, resumable jobs, caption ownership, and existing
conflict/correction grouping. Those tests use controlled embeddings and model
responses to verify contracts. The separate fixed neutral model evaluation
measures retrieval quality; these tests alone do not establish it.

This increment does not provide audio/video embeddings, unrestricted media URL
fetching, automatic embedding-provider replacement or migration of unlinked
historical graph memories into canonical sources. Historical graph records are
not read by automatic context or the canonical source API. Query latency and retrieval coverage on
a large deployed corpus still require measurements with that corpus.

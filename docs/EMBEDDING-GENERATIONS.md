# Embedding generations

Canonical source messages and graph facts are retained independently of their
embeddings. A vector is a replaceable search projection. Equal vector dimensions
do not establish that two models produce compatible embeddings.

The runtime now binds its vector store to an immutable embedding identity:
requested model alias, reported serving model, declared weights or deployment
revision, dimension, document and query formats, normalization and quantization.
The requested alias and serving model are separate fields. Missing revision or
serving identity is recorded as `unknown`. `COLONY_EMBED_REVISION` is an operator
declaration, not verified weights attestation. An endpoint that silently changes
weights behind the same alias without reporting a change cannot be detected
from that alias alone; declare a new revision and rebuild after such a change.
Endpoint addresses and credentials are not index identity and are not copied
into index metadata.

The pipeline retains its query instruction for its lifetime, and graph recall
uses that same query formatter. The API embedder checks response cardinality,
ordering, finite values and dimensions before returning vectors. A changed
reported serving model cannot silently reuse the old pipeline. This change does
not add dynamic embedding provider configuration; selecting a different
embedding provider still uses the existing deployment configuration path.
Index promotion itself takes effect in the running store without a restart.

Existing Lance collections have no trustworthy embedding identity. They remain
on disk as an unverified legacy generation, and semantic search through the
managed runtime refuses to compare them with a newly configured model. Graph
keyword recall and canonical lexical source recall remain available. The
installer's optional vector dependency remains optional.

The existing `colony migrate-tier` and `/v1/host/memory/migrate` operation rebuild
all retained text collections into a separate Lance directory. The server now
actually registers the same store used by graph recall with that operation.
For graph memories, rebuilding reads current graph facts, including facts whose
old vector write failed. Other retained text collections use their retained
text and metadata. A model-filtered partial rebuild cannot be promoted because
it would leave the replacement incomplete.

The generation catalog, active pointer and exact-ID deletion fences use the
existing source ledger, not another database service. Rebuilding is resumable:
already staged IDs, including new live writes, are retained. A failed batch
leaves the prior active pointer intact. A complete generation is promoted by
one SQLite transaction. Old generations remain available for inspection or a
separately qualified recovery. This packet provides no unattended old-generation
rollback command and does not restore authoritative memory or authority state.
The ordinary migration API rejects concurrent rebuilds in one server process.

All managed vector deletes create an exact-ID tombstone first and remove that
ID from active, staged and retained generations. Canonical-source erasure also
scans exact source links, including orphaned vectors whose graph row has already
disappeared. Recall and late writes check the same fences. The forget response
reports `vector_cleanup` independently; `pending` requires a retry and does not
mean that physical cleanup completed. Unlinked historical vectors cannot acquire
invented source ownership from matching text. Their source provenance remains
unknown.

Rebuilding image embeddings requires a qualified model that can reproduce that
image embedding space. The text rebuild refuses retained image-vector rows
instead of replacing them with caption vectors and claiming compatibility.
Canonical retained originals and their fallible descriptions remain separate.
Semantic source and caption candidates are the next increment and must use the
existing single context selector and budget.

`test_embedding_generations.py` uses real LanceDB for identity mismatch, changed
dimensions, non-destructive failed update, failed rebuild/resume, new arrivals,
erasure, and the real migration/forget API. Controlled vectors test distinct
spaces; a separate private neutral qualification uses the actual LAN embedder.
That small qualification does not establish comparative retrieval quality or
the performance of re-embedding a production corpus.

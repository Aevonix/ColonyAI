# Shared facts and source erasure

Contact knowledge lives in the canonical SharedFacts SQLite store. The manual
fact and ToM extraction APIs write that store and, when enabled, append P8
visibility envelopes. They do not write graph copies. Fact listing reads the
same store with source, confidence, expiration and contact filters; its total
is the full filtered count before pagination.

Automatic recall reads current source-linked contact estimates directly from
the fact store or its scoped P8 projection. It checks query relevance, exact
source membership and corrections before the shared memory selector packs the
context. A model estimate remains labelled as an estimate. See
[memory quality](MEMORY-QUALITY.md) and [P8 integration](P8-SHARED-INTEGRATION.md).
Ordinary fact learning uses canonical source assertions; ordinary ingestion
does not run a second ToM fact extractor.

A linked fact records its canonical turn, session, exact message hashes,
observation and ingestion times, and model provenance. Identical wording from
independent sources retains separate support. The source cannot be inferred
from matching text. An unlinked manual record remains explicitly inspectable,
but its source enum and free-form metadata do not make it eligible for
automatic injection.

Forgetting commits the source tombstone first, then removes linked shared
facts. The response reports `shared_facts_cleanup`; `pending` means physical
cleanup must be retried. Reads check source validity even when cleanup fails.
Late writes recheck the source, restart reconciles physical fact cleanup, and
repeating the forget operation retries cleanup. A source database read failure
does not reveal linked facts through another store.

Linked facts without exact quotation spans conservatively depend on their
whole source turn. Erasing any supporting message suppresses those facts.
Unrelated quotations in a partially erased checkpoint remain available through
canonical source recall. Unlinked records cannot be assigned source ownership
retrospectively without evidence.

The separate graph still has other consumers. Existing graph-copy exclusions
and graph source-erasure cleanup remain until that dependency is removed; this
change neither creates new graph copies nor deletes existing graph data.
It does not establish complete retrospective erasure for unlinked affect or
engagement state.

`test_tom_source_lineage.py` exercises ordinary API ingestion, the actual SQLite
store, cross-session context, source erasure, failed physical cleanup, late
writes and independent supports with no graph. The contact-fact context tests
also cover exact citations, current corrections, visibility and relevance.
These checks establish storage and selection behavior, not model extraction
quality or ordinary production-channel usefulness.

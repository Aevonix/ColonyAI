# Ordinary-turn shared facts follow source erasure

The ordinary turn API previously retained a canonical source but ran Theory
of Mind fact extraction from a generated summary. Its shared-fact records and
graph mirrors had no source lineage. Forgetting the source could therefore
leave its inferred facts available to later sessions as `tom:shared_fact`.

Ordinary extraction now reads retained canonical messages for the resolved
person. Each inferred fact retains the canonical turn, session, exact message
hashes, observed and ingestion times, and extractor model provenance when
available. The fact remains an inference. Model identity records how it was
produced, and does not determine whether another model can use it. Raw
multimodal blocks contribute text only; this extractor does not interpret
images or treat their transport representation as evidence.

The existing shared-facts SQLite table gains one additive nullable lineage
column. Graph mirrors use `turn:<canonical-id>` and the existing graph erasure
fence. Mirror deduplication includes source and contact, so identical wording
from an independent turn or a legacy fact cannot become solely owned by the
new source. Repeated extraction of one source can still create multiple fact
rows; its graph mirror deduplicates within that source. This change does not
introduce a new claim store or merge independent supports.

Forgetting commits the source tombstone first, then removes linked shared
facts and graph projections. The response reports `shared_facts_cleanup`
separately from `graph_cleanup`; `pending` means cleanup must be retried.
Shared-fact reads, including list counts and backfill candidates, check the
canonical support. Graph recall already checks the source tombstone. Late
extractors and captured backfill candidates recheck it before writing, and
the existing graph writer fences source erasure after asynchronous work.
Restart reconciles physical fact cleanup, and repeating the forget operation
retries both stores. A source database read failure does not reveal linked
facts through an unchecked fallback.

The existing extractor does not provide exact per-fact quotation spans.
Consequently each extracted fact is conservatively supported by the whole
turn. If any supporting message is erased, all inferred facts from that turn
are suppressed. Unrelated raw quotations in a partially erased checkpoint
remain available through canonical source recall.

Legacy and manually entered unlinked facts are preserved. Reliable source
ownership cannot be inferred from matching text alone. Previously leaked
neutral qualification rows require an explicit, separately identified cleanup;
deploying this change does not silently erase legacy owner data. Existing
affect events and engagement profiles still lack complete retrospective
source erasure lineage. This packet checks source validity around new
asynchronous ToM steps but does not claim to make those older derived states
erasable. Their migration remains separate work.

`test_tom_source_lineage.py` exercises ordinary API ingestion, shared-facts
SQLite, cross-session context, source erasure, failed physical cleanup, late
jobs and independent supports. Model extraction and graph I/O are controlled;
the graph read fence is the production implementation. These checks prove
lineage and erasure behavior, not extraction quality or live database service
compatibility. The deployment qualification must repeat the ordinary neutral
fact and forget loop with the actual extractor and graph.

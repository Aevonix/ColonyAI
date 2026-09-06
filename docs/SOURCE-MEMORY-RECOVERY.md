# Recovering original source memory

Backup format 2 includes original source images referenced by the captured
`turn-idempotency.db`. A caption, asset handle or vector cannot reconstruct the
original pixels. The existing `colony backup --full` and `colony restore --full`
commands use this path; there is no separate memory backup service.

Each database is captured consistently. If SQLite `VACUUM INTO` fails, ordinary
databases use SQLite's backup API instead of copying only the main file and
losing committed WAL records. An unavailable consistent snapshot aborts the
archive. The governed action ledger retains its stricter existing contract.

Originals are selected from the captured ledger, not from a later query of live
state. Hash and byte length must match. Unowned files and regenerable thumbnails
are omitted. If concurrent forgetting removes a required original before it is
copied, backup fails visibly; retry against a new snapshot. This is a consistent
source-image set, not an atomic snapshot across every Colony database.

Restore checks all referenced originals before replacing state. Database
restoration uses SQLite's backup API, so an old destination WAL cannot overlay
the recovered main file. Original images return to their source namespace, with
the same scope and source links. Captions and source-erasure behavior survive
with the ledger. Format 1 archives without source-image references remain
readable; an older archive that omitted referenced originals fails with a
missing-image error. It cannot recover bytes it never retained.

Restore into a fresh destination with services stopped, then verify source
retrieval before starting them. Existing installations also require their
private configuration, credentials and runtime backup procedures. Do not restore
an old archive over ongoing work as an ordinary deployment rollback.

These changes do not qualify the legacy graph export as a graph restore, fix
every external vector-directory layout, or coordinate native Hermes transcripts
and private hardware state. Those remain separate recovery responsibilities.
Vectors are derived projections and can be rebuilt from retained source evidence;
canonical image originals are not. Retained backups also have their own deletion
policy: restoring an old archive can restore historical evidence, including
evidence forgotten after that archive was created.

The regression tests perform a real image-source backup and restore, verify the
original bytes and caption in another session, check contact scope, and forget
the restored source. They also restore over a deliberately crashed SQLite target
with committed WAL, and reject missing or corrupted image originals before
overwriting the destination.

# Recovering original source memory

Backup format 2 includes original source images referenced by the captured
`turn-idempotency.db`. A caption, asset handle or vector cannot reconstruct the
original pixels. The existing `apsimo backup --full` and `apsimo restore --full`
commands use this path; there is no separate memory backup service.

Each database is captured consistently. If SQLite `VACUUM INTO` fails, ordinary
databases use SQLite's backup API instead of copying only the main file and
losing committed WAL records. An unavailable consistent snapshot aborts the
archive. The governed action ledger retains its stricter existing contract.

Originals are selected from the captured ledger, not from a later query of live
state. Hash and byte length must match. Unowned files and regenerable thumbnails
are omitted. If concurrent forgetting removes a required original before it is
copied, backup fails visibly; retry against a new snapshot. This is a consistent
source-image set, not an atomic snapshot across every Apsimo database.

Restore checks all referenced originals before replacing state. Database
restoration uses SQLite's backup API, so an old destination WAL cannot overlay
the recovered main file. Original images return to their source namespace, with
the same scope and source links. Captions and source-erasure behavior survive
with the ledger. An archive that omitted referenced originals fails with a
missing-image error. It cannot recover bytes it never retained.

Restore into a fresh destination with services stopped, then verify source
retrieval before starting them. A deployed instance also requires its
private configuration, credentials and runtime backup procedures. Do not restore
an old archive over ongoing work as an ordinary deployment rollback.

`restore --full` reconstructs archive state. It does not establish current
serving authority, and the CLI no longer recommends immediately starting that
state. A later erasure, scope correction, revoked grant or completed effect
cannot be recovered by trusting an old archive's own timestamp or counters.

## Recover current source memory from surviving metadata

When the current canonical source ledger survives but original-image storage is
missing or damaged, use the existing restore CLI's bounded memory mode:

```sh
apsimo restore --memory-only --input ./backup.tar.gz \
  --current-state ./surviving-current-state --output ./recovered-memory
```

The output must be a new directory outside the surviving state. The command
requires the surviving colony identity and `turn-idempotency.db`; the archive
must belong to the same colony. The caller is responsible for selecting an
authoritative current source, rather than another stale copy. Capture a final
bundle with its writers stopped before installing it. A consistent SQLite
snapshot is not an assertion that no later change occurred.

The surviving ledger supplies current source membership, exact revisions,
scope, correction annotations, derived memory and erasure history. Every
captured erasure must occur unchanged in that surviving history; a rewound or
divergent history fails. The old archive's source rows and claims are not merged
back. Consequently an erased source cannot return merely because its image or
old derived claim is still in the archive. The recovered erasure sequence also
remains compatible with an offline adapter that already observed that history.

For each original still owned by the selected current ledger, the command uses
matching current bytes or recovers matching bytes from the archive. It verifies
the exact content hash and length before creating the destination. Missing or
damaged originals with no matching copy fail visibly. Images no longer owned
by current sources are excluded. Encrypted archives use the existing passphrase
support.

The bundle contains `turn-idempotency.db`, its owned image originals under
`images/sources/originals/`, and `source-memory-recovery.json`. The receipt binds
the archive and selected ledger hashes, output file hashes, counts and erasure
head. The canonical ledger retains its current ingestion/idempotency records;
no older version is restored over them. The bundle does not install archived
identity/keys, contact or authorization databases, configuration, task queues,
governed effects, native transcripts, graph or external vector directories.
Those systems require separately current recovery bindings.

To use the recovered memory, retain the intended runtime's current identity,
viewer/contact authority and effect/task stores, stop its memory writers, and
install only the memory files listed in the receipt. Verify exact receipt
hashes, the selected ledger's currentness and the existing scoped source/image
and erasure-feed reads before resuming those writers. Regenerate the bundle if
the surviving source or its authority changed after capture. Use the normal
runtime setup and recovery procedures for the excluded stores; do not start
the memory bundle as a complete Apsimo instance. This mode does not certify
runtime authority or recover current source state when no authoritative ledger
survives. In that case full archive reconstruction remains isolated pending
reconciliation; it is not a total-disaster recovery claim.

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

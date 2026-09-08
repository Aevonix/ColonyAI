"""Compatibility readers for later explicit source-attribution corrections.

This release does not perform corrections or enable any new projection writer.
The marker table is additive so an older functional release can safely consume
the same database after a newer release invalidates derived source copies.
"""


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_attribution_invalidations (
        source_id TEXT NOT NULL,corrected_source_id TEXT NOT NULL,operation_id TEXT NOT NULL,
        PRIMARY KEY(source_id,corrected_source_id,operation_id))''')


def is_invalidated(conn, source_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_attribution_invalidations'").fetchone():
        return False
    return bool(conn.execute('SELECT 1 FROM source_attribution_invalidations WHERE source_id=? LIMIT 1',
                             (source_id,)).fetchone())

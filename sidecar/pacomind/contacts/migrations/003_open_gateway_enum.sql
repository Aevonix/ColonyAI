-- Remove the hardcoded gateway CHECK constraint from contact_handles.
-- SQLite has no ALTER TABLE DROP CONSTRAINT, so we rebuild the table.
-- All existing data is preserved; the only change is that gateway now
-- accepts any string, validated at the application layer.

CREATE TABLE IF NOT EXISTS contact_handles_new (
    handle_id    TEXT PRIMARY KEY,
    contact_id   TEXT NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
    gateway      TEXT NOT NULL,
    address      TEXT NOT NULL,
    is_primary   INTEGER NOT NULL DEFAULT 0
                   CHECK(is_primary IN (0,1)),
    verified     INTEGER NOT NULL DEFAULT 0
                   CHECK(verified IN (0,1)),
    confidence   REAL NOT NULL DEFAULT 1.0
                   CHECK(confidence >= 0.0 AND confidence <= 1.0),
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(gateway, address)
);

INSERT OR IGNORE INTO contact_handles_new
    SELECT handle_id, contact_id, gateway, address,
           is_primary, verified, confidence, source, created_at
    FROM contact_handles;

DROP TABLE IF EXISTS contact_handles;

ALTER TABLE contact_handles_new RENAME TO contact_handles;

CREATE INDEX IF NOT EXISTS idx_handles_contact
    ON contact_handles(contact_id);

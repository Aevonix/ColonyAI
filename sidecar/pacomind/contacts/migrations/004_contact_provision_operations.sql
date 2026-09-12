-- Durable idempotency receipts for owner-operated contact provisioning.
--
-- The contact, exact handle, audit rows, and this receipt are committed in one
-- SQLite transaction by SQLiteContactStore.provision_verified_handle().  A
-- retry after a lost response therefore returns the original result without
-- creating a second contact or handle.

CREATE TABLE IF NOT EXISTS contact_provision_operations (
  operation_id    TEXT PRIMARY KEY,
  request_sha256  TEXT NOT NULL,
  performed_by    TEXT NOT NULL,
  contact_id      TEXT NOT NULL,
  result_json     TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_provision_operations_contact
  ON contact_provision_operations(contact_id, created_at DESC);

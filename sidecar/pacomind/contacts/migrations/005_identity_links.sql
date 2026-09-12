-- Candidate associations never participate in message attribution.
CREATE TABLE IF NOT EXISTS contact_identity_candidates (
  candidate_id TEXT PRIMARY KEY,
  contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
  gateway TEXT NOT NULL,
  address TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE(contact_id,gateway,address)
);
CREATE TABLE IF NOT EXISTS contact_identity_operations (
  operation_id TEXT PRIMARY KEY,
  request_sha256 TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Retain historical name guesses as candidates rather than usable handles.
-- Source conversations are not moved or erased by this migration.
INSERT OR IGNORE INTO contact_identity_candidates
  (candidate_id,contact_id,gateway,address,source,evidence_refs_json,created_at)
SELECT 'identity-candidate:' || handle_id,contact_id,gateway,address,source,'[]',created_at
FROM contact_handles WHERE source='auto:scoped-name' AND verified=0;
DELETE FROM contact_handles WHERE source='auto:scoped-name' AND verified=0;

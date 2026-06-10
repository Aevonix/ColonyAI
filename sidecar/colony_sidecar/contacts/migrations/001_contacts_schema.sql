-- colony/contacts/migrations/001_contacts_schema.sql

-- ─── Contacts ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contacts (
  contact_id          TEXT PRIMARY KEY,         -- cid-<timestamp_ms>-<random7>
  display_name        TEXT,
  given_name          TEXT,
  family_name         TEXT,
  organization        TEXT,
  relationship_score  REAL NOT NULL DEFAULT 0.0
                        CHECK(relationship_score >= 0.0 AND relationship_score <= 1.0),
  trust_tier          TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(trust_tier IN ('inner_circle','trusted','regular',
                                             'peripheral','silenced','acquaintance','unknown')),
  interaction_allowed INTEGER NOT NULL DEFAULT 1
                        CHECK(interaction_allowed IN (0,1)),
  tags_json           TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings
  privacy_level       TEXT NOT NULL DEFAULT 'private'
                        CHECK(privacy_level IN ('public','private','restricted')),
  person_node_id      TEXT,
  notes               TEXT,
  import_source       TEXT NOT NULL DEFAULT 'manual',
  first_seen_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  last_interaction_at TEXT,
  interaction_count   INTEGER NOT NULL DEFAULT 0,
  enrichment_source   TEXT NOT NULL DEFAULT '[]',  -- JSON array
  enrichment_last_at  TEXT,
  deleted_at          TEXT,                        -- NULL until soft-deleted
  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_contacts_tier
  ON contacts(trust_tier) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_contacts_person_node
  ON contacts(person_node_id) WHERE person_node_id IS NOT NULL;

-- ─── Contact Handles ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_handles (
  handle_id    TEXT PRIMARY KEY,               -- hdl-<timestamp_ms>-<random7>
  contact_id   TEXT NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
  -- Keep in sync with the delivery channel set (delivery/channels.py):
  -- whatsapp/discord/slack were deliverable but unstorable before v0.17.
  gateway      TEXT NOT NULL
                 CHECK(gateway IN ('imessage','telegram','whatsapp','discord',
                                   'slack','email','sms','signal','custom')),
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

CREATE INDEX IF NOT EXISTS idx_handles_contact
  ON contact_handles(contact_id);

CREATE INDEX IF NOT EXISTS idx_handles_gateway_address
  ON contact_handles(gateway, address);

-- ─── Contact Audit ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_audit (
  id           TEXT PRIMARY KEY,               -- cau-<timestamp_ms>-<random7>
  contact_id   TEXT NOT NULL,
  action       TEXT NOT NULL,
    -- created, handle_added, tier_changed, interaction_toggled,
    -- enriched, soft_deleted, hard_deleted, blocked, unblocked
  detail       TEXT,                           -- JSON object with action-specific fields
  performed_by TEXT NOT NULL DEFAULT 'system', -- 'operator', 'system', 'auto'
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_contact_audit_contact
  ON contact_audit(contact_id, created_at DESC);

-- ─── Merge Proposals ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_merge_proposals (
  id              TEXT PRIMARY KEY,            -- cmp-<timestamp_ms>-<random7>
  contact_id_a    TEXT NOT NULL REFERENCES contacts(contact_id),
  contact_id_b    TEXT NOT NULL REFERENCES contacts(contact_id),
  confidence      REAL NOT NULL,
  reason          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected','auto_merged')),
  proposed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  resolved_at     TEXT,
  UNIQUE(contact_id_a, contact_id_b)
);

CREATE INDEX IF NOT EXISTS idx_merge_proposals_pending
  ON contact_merge_proposals(status)
  WHERE status = 'pending';

-- ─── Merge Audit ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_merge_audit (
  id                   TEXT PRIMARY KEY,       -- cma-<timestamp_ms>-<random7>
  canonical_id         TEXT NOT NULL,
  absorbed_id          TEXT NOT NULL,
  confidence           REAL NOT NULL,
  merge_reason         TEXT NOT NULL,
  triggered_by         TEXT NOT NULL,          -- 'auto' | 'manual'
  contact_a_snapshot   TEXT NOT NULL,          -- JSON snapshot of canonical before merge
  contact_b_snapshot   TEXT NOT NULL,          -- JSON snapshot of absorbed before merge
  merged_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ─── Blocklist ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_blocklist (
  contact_id   TEXT PRIMARY KEY REFERENCES contacts(contact_id),
  reason       TEXT,
  blocked_by   TEXT NOT NULL DEFAULT 'operator',
  blocked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  unblocked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_blocklist_active
  ON contact_blocklist(contact_id)
  WHERE unblocked_at IS NULL;

-- ─── Confirmed Distinct Pairs ────────────────────────────────────────────────
-- Prevents re-proposing merges that have been explicitly rejected.

CREATE TABLE IF NOT EXISTS contact_confirmed_distinct (
  id           TEXT PRIMARY KEY,
  contact_id_a TEXT NOT NULL,
  contact_id_b TEXT NOT NULL,
  confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  UNIQUE(contact_id_a, contact_id_b)
);

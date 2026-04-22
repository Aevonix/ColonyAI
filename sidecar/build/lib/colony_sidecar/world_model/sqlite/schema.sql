-- Colony World Model SQLite schema
-- All tables prefixed with wm_ to avoid collisions with existing schema.

-- Base entity table (all types)
CREATE TABLE IF NOT EXISTS wm_entities (
  id              TEXT PRIMARY KEY,       -- we-<timestamp>-<random7>
  name            TEXT NOT NULL,
  entity_type     TEXT NOT NULL
                    CHECK(entity_type IN (
                      'person', 'company', 'project', 'product',
                      'location', 'event', 'concept'
                    )),
  aliases         TEXT NOT NULL DEFAULT '[]',     -- JSON array
  external_ids    TEXT NOT NULL DEFAULT '{}',     -- JSON object
  properties      TEXT NOT NULL DEFAULT '{}',     -- JSON object
  confidence      REAL NOT NULL DEFAULT 0.5,
  first_seen      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  last_seen       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_wm_entities_type
  ON wm_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_wm_entities_name
  ON wm_entities(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_wm_entities_confidence
  ON wm_entities(confidence DESC);

-- Full-text search index on name + aliases
CREATE VIRTUAL TABLE IF NOT EXISTS wm_entities_fts
  USING fts5(
    id UNINDEXED,
    name,
    aliases,
    content='wm_entities',
    content_rowid='rowid'
  );

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS wm_entities_fts_insert
  AFTER INSERT ON wm_entities BEGIN
    INSERT INTO wm_entities_fts(rowid, id, name, aliases)
      VALUES (new.rowid, new.id, new.name, new.aliases);
  END;

CREATE TRIGGER IF NOT EXISTS wm_entities_fts_update
  AFTER UPDATE ON wm_entities BEGIN
    INSERT INTO wm_entities_fts(wm_entities_fts, rowid, id, name, aliases)
      VALUES ('delete', old.rowid, old.id, old.name, old.aliases);
    INSERT INTO wm_entities_fts(rowid, id, name, aliases)
      VALUES (new.rowid, new.id, new.name, new.aliases);
  END;

CREATE TRIGGER IF NOT EXISTS wm_entities_fts_delete
  AFTER DELETE ON wm_entities BEGIN
    INSERT INTO wm_entities_fts(wm_entities_fts, rowid, id, name, aliases)
      VALUES ('delete', old.rowid, old.id, old.name, old.aliases);
  END;

-- Relationships
CREATE TABLE IF NOT EXISTS wm_relationships (
  id                    TEXT PRIMARY KEY,   -- wr-<timestamp>-<random7>
  source_id             TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
  target_id             TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
  relationship_type     TEXT NOT NULL,
  confidence            REAL NOT NULL DEFAULT 0.5,
  valid_from            TEXT,               -- ISO8601; NULL = unknown start
  valid_to              TEXT,               -- ISO8601; NULL = still active
  properties            TEXT NOT NULL DEFAULT '{}',  -- JSON
  source_observation_id TEXT,
  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_wm_rels_source
  ON wm_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_wm_rels_target
  ON wm_relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_wm_rels_type
  ON wm_relationships(relationship_type);
CREATE INDEX IF NOT EXISTS idx_wm_rels_active
  ON wm_relationships(valid_to)
  WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_wm_rels_temporal
  ON wm_relationships(valid_from, valid_to);

-- Observations (provenance trail)
CREATE TABLE IF NOT EXISTS wm_observations (
  id              TEXT PRIMARY KEY,         -- wo-<timestamp>-<random7>
  entity_id       TEXT REFERENCES wm_entities(id) ON DELETE CASCADE,
  relationship_id TEXT REFERENCES wm_relationships(id) ON DELETE CASCADE,
  observation     TEXT NOT NULL,
  source          TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_wm_obs_entity
  ON wm_observations(entity_id, created_at DESC);

-- Merge proposals
CREATE TABLE IF NOT EXISTS wm_merge_proposals (
  id              TEXT PRIMARY KEY,         -- mp-<timestamp>-<random7>
  candidate_id    TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
  existing_id     TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
  match_confidence REAL NOT NULL,
  match_reason    TEXT NOT NULL,
  evidence        TEXT NOT NULL DEFAULT '{}',   -- JSON
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected', 'expired')),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  resolved_at     TEXT
);

-- Merge audit log
CREATE TABLE IF NOT EXISTS wm_merge_log (
  id                      TEXT PRIMARY KEY,  -- ma-<timestamp>-<random7>
  surviving_id            TEXT NOT NULL,
  retired_id              TEXT NOT NULL,
  relationships_repointed INTEGER NOT NULL DEFAULT 0,
  properties_updated      INTEGER NOT NULL DEFAULT 0,
  executed_by             TEXT NOT NULL,     -- "auto" | "owner_approved"
  merge_proposal_id       TEXT,
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS wm_schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  description TEXT
);

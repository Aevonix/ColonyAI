-- 002_trust_scopes.sql — context-scoped trust (group chats, households, project rooms).
--
-- A trust_scope grants its members a tier of trust that applies ONLY inside the
-- scope. Membership never confers global 1:1 rights (the member's contacts row is
-- untouched). This is Colony's generic "trusted in this room, not in my DMs" primitive.

CREATE TABLE IF NOT EXISTS trust_scopes (
  scope_id     TEXT PRIMARY KEY,                 -- ts-<ts>-<rand>
  scope_type   TEXT NOT NULL DEFAULT 'group'
               CHECK(scope_type IN ('group','household','project','event','custom')),
  platform     TEXT,                             -- e.g. 'rcs'; NULL for abstract scopes
  external_id  TEXT,                             -- platform conversation/group id
  label        TEXT,
  granted_tier TEXT NOT NULL DEFAULT 'group_guest'
               CHECK(granted_tier IN ('inner_circle','trusted','regular','group_guest','peripheral','silenced','acquaintance','unknown')),
  created_by   TEXT NOT NULL DEFAULT 'agent',
  active       INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_trust_scopes_extid
  ON trust_scopes(platform, external_id) WHERE active = 1;

CREATE TABLE IF NOT EXISTS scope_members (
  scope_id   TEXT NOT NULL REFERENCES trust_scopes(scope_id) ON DELETE CASCADE,
  contact_id TEXT NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('owner','member','observer')),
  joined_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  left_at    TEXT,                               -- NULL = current member
  PRIMARY KEY (scope_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_scope_members_contact
  ON scope_members(contact_id) WHERE left_at IS NULL;

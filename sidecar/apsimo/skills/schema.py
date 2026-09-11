"""Colony Skills — SQLite schema for the skill registry."""

SKILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id          TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    version           TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    author_colony_id  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'draft',
    tags              TEXT NOT NULL DEFAULT '',
    dependencies      TEXT NOT NULL DEFAULT '',
    entry_point       TEXT NOT NULL DEFAULT 'skill:run',
    checksum_sha256   TEXT NOT NULL DEFAULT '',
    origin_task_id    TEXT,
    parent_skill_id   TEXT,
    trust_score       REAL NOT NULL DEFAULT 0.0,
    execution_count   INTEGER NOT NULL DEFAULT 0,
    last_executed_at  TEXT,
    skill_dir         TEXT,
    trigger_patterns  TEXT NOT NULL DEFAULT '[]',
    context_tokens_estimate INTEGER NOT NULL DEFAULT 2048,
    lazy_loader       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (parent_skill_id) REFERENCES skills(skill_id)
);

CREATE INDEX IF NOT EXISTS idx_skills_status    ON skills(status);
CREATE INDEX IF NOT EXISTS idx_skills_tags      ON skills(tags);
CREATE INDEX IF NOT EXISTS idx_skills_trust     ON skills(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_skills_author    ON skills(author_colony_id);

CREATE TABLE IF NOT EXISTS skill_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id          TEXT NOT NULL,
    version           TEXT NOT NULL,
    checksum_sha256   TEXT NOT NULL,
    skill_dir         TEXT NOT NULL,
    promoted_at       TEXT NOT NULL,
    promoted_by       TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE IF NOT EXISTS skill_attestations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id          TEXT NOT NULL,
    attesting_colony  TEXT NOT NULL,
    risk_level        TEXT NOT NULL,
    days_clean        INTEGER NOT NULL DEFAULT 0,
    total_executions  INTEGER NOT NULL DEFAULT 0,
    violations        INTEGER NOT NULL DEFAULT 0,
    signature         TEXT NOT NULL,
    attested_at       TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE IF NOT EXISTS skill_quarantine_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id          TEXT NOT NULL,
    reason            TEXT NOT NULL,
    resolved_by       TEXT,
    resolved_at       TEXT,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE IF NOT EXISTS skill_execution_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id          TEXT NOT NULL,
    execution_id      TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL,
    duration_ms       INTEGER,
    peak_memory_mb    REAL,
    violations        TEXT,
    executed_at       TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);
"""

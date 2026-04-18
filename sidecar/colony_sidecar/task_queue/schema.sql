-- Colony Distributed Task Queue — SQLite schema
-- WAL mode is set at connection time (PRAGMA journal_mode=WAL)

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    payload         TEXT NOT NULL,          -- JSON
    priority        INTEGER NOT NULL DEFAULT 50,
    capabilities    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    deadline        TEXT,                   -- ISO8601 UTC
    max_retries     INTEGER NOT NULL DEFAULT 3,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    timeout_secs    REAL NOT NULL DEFAULT 3600.0,
    depends_on      TEXT NOT NULL DEFAULT '[]',  -- JSON array of job_ids
    posted_by       TEXT NOT NULL DEFAULT '',
    posted_at       TEXT NOT NULL,          -- ISO8601 UTC
    status          TEXT NOT NULL DEFAULT 'queued',
    claimed_by      TEXT,
    claimed_at      TEXT,
    last_heartbeat  TEXT,
    result          TEXT,                   -- JSON
    tags            TEXT NOT NULL DEFAULT '{}'   -- JSON object
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_priority   ON jobs(priority DESC, posted_at ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_deadline   ON jobs(deadline) WHERE deadline IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_claimed_by ON jobs(claimed_by) WHERE claimed_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS workers (
    node_id         TEXT PRIMARY KEY,
    capabilities    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    capacity        TEXT NOT NULL DEFAULT '{}',  -- JSON object
    max_concurrent  INTEGER NOT NULL DEFAULT 4,
    job_types       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    available       INTEGER NOT NULL DEFAULT 1,
    load            REAL NOT NULL DEFAULT 0.0,
    registered_at   TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,          -- ISO8601 UTC
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    node_id         TEXT,
    reason          TEXT,
    details         TEXT                    -- JSON
);

CREATE INDEX IF NOT EXISTS idx_audit_job_id     ON job_audit(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts         ON job_audit(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_workers_available ON workers(available);
CREATE INDEX IF NOT EXISTS idx_workers_last_seen ON workers(last_seen DESC);

CREATE TABLE IF NOT EXISTS heartbeats (
    node_id         TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    progress        REAL,
    PRIMARY KEY (node_id, job_id)
);

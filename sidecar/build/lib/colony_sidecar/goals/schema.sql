-- Colony Goal Engine persistence schema

CREATE TABLE IF NOT EXISTS goals (
    goal_id         TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'explicit',
    status          TEXT NOT NULL DEFAULT 'proposed',
    priority        INTEGER NOT NULL DEFAULT 50,
    outcome_json    TEXT,           -- GoalOutcome serialised as JSON
    deadline        TEXT,           -- ISO-8601 datetime
    parent_goal_id  TEXT,           -- self-reference for sub-goals
    tags_json       TEXT NOT NULL DEFAULT '{}',
    context_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    accepted_at     TEXT,
    completed_at    TEXT,
    abandoned_at    TEXT,
    abandon_reason  TEXT,
    replan_count    INTEGER NOT NULL DEFAULT 0,
    estimated_hours REAL,
    progress_pct    REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (parent_goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_priority ON goals(priority DESC);
CREATE INDEX IF NOT EXISTS idx_goals_deadline ON goals(deadline);

CREATE TABLE IF NOT EXISTS subtasks (
    subtask_id          TEXT PRIMARY KEY,
    goal_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    job_type            TEXT NOT NULL DEFAULT 'custom',
    payload_json        TEXT NOT NULL DEFAULT '{}',
    capabilities_json   TEXT NOT NULL DEFAULT '[]',
    depends_on_json     TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'pending',
    job_id              TEXT,           -- task queue job_id
    result_json         TEXT,
    depth               INTEGER NOT NULL DEFAULT 0,
    is_critical_path    INTEGER NOT NULL DEFAULT 0,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 2,
    estimated_hours     REAL,
    started_at          TEXT,
    completed_at        TEXT,
    error               TEXT,
    dag_version         INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_subtasks_goal ON subtasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_subtasks_status ON subtasks(status);
CREATE INDEX IF NOT EXISTS idx_subtasks_job ON subtasks(job_id);

CREATE TABLE IF NOT EXISTS goal_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL,
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_goal ON goal_audit_log(goal_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON goal_audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS goal_dag_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    dag_json        TEXT NOT NULL,  -- Full GoalDAG serialised as JSON
    created_at      TEXT NOT NULL,
    UNIQUE(goal_id, version),
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

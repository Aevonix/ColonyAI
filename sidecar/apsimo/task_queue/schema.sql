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
    claim_attempt_id TEXT,
    claim_expires_at TEXT,
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
    claim_attempt_id TEXT,
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
    claim_attempt_id TEXT,
    progress        REAL,
    PRIMARY KEY (node_id, job_id)
);

-- Completion/failure competence delivery is an outbox, not a best-effort
-- callback after the job transaction. event_id is stable per claim attempt.
CREATE TABLE IF NOT EXISTS worker_outcome_outbox (
    event_id         TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    claim_attempt_id TEXT NOT NULL,
    report           TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    worker_mode      TEXT NOT NULL,
    success_attested INTEGER NOT NULL DEFAULT 0,
    latency          REAL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    delivered_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_worker_outcome_pending
ON worker_outcome_outbox(state, created_at);

-- WorkControlV1 is an additive control ledger over real queue jobs.  It does
-- not duplicate executable payloads and deliberately survives job pruning so
-- operation receipts remain readable after the work itself is archived.
CREATE TABLE IF NOT EXISTS work_control_targets (
    target_id        TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL UNIQUE,
    authority_digest TEXT NOT NULL,
    revision         INTEGER NOT NULL DEFAULT 0,
    state_digest     TEXT NOT NULL DEFAULT '',
    projected_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_control_operations (
    operation_id      TEXT PRIMARY KEY,
    target_id         TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    authority_digest  TEXT NOT NULL,
    operation_type    TEXT NOT NULL,
    request_digest    TEXT NOT NULL,
    request_json      TEXT NOT NULL,
    requested_by      TEXT NOT NULL,
    request_authority TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    expected_state_digest TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL,
    result_revision   INTEGER,
    result_state_digest TEXT,
    attempt_id        TEXT,
    worker_id         TEXT,
    from_job_status   TEXT,
    to_job_status     TEXT,
    effect_disposition TEXT NOT NULL,
    ack_details       TEXT NOT NULL DEFAULT '{}',
    ack_authority     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    ack_deadline      TEXT,
    acknowledged_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_control_target
ON work_control_operations(target_id, created_at);

CREATE INDEX IF NOT EXISTS idx_work_control_pending_worker
ON work_control_operations(worker_id, status, created_at);

-- Accepted and outcome receipts are immutable append-only facts.  The
-- operation row above is only a current projection for efficient polling.
CREATE TABLE IF NOT EXISTS work_control_receipts (
    receipt_id       TEXT PRIMARY KEY,
    operation_id     TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    authority_digest TEXT NOT NULL,
    phase            TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    receipt_digest   TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_control_receipts_operation
ON work_control_receipts(operation_id, created_at, receipt_id);

-- A steer handler may finish before its acknowledgement reaches the queue.
-- This exact-attempt ledger lets a restarted worker retry the acknowledgement
-- before invoking the handler again. Steer handlers must additionally attest
-- durable operation-id idempotency to cover the handler/ledger crash window.
CREATE TABLE IF NOT EXISTS work_control_worker_outcomes (
    operation_id     TEXT PRIMARY KEY,
    target_id        TEXT NOT NULL,
    attempt_id       TEXT NOT NULL,
    worker_id        TEXT NOT NULL,
    authority_digest TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    details_json     TEXT NOT NULL,
    outcome_digest   TEXT NOT NULL UNIQUE,
    recorded_at      TEXT NOT NULL
);

-- Independent evidence is first-valid-decision-wins for an exact started
-- attempt. It can prove either that the effect applied or that it did not.
CREATE TABLE IF NOT EXISTS work_effect_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    target_id          TEXT NOT NULL,
    attempt_id         TEXT NOT NULL,
    authority_digest   TEXT NOT NULL,
    finding            TEXT NOT NULL,
    evidence_json      TEXT NOT NULL,
    evidence_digest    TEXT NOT NULL,
    verifier_identity  TEXT NOT NULL,
    verifier_type      TEXT NOT NULL,
    verifier_authority TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE(target_id, attempt_id)
);

-- Pruning retires an identity permanently. Without this tombstone a
-- deterministic WorkOrder/job ID could be reposted after its evidence row was
-- deleted and accidentally acquire a second execution history.
CREATE TABLE IF NOT EXISTS job_tombstones (
    job_id          TEXT PRIMARY KEY,
    final_status    TEXT NOT NULL,
    job_digest      TEXT NOT NULL,
    pruned_at       TEXT NOT NULL
);

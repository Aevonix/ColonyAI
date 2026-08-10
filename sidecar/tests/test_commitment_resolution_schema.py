"""Fail-closed schema and readiness gates for commitment recovery proofs."""

import hashlib
import inspect
import sqlite3

import pytest

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.commitments.store import (
    CommitmentResolutionSchemaError,
    CommitmentStore,
    RESOLUTION_RECOVERY_CAPABILITY,
    _RESOLUTION_BOUND_DELETE_TRIGGER_SQL,
    _RESOLUTION_OPERATION_DELETE_TRIGGER_SQL,
    _RESOLUTION_OPERATION_UPDATE_TRIGGER_SQL,
    _operation_material,
)


_LEGACY_COMMITMENTS_SQL = """
CREATE TABLE commitments (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    description TEXT NOT NULL,
    made_at TEXT NOT NULL,
    due_at TEXT,
    fulfilled_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    source_context TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    priority INTEGER NOT NULL DEFAULT 50,
    metadata TEXT
)
"""

_D829_RECOVERY_SQL = """
CREATE TABLE IF NOT EXISTS commitment_resolution_operations (
    operation_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    note TEXT NOT NULL,
    note_digest TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    status TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    record_digest TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS commitment_resolution_operations_no_update
BEFORE UPDATE ON commitment_resolution_operations
BEGIN
    SELECT RAISE(ABORT, 'commitment resolution operations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS commitment_resolution_operations_no_delete
BEFORE DELETE ON commitment_resolution_operations
BEGIN
    SELECT RAISE(ABORT, 'commitment resolution operations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS commitment_resolution_bound_row_no_delete
BEFORE DELETE ON commitments
WHEN EXISTS (
    SELECT 1 FROM commitment_resolution_operations
    WHERE commitment_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'operation-bound commitments cannot be deleted');
END;
"""


def _connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _recovery_objects(path):
    with _connect(path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                """SELECT type,name,tbl_name,sql FROM sqlite_master
                   WHERE name LIKE 'commitment_resolution_%'
                   ORDER BY name"""
            ).fetchall()
        ]


def _replace_operation_table(path, table_sql):
    with _connect(path) as connection:
        connection.execute("DROP TRIGGER commitment_resolution_bound_row_no_delete")
        connection.execute(
            "DROP TRIGGER commitment_resolution_operations_no_delete"
        )
        connection.execute(
            "DROP TRIGGER commitment_resolution_operations_no_update"
        )
        connection.execute("DROP TABLE commitment_resolution_operations")
        connection.execute(table_sql)
        connection.execute(_RESOLUTION_OPERATION_UPDATE_TRIGGER_SQL)
        connection.execute(_RESOLUTION_OPERATION_DELETE_TRIGGER_SQL)
        connection.execute(_RESOLUTION_BOUND_DELETE_TRIGGER_SQL)


def test_exact_legacy_absence_migrates_preserves_data_and_restarts(tmp_path):
    db_path = tmp_path / "legacy.db"
    with _connect(db_path) as connection:
        connection.execute(_LEGACY_COMMITMENTS_SQL)
        connection.execute(
            """INSERT INTO commitments
               (id,person_id,description,made_at,status,source_type,priority)
               VALUES ('legacy-1','owner','preserve me','2026-01-01T00:00:00+00:00',
                       'pending','manual',50)"""
        )
    assert _recovery_objects(db_path) == []

    first = CommitmentStore(db_path)
    assert first.get("legacy-1")["description"] == "preserve me"
    assert first.resolution_recovery_readiness() == {
        "ready": True,
        "capability": RESOLUTION_RECOVERY_CAPABILITY,
        "schema": "ColonyCommitmentResolutionRecoveryV1",
        "version": 1,
    }
    migrated_objects = _recovery_objects(db_path)
    assert len(migrated_objects) == 4

    restarted = CommitmentStore(db_path)
    assert restarted.get("legacy-1")["status"] == "pending"
    assert restarted.resolution_recovery_readiness()["ready"] is True
    assert _recovery_objects(db_path) == migrated_objects


def test_exact_d829_schema_preserves_bound_proof_across_restart(tmp_path):
    db_path = tmp_path / "d829.db"
    resolved_at = "2026-07-16T20:00:00+00:00"
    operation = _operation_material(
        operation_id="concern-source-operation:d829",
        commitment_id="d829-bound",
        outcome="done",
        note="preserved",
        note_digest=hashlib.sha256(b"preserved").hexdigest(),
        resolved_by="owner",
        status="fulfilled",
        resolved_at=resolved_at,
    )
    with _connect(db_path) as connection:
        connection.execute(_LEGACY_COMMITMENTS_SQL)
        connection.executescript(_D829_RECOVERY_SQL)
        connection.execute(
            """INSERT INTO commitments
               (id,person_id,description,made_at,fulfilled_at,status,
                source_type,priority)
               VALUES (?,?,?,?,?,'fulfilled','manual',50)""",
            (
                "d829-bound", "owner", "preserve bound proof",
                resolved_at, resolved_at,
            ),
        )
        connection.execute(
            """INSERT INTO commitment_resolution_operations
               (operation_id,commitment_id,outcome,note,note_digest,
                resolved_by,status,resolved_at,record_digest)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                operation["operation_id"], operation["commitment_id"],
                operation["outcome"], operation["note"],
                operation["note_digest"], operation["resolved_by"],
                operation["status"], operation["resolved_at"],
                operation["record_digest"],
            ),
        )

    first = CommitmentStore(db_path)
    assert first.resolution_recovery_readiness()["ready"] is True
    assert first.get_resolution_operation("d829-bound") == {
        "schema": "ColonyCommitmentResolutionOperationV1",
        "version": 1,
        **operation,
    }
    restarted = CommitmentStore(db_path)
    assert restarted.get_resolution_operation("d829-bound")[
        "record_digest"
    ] == operation["record_digest"]


def test_partial_recovery_schema_rejects_without_auto_repair(tmp_path):
    db_path = tmp_path / "partial.db"
    CommitmentStore(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER commitment_resolution_operations_no_update"
        )

    with pytest.raises(CommitmentResolutionSchemaError, match="partial"):
        CommitmentStore(db_path)

    names = {row[1] for row in _recovery_objects(db_path)}
    assert "commitment_resolution_operations_no_update" not in names
    assert len(names) == 3


@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    [
        (
            "commitment_resolution_operations_no_update",
            """CREATE TRIGGER commitment_resolution_operations_no_update
               BEFORE UPDATE ON commitment_resolution_operations
               BEGIN SELECT 1; END""",
        ),
        (
            "commitment_resolution_operations_no_delete",
            """CREATE TRIGGER commitment_resolution_operations_no_delete
               BEFORE DELETE ON commitment_resolution_operations
               BEGIN SELECT 1; END""",
        ),
        (
            "commitment_resolution_bound_row_no_delete",
            """CREATE TRIGGER commitment_resolution_bound_row_no_delete
               BEFORE DELETE ON commitments
               BEGIN SELECT 1; END""",
        ),
    ],
    ids=["proof-update", "proof-delete", "bound-row-delete"],
)
def test_same_name_noop_trigger_is_rejected_before_store_use(
    tmp_path, trigger_name, trigger_sql,
):
    db_path = tmp_path / "noop-trigger.db"
    CommitmentStore(db_path)
    with _connect(db_path) as connection:
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute(trigger_sql)

    with pytest.raises(CommitmentResolutionSchemaError, match="object"):
        CommitmentStore(db_path)


@pytest.mark.parametrize(
    "table_sql",
    [
        """CREATE TABLE commitment_resolution_operations (
               operation_id TEXT PRIMARY KEY
           )""",
        """CREATE TABLE commitment_resolution_operations (
               operation_id TEXT PRIMARY KEY,
               commitment_id TEXT NOT NULL,
               outcome TEXT NOT NULL,
               note TEXT NOT NULL,
               note_digest TEXT NOT NULL,
               resolved_by TEXT NOT NULL,
               status TEXT NOT NULL,
               resolved_at TEXT NOT NULL,
               record_digest TEXT NOT NULL UNIQUE
           )""",
    ],
    ids=["malformed-columns", "missing-commitment-unique"],
)
def test_malformed_operation_columns_or_constraints_are_rejected(
    tmp_path, table_sql,
):
    db_path = tmp_path / "malformed.db"
    CommitmentStore(db_path)
    _replace_operation_table(db_path, table_sql)

    with pytest.raises(CommitmentResolutionSchemaError, match="object"):
        CommitmentStore(db_path)


def test_runtime_trigger_tamper_revokes_readiness_and_blocks_resolution(
    tmp_path,
):
    db_path = tmp_path / "tampered.db"
    store = CommitmentStore(db_path)
    commitment = store.create(person_id="owner", description="still open")
    with _connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER commitment_resolution_bound_row_no_delete"
        )

    with pytest.raises(CommitmentResolutionSchemaError, match="incomplete"):
        store.resolution_recovery_readiness()
    with pytest.raises(CommitmentResolutionSchemaError, match="incomplete"):
        store.resolve(
            commitment["id"], operation_id="concern-source-operation:test",
        )
    assert store.get(commitment["id"])["status"] == "pending"


def test_extra_protected_table_trigger_cannot_rewrite_bound_resolution(
    tmp_path,
):
    db_path = tmp_path / "extra-trigger.db"
    store = CommitmentStore(db_path)
    commitment = store.create(person_id="owner", description="must stay done")
    with _connect(db_path) as connection:
        connection.execute(
            """CREATE TRIGGER extra_resolution_rewriter
               AFTER UPDATE ON commitments
               BEGIN
                   UPDATE commitments SET status='pending' WHERE id=NEW.id;
               END"""
        )

    with pytest.raises(CommitmentResolutionSchemaError, match="trigger set"):
        store.resolution_recovery_readiness()
    with pytest.raises(CommitmentResolutionSchemaError, match="trigger set"):
        store.resolve(
            commitment["id"], operation_id="concern-source-operation:test",
        )
    assert store.get(commitment["id"])["status"] == "pending"
    with _connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM commitment_resolution_operations"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["resolve", "update", "delete"])
def test_proof_related_mutations_hold_schema_write_lock(
    tmp_path, monkeypatch, operation,
):
    db_path = tmp_path / f"{operation}-lock.db"
    store = CommitmentStore(db_path)
    commitment = store.create(person_id="owner", description="locked schema")
    if operation in {"update", "delete"}:
        store.resolve(
            commitment["id"], operation_id="concern-source-operation:bound",
        )

    original_validate = store._validate_resolution_recovery_schema
    competing_writes = []

    def validate_while_competing(conn):
        original_validate(conn)
        contender = sqlite3.connect(db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "DROP TRIGGER commitment_resolution_operations_no_update"
                )
            competing_writes.append("blocked")
        finally:
            contender.close()

    monkeypatch.setattr(
        store, "_validate_resolution_recovery_schema",
        validate_while_competing,
    )
    if operation == "resolve":
        result = store.resolve(
            commitment["id"], operation_id="concern-source-operation:new",
        )
        assert result["status"] == "fulfilled"
    elif operation == "update":
        result = store.update(commitment["id"], metadata={"replacement": True})
        assert result["metadata"] == {"replacement": True}
    else:
        assert store.delete(commitment["id"]) is False

    assert competing_writes == ["blocked"]
    with _connect(db_path) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type='trigger'
                 AND name='commitment_resolution_operations_no_update'"""
        ).fetchone()[0] == 1


def test_proof_read_is_snapshot_bound_to_schema_validation(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "proof-read-snapshot.db"
    store = CommitmentStore(db_path)
    commitment = store.create(person_id="owner", description="original proof")
    operation_id = "concern-source-operation:read-snapshot"
    store.resolve(
        commitment["id"], note="original", operation_id=operation_id,
    )
    original_proof = store.get_resolution_operation(commitment["id"])
    forged_note = "forged"
    forged = _operation_material(
        operation_id=operation_id,
        commitment_id=commitment["id"],
        outcome="done",
        note=forged_note,
        note_digest=hashlib.sha256(forged_note.encode("utf-8")).hexdigest(),
        resolved_by="owner",
        status="fulfilled",
        resolved_at=original_proof["resolved_at"],
    )
    original_validate = store._validate_resolution_recovery_schema
    competing_outcomes = []

    def validate_then_compete(conn):
        original_validate(conn)
        contender = sqlite3.connect(db_path, timeout=0)
        try:
            try:
                contender.execute(
                    "DROP TRIGGER commitment_resolution_operations_no_update"
                )
                contender.execute(
                    """UPDATE commitment_resolution_operations
                       SET note=?,note_digest=?,record_digest=?
                       WHERE commitment_id=?""",
                    (
                        forged["note"], forged["note_digest"],
                        forged["record_digest"], commitment["id"],
                    ),
                )
                contender.execute(_RESOLUTION_OPERATION_UPDATE_TRIGGER_SQL)
                contender.commit()
                competing_outcomes.append("committed")
            except sqlite3.OperationalError as exc:
                contender.rollback()
                assert "locked" in str(exc).lower()
                competing_outcomes.append("blocked")
        finally:
            contender.close()

    monkeypatch.setattr(
        store, "_validate_resolution_recovery_schema", validate_then_compete,
    )
    observed = store.get_resolution_operation(commitment["id"])
    assert competing_outcomes in (["blocked"], ["committed"])
    assert observed == original_proof


@pytest.mark.asyncio
async def test_host_capability_and_health_follow_live_recovery_readiness(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "health.db"
    store = CommitmentStore(db_path)
    monkeypatch.setattr(host_mod, "_commitment_store", store)

    assert RESOLUTION_RECOVERY_CAPABILITY in host_mod.supported_capabilities()
    healthy = await host_mod.health()
    assert healthy.status == "ok"
    assert healthy.notes["commitments"].endswith("recovery v1 ready)")

    with _connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER commitment_resolution_operations_no_delete"
        )

    assert RESOLUTION_RECOVERY_CAPABILITY not in host_mod.supported_capabilities()
    unhealthy = await host_mod.health()
    assert unhealthy.status == "degraded"
    assert unhealthy.notes["commitments"].endswith("recovery unavailable)")


def test_server_validates_before_wiring_and_fails_lifespan_closed():
    from colony_sidecar import server

    source = inspect.getsource(server.lifespan)
    section = source[
        source.index("# --- 7b. Commitment Store ---"):
        source.index("# --- 7c. Theory of Mind ---")
    ]
    assert (
        section.index("resolution_recovery_readiness()")
        < section.index("set_commitment_store(commitment_store)")
    )
    assert "set_commitment_store(None)" in section
    assert (
        'raise RuntimeError("CommitmentStore initialization failed") from exc'
        in section
    )
    assert "CommitmentStore init failed: %s" not in section

"""Exact rollback-floor compatibility for commitment resolution proofs."""

from pathlib import Path
import sqlite3

import pytest

from colony_sidecar.commitments.store import CommitmentStore


# Deployment evidence: rollback floor
# 4669d6618343175d3439acf37c03364bf9eb53d2 has complete store-source SHA-256
# 393eb58f8dfd70bb3e6c94b4a39f70f071321e526b5e92a77e3ed2329d93c9d3.
# These local helpers preserve its raw create/update/delete SQL shape without
# requiring repository history in shallow CI or a source archive.


def _legacy_get(db_path: Path, commitment_id: str):
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def _legacy_create(db_path: Path, commitment_id: str):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO commitments
               (id, person_id, description, made_at, due_at, status,
                source_type, source_context, priority, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                commitment_id, "owner", "legacy write",
                "2026-07-16T20:00:00+00:00", None, "pending",
                "manual", None, 50, None,
            ),
        )


def _legacy_update_status(
    db_path: Path, commitment_id: str, status: str,
):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE commitments SET status = ? WHERE id = ?",
            (status, commitment_id),
        )
    return _legacy_get(db_path, commitment_id)


def _legacy_delete(db_path: Path, commitment_id: str) -> bool:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        current = connection.execute(
            "SELECT status FROM commitments WHERE id = ?", (commitment_id,),
        ).fetchone()
        if not current or current["status"] not in ("fulfilled", "cancelled"):
            return False
        connection.execute(
            "DELETE FROM commitments WHERE id = ?", (commitment_id,),
        )
        return True


def _operation_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM commitment_resolution_operations"
        ).fetchone()[0])


def test_exact_legacy_floor_can_use_schema_only_zero_proof_database(tmp_path):
    db_path = tmp_path / "schema-only.db"
    current = CommitmentStore(db_path)
    terminal = current.create(person_id="owner", description="ordinary result")
    current.resolve(terminal["id"], outcome="done")
    assert _operation_count(db_path) == 0

    assert _legacy_get(db_path, terminal["id"])["status"] == "fulfilled"
    assert _legacy_delete(db_path, terminal["id"]) is True

    _legacy_create(db_path, "legacy-created")
    updated = _legacy_update_status(db_path, "legacy-created", "cancelled")
    assert updated["status"] == "cancelled"
    assert _legacy_delete(db_path, "legacy-created") is True
    assert _operation_count(db_path) == 0


def test_first_bound_proof_makes_current_store_the_minimum_data_floor(tmp_path):
    db_path = tmp_path / "proof-bound.db"
    current = CommitmentStore(db_path)
    commitment = current.create(person_id="owner", description="bound result")
    current.resolve(
        commitment["id"],
        outcome="done",
        operation_id="concern-source-operation:downgrade-floor",
    )
    assert _operation_count(db_path) == 1

    assert _legacy_get(db_path, commitment["id"])["status"] == "fulfilled"
    with pytest.raises(
        sqlite3.IntegrityError, match="operation-bound commitments",
    ):
        _legacy_delete(db_path, commitment["id"])
    assert current.get_resolution_operation(commitment["id"])[
        "operation_id"
    ] == "concern-source-operation:downgrade-floor"

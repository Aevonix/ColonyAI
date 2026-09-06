"""SQLite-backed commitment store."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Statuses that mean "still awaiting action". 'overdue' is a pending item
# whose due date passed (the condition worker flips it so the overdue event
# fires exactly once) — every open-work query must include BOTH, or a flipped
# item silently vanishes from dedup lists, prompt sections, and workspace
# ingest while still being owed.
OPEN_STATUSES = ("pending", "overdue")

# How a resolution outcome maps onto the terminal status. 'done' is the only
# outcome that counts as kept; everything else is a cancellation whose reason
# the system learns from (see resolution_stats / recent_rejections).
OUTCOME_TO_STATUS = {
    "done": "fulfilled",
    "invalid": "cancelled",
    "duplicate": "cancelled",
    "wont_do": "cancelled",
    "obsolete": "cancelled",
}


RESOLUTION_RECOVERY_CAPABILITY = "commitment_resolution_recovery_v1"
_RESOLUTION_RECOVERY_SCHEMA = "ColonyCommitmentResolutionRecoveryV1"
_RESOLUTION_OPERATION_TABLE = "commitment_resolution_operations"
_RESOLUTION_OPERATION_UPDATE_TRIGGER = (
    "commitment_resolution_operations_no_update"
)
_RESOLUTION_OPERATION_DELETE_TRIGGER = (
    "commitment_resolution_operations_no_delete"
)
_RESOLUTION_BOUND_DELETE_TRIGGER = "commitment_resolution_bound_row_no_delete"

_RESOLUTION_OPERATION_TABLE_SQL = """\
CREATE TABLE commitment_resolution_operations (
    operation_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    note TEXT NOT NULL,
    note_digest TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    status TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    record_digest TEXT NOT NULL UNIQUE
)"""

_RESOLUTION_OPERATION_UPDATE_TRIGGER_SQL = """\
CREATE TRIGGER commitment_resolution_operations_no_update
BEFORE UPDATE ON commitment_resolution_operations
BEGIN
    SELECT RAISE(ABORT, 'commitment resolution operations are immutable');
END"""

_RESOLUTION_OPERATION_DELETE_TRIGGER_SQL = """\
CREATE TRIGGER commitment_resolution_operations_no_delete
BEFORE DELETE ON commitment_resolution_operations
BEGIN
    SELECT RAISE(ABORT, 'commitment resolution operations are immutable');
END"""

_RESOLUTION_BOUND_DELETE_TRIGGER_SQL = """\
CREATE TRIGGER commitment_resolution_bound_row_no_delete
BEFORE DELETE ON commitments
WHEN EXISTS (
    SELECT 1 FROM commitment_resolution_operations
    WHERE commitment_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'operation-bound commitments cannot be deleted');
END"""

_RESOLUTION_RECOVERY_OBJECTS = {
    _RESOLUTION_OPERATION_TABLE: (
        "table", _RESOLUTION_OPERATION_TABLE,
        _RESOLUTION_OPERATION_TABLE_SQL,
    ),
    _RESOLUTION_OPERATION_UPDATE_TRIGGER: (
        "trigger", _RESOLUTION_OPERATION_TABLE,
        _RESOLUTION_OPERATION_UPDATE_TRIGGER_SQL,
    ),
    _RESOLUTION_OPERATION_DELETE_TRIGGER: (
        "trigger", _RESOLUTION_OPERATION_TABLE,
        _RESOLUTION_OPERATION_DELETE_TRIGGER_SQL,
    ),
    _RESOLUTION_BOUND_DELETE_TRIGGER: (
        "trigger", "commitments", _RESOLUTION_BOUND_DELETE_TRIGGER_SQL,
    ),
}

_RESOLUTION_OPERATION_COLUMNS = (
    (0, "operation_id", "TEXT", 0, None, 1),
    (1, "commitment_id", "TEXT", 1, None, 0),
    (2, "outcome", "TEXT", 1, None, 0),
    (3, "note", "TEXT", 1, None, 0),
    (4, "note_digest", "TEXT", 1, None, 0),
    (5, "resolved_by", "TEXT", 1, None, 0),
    (6, "status", "TEXT", 1, None, 0),
    (7, "resolved_at", "TEXT", 1, None, 0),
    (8, "record_digest", "TEXT", 1, None, 0),
)

_RESOLUTION_OPERATION_INDEXES = {
    "sqlite_autoindex_commitment_resolution_operations_1": (
        "pk", "operation_id",
    ),
    "sqlite_autoindex_commitment_resolution_operations_2": (
        "u", "commitment_id",
    ),
    "sqlite_autoindex_commitment_resolution_operations_3": (
        "u", "record_digest",
    ),
}


class CommitmentResolutionSchemaError(RuntimeError):
    """The durable resolution-recovery schema is absent or untrustworthy."""


def _sql_signature(sql: Optional[str]) -> str:
    """Stable exact-DDL comparison without depending on indentation."""

    return " ".join((sql or "").strip().rstrip(";").split())


class CommitmentResolutionConflict(ValueError):
    """A terminal commitment does not match a bound cascade operation."""

    settlement_error_code = "operation_conflict"


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _operation_material(
    *,
    operation_id: str,
    commitment_id: str,
    outcome: str,
    note: str,
    note_digest: str,
    resolved_by: str,
    status: str,
    resolved_at: str,
) -> Dict[str, Any]:
    if (
        type(operation_id) is not str or not operation_id
        or operation_id != operation_id.strip() or len(operation_id) > 192
    ):
        raise ValueError("commitment resolution operation_id is invalid")
    if type(commitment_id) is not str or not commitment_id:
        raise ValueError("commitment resolution operation commitment is invalid")
    if outcome not in OUTCOME_TO_STATUS or OUTCOME_TO_STATUS[outcome] != status:
        raise ValueError("commitment resolution operation outcome is invalid")
    if type(note) is not str or len(note) > 300:
        raise ValueError("commitment resolution operation note is invalid")
    if (
        type(note_digest) is not str or len(note_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in note_digest)
    ):
        raise ValueError("commitment resolution operation note digest is invalid")
    if type(resolved_by) is not str or len(resolved_by) > 128:
        raise ValueError("commitment resolution operation resolver is invalid")
    if type(resolved_at) is not str or not resolved_at:
        raise ValueError("commitment resolution operation time is invalid")
    payload = {
        "schema": "ColonyCommitmentResolutionOperationV1",
        "version": 1,
        "operation_id": operation_id,
        "commitment_id": commitment_id,
        "outcome": outcome,
        "note": note,
        "note_digest": note_digest,
        "resolved_by": resolved_by,
        "status": status,
        "resolved_at": resolved_at,
    }
    return {
        **payload,
        "record_digest": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
    }


def _normalize_desc(text: str) -> str:
    """Lowercased, punctuation-collapsed form used for duplicate detection."""
    out = []
    for ch in (text or "").lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def _similar_desc(a: str, b: str) -> bool:
    """True when two normalized descriptions describe the same item: exact,
    containment, or high token overlap (Jaccard >= 0.6)."""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.6


class CommitmentStore:
    """Persistent store for commitment tracking.

    Thread-safe via a threading lock. All datetime values stored as
    ISO 8601 UTC strings.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS commitments (
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
                    );

                    CREATE INDEX IF NOT EXISTS idx_commitments_person
                        ON commitments(person_id);
                    CREATE INDEX IF NOT EXISTS idx_commitments_status
                        ON commitments(status);
                    CREATE INDEX IF NOT EXISTS idx_commitments_due
                        ON commitments(due_at) WHERE status = 'pending';
                    CREATE INDEX IF NOT EXISTS idx_commitments_person_status
                        ON commitments(person_id, status);
                """)
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                existing = self._resolution_recovery_object_names(conn)
                expected = set(_RESOLUTION_RECOVERY_OBJECTS)
                if existing and existing != expected:
                    raise CommitmentResolutionSchemaError(
                        "commitment resolution recovery schema is partial"
                    )
                if not existing:
                    for _kind, _table, ddl in (
                        _RESOLUTION_RECOVERY_OBJECTS[name]
                        for name in _RESOLUTION_RECOVERY_OBJECTS
                    ):
                        conn.execute(ddl)
                self._validate_resolution_recovery_schema(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _resolution_recovery_object_names(
        conn: sqlite3.Connection,
    ) -> set[str]:
        placeholders = ",".join("?" for _ in _RESOLUTION_RECOVERY_OBJECTS)
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(_RESOLUTION_RECOVERY_OBJECTS),
        ).fetchall()
        return {str(row["name"]) for row in rows}

    @classmethod
    def _validate_resolution_recovery_schema(
        cls, conn: sqlite3.Connection,
    ) -> None:
        """Reject any same-name, partial, or weakened recovery schema."""

        placeholders = ",".join("?" for _ in _RESOLUTION_RECOVERY_OBJECTS)
        rows = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            f"WHERE name IN ({placeholders})",
            tuple(_RESOLUTION_RECOVERY_OBJECTS),
        ).fetchall()
        objects = {str(row["name"]): row for row in rows}
        if set(objects) != set(_RESOLUTION_RECOVERY_OBJECTS):
            raise CommitmentResolutionSchemaError(
                "commitment resolution recovery schema is incomplete"
            )
        for name, (object_type, table_name, expected_sql) in (
            _RESOLUTION_RECOVERY_OBJECTS.items()
        ):
            row = objects[name]
            if (
                row["type"] != object_type
                or row["tbl_name"] != table_name
                or _sql_signature(row["sql"]) != _sql_signature(expected_sql)
            ):
                raise CommitmentResolutionSchemaError(
                    "commitment resolution recovery object is invalid"
                )

        protected_triggers = {
            str(row["name"]): str(row["tbl_name"])
            for row in conn.execute(
                """SELECT name,tbl_name FROM sqlite_master
                   WHERE type='trigger'
                     AND tbl_name IN (
                         'commitments',
                         'commitment_resolution_operations'
                     )"""
            ).fetchall()
        }
        expected_triggers = {
            name: table_name
            for name, (object_type, table_name, _sql) in (
                _RESOLUTION_RECOVERY_OBJECTS.items()
            )
            if object_type == "trigger"
        }
        if protected_triggers != expected_triggers:
            raise CommitmentResolutionSchemaError(
                "commitment resolution recovery trigger set is invalid"
            )

        columns = tuple(
            tuple(row)
            for row in conn.execute(
                "PRAGMA table_info(commitment_resolution_operations)"
            ).fetchall()
        )
        if columns != _RESOLUTION_OPERATION_COLUMNS:
            raise CommitmentResolutionSchemaError(
                "commitment resolution recovery columns are invalid"
            )

        indexes: Dict[str, tuple[str, str]] = {}
        for row in conn.execute(
            "PRAGMA index_list(commitment_resolution_operations)"
        ).fetchall():
            if int(row["unique"]) != 1 or int(row["partial"]) != 0:
                raise CommitmentResolutionSchemaError(
                    "commitment resolution recovery indexes are invalid"
                )
            index_name = str(row["name"])
            quoted_index_name = index_name.replace('"', '""')
            index_columns = conn.execute(
                f'PRAGMA index_info("{quoted_index_name}")'
            ).fetchall()
            if len(index_columns) != 1:
                raise CommitmentResolutionSchemaError(
                    "commitment resolution recovery indexes are invalid"
                )
            indexes[index_name] = (
                str(row["origin"]), str(index_columns[0]["name"]),
            )
        if indexes != _RESOLUTION_OPERATION_INDEXES:
            raise CommitmentResolutionSchemaError(
                "commitment resolution recovery indexes are invalid"
            )
        if conn.execute(
            "PRAGMA foreign_key_list(commitment_resolution_operations)"
        ).fetchall():
            raise CommitmentResolutionSchemaError(
                "commitment resolution recovery constraints are invalid"
            )

    def resolution_recovery_readiness(self) -> Dict[str, Any]:
        """Revalidate and publish the generic durable-recovery contract."""

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                self._validate_resolution_recovery_schema(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return {
            "ready": True,
            "capability": RESOLUTION_RECOVERY_CAPABILITY,
            "schema": _RESOLUTION_RECOVERY_SCHEMA,
            "version": 1,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = None
        else:
            d["metadata"] = None
        return d

    @staticmethod
    def _operation_receipt(row: sqlite3.Row) -> Dict[str, Any]:
        receipt = {
            "schema": "ColonyCommitmentResolutionOperationV1",
            "version": 1,
            "operation_id": row["operation_id"],
            "commitment_id": row["commitment_id"],
            "outcome": row["outcome"],
            "note": row["note"],
            "note_digest": row["note_digest"],
            "resolved_by": row["resolved_by"],
            "status": row["status"],
            "resolved_at": row["resolved_at"],
            "record_digest": row["record_digest"],
        }
        expected = _operation_material(
            operation_id=receipt["operation_id"],
            commitment_id=receipt["commitment_id"],
            outcome=receipt["outcome"],
            note=receipt["note"],
            note_digest=receipt["note_digest"],
            resolved_by=receipt["resolved_by"],
            status=receipt["status"],
            resolved_at=receipt["resolved_at"],
        )
        if receipt != expected:
            raise ValueError(
                "commitment resolution operation integrity check failed"
            )
        return receipt

    def get_resolution_operation(
        self, commitment_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the immutable exact-operation proof for one commitment."""

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                self._validate_resolution_recovery_schema(conn)
                row = conn.execute(
                    """SELECT * FROM commitment_resolution_operations
                       WHERE commitment_id=?""",
                    (commitment_id,),
                ).fetchone()
                receipt = (
                    self._operation_receipt(row) if row is not None else None
                )
                conn.commit()
                return receipt
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        person_id: str,
        description: str,
        due_at: Optional[str] = None,
        priority: int = 50,
        source_type: str = "manual",
        source_context: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *, dedupe: bool = False,
    ) -> Dict[str, Any]:
        """Create, optionally reusing the same open obligation under one write lock."""
        commitment_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Validate due_at is in the future AND normalize it to canonical UTC ISO.
        # get_overdue() compares due_at as a STRING against a +00:00 `now`, so a
        # naive or non-UTC-offset stored value sorts wrong — overdue commitments
        # then surface late or never (a forgotten promise). Persist the
        # normalized value, not the caller's raw string.
        if due_at:
            try:
                due_dt = datetime.fromisoformat(due_at)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                due_dt = due_dt.astimezone(timezone.utc)
                if due_dt < datetime.now(timezone.utc):
                    raise ValueError("due_at must be in the future")
                due_at = due_dt.isoformat()
            except ValueError:
                raise

        meta_json = json.dumps(metadata) if metadata else None

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if dedupe:
                    existing = self._find_open_duplicate(conn, person_id, description)
                    if existing is not None:
                        conn.commit()
                        return {**existing, "deduped": True}
                conn.execute(
                    """INSERT INTO commitments
                       (id, person_id, description, made_at, due_at, status,
                        source_type, source_context, priority, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (commitment_id, person_id, description, now, due_at,
                     "pending", source_type, source_context, priority, meta_json),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                return self._row_to_dict(row)
            finally:
                conn.close()

    def get(self, commitment_id: str) -> Optional[Dict[str, Any]]:
        """Get a single commitment by ID. Returns None if not found."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    def list(
        self,
        person_id: Optional[str] = None,
        status: Optional[List[str]] = None,
        overdue_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List commitments with optional filters.

        Returns dict with commitments, total, limit, offset.
        """
        conditions: List[str] = []
        params: List[Any] = []

        if person_id:
            conditions.append("person_id = ?")
            params.append(person_id)

        if overdue_only:
            conditions.append("status = 'overdue'")
        elif status:
            placeholders = ",".join("?" for _ in status)
            conditions.append(f"status IN ({placeholders})")
            params.extend(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM commitments {where}", params
                ).fetchone()[0]

                rows = conn.execute(
                    f"SELECT * FROM commitments {where} ORDER BY made_at DESC LIMIT ? OFFSET ?",
                    params + [limit, offset],
                ).fetchall()

                return {
                    "commitments": [self._row_to_dict(r) for r in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            finally:
                conn.close()

    def update(
        self,
        commitment_id: str,
        status: Optional[str] = None,
        fulfilled_at: Optional[str] = None,
        description: Optional[str] = None,
        due_at: Optional[str] = None,
        priority: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a commitment. Returns updated record or None if not found.

        Validates status transitions:
          pending → fulfilled, overdue, cancelled
          overdue → fulfilled, cancelled
          fulfilled, cancelled → no transitions allowed (terminal)
        """
        VALID_TRANSITIONS = {
            "pending": {"fulfilled", "overdue", "cancelled"},
            "overdue": {"fulfilled", "cancelled"},
        }

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._validate_resolution_recovery_schema(conn)
                current = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                if not current:
                    conn.commit()
                    return None

                current_status = current["status"]

                if status and status != current_status:
                    allowed = VALID_TRANSITIONS.get(current_status, set())
                    if status not in allowed:
                        raise ValueError(
                            f"Cannot transition from '{current_status}' to '{status}'"
                        )

                    # Auto-fill fulfilled_at when transitioning to fulfilled
                    if status == "fulfilled" and not fulfilled_at:
                        fulfilled_at = datetime.now(timezone.utc).isoformat()

                # Build UPDATE statement
                updates: List[str] = []
                params: List[Any] = []

                if status is not None:
                    updates.append("status = ?")
                    params.append(status)
                if fulfilled_at is not None:
                    updates.append("fulfilled_at = ?")
                    params.append(fulfilled_at)
                if description is not None:
                    updates.append("description = ?")
                    params.append(description)
                if due_at is not None:
                    updates.append("due_at = ?")
                    params.append(due_at)
                if priority is not None:
                    updates.append("priority = ?")
                    params.append(priority)
                if metadata is not None:
                    updates.append("metadata = ?")
                    params.append(json.dumps(metadata))

                if not updates:
                    conn.commit()
                    return self._row_to_dict(current)

                params.append(commitment_id)
                conn.execute(
                    f"UPDATE commitments SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                conn.commit()

                row = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                return self._row_to_dict(row)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def delete(self, commitment_id: str) -> bool:
        """Delete a commitment. Only allowed for terminal states.

        Returns True if deleted, False if not found or not terminal.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._validate_resolution_recovery_schema(conn)
                current = conn.execute(
                    "SELECT status FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                if not current:
                    conn.commit()
                    return False
                if current["status"] not in ("fulfilled", "cancelled"):
                    conn.commit()
                    return False
                operation = conn.execute(
                    """SELECT 1 FROM commitment_resolution_operations
                       WHERE commitment_id=?""",
                    (commitment_id,),
                ).fetchone()
                if operation is not None:
                    conn.commit()
                    return False
                conn.execute(
                    "DELETE FROM commitments WHERE id = ?", (commitment_id,)
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_overdue(self) -> List[Dict[str, Any]]:
        """Get open commitments (pending OR already flipped to overdue) whose
        due_at has passed. Including 'overdue' matters: the condition worker
        flips pending→overdue, and a pending-only query would make flipped
        items invisible to everything that surfaces owed work."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM commitments
                       WHERE status IN ('pending', 'overdue')
                         AND due_at IS NOT NULL AND due_at < ?
                       ORDER BY due_at ASC""",
                    (now,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def get_pending_for_person(self, person_id: str) -> List[Dict[str, Any]]:
        """Get OPEN commitments (pending + overdue) for a specific person.
        Callers use this as "what is still owed" — an item that went overdue
        is owed more, not less, so it must stay in this list (it is also the
        dedup list the introspection extractor sees)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM commitments
                       WHERE person_id = ? AND status IN ('pending', 'overdue')
                       ORDER BY priority DESC, due_at ASC""",
                    (person_id,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def _find_open_duplicate(self, conn, person_id, description):
        norm = _normalize_desc(description)
        if not norm:
            return None
        rows = conn.execute(
            "SELECT * FROM commitments WHERE person_id=? AND status IN (?,?) ORDER BY made_at DESC",
            (person_id, *OPEN_STATUSES))
        for row in rows:
            if _similar_desc(norm, _normalize_desc(row["description"] or "")):
                return self._row_to_dict(row)
        return None

    def find_open_duplicate(
        self, person_id: str, description: str
    ) -> Optional[Dict[str, Any]]:
        """Inspect the existing normalized/containment/token-overlap predicate.

        Writers needing deduplication must use create(dedupe=True), which checks
        and inserts in the same SQLite transaction across independent processes.
        """
        with self._lock:
            conn = self._connect()
            try:
                return self._find_open_duplicate(conn, person_id, description)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Resolution: settle an item with a reason the system can learn from
    # ------------------------------------------------------------------

    def resolve(
        self,
        commitment_id: str,
        outcome: str = "done",
        note: Optional[str] = None,
        resolved_by: str = "owner",
        operation_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Settle a commitment with an outcome (done | invalid | duplicate |
        wont_do | obsolete). Legacy calls remain state-idempotent.  A caller
        supplying ``operation_id`` gets exact idempotency: an existing terminal
        row is accepted only when its stored operation, outcome, note, resolver,
        and resulting status all match. Emits the matching commitment.* event
        on an actual transition."""
        status = OUTCOME_TO_STATUS.get(outcome)
        if status is None:
            raise ValueError(
                f"unknown outcome '{outcome}' (expected one of "
                f"{sorted(OUTCOME_TO_STATUS)})")
        if operation_id is not None and (
            type(operation_id) is not str
            or not operation_id
            or operation_id != operation_id.strip()
            or len(operation_id) > 192
        ):
            raise ValueError("commitment resolution operation_id is invalid")
        full_note = note or ""
        note_text = full_note[:300]
        resolver = str(resolved_by)
        expected_resolution = {
            "outcome": outcome,
            "note": note_text,
            "note_digest": hashlib.sha256(full_note.encode("utf-8")).hexdigest(),
            "by": resolver,
        }
        if operation_id is not None:
            expected_resolution["operation_id"] = operation_id

        transitioned = False
        row: Optional[Dict[str, Any]] = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._validate_resolution_recovery_schema(conn)
                stored = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                stored_operation = conn.execute(
                    """SELECT * FROM commitment_resolution_operations
                       WHERE commitment_id=?""",
                    (commitment_id,),
                ).fetchone()
                operation = (
                    self._operation_receipt(stored_operation)
                    if stored_operation is not None else None
                )
                if stored is None:
                    if operation is not None:
                        raise CommitmentResolutionConflict(
                            "operation-bound commitment row is missing"
                        )
                    conn.commit()
                    return None
                current = self._row_to_dict(stored)
                if current["status"] in ("fulfilled", "cancelled"):
                    if operation_id is not None:
                        expected_operation = {
                            "operation_id": operation_id,
                            "commitment_id": commitment_id,
                            "outcome": outcome,
                            "note": note_text,
                            "note_digest": expected_resolution["note_digest"],
                            "resolved_by": resolver,
                            "status": status,
                        }
                        if (
                            operation is None
                            or any(
                                operation.get(name) != value
                                for name, value in expected_operation.items()
                            )
                            or current["status"] != status
                        ):
                            raise CommitmentResolutionConflict(
                                "terminal commitment has a different operation"
                            )
                    conn.commit()
                    return current
                if current["status"] not in OPEN_STATUSES:
                    raise CommitmentResolutionConflict(
                        "commitment is not open or terminal"
                    )
                if operation is not None:
                    raise CommitmentResolutionConflict(
                        "open commitment already has a resolution operation"
                    )

                meta = dict(current.get("metadata") or {})
                resolved_at = datetime.now(timezone.utc).isoformat()
                meta["resolution"] = {
                    **expected_resolution,
                    "at": resolved_at,
                }
                if operation_id is not None:
                    operation = _operation_material(
                        operation_id=operation_id,
                        commitment_id=commitment_id,
                        outcome=outcome,
                        note=note_text,
                        note_digest=expected_resolution["note_digest"],
                        resolved_by=resolver,
                        status=status,
                        resolved_at=resolved_at,
                    )
                    conn.execute(
                        """INSERT INTO commitment_resolution_operations
                           (operation_id,commitment_id,outcome,note,note_digest,
                            resolved_by,status,resolved_at,record_digest)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            operation["operation_id"],
                            operation["commitment_id"], operation["outcome"],
                            operation["note"], operation["note_digest"],
                            operation["resolved_by"], operation["status"],
                            operation["resolved_at"], operation["record_digest"],
                        ),
                    )
                fulfilled_at = (
                    resolved_at
                    if status == "fulfilled" else current.get("fulfilled_at")
                )
                changed = conn.execute(
                    """UPDATE commitments
                       SET status=?,fulfilled_at=?,metadata=?
                       WHERE id=? AND status=?""",
                    (
                        status, fulfilled_at, json.dumps(meta),
                        commitment_id, current["status"],
                    ),
                ).rowcount
                if changed != 1:
                    raise CommitmentResolutionConflict(
                        "commitment changed before resolution was recorded"
                    )
                conn.commit()
                stored = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                row = self._row_to_dict(stored)
                transitioned = True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        if transitioned and row is not None:
            try:
                from colony_sidecar.events.broadcaster import emit
                emit(f"commitment.{status}", {
                    "commitment_id": row["id"],
                    "person_id": row["person_id"],
                    "outcome": outcome,
                    "resolved_by": resolver,
                })
            except Exception:
                pass
        return row

    def resolution_stats(self, days: int = 30) -> Dict[str, Any]:
        """Per-source_type counts of how items created in the window ended up:
        fulfilled, cancelled (with outcome breakdown), still open. This is the
        calibration signal for whatever generates items — a source whose items
        keep getting cancelled as invalid should get more conservative."""
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM commitments").fetchall()
            finally:
                conn.close()
        by_source: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            d = self._row_to_dict(r)
            try:
                made = datetime.fromisoformat(d["made_at"]).timestamp()
            except (ValueError, TypeError):
                continue
            if made < cutoff:
                continue
            src = d.get("source_type") or "manual"
            s = by_source.setdefault(src, {
                "created": 0, "fulfilled": 0, "cancelled": 0,
                "open": 0, "outcomes": {},
            })
            s["created"] += 1
            status = d.get("status")
            if status in OPEN_STATUSES:
                s["open"] += 1
            elif status in ("fulfilled", "cancelled"):
                s[status] += 1
                res = (d.get("metadata") or {}).get("resolution") or {}
                oc = res.get("outcome") or (
                    "done" if status == "fulfilled" else "unspecified")
                s["outcomes"][oc] = s["outcomes"].get(oc, 0) + 1
        return {"days": days, "by_source": by_source}

    def recent_rejections(
        self, limit: int = 6,
        source_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Most recently cancelled items judged invalid or duplicate — the
        negative examples the extraction side injects into its prompt so the
        same bad item is not recorded again."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM commitments WHERE status = 'cancelled'
                       ORDER BY made_at DESC LIMIT 200""",
                ).fetchall()
            finally:
                conn.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = self._row_to_dict(r)
            if source_types and (d.get("source_type") or "manual") not in source_types:
                continue
            res = (d.get("metadata") or {}).get("resolution") or {}
            if res.get("outcome") not in ("invalid", "duplicate"):
                continue
            out.append({
                "description": d.get("description") or "",
                "outcome": res.get("outcome"),
                "note": res.get("note") or "",
                "at": res.get("at") or "",
            })
            if len(out) >= limit:
                break
        return out

"""SQLite-backed commitment store."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
            finally:
                conn.close()

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
    ) -> Dict[str, Any]:
        """Create a new commitment. Returns the full record."""
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
                current = conn.execute(
                    "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                if not current:
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
            finally:
                conn.close()

    def delete(self, commitment_id: str) -> bool:
        """Delete a commitment. Only allowed for terminal states.

        Returns True if deleted, False if not found or not terminal.
        """
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM commitments WHERE id = ?", (commitment_id,)
                ).fetchone()
                if not current:
                    return False
                if current["status"] not in ("fulfilled", "cancelled"):
                    return False
                conn.execute(
                    "DELETE FROM commitments WHERE id = ?", (commitment_id,)
                )
                conn.commit()
                return True
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_overdue(self) -> List[Dict[str, Any]]:
        """Get commitments that are past their due_at and still pending."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM commitments
                       WHERE status = 'pending' AND due_at IS NOT NULL AND due_at < ?
                       ORDER BY due_at ASC""",
                    (now,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def get_pending_for_person(self, person_id: str) -> List[Dict[str, Any]]:
        """Get pending commitments for a specific person."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM commitments
                       WHERE person_id = ? AND status = 'pending'
                       ORDER BY priority DESC, due_at ASC""",
                    (person_id,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

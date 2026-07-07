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

    def find_open_duplicate(
        self, person_id: str, description: str
    ) -> Optional[Dict[str, Any]]:
        """Return an open commitment for this person that already says the
        same thing (normalized/containment/token-overlap match), or None."""
        norm = _normalize_desc(description)
        if not norm:
            return None
        for c in self.get_pending_for_person(person_id):
            if _similar_desc(norm, _normalize_desc(c.get("description") or "")):
                return c
        return None

    # ------------------------------------------------------------------
    # Resolution: settle an item with a reason the system can learn from
    # ------------------------------------------------------------------

    def resolve(
        self,
        commitment_id: str,
        outcome: str = "done",
        note: Optional[str] = None,
        resolved_by: str = "owner",
    ) -> Optional[Dict[str, Any]]:
        """Settle a commitment with an outcome (done | invalid | duplicate |
        wont_do | obsolete). Idempotent: an already-terminal commitment is
        returned unchanged instead of raising, so a double-click or a cascade
        arriving after a direct resolve never errors. Emits the matching
        commitment.* event on an actual transition."""
        status = OUTCOME_TO_STATUS.get(outcome)
        if status is None:
            raise ValueError(
                f"unknown outcome '{outcome}' (expected one of "
                f"{sorted(OUTCOME_TO_STATUS)})")
        current = self.get(commitment_id)
        if current is None:
            return None
        if current["status"] in ("fulfilled", "cancelled"):
            return current
        meta = dict(current.get("metadata") or {})
        meta["resolution"] = {
            "outcome": outcome,
            "note": (note or "")[:300],
            "by": resolved_by,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        row = self.update(commitment_id, status=status, metadata=meta)
        if row is not None:
            try:
                from colony_sidecar.events.broadcaster import emit
                emit(f"commitment.{status}", {
                    "commitment_id": row["id"],
                    "person_id": row["person_id"],
                    "outcome": outcome,
                    "resolved_by": resolved_by,
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

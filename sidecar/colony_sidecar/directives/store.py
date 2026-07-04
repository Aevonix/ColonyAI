"""DirectiveStore -- durable persistence for owner directives / boundaries.

SQLite-backed so standing boundaries survive restarts (a crash must never
forget that the owner said "leave X alone"). Mirrors the persistence pattern
of the delivery rate limiter and preference learner.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from colony_sidecar.directives.models import Directive, DirectiveStatus, Polarity

logger = logging.getLogger(__name__)


class DirectiveStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = str(db_path) if db_path else ":memory:"
        self._lock = threading.RLock()
        # A single shared connection (check_same_thread=False) guarded by a lock;
        # directive writes are rare, reads are small.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directives (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    polarity TEXT NOT NULL,
                    raw_text TEXT,
                    match_terms TEXT,
                    entity_ids TEXT,
                    action_kinds TEXT,
                    source TEXT,
                    confidence REAL DEFAULT 0.9,
                    status TEXT DEFAULT 'active',
                    created_at REAL,
                    updated_at REAL,
                    expires_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dir_status ON directives(status)"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def add(self, directive: Directive) -> Directive:
        """Insert or update a directive (idempotent on id)."""
        row = directive.to_row()
        with self._lock:
            cols = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
            self._conn.execute(
                f"INSERT INTO directives ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                list(row.values()),
            )
            self._conn.commit()
        logger.info(
            "Directive stored: [%s] %r (id=%s, source=%s)",
            directive.polarity.value, directive.subject, directive.id, directive.source,
        )
        return directive

    def get(self, directive_id: str) -> Optional[Directive]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM directives WHERE id=?", (directive_id,)
            )
            r = cur.fetchone()
        return Directive.from_row(dict(r)) if r else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        polarity: Optional[str] = None,
    ) -> List[Directive]:
        q = "SELECT * FROM directives"
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if polarity:
            clauses.append("polarity=?"); params.append(polarity)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Directive.from_row(dict(r)) for r in rows]

    def active(self, polarity: Optional[Polarity] = None) -> List[Directive]:
        """All currently-active directives (status active + not expired)."""
        now = time.time()
        pol = polarity.value if isinstance(polarity, Polarity) else polarity
        out = [d for d in self.list(status=DirectiveStatus.ACTIVE.value, polarity=pol)
               if d.is_active(now)]
        return out

    def set_status(self, directive_id: str, status: DirectiveStatus) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE directives SET status=?, updated_at=? WHERE id=?",
                (status.value, time.time(), directive_id),
            )
            self._conn.commit()
            changed = cur.rowcount > 0
        if changed:
            logger.info("Directive %s -> %s", directive_id, status.value)
        return changed

    def revoke(self, directive_id: str) -> bool:
        return self.set_status(directive_id, DirectiveStatus.REVOKED)

    def count_active(self) -> int:
        return len(self.active())

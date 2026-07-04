"""Unified action journal (Amendment 1.4).

Every autonomous action is logged with its reasoning, confidence,
reversibility class, gate decision, and outcome. The journal is the
accountability layer that makes action-with-journaling safe: the owner can
always ask "what did you do today and why" and get the real record.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

DECISIONS = ("acted", "asked", "held", "blocked", "noted")
REVERSIBILITY = ("reversible", "recoverable", "irreversible")


class ActionJournal:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS action_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    domain TEXT NOT NULL,
                    description TEXT,
                    reasoning TEXT,
                    confidence REAL,
                    reversibility TEXT,
                    decision TEXT,
                    outcome TEXT,
                    ref TEXT
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_ts ON action_journal(ts)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_domain "
                "ON action_journal(domain)")
            self._conn.commit()

    def record(self, domain: str, description: str, *,
               reasoning: str = "", confidence: Optional[float] = None,
               reversibility: str = "reversible", decision: str = "acted",
               outcome: str = "", ref: str = "") -> int:
        """Append one journal entry; returns its row id. Never raises."""
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT INTO action_journal
                       (ts, domain, description, reasoning, confidence,
                        reversibility, decision, outcome, ref)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), (domain or "unknown").lower(),
                     (description or "")[:500], (reasoning or "")[:800],
                     confidence,
                     reversibility if reversibility in REVERSIBILITY else "reversible",
                     decision if decision in DECISIONS else "acted",
                     (outcome or "")[:500], (ref or "")[:120]))
                self._conn.commit()
                return int(cur.lastrowid)
        except Exception:
            return -1

    def set_outcome(self, entry_id: int, outcome: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE action_journal SET outcome=? WHERE id=?",
                    ((outcome or "")[:500], entry_id))
                self._conn.commit()
        except Exception:
            pass

    def recent(self, limit: int = 50, domain: Optional[str] = None,
               since: Optional[float] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM action_journal WHERE 1=1"
        params: List[Any] = []
        if domain:
            q += " AND domain=?"; params.append(domain.lower())
        if since is not None:
            q += " AND ts >= ?"; params.append(since)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(max(1, min(500, limit)))
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def today(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """Entries from the last 24h (the "what did you do today" view)."""
        return self.recent(limit=200, domain=domain,
                           since=time.time() - 86400)

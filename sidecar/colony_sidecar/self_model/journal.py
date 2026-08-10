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
                    ref TEXT,
                    event_key TEXT,
                    prompt_version TEXT
                )""")
            # Additive migrations preserve existing accountability history.
            try:
                cols = {r[1] for r in self._conn.execute(
                    "PRAGMA table_info(action_journal)").fetchall()}
                if "prompt_version" not in cols:
                    self._conn.execute(
                        "ALTER TABLE action_journal ADD COLUMN prompt_version TEXT")
                if "event_key" not in cols:
                    self._conn.execute(
                        "ALTER TABLE action_journal ADD COLUMN event_key TEXT")
            except Exception:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_ts ON action_journal(ts)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_domain "
                "ON action_journal(domain)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_event_key "
                "ON action_journal(event_key) WHERE event_key IS NOT NULL")
            self._conn.commit()

    def record(self, domain: str, description: str, *,
               reasoning: str = "", confidence: Optional[float] = None,
               reversibility: str = "reversible", decision: str = "acted",
               outcome: str = "", ref: str = "",
               event_key: Optional[str] = None) -> int:
        """Append one journal entry; returns its row id. Never raises.

        Each entry carries the active charter PROMPT_VERSION so behavior
        shifts are attributable to prompt changes.
        """
        try:
            try:
                from colony_sidecar.cognition.charter import PROMPT_VERSION
            except Exception:
                PROMPT_VERSION = ""
            event_key = str(event_key or "").strip()[:512] or None
            with self._lock:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO action_journal
                       (ts, domain, description, reasoning, confidence,
                        reversibility, decision, outcome, ref, event_key,
                        prompt_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), (domain or "unknown").lower(),
                     (description or "")[:500], (reasoning or "")[:800],
                     confidence,
                     reversibility if reversibility in REVERSIBILITY else "reversible",
                     decision if decision in DECISIONS else "acted",
                     (outcome or "")[:500], (ref or "")[:120],
                     event_key,
                     PROMPT_VERSION))
                self._conn.commit()
                if cur.rowcount == 0 and event_key is not None:
                    existing = self._conn.execute(
                        "SELECT id FROM action_journal WHERE event_key=?",
                        (event_key,),
                    ).fetchone()
                    return int(existing["id"]) if existing else -1
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

    def has_event_key(self, event_key: str) -> bool:
        """Whether an exact, durable accountability event already exists."""

        key = str(event_key or "").strip()[:512]
        if not key:
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM action_journal WHERE event_key=? LIMIT 1",
                    (key,),
                ).fetchone()
            return row is not None
        except Exception:
            return False

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

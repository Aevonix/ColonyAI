"""GuardAuditStore — durable record of response-gate cross-context events.

Every time the gate sees a cross-context disclosure it records one row, tagged with whether
the disclosure was ``authorized`` (owner-directed) or not. This is what lets an operator
measure, while the gate runs in shadow, whether the classifier actually separates legitimate
owner-directed transfers from accidental leaks BEFORE enforcement is turned on.

Generic: ``conversation_key`` is an opaque host-supplied id; the store attaches no meaning.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class GuardAuditStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guard_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                conversation_key TEXT,
                mode             TEXT,
                decision         TEXT,
                authorized       INTEGER NOT NULL DEFAULT 0,
                checks           TEXT,
                entities         TEXT,
                response_excerpt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_guard_events_ts ON guard_events(ts);
            CREATE INDEX IF NOT EXISTS idx_guard_events_auth ON guard_events(authorized);
            """
        )
        self._conn.commit()

    def record(self, *, conversation_key: Optional[str], mode: str, decision: str,
               authorized: bool, checks: Sequence[str], entities: Sequence[str],
               response_text: str = "") -> None:
        self._conn.execute(
            "INSERT INTO guard_events (ts, conversation_key, mode, decision, authorized, "
            "checks, entities, response_excerpt) VALUES (?,?,?,?,?,?,?,?)",
            (_now(), conversation_key, mode, decision, 1 if authorized else 0,
             ",".join(checks), ",".join(entities), (response_text or "")[:240]),
        )
        self._conn.commit()

    def recent(self, *, limit: int = 50, authorized: Optional[bool] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM guard_events"
        params: list = []
        if authorized is not None:
            sql += " WHERE authorized = ?"
            params.append(1 if authorized else 0)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def summary(self) -> Dict[str, Any]:
        """Counts to gauge classifier behavior: cross-context events split by authorized."""
        rows = self._conn.execute(
            "SELECT authorized, COUNT(*) n FROM guard_events GROUP BY authorized"
        ).fetchall()
        by_auth = {("authorized" if r["authorized"] else "unauthorized"): r["n"] for r in rows}
        total = sum(by_auth.values())
        return {"total": total,
                "authorized_transfers": by_auth.get("authorized", 0),
                "unauthorized_flags": by_auth.get("unauthorized", 0)}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

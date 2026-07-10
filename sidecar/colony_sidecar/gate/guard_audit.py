"""GuardAuditStore — durable record of response-guard evaluations.

Two kinds of rows:

* ``guard_events`` — one row per evaluation that produced ANY finding or a
  non-allow decision (not just cross_context). ``would_block`` marks whether
  the findings would have suppressed the reply under enforce, regardless of
  the mode actually running — this is what a false-positive budget is
  measured against while the guard is still in shadow.
* ``guard_eval_days`` — a per-UTC-day counter of TOTAL evaluations (clean or
  not), the denominator that turns finding counts into rates.

``summary()`` reports the historical authorized/unauthorized split plus, for
24h/7d/14d windows: evaluations, flagged events, per-check counts and the
``would_block_rate`` (would-block events / evaluations). Evaluation counts
are day-granular, so the "24h" window is really "today + yesterday" (UTC) —
close enough for budget tracking, cheap enough to keep forever.

Generic: ``conversation_key`` is an opaque host-supplied id; the store
attaches no meaning.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


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
            CREATE TABLE IF NOT EXISTS guard_eval_days (
                day         TEXT PRIMARY KEY,
                evaluations INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Migration: pre-existing DBs lack the would_block column.
        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(guard_events)").fetchall()}
        if "would_block" not in cols:
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN would_block INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()

    def count_evaluation(self) -> None:
        """One evaluation happened (finding or not) — bump today's counter."""
        self._conn.execute(
            "INSERT INTO guard_eval_days (day, evaluations) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET evaluations = evaluations + 1",
            (_today(),),
        )
        self._conn.commit()

    def record(self, *, conversation_key: Optional[str], mode: str, decision: str,
               authorized: bool, checks: Sequence[str], entities: Sequence[str],
               response_text: str = "", would_block: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO guard_events (ts, conversation_key, mode, decision, authorized, "
            "checks, entities, response_excerpt, would_block) VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), conversation_key, mode, decision, 1 if authorized else 0,
             ",".join(checks), ",".join(entities), (response_text or "")[:240],
             1 if would_block else 0),
        )
        self._conn.commit()

    def recent(self, *, limit: int = 50, authorized: Optional[bool] = None,
               check: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM guard_events"
        where: list = []
        params: list = []
        if authorized is not None:
            where.append("authorized = ?")
            params.append(1 if authorized else 0)
        if check:
            # checks is a comma-joined list; match the whole token.
            where.append("(',' || checks || ',') LIKE ?")
            params.append(f"%,{check},%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _window(self, days: int) -> Dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        ts_cutoff = (now - timedelta(days=days)).isoformat()
        day_cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        evals = self._conn.execute(
            "SELECT COALESCE(SUM(evaluations), 0) n FROM guard_eval_days WHERE day >= ?",
            (day_cutoff,)).fetchone()["n"]
        rows = self._conn.execute(
            "SELECT checks, would_block FROM guard_events WHERE ts >= ?",
            (ts_cutoff,)).fetchall()
        by_check: Dict[str, int] = {}
        would_block = 0
        for r in rows:
            if r["would_block"]:
                would_block += 1
            for c in (r["checks"] or "").split(","):
                c = c.strip()
                if c:
                    by_check[c] = by_check.get(c, 0) + 1
        return {
            "evaluations": int(evals),
            "flagged_events": len(rows),
            "would_block": would_block,
            "would_block_rate": round(would_block / evals, 4) if evals else None,
            "by_check": by_check,
        }

    def summary(self) -> Dict[str, Any]:
        """All-time authorized/unauthorized split + windowed rates for the
        false-positive budget (see module docstring for granularity)."""
        rows = self._conn.execute(
            "SELECT authorized, COUNT(*) n FROM guard_events GROUP BY authorized"
        ).fetchall()
        by_auth = {("authorized" if r["authorized"] else "unauthorized"): r["n"] for r in rows}
        total = sum(by_auth.values())
        return {"total": total,
                "authorized_transfers": by_auth.get("authorized", 0),
                "unauthorized_flags": by_auth.get("unauthorized", 0),
                "windows": {"24h": self._window(1),
                            "7d": self._window(7),
                            "14d": self._window(14)}}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

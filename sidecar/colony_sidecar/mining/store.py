"""MiningStore: SQLite persistence for verbatim turns + escalation records.

The rest of the sidecar keeps salience-gated summaries (memory graph, comms
ledger, rolling journal); none of those is a durable verbatim corpus. This
store is: every accepted turn is banked verbatim (capped per side), queryable
by channel / contact / date range, feeding the escalation miner and the
training-corpus exporter. Data never leaves COLONY_STATE_DIR.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, List, Optional

from colony_sidecar.mining.models import EscalationRecord, MinedTurn


class MiningStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path) if db_path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS mined_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    contact_id TEXT,
                    channel_id TEXT,
                    user_text TEXT,
                    assistant_text TEXT,
                    summary TEXT,
                    tools_used TEXT,
                    model TEXT,
                    ts REAL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS escalations (
                    id TEXT PRIMARY KEY,
                    kind TEXT,
                    session_id TEXT,
                    contact_id TEXT,
                    channel_id TEXT,
                    task_context TEXT,
                    local_attempt TEXT,
                    escalated_answer TEXT,
                    model TEXT,
                    matched TEXT,
                    outcome TEXT,
                    outcome_note TEXT,
                    distilled INTEGER DEFAULT 0,
                    ts REAL
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_session ON mined_turns(session_id, ts)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_ts ON mined_turns(ts)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_esc_session ON escalations(session_id, ts)"
            )
            self._conn.commit()

    # -- turns ---------------------------------------------------------------

    def add_turn(self, t: MinedTurn) -> MinedTurn:
        row = t.to_row()
        with self._lock:
            cols = ", ".join(row)
            ph = ", ".join(["?"] * len(row))
            self._conn.execute(
                f"INSERT OR REPLACE INTO mined_turns ({cols}) VALUES ({ph})",
                list(row.values()),
            )
            self._conn.commit()
        return t

    def last_turn_in_session(self, session_id: str) -> Optional[MinedTurn]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM mined_turns WHERE session_id=? ORDER BY ts DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return MinedTurn.from_row(dict(r)) if r else None

    def list_turns(
        self,
        *,
        contact_id: Optional[str] = None,
        channels: Optional[List[str]] = None,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        limit: int = 5000,
    ) -> List[MinedTurn]:
        q = "SELECT * FROM mined_turns WHERE 1=1"
        params: List[Any] = []
        if contact_id and contact_id != "*":
            q += " AND contact_id=?"
            params.append(contact_id)
        if channels:
            q += f" AND channel_id IN ({','.join(['?'] * len(channels))})"
            params.extend(channels)
        if since_ts is not None:
            q += " AND ts >= ?"
            params.append(since_ts)
        if until_ts is not None:
            q += " AND ts <= ?"
            params.append(until_ts)
        q += " ORDER BY session_id, ts ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [MinedTurn.from_row(dict(r)) for r in rows]

    def turn_count(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) AS n FROM mined_turns").fetchone()
        return int(r["n"])

    # -- escalations -----------------------------------------------------------

    def add_escalation(self, e: EscalationRecord) -> EscalationRecord:
        row = e.to_row()
        with self._lock:
            cols = ", ".join(row)
            ph = ", ".join(["?"] * len(row))
            self._conn.execute(
                f"INSERT OR REPLACE INTO escalations ({cols}) VALUES ({ph})",
                list(row.values()),
            )
            self._conn.commit()
        return e

    def update_escalation(self, e: EscalationRecord) -> EscalationRecord:
        return self.add_escalation(e)

    def latest_open_escalation(self, session_id: str) -> Optional[EscalationRecord]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM escalations WHERE session_id=? AND outcome='unknown' "
                "ORDER BY ts DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return EscalationRecord.from_row(dict(r)) if r else None

    def list_escalations(
        self, *, kind: Optional[str] = None, limit: int = 50
    ) -> List[EscalationRecord]:
        q = "SELECT * FROM escalations"
        params: List[Any] = []
        if kind:
            q += " WHERE kind=?"
            params.append(kind)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [EscalationRecord.from_row(dict(r)) for r in rows]

    def escalation_stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, outcome, COUNT(*) AS n, SUM(distilled) AS d "
                "FROM escalations GROUP BY kind, outcome"
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM escalations"
            ).fetchone()
        by_bucket = [
            {"kind": r["kind"], "outcome": r["outcome"], "count": int(r["n"]),
             "distilled": int(r["d"] or 0)}
            for r in rows
        ]
        return {"total": int(total["n"]), "buckets": by_bucket,
                "turns_banked": self.turn_count()}

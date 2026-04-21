"""Colony Briefing System — SQLite persistence store.

Persists Briefing objects, schedule state, and section engagement records.
Thread-safe via connection-per-thread (check_same_thread=False + threading.Lock).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import (
    Briefing,
    BriefingPriority,
    BriefingSection,
    BriefingStatus,
    BriefingType,
    ScheduleEntry,
    SectionEngagementRecord,
)

logger = logging.getLogger(__name__)

_briefing_store_instance: Optional["BriefingStore"] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefings (
    briefing_id     TEXT PRIMARY KEY,
    briefing_type   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    sections_json   TEXT NOT NULL DEFAULT '[]',
    priority        TEXT NOT NULL DEFAULT 'normal',
    triggered_by    TEXT,
    gateway         TEXT,
    created_at      TEXT NOT NULL,
    delivered_at    TEXT,
    read_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_briefings_status  ON briefings(status);
CREATE INDEX IF NOT EXISTS idx_briefings_type    ON briefings(briefing_type);
CREATE INDEX IF NOT EXISTS idx_briefings_created ON briefings(created_at DESC);

CREATE TABLE IF NOT EXISTS briefing_schedule (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    type     TEXT NOT NULL UNIQUE,
    last_run TEXT,
    next_run TEXT
);

INSERT OR IGNORE INTO briefing_schedule (type) VALUES ('daily');
INSERT OR IGNORE INTO briefing_schedule (type) VALUES ('weekly');

CREATE TABLE IF NOT EXISTS section_engagement (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    section_name TEXT NOT NULL,
    briefing_id  TEXT NOT NULL,
    signal       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    context      TEXT,
    FOREIGN KEY (briefing_id) REFERENCES briefings(briefing_id)
);

CREATE INDEX IF NOT EXISTS idx_engagement_section ON section_engagement(section_name);
CREATE INDEX IF NOT EXISTS idx_engagement_briefing ON section_engagement(briefing_id);
CREATE INDEX IF NOT EXISTS idx_engagement_time     ON section_engagement(recorded_at DESC);

CREATE TABLE IF NOT EXISTS suppressed_sections (
    section_name  TEXT PRIMARY KEY,
    suppressed_at TEXT NOT NULL,
    reason        TEXT
);
"""


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _briefing_to_row(b: Briefing) -> dict:
    sections = [
        {
            "section_id": s.section_id,
            "name": s.name,
            "content": s.content,
            "narrative": s.narrative,
            "priority": s.priority,
            "suppressed": s.suppressed,
            "engagement": s.engagement,
        }
        for s in b.sections
    ]
    return {
        "briefing_id": b.briefing_id,
        "briefing_type": b.briefing_type.value,
        "status": b.status.value,
        "sections_json": json.dumps(sections),
        "priority": b.priority.value,
        "triggered_by": b.triggered_by,
        "gateway": b.gateway,
        "created_at": _dt_to_str(b.created_at),
        "delivered_at": _dt_to_str(b.delivered_at),
        "read_at": _dt_to_str(b.read_at),
    }


def _row_to_briefing(row: sqlite3.Row) -> Briefing:
    raw_sections = json.loads(row["sections_json"] or "[]")
    sections = [
        BriefingSection(
            section_id=s.get("section_id", ""),
            name=s.get("name", ""),
            content=s.get("content", {}),
            narrative=s.get("narrative", ""),
            priority=s.get("priority", 50),
            suppressed=s.get("suppressed", False),
            engagement=s.get("engagement"),
        )
        for s in raw_sections
    ]
    return Briefing(
        briefing_id=row["briefing_id"],
        briefing_type=BriefingType(row["briefing_type"]),
        status=BriefingStatus(row["status"]),
        sections=sections,
        priority=BriefingPriority(row["priority"]),
        triggered_by=row["triggered_by"],
        gateway=row["gateway"],
        created_at=_str_to_dt(row["created_at"]) or datetime.now(timezone.utc),
        delivered_at=_str_to_dt(row["delivered_at"]),
        read_at=_str_to_dt(row["read_at"]),
    )


class BriefingStore:
    """Thread-safe SQLite-backed store for briefings and schedule state."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @classmethod
    def get_instance(cls) -> "BriefingStore":
        """Return the process-wide singleton backed by ~/.colony/briefings.db."""
        global _briefing_store_instance
        if _briefing_store_instance is None:
            colony_home = Path(os.environ.get("COLONY_HOME", str(Path.home() / ".colony")))
            colony_home.mkdir(parents=True, exist_ok=True)
            _briefing_store_instance = cls(str(colony_home / "briefings.db"))
        return _briefing_store_instance

    def list(self, limit: int = 50, cursor: Optional[str] = None) -> dict:
        """Return a paginated dict of briefings for the API router."""
        offset = 0
        if cursor:
            try:
                offset = int(cursor)
            except ValueError:
                pass
        briefings = self.list_recent(limit=limit + 1)
        has_more = len(briefings) > limit
        if has_more:
            briefings = briefings[:limit]
        items = [
            {
                "briefing_id": b.briefing_id,
                "briefing_type": b.briefing_type.value,
                "status": b.status.value,
                "priority": b.priority.value,
                "triggered_by": b.triggered_by,
                "gateway": b.gateway,
                "created_at": b.created_at.isoformat(),
                "delivered_at": b.delivered_at.isoformat() if b.delivered_at else None,
                "section_count": len(b.sections),
            }
            for b in briefings
        ]
        return {
            "data": items,
            "meta": {
                "total": len(items),
                "page_size": limit,
                "has_more": has_more,
                "cursor": str(offset + limit) if has_more else None,
            },
        }

    # ------------------------------------------------------------------
    # Briefing CRUD
    # ------------------------------------------------------------------

    def save(self, briefing: Briefing) -> Briefing:
        row = _briefing_to_row(briefing)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO briefings
                    (briefing_id, briefing_type, status, sections_json, priority,
                     triggered_by, gateway, created_at, delivered_at, read_at)
                VALUES
                    (:briefing_id, :briefing_type, :status, :sections_json, :priority,
                     :triggered_by, :gateway, :created_at, :delivered_at, :read_at)
                ON CONFLICT(briefing_id) DO UPDATE SET
                    status        = excluded.status,
                    sections_json = excluded.sections_json,
                    priority      = excluded.priority,
                    triggered_by  = excluded.triggered_by,
                    gateway       = excluded.gateway,
                    delivered_at  = excluded.delivered_at,
                    read_at       = excluded.read_at
                """,
                row,
            )
            self._conn.commit()
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("briefing", {
                "briefing_id": briefing.briefing_id,
                "status": getattr(briefing.status, "value", str(briefing.status)),
                "priority": briefing.priority,
            })
        except Exception:
            pass
        return briefing

    def get(self, briefing_id: str) -> Optional[Briefing]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM briefings WHERE briefing_id = ?", (briefing_id,)
            )
            row = cur.fetchone()
        return _row_to_briefing(row) if row else None

    def list_by_status(self, status: BriefingStatus, limit: int = 50) -> List[Briefing]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM briefings WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
            rows = cur.fetchall()
        return [_row_to_briefing(r) for r in rows]

    def list_recent(self, limit: int = 10) -> List[Briefing]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM briefings ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [_row_to_briefing(r) for r in rows]

    def mark_queued(self, briefing_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE briefings SET status = 'queued' WHERE briefing_id = ?",
                (briefing_id,),
            )
            self._conn.commit()

    def mark_delivering(self, briefing_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE briefings SET status = 'delivering' WHERE briefing_id = ?",
                (briefing_id,),
            )
            self._conn.commit()

    def mark_delivered(self, briefing_id: str, gateway: str) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._lock:
            self._conn.execute(
                "UPDATE briefings SET status = 'delivered', gateway = ?, delivered_at = ? WHERE briefing_id = ?",
                (gateway, now, briefing_id),
            )
            self._conn.commit()

    def mark_read(self, briefing_id: str) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._lock:
            self._conn.execute(
                "UPDATE briefings SET status = 'read', read_at = ? WHERE briefing_id = ?",
                (now, briefing_id),
            )
            self._conn.commit()

    def count_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM briefings WHERE created_at >= ?",
                (today,),
            )
            return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def get_schedule(self) -> List[ScheduleEntry]:
        with self._lock:
            cur = self._conn.execute("SELECT type, last_run, next_run FROM briefing_schedule")
            rows = cur.fetchall()
        return [
            ScheduleEntry(
                type=r["type"],
                last_run=_str_to_dt(r["last_run"]),
                next_run=_str_to_dt(r["next_run"]),
            )
            for r in rows
        ]

    def get_schedule_entry(self, briefing_type: BriefingType) -> Optional[ScheduleEntry]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT type, last_run, next_run FROM briefing_schedule WHERE type = ?",
                (briefing_type.value,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return ScheduleEntry(
            type=row["type"],
            last_run=_str_to_dt(row["last_run"]),
            next_run=_str_to_dt(row["next_run"]),
        )

    def update_schedule_last_run(self, briefing_type: BriefingType) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._lock:
            self._conn.execute(
                "UPDATE briefing_schedule SET last_run = ? WHERE type = ?",
                (now, briefing_type.value),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Engagement
    # ------------------------------------------------------------------

    def record_engagement(self, record: SectionEngagementRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO section_engagement
                    (section_name, briefing_id, signal, recorded_at, context)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.section_name,
                    record.briefing_id,
                    record.signal,
                    _dt_to_str(record.recorded_at),
                    record.context,
                ),
            )
            self._conn.commit()

    def get_engagement_records(
        self,
        section_name: Optional[str] = None,
        limit: int = 500,
    ) -> List[SectionEngagementRecord]:
        with self._lock:
            if section_name:
                cur = self._conn.execute(
                    "SELECT * FROM section_engagement WHERE section_name = ? ORDER BY recorded_at DESC LIMIT ?",
                    (section_name, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM section_engagement ORDER BY recorded_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cur.fetchall()
        return [
            SectionEngagementRecord(
                section_name=r["section_name"],
                briefing_id=r["briefing_id"],
                signal=r["signal"],
                recorded_at=_str_to_dt(r["recorded_at"]) or datetime.now(timezone.utc),
                context=r["context"],
            )
            for r in rows
        ]

    def get_suppressed_sections(self) -> List[str]:
        with self._lock:
            cur = self._conn.execute("SELECT section_name FROM suppressed_sections")
            return [r["section_name"] for r in cur.fetchall()]

    def suppress_section(self, section_name: str, reason: Optional[str] = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO suppressed_sections (section_name, suppressed_at, reason) VALUES (?, ?, ?)",
                (section_name, now, reason),
            )
            self._conn.commit()

    def unsuppress_section(self, section_name: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM suppressed_sections WHERE section_name = ?",
                (section_name,),
            )
            self._conn.commit()

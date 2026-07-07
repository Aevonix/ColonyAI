"""Affect Tracker — per-contact emotional valence and arousal over time.

Stores discrete affect events and maintains a computed current state per
contact with exponential decay toward neutral.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default decay: 5% per hour toward neutral.
DEFAULT_DECAY_FACTOR = 0.95
DEFAULT_AROUSAL_BASELINE = 0.3


class AffectStore:
    """SQLite-backed affect event store with computed state."""

    def __init__(self, db_path: str, *, decay_factor: float = DEFAULT_DECAY_FACTOR) -> None:
        self._db_path = db_path
        self._decay_factor = decay_factor
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS affect_events (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                valence REAL NOT NULL,
                arousal REAL NOT NULL DEFAULT 0.5,
                source TEXT NOT NULL,
                trigger TEXT,
                timestamp TEXT NOT NULL,
                session_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_affect_contact
                ON affect_events(contact_id);
            CREATE INDEX IF NOT EXISTS idx_affect_timestamp
                ON affect_events(timestamp);

            CREATE TABLE IF NOT EXISTS affect_state (
                contact_id TEXT PRIMARY KEY,
                current_valence REAL NOT NULL DEFAULT 0.0,
                current_arousal REAL NOT NULL DEFAULT 0.3,
                trend TEXT NOT NULL DEFAULT 'stable',
                last_event_id TEXT,
                last_updated TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Event CRUD
    # ------------------------------------------------------------------

    def create_event(
        self,
        *,
        contact_id: str,
        valence: float,
        arousal: float = 0.5,
        source: str = "explicit",
        trigger: Optional[str] = None,
        session_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an affect event and update the contact's computed state.

        Returns the created event dict.
        """
        # Clamp valence and arousal to valid ranges.
        valence = max(-1.0, min(1.0, valence))
        arousal = max(0.0, min(1.0, arousal))

        event_id = str(uuid.uuid4())
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """INSERT INTO affect_events (id, contact_id, valence, arousal, source, trigger, timestamp, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, contact_id, valence, arousal, source, trigger, ts, session_id),
        )
        self._conn.commit()

        # Update computed state.
        self._recompute_state(contact_id)

        event = self.get_event(event_id)
        assert event is not None
        return event

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM affect_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_events(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List affect events with optional filters."""
        clauses: List[str] = []
        params: List[Any] = []

        if contact_id is not None:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM affect_events{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    def count_events(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """Total matching events, so paginated views can report a real total."""
        clauses: List[str] = []
        params: List[Any] = []
        if contact_id is not None:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self._conn.execute(
            f"SELECT COUNT(*) FROM affect_events{where}", params
        ).fetchone()[0])

    def delete_event(self, event_id: str) -> bool:
        """Delete an affect event and recompute state. Returns True if deleted."""
        event = self.get_event(event_id)
        if event is None:
            return False
        self._conn.execute("DELETE FROM affect_events WHERE id = ?", (event_id,))
        self._conn.commit()
        self._recompute_state(event["contact_id"])
        return True

    # ------------------------------------------------------------------
    # Computed state
    # ------------------------------------------------------------------

    def get_state(self, contact_id: str) -> Dict[str, Any]:
        """Get the current affect state for a contact.

        Applies time-based decay before returning.
        """
        self._apply_decay(contact_id)
        row = self._conn.execute(
            "SELECT * FROM affect_state WHERE contact_id = ?", (contact_id,)
        ).fetchone()
        if row is None:
            return {
                "contact_id": contact_id,
                "current_valence": 0.0,
                "current_arousal": DEFAULT_AROUSAL_BASELINE,
                "trend": "stable",
                "last_event_id": None,
                "last_updated": None,
                "event_count": 0,
            }
        return dict(row)

    def get_all_states(self) -> List[Dict[str, Any]]:
        """Get affect states for all contacts with recorded events."""
        rows = self._conn.execute("SELECT * FROM affect_state").fetchall()
        results = []
        for row in rows:
            self._apply_decay(row["contact_id"])
            results.append(self.get_state(row["contact_id"]))
        return results

    def detect_negative_spike(self, contact_id: str, threshold: float = -0.5) -> bool:
        """Check if the most recent event is a negative spike."""
        row = self._conn.execute(
            "SELECT valence FROM affect_events WHERE contact_id = ? ORDER BY timestamp DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        return row is not None and row["valence"] <= threshold

    def detect_sustained_decline(self, contact_id: str, min_events: int = 3) -> bool:
        """Check if the contact has a sustained declining trend."""
        state = self.get_state(contact_id)
        return state["trend"] == "declining" and state["event_count"] >= min_events

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_decay(self, contact_id: str) -> None:
        """Apply exponential decay to a contact's affect state."""
        row = self._conn.execute(
            "SELECT * FROM affect_state WHERE contact_id = ?", (contact_id,)
        ).fetchone()
        if row is None or row["last_updated"] is None:
            return

        last_updated = datetime.fromisoformat(row["last_updated"])
        now = datetime.now(timezone.utc)
        hours_elapsed = (now - last_updated).total_seconds() / 3600.0

        if hours_elapsed <= 0:
            return

        decayed_valence = row["current_valence"] * (self._decay_factor ** hours_elapsed)
        decayed_arousal = DEFAULT_AROUSAL_BASELINE + (row["current_arousal"] - DEFAULT_AROUSAL_BASELINE) * (self._decay_factor ** hours_elapsed)

        # Round to avoid floating-point drift.
        decayed_valence = round(decayed_valence, 4)
        decayed_arousal = round(decayed_arousal, 4)

        self._conn.execute(
            """UPDATE affect_state SET current_valence = ?, current_arousal = ?, last_updated = ?
               WHERE contact_id = ?""",
            (decayed_valence, decayed_arousal, now.isoformat(), contact_id),
        )
        self._conn.commit()

    def _recompute_state(self, contact_id: str) -> None:
        """Recompute the affect state from recent events.

        Uses a weighted average with recency bias: more recent events
        contribute more to the current state.
        """
        rows = self._conn.execute(
            "SELECT * FROM affect_events WHERE contact_id = ? ORDER BY timestamp ASC",
            (contact_id,),
        ).fetchall()

        if not rows:
            self._conn.execute(
                "DELETE FROM affect_state WHERE contact_id = ?", (contact_id,)
            )
            self._conn.commit()
            return

        # Weighted average with exponential recency bias.
        total_weight = 0.0
        weighted_valence = 0.0
        weighted_arousal = 0.0
        weight = 1.0

        for row in rows:
            weighted_valence += row["valence"] * weight
            weighted_arousal += row["arousal"] * weight
            total_weight += weight
            weight *= 0.9  # decay weight for older events

        current_valence = round(weighted_valence / total_weight, 4)
        current_arousal = round(weighted_arousal / total_weight, 4)

        # Determine trend from last 5 events.
        recent = rows[-5:] if len(rows) >= 5 else rows
        if len(recent) >= 2:
            early_avg = sum(r["valence"] for r in recent[: len(recent) // 2]) / (len(recent) // 2)
            late_avg = sum(r["valence"] for r in recent[len(recent) // 2 :]) / (len(recent) - len(recent) // 2)
            diff = late_avg - early_avg
            if diff > 0.1:
                trend = "improving"
            elif diff < -0.1:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        last_event = rows[-1]
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """INSERT INTO affect_state (contact_id, current_valence, current_arousal, trend, last_event_id, last_updated, event_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(contact_id) DO UPDATE SET
                   current_valence = excluded.current_valence,
                   current_arousal = excluded.current_arousal,
                   trend = excluded.trend,
                   last_event_id = excluded.last_event_id,
                   last_updated = excluded.last_updated,
                   event_count = excluded.event_count""",
            (contact_id, current_valence, current_arousal, trend, last_event["id"], now, len(rows)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

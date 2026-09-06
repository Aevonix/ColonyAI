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
DEFAULT_SUSTAINED_DECLINE_MAGNITUDE = 0.3
_DECAY_ONLY_SOURCES = frozenset({"decay"})


from .source_lineage import SourceLinkedStore


class AffectStore(SourceLinkedStore):
    """SQLite-backed affect event store with computed state."""

    def __init__(self, db_path: str, *, decay_factor: float = DEFAULT_DECAY_FACTOR, source_ledger=None) -> None:
        self._db_path = db_path
        self._source_ledger = source_ledger
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
        if 'source_lineage_json' not in {row[1] for row in self._conn.execute('PRAGMA table_info(affect_events)')}:
            self._conn.execute('ALTER TABLE affect_events ADD COLUMN source_lineage_json TEXT')
        self._conn.commit()

    def purge_erased_sources(self, turn_ids=None, *, contact_id=None) -> int:
        sql = 'SELECT id,contact_id,source_lineage_json FROM affect_events WHERE source_lineage_json IS NOT NULL'
        rows = self._conn.execute(sql + (' AND contact_id=?' if contact_id else ''),
                                  (contact_id,) if contact_id else ()).fetchall()
        invalid = self._invalid_sources(rows, turn_ids)
        with self._conn:
            self._conn.executemany('DELETE FROM affect_events WHERE id=?', [(row['id'],) for row in invalid])
            for person in {row['contact_id'] for row in invalid}:
                self._recompute_state(person, commit=False)
        return len(invalid)

    @staticmethod
    def _decode(row):
        event = dict(row)
        raw = event.pop('source_lineage_json', None)
        event['source_lineage'] = json.loads(raw) if raw else None
        event['evidence_basis'] = 'canonical_source' if raw else 'unlinked_observation'
        return event

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
        source_lineage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an affect event and update the contact's computed state.

        Returns the created event dict.
        """
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            from colony_sidecar.turns.idempotency import SourceErased
            raise SourceErased('source_erased')
        # Clamp valence and arousal to valid ranges.
        valence = max(-1.0, min(1.0, valence))
        arousal = max(0.0, min(1.0, arousal))

        event_id = str(uuid.uuid4())
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        with self._conn:
            self._conn.execute(
                """INSERT INTO affect_events (id, contact_id, valence, arousal, source, trigger, timestamp, session_id, source_lineage_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, contact_id, valence, arousal, source, trigger, ts, session_id,
                 json.dumps(source_lineage) if source_lineage is not None else None),
            )
            self._recompute_state(contact_id, commit=False)

        event = self.get_event(event_id)
        if event is None:
            from colony_sidecar.turns.idempotency import SourceErased
            raise SourceErased('source_erased')
        return event

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        self.purge_erased_sources()
        row = self._conn.execute(
            "SELECT * FROM affect_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode(row)

    def list_events(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List affect events with optional filters."""
        self.purge_erased_sources(contact_id=contact_id)
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
        return [self._decode(r) for r in rows]

    def count_events(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """Total matching events, so paginated views can report a real total."""
        self.purge_erased_sources(contact_id=contact_id)
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
        self.purge_erased_sources(contact_id=contact_id)
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
        self.purge_erased_sources()
        rows = self._conn.execute("SELECT * FROM affect_state").fetchall()
        results = []
        for row in rows:
            self._apply_decay(row["contact_id"])
            results.append(self.get_state(row["contact_id"]))
        return results

    def detect_negative_spike(self, contact_id: str, threshold: float = -0.5) -> bool:
        """Check if the most recent event is a negative spike."""
        self.purge_erased_sources(contact_id=contact_id)
        row = self._conn.execute(
            "SELECT valence FROM affect_events WHERE contact_id = ? ORDER BY timestamp DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        return row is not None and row["valence"] <= threshold

    def detect_sustained_decline(
        self,
        contact_id: str,
        min_events: int = 3,
        min_magnitude: float = DEFAULT_SUSTAINED_DECLINE_MAGNITUDE,
    ) -> bool:
        """Check for a material decline supported by observed affect events.

        Time decay changes the current state toward neutral but is not new
        evidence about a contact.  Recompute the trend from non-decay events
        here so a stale/decayed state cannot trigger proactive initiative.
        """
        state = self.get_state(contact_id)
        if state["current_valence"] > -abs(min_magnitude):
            return False

        rows = self._conn.execute(
            "SELECT * FROM affect_events WHERE contact_id = ? ORDER BY timestamp ASC",
            (contact_id,),
        ).fetchall()
        observed = [row for row in rows if not self._is_decay_only(row)]
        return len(observed) >= min_events and self._event_trend(observed) == "declining"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _is_decay_only(event: sqlite3.Row) -> bool:
        return str(event["source"]).strip().lower() in _DECAY_ONLY_SOURCES

    @staticmethod
    def _event_trend(rows: List[sqlite3.Row]) -> str:
        """Return the trend of the latest observed affect events."""
        recent = rows[-5:] if len(rows) >= 5 else rows
        if len(recent) < 2:
            return "stable"

        midpoint = len(recent) // 2
        early_avg = sum(row["valence"] for row in recent[:midpoint]) / midpoint
        late_avg = sum(row["valence"] for row in recent[midpoint:]) / (len(recent) - midpoint)
        diff = late_avg - early_avg
        if diff > 0.1:
            return "improving"
        if diff < -0.1:
            return "declining"
        return "stable"

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

    def _recompute_state(self, contact_id: str, *, commit=True) -> None:
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
            if commit:
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

        # Decay is a state projection, not a new observation.  Keep it out of
        # the event trend even if an imported/legacy row labels it explicitly.
        observed = [row for row in rows if not self._is_decay_only(row)]
        trend = self._event_trend(observed)

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
        if commit:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

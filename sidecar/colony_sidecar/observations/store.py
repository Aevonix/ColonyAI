"""SQLite store for agent-reported domain observations (v0.16.0).

One row per (domain, entity_id) — the latest snapshot wins. Colony's
initiative generators read these; the autonomy loop requests refreshes
by posting read-only ``agent_sync_<domain>`` jobs when a domain's
newest observation outlives its sync interval.

Expected payload shapes per domain (generators are defensive — extra
keys pass through into initiative context, missing keys skip rules):

- coding:   {title, url, ci_status, review_requested, draft, author}
- task:     {title, url, state, assignee, due, stale_days}
- calendar: {title, start_time, location, attendees, needs_prep}
- research: {title, url, status, last_checked}
- project:  {title, due_on, open_issues, closed_issues}
- system:   {status, latency_ms, error_rate, message}
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Domains the sensor loop knows about. Adding a domain means: a sync
# action in the action registry, a generator in the initiative engine,
# and (if volatile) a durability entry in context_freshness.py.
OBSERVATION_DOMAINS = (
    "coding",
    "task",
    "calendar",
    "research",
    "project",
    "system",
)

# How old a domain's newest observation may be before the autonomy loop
# requests a fresh sync from the agent (seconds). Coarser than the
# context-freshness TTLs — those govern acting on one snapshot, these
# govern re-scanning a whole domain.
OBSERVATION_SYNC_INTERVALS: Dict[str, int] = {
    "coding": 900,
    "task": 3600,
    "calendar": 900,
    "research": 86400,
    "project": 86400,
    "system": 300,
}


@dataclass
class Observation:
    domain: str
    entity_id: str
    payload: Dict[str, Any]
    observed_at: datetime
    reported_by: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "Observation":
        payload_raw = row.get("payload") or "{}"
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (ValueError, TypeError):
            payload = {}
        observed_at = row.get("observed_at")
        if isinstance(observed_at, str):
            try:
                observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                observed_at = datetime.now(timezone.utc)
        return cls(
            domain=row["domain"],
            entity_id=row["entity_id"],
            payload=payload,
            observed_at=observed_at or datetime.now(timezone.utc),
            reported_by=row.get("reported_by"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "observed_at": self.observed_at.isoformat(),
            "reported_by": self.reported_by,
        }


class ObservationStore:
    """Latest-snapshot-per-entity observation persistence."""

    def __init__(self, state_dir: Optional[Path] = None):
        if state_dir is None:
            from colony_sidecar.initiatives.store import get_state_dir
            state_dir = get_state_dir()
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "observations.db"
        self._db = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                domain TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT NOT NULL,
                reported_by TEXT,
                PRIMARY KEY (domain, entity_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_domain "
            "ON observations(domain, observed_at DESC)"
        )
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(
        self,
        domain: str,
        entity_id: str,
        payload: Dict[str, Any],
        reported_by: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> Observation:
        """Upsert the latest snapshot for one entity."""
        observed_at = observed_at or datetime.now(timezone.utc)
        self._db.execute(
            """
            INSERT INTO observations (domain, entity_id, payload, observed_at, reported_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain, entity_id) DO UPDATE SET
                payload = excluded.payload,
                observed_at = excluded.observed_at,
                reported_by = excluded.reported_by
            """,
            [domain, entity_id, json.dumps(payload), observed_at.isoformat(), reported_by],
        )
        self._db.commit()
        return Observation(domain, entity_id, payload, observed_at, reported_by)

    def record_batch(
        self,
        domain: str,
        observations: List[Dict[str, Any]],
        reported_by: Optional[str] = None,
    ) -> int:
        """Record many snapshots for one domain. Returns rows written."""
        written = 0
        for obs in observations:
            entity_id = obs.get("entity_id")
            if not entity_id:
                continue
            observed_at = obs.get("observed_at")
            if isinstance(observed_at, str):
                try:
                    observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    observed_at = None
            self.record(
                domain=domain,
                entity_id=str(entity_id),
                payload=obs.get("payload") or {},
                reported_by=reported_by,
                observed_at=observed_at,
            )
            written += 1
        return written

    def delete(self, domain: str, entity_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM observations WHERE domain = ? AND entity_id = ?",
            [domain, entity_id],
        )
        self._db.commit()
        return cur.rowcount > 0

    def prune(self, older_than_days: float = 30.0) -> int:
        """Drop snapshots nothing has refreshed in a long time."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cur = self._db.execute(
            "DELETE FROM observations WHERE observed_at < ?", [cutoff_iso]
        )
        self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, domain: str, entity_id: str) -> Optional[Observation]:
        cur = self._db.execute(
            "SELECT * FROM observations WHERE domain = ? AND entity_id = ?",
            [domain, entity_id],
        )
        row = cur.fetchone()
        return Observation.from_row(dict(row)) if row else None

    def list(self, domain: str, limit: int = 100) -> List[Observation]:
        cur = self._db.execute(
            "SELECT * FROM observations WHERE domain = ? "
            "ORDER BY observed_at DESC LIMIT ?",
            [domain, limit],
        )
        return [Observation.from_row(dict(r)) for r in cur.fetchall()]

    def domain_age_seconds(self, domain: str) -> Optional[float]:
        """Seconds since the NEWEST observation in a domain.

        None when the domain has never been observed — callers treat
        that as maximally stale (a sync is needed to prime it).
        """
        cur = self._db.execute(
            "SELECT MAX(observed_at) AS newest FROM observations WHERE domain = ?",
            [domain],
        )
        row = cur.fetchone()
        newest = row["newest"] if row else None
        if not newest:
            return None
        try:
            stamp = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds()

    def summary(self) -> Dict[str, Dict[str, Any]]:
        """Per-domain counts and freshness."""
        cur = self._db.execute(
            "SELECT domain, COUNT(*) AS n, MAX(observed_at) AS newest "
            "FROM observations GROUP BY domain"
        )
        out: Dict[str, Dict[str, Any]] = {}
        for row in cur.fetchall():
            out[row["domain"]] = {
                "count": row["n"],
                "newest_observed_at": row["newest"],
                "age_seconds": self.domain_age_seconds(row["domain"]),
            }
        return out

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

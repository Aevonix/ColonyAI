"""SQLite-backed store for dismissed-insight overlays.

Insights themselves are re-computed each call by ConnectionDiscoverer —
we don't persist the insights. This store tracks which insight IDs the
user has dismissed so ``list_insights`` can filter them out.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Set

logger = logging.getLogger(__name__)


class InsightStore:
    """Persistent record of dismissed insight IDs."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dismissed_insights (
                    insight_id TEXT PRIMARY KEY,
                    dismissed_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def dismiss(self, insight_id: str) -> None:
        """Mark the given insight as dismissed. Idempotent."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dismissed_insights (insight_id, dismissed_at)
                VALUES (?, ?)
                ON CONFLICT(insight_id) DO NOTHING
                """,
                (insight_id, datetime.now(timezone.utc).isoformat()),
            )

    def is_dismissed(self, insight_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM dismissed_insights WHERE insight_id=? LIMIT 1",
                (insight_id,),
            ).fetchone()
            return row is not None

    def list_dismissed(self) -> Set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT insight_id FROM dismissed_insights"
            ).fetchall()
            return {r[0] for r in rows}

    def undismiss(self, insight_id: str) -> bool:
        """Remove a dismissal. Returns True if a row was removed."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM dismissed_insights WHERE insight_id=?",
                (insight_id,),
            )
            return cur.rowcount > 0

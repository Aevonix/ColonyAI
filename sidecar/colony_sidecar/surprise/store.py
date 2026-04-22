"""Surprise Store — record and track unexpected observations."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SurpriseStore:
    """SQLite-backed surprise observation store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS surprises (
                id TEXT PRIMARY KEY,
                observation TEXT NOT NULL,
                expected TEXT,
                surprise_score REAL NOT NULL,
                pattern_id TEXT,
                context TEXT,
                timestamp TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                resolution TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_surprises_score
                ON surprises(surprise_score DESC);
            CREATE INDEX IF NOT EXISTS idx_surprises_timestamp
                ON surprises(timestamp);
            CREATE INDEX IF NOT EXISTS idx_surprises_resolved
                ON surprises(resolved);
        """)
        self._conn.commit()

    def create_surprise(
        self,
        *,
        observation: str,
        expected: Optional[str] = None,
        surprise_score: float = 0.5,
        pattern_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a surprise observation.

        Returns the created surprise dict.
        """
        surprise_score = max(0.0, min(1.0, surprise_score))

        surprise_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        ctx_json = json.dumps(context) if context else None

        self._conn.execute(
            """INSERT INTO surprises (id, observation, expected, surprise_score,
                  pattern_id, context, timestamp, resolved, resolution)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
            (surprise_id, observation, expected, surprise_score,
             pattern_id, ctx_json, now),
        )
        self._conn.commit()

        result = self.get_surprise(surprise_id)
        assert result is not None
        return result

    def get_surprise(self, surprise_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM surprises WHERE id = ?", (surprise_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["resolved"] = bool(d["resolved"])
        if d.get("context"):
            try:
                d["context"] = json.loads(d["context"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def list_surprises(
        self,
        *,
        min_score: float = 0.0,
        resolved: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List surprises with optional filters, sorted by score descending."""
        clauses: List[str] = []
        params: List[Any] = []

        if min_score > 0:
            clauses.append("surprise_score >= ?")
            params.append(min_score)
        if resolved is not None:
            clauses.append("resolved = ?")
            params.append(1 if resolved else 0)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM surprises{where}", params
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = self._conn.execute(
            f"SELECT * FROM surprises{where} ORDER BY surprise_score DESC, timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        surprises = []
        for row in rows:
            d = dict(row)
            d["resolved"] = bool(d["resolved"])
            if d.get("context"):
                try:
                    d["context"] = json.loads(d["context"])
                except (json.JSONDecodeError, TypeError):
                    pass
            surprises.append(d)

        return {"surprises": surprises, "total": total, "limit": limit, "offset": offset}

    def get_unresolved(self, min_score: float = 0.5, limit: int = 10) -> List[Dict[str, Any]]:
        """Get unresolved high-score surprises."""
        result = self.list_surprises(min_score=min_score, resolved=False, limit=limit)
        return result["surprises"]

    def resolve_surprise(
        self,
        surprise_id: str,
        *,
        resolution: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Mark a surprise as resolved. Returns updated surprise or None."""
        existing = self.get_surprise(surprise_id)
        if existing is None:
            return None

        self._conn.execute(
            "UPDATE surprises SET resolved = 1, resolution = ? WHERE id = ?",
            (resolution, surprise_id),
        )
        self._conn.commit()
        return self.get_surprise(surprise_id)

    def delete_surprise(self, surprise_id: str) -> bool:
        """Delete a surprise. Returns True if deleted."""
        cursor = self._conn.execute("DELETE FROM surprises WHERE id = ?", (surprise_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def count_unresolved(self, since_hours: float = 1.0) -> int:
        """Count unresolved surprises within the last N hours."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM surprises WHERE resolved = 0 AND timestamp >= ?",
            (cutoff,),
        ).fetchone()
        return row["cnt"] if row else 0

    def close(self) -> None:
        self._conn.close()

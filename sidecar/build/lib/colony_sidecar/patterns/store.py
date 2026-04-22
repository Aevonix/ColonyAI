"""Pattern Store — observed patterns with frequency and recency."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PatternStore:
    """SQLite-backed pattern store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS patterns (
                id TEXT PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_key TEXT NOT NULL,
                frequency INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                metadata TEXT,
                source TEXT NOT NULL DEFAULT 'extraction',
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_key ON patterns(pattern_key);
            CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type);
            CREATE INDEX IF NOT EXISTS idx_patterns_frequency ON patterns(frequency DESC);
        """)
        self._conn.commit()

    def create_pattern(
        self,
        *,
        pattern_type: str,
        description: str,
        pattern_key: str,
        frequency: int = 1,
        confidence: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "extraction",
    ) -> Dict[str, Any]:
        """Register a pattern. If pattern_key exists, increment frequency.

        Returns the created or updated pattern dict.
        """
        confidence = max(0.0, min(1.0, confidence))
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None

        # Upsert: increment frequency if key exists.
        existing = self._conn.execute(
            "SELECT id, frequency FROM patterns WHERE pattern_key = ?",
            (pattern_key,),
        ).fetchone()

        if existing is not None:
            new_freq = existing["frequency"] + 1
            self._conn.execute(
                """UPDATE patterns SET frequency = ?, last_seen = ?, confidence = ?,
                      description = ?, metadata = ?, source = ?, active = 1
                   WHERE id = ?""",
                (new_freq, now, confidence, description, meta_json, source, existing["id"]),
            )
            self._conn.commit()
            result = self.get_pattern(existing["id"])
            assert result is not None
            return result

        pattern_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO patterns (id, pattern_type, description, pattern_key, frequency,
                  last_seen, first_seen, confidence, metadata, source, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (pattern_id, pattern_type, description, pattern_key, frequency,
             now, now, confidence, meta_json, source),
        )
        self._conn.commit()

        result = self.get_pattern(pattern_id)
        assert result is not None
        return result

    def get_pattern(self, pattern_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["active"] = bool(d["active"])
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def list_patterns(
        self,
        *,
        pattern_type: Optional[str] = None,
        min_frequency: int = 1,
        source: Optional[str] = None,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List patterns with optional filters."""
        clauses: List[str] = []
        params: List[Any] = []

        if pattern_type is not None:
            clauses.append("pattern_type = ?")
            params.append(pattern_type)
        if min_frequency > 1:
            clauses.append("frequency >= ?")
            params.append(min_frequency)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if active_only:
            clauses.append("active = 1")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM patterns{where}", params
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = self._conn.execute(
            f"SELECT * FROM patterns{where} ORDER BY frequency DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        patterns = []
        for row in rows:
            d = dict(row)
            d["active"] = bool(d["active"])
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            patterns.append(d)

        return {"patterns": patterns, "total": total, "limit": limit, "offset": offset}

    def update_pattern(
        self,
        pattern_id: str,
        *,
        description: Optional[str] = None,
        confidence: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        active: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a pattern. Returns updated pattern or None if not found."""
        existing = self.get_pattern(pattern_id)
        if existing is None:
            return None

        updates: List[str] = []
        params: List[Any] = []

        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if confidence is not None:
            updates.append("confidence = ?")
            params.append(max(0.0, min(1.0, confidence)))
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))
        if active is not None:
            updates.append("active = ?")
            params.append(1 if active else 0)

        if not updates:
            return existing

        params.append(pattern_id)
        self._conn.execute(
            f"UPDATE patterns SET {', '.join(updates)} WHERE id = ?", params
        )
        self._conn.commit()
        return self.get_pattern(pattern_id)

    def delete_pattern(self, pattern_id: str) -> bool:
        """Delete a pattern. Returns True if deleted."""
        cursor = self._conn.execute("DELETE FROM patterns WHERE id = ?", (pattern_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def deactivate_stale(self, min_frequency: int = 2, days_inactive: int = 30) -> int:
        """Deactivate patterns that haven't been seen recently.

        Returns count of deactivated patterns.
        """
        cutoff = datetime.now(timezone.utc).isoformat()
        # Simple approach: deactivate patterns with low frequency.
        cursor = self._conn.execute(
            "UPDATE patterns SET active = 0 WHERE frequency < ? AND active = 1",
            (min_frequency,),
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

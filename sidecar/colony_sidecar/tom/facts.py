"""Shared Facts — what the agent believes each contact knows.

Tracks information asymmetry: which facts are shared with a contact,
which were told by them, and which the agent inferred they know.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SharedFactsStore:
    """SQLite-backed shared facts store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS shared_facts (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                metadata TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_shared_facts_contact
                ON shared_facts(contact_id);
            CREATE INDEX IF NOT EXISTS idx_shared_facts_source
                ON shared_facts(source);
        """)
        self._conn.commit()

    def create_fact(
        self,
        *,
        contact_id: str,
        fact: str,
        source: str = "shared_context",
        confidence: float = 0.8,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a shared fact. Returns the created fact dict."""
        confidence = max(0.0, min(1.0, confidence))

        fact_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        meta_json = None
        if metadata is not None:
            import json
            meta_json = json.dumps(metadata)

        self._conn.execute(
            """INSERT INTO shared_facts (id, contact_id, fact, source, confidence, created_at, expires_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fact_id, contact_id, fact, source, confidence, now, expires_at, meta_json),
        )
        self._conn.commit()

        result = self.get_fact(fact_id)
        assert result is not None
        return result

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM shared_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("metadata"):
            import json
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def list_facts(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List shared facts with optional filters.

        Returns {"facts": [...], "total": N, "limit": N, "offset": N}.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if contact_id is not None:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if min_confidence > 0:
            clauses.append("confidence >= ?")
            params.append(min_confidence)

        # Filter out expired facts.
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(datetime.now(timezone.utc).isoformat())

        where = f" WHERE {' AND '.join(clauses)}"

        total_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM shared_facts{where}", params
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = self._conn.execute(
            f"SELECT * FROM shared_facts{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        facts = []
        for row in rows:
            d = dict(row)
            if d.get("metadata"):
                import json
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            facts.append(d)

        return {"facts": facts, "total": total, "limit": limit, "offset": offset}

    def update_fact(
        self,
        fact_id: str,
        *,
        confidence: Optional[float] = None,
        expires_at: Optional[str] = None,
        fact: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a shared fact. Returns updated fact or None if not found."""
        existing = self.get_fact(fact_id)
        if existing is None:
            return None

        updates: List[str] = []
        params: List[Any] = []

        if confidence is not None:
            updates.append("confidence = ?")
            params.append(max(0.0, min(1.0, confidence)))
        if expires_at is not None:
            updates.append("expires_at = ?")
            params.append(expires_at)
        if fact is not None:
            updates.append("fact = ?")
            params.append(fact)
        if metadata is not None:
            import json
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))

        if not updates:
            return existing

        params.append(fact_id)
        self._conn.execute(
            f"UPDATE shared_facts SET {', '.join(updates)} WHERE id = ?", params
        )
        self._conn.commit()

        return self.get_fact(fact_id)

    def delete_fact(self, fact_id: str) -> bool:
        """Delete a shared fact. Returns True if deleted."""
        cursor = self._conn.execute("DELETE FROM shared_facts WHERE id = ?", (fact_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def purge_expired(self) -> int:
        """Remove expired facts. Returns count purged."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM shared_facts WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

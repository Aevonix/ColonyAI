"""Shared Facts — what the agent believes each contact knows.

Tracks information asymmetry: which facts are shared with a contact,
which were told by them, and which the agent inferred they know.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SharedFactsStore:
    """SQLite-backed shared facts store."""

    def __init__(self, db_path: str, *, source_ledger=None) -> None:
        self._db_path = db_path
        self._source_ledger = source_ledger
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        self._conn.create_function('source_fact_visible', 2, self._source_visible)

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
        if 'source_lineage_json' not in {row[1] for row in self._conn.execute('PRAGMA table_info(shared_facts)')}:
            self._conn.execute('ALTER TABLE shared_facts ADD COLUMN source_lineage_json TEXT')
            self._conn.commit()

    def _ledger(self):
        if self._source_ledger is None:
            from colony_sidecar import get_state_dir
            from colony_sidecar.turns import get_turn_idempotency_ledger
            self._source_ledger = get_turn_idempotency_ledger(get_state_dir())
        return self._source_ledger

    def source_input(self, turn_id: str, contact_id: str) -> tuple[dict, str]:
        """Canonical input and exact support hashes, never a generated summary."""
        from colony_sidecar.turns.idempotency import source_message_hash, SourceErased
        ledger = self._ledger()
        if ledger.is_projection_erased(turn_id):
            raise SourceErased('source_erased')
        conn = ledger._connect()
        try:
            row = conn.execute(
                'SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?',
                (turn_id, contact_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row['scope'] != 'person':
            raise SourceErased('canonical_person_source_unavailable')
        messages = json.loads(row['messages_json'])
        lineage = {
            'turn_id': turn_id, 'session_id': row['session_id'],
            'message_hashes': [source_message_hash(row['session_id'], m) for m in messages],
            'occurred_at': row['occurred_at'], 'ingested_at': row['ingested_at'],
        }
        texts = []
        for message in messages:
            content = message.get('content')
            if isinstance(content, list):
                content = '\n'.join(
                    b['text'] for b in content
                    if isinstance(b, dict) and isinstance(b.get('text'), str)
                )
            if isinstance(content, str) and content:
                texts.append(message['role'] + ': ' + content)
        return lineage, '\n'.join(texts)

    def _source_visible(self, contact_id: str, raw) -> bool:
        if raw is None:
            return True  # No invented provenance or blanket removal of legacy facts.
        from colony_sidecar.turns.idempotency import SourceErased
        lineage = json.loads(raw) if isinstance(raw, str) else raw
        try:
            current, _ = self.source_input(lineage['turn_id'], contact_id)
        except SourceErased:
            return False
        return (current['session_id'] == lineage['session_id']
                and current['message_hashes'] == lineage['message_hashes'])

    def source_visible(self, record: dict) -> bool:
        return self._source_visible(record['contact_id'], record.get('source_lineage'))

    def purge_erased_sources(self, turn_ids: Optional[List[str]] = None) -> int:
        """Physical cleanup follows durable tombstones; unknown origins stay intact."""
        count = 0
        rows = self._conn.execute(
            'SELECT id,source_lineage_json FROM shared_facts WHERE source_lineage_json IS NOT NULL'
        ).fetchall()
        for row in rows:
            lineage = json.loads(row['source_lineage_json'])
            if turn_ids is not None and lineage['turn_id'] not in turn_ids:
                continue
            if self._ledger().is_projection_erased(lineage['turn_id']):
                count += self._conn.execute('DELETE FROM shared_facts WHERE id=?', (row['id'],)).rowcount
        self._conn.commit()
        return count

    @staticmethod
    def _decode(row):
        data = dict(row)
        if data.get('metadata'):
            try:
                data['metadata'] = json.loads(data['metadata'])
            except (ValueError, TypeError):
                pass
        raw = data.pop('source_lineage_json', None)
        if raw:
            data['source_lineage'] = json.loads(raw)
            data['metadata'] = {
                **(data.get('metadata') or {}),
                'canonical_source': 'turn:' + data['source_lineage']['turn_id'],
            }
        return data

    def create_fact(
        self,
        *,
        contact_id: str,
        fact: str,
        source: str = "shared_context",
        confidence: float = 0.8,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_lineage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a shared fact. Returns the created fact dict."""
        confidence = max(0.0, min(1.0, confidence))
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            from colony_sidecar.turns.idempotency import SourceErased
            raise SourceErased('source_erased')

        fact_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        meta_json = None
        if metadata is not None:
            meta_json = json.dumps(metadata)

        self._conn.execute(
            """INSERT INTO shared_facts (id, contact_id, fact, source, confidence, created_at, expires_at, metadata,source_lineage_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fact_id, contact_id, fact, source, confidence, now, expires_at, meta_json,
             json.dumps(source_lineage) if source_lineage is not None else None),
        )
        self._conn.commit()

        result = self.get_fact(fact_id)
        if result is None:
            self.purge_erased_sources()
            from colony_sidecar.turns.idempotency import SourceErased
            raise SourceErased('source_erased')
        return result

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM shared_facts WHERE id = ? AND source_fact_visible(contact_id,source_lineage_json)", (fact_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode(row)

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
        clauses.append('source_fact_visible(contact_id,source_lineage_json)')
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

        facts = [self._decode(row) for row in rows]

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

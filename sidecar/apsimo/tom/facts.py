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


from .source_lineage import SourceLinkedStore


class SharedFactsStore(SourceLinkedStore):
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
        self._conn.create_function('source_fact_automatic', 2, self._automatic_visible)

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

    def source_visible(self, record: dict) -> bool:
        return self._source_visible(record['contact_id'], record.get('source_lineage'))

    def automatic_view(self):
        """Current source-linked estimates only; explicit history stays intact."""
        return AutomaticFactsView(self)

    def _automatic_visible(self, contact_id, raw) -> bool:
        if not raw:
            return False
        try:
            lineage = json.loads(raw) if isinstance(raw, str) else raw
            return bool(isinstance(lineage, dict) and lineage.get('message_hashes')
                        and self._source_visible(contact_id, lineage))
        except (ValueError, KeyError, TypeError):
            return False

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
            from apsimo.turns.idempotency import SourceErased
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
            from apsimo.turns.idempotency import SourceErased
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
        source_linked_only: bool = False,
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
        if source_linked_only:
            # Apply before the window so recent legacy noise cannot crowd out
            # older supported estimates. Visibility still checks exact current
            # contact, session and message hashes in the canonical ledger.
            clauses.append('source_fact_automatic(contact_id,source_lineage_json)')
        else:
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


class AutomaticFactsView:
    """Existing read interface with canonical eligibility, never a new store."""

    def __init__(self, store):
        self._store = store

    def get_fact(self, fact_id):
        record = self._store.get_fact(fact_id)
        if record is None or not self._store._automatic_visible(
                record['contact_id'], record.get('source_lineage')):
            return None
        expires = record.get('expires_at')
        if expires and expires <= datetime.now(timezone.utc).isoformat():
            return None
        return record

    def list_facts(self, **kwargs):
        return self._store.list_facts(**{**kwargs, 'source_linked_only': True})


class InferenceFactsView(SourceLinkedStore):
    """ToM2 cannot reuse an old knowledge inference across a source correction.

    Ordinary recall can bundle attributed corrections with the original. The
    compact knowledge renderers cannot represent that packet, so omit the
    inference while retaining its fact and correction for explicit inspection.
    """

    def __init__(self, view, ledger):
        self._view, self._source_ledger = view, ledger
        self._read = {}

    def get_fact(self, fact_id):
        from apsimo.turns.source_annotations import expand
        try:
            row = self._view.get_fact(fact_id)
            lineage = (row or {}).get('source_lineage') or {}
            # Projected views can cache authorized rows. Recheck canonical
            # membership independently, including all hashes and source scope.
            if not lineage.get('message_hashes') or not self._source_visible(row['contact_id'], lineage):
                return None
            source = lineage['turn_id']
            packet = expand(self._ledger(), [{
                'id': fact_id, 'content': row['fact'], 'source_turn_id': source,
                '_source_message_hashes': {source: lineage['message_hashes']},
            }], contact_id=row['contact_id'], session_id=lineage['session_id'])
            if (len(packet) != 1 or packet[0].get('_annotation_ids')
                    or source not in {ref['source_id'] for ref in packet[0].get('_annotation_source_refs', [])}):
                return None
            self._read.setdefault(fact_id, row)
            return row
        except Exception:
            logger.debug('ToM2 source correction check unavailable', exc_info=True)
            return None

    def current(self):
        """Recheck after other context producers may have awaited work."""
        return all(self.get_fact(key) == row for key, row in list(self._read.items()))

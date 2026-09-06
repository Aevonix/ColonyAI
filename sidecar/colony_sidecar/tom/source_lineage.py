"""Canonical source checks shared by the existing ToM projections.

Unknown historical origins remain explicitly unlinked. New conversation-derived
records retain the exact canonical turn, session and message hashes.
"""
from __future__ import annotations

import json


class SourceLinkedStore:
    """A source reader only; each store owns its own projection and cleanup."""

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

    def _invalid_sources(self, rows, turn_ids=None):
        """Read one canonical snapshot, not a new connection per observation."""
        from contextlib import closing
        from colony_sidecar.turns.idempotency import source_message_hash
        selected = set(turn_ids) if turn_ids is not None else None
        candidates = []
        for row in rows:
            lineage = json.loads(row['source_lineage_json'])
            if selected is None or lineage['turn_id'] in selected:
                candidates.append((row, lineage))
        if not candidates:
            return []
        ids = list(dict.fromkeys(lineage['turn_id'] for _, lineage in candidates))
        current = {}
        with closing(self._ledger()._connect()) as conn:
            conn.execute('BEGIN')
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                sql = ("SELECT turn_id,contact_id,session_id,messages_json FROM turn_sources s "
                       "WHERE scope='person' AND turn_id IN (" + ','.join('?' for _ in batch) + ") "
                       "AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)")
                for source in conn.execute(sql, batch):
                    current[source['turn_id']] = (source['contact_id'], source['session_id'],
                        [source_message_hash(source['session_id'], m) for m in json.loads(source['messages_json'])])
        return [row for row, lineage in candidates
                if current.get(lineage['turn_id']) !=
                (row['contact_id'], lineage['session_id'], lineage['message_hashes'])]

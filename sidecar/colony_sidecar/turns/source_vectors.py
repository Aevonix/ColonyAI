"""Replaceable semantic projections of canonical quotations and image descriptions.

Jobs share the source ledger. Search is scoped before nearest-neighbor selection;
every result is re-read from its current canonical message or asset description.
Vector text is never an authoritative source, and retrieval never resolves truth.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import time
import uuid

FORMAT = 'canonical-source-chunks-v1'
CHUNK = 2000
STRIDE = 1800


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_vector_jobs (
        turn_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL DEFAULT '',
        cursor INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',
        lease_token TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,error TEXT)''')


def enqueue(conn, turn_id):
    conn.execute('''INSERT INTO source_vector_jobs(turn_id) VALUES (?)
        ON CONFLICT(turn_id) DO UPDATE SET generation_id='',cursor=0,status='pending',
        lease_token='',lease_until=0,next_attempt=0,error=NULL''', (turn_id,))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _scope_key(scope, contact_id, session_id):
    return _hash(json.dumps([scope, contact_id, session_id if scope == 'session' else ''], separators=(',', ':')))


def _text(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(block['text'] for block in content if isinstance(block, dict)
                         and block.get('type') in {'text', 'input_text', 'output_text'}
                         and isinstance(block.get('text'), str))
    return ''


def _metadata(source, message_hash, *, kind, text, **extra):
    meta = {'source_projection': FORMAT, 'source_uri': 'turn:' + source['turn_id'],
            'source_turn_id': source['turn_id'], 'source_message_hash': message_hash,
            'contact_id': source['contact_id'], 'session_id': source['session_id'],
            'scope_key': _scope_key(source['scope'], source['contact_id'], source['session_id']),
            'kind': kind, 'content_hash': _hash(text), **extra}
    meta['projection_id'] = 'source-vector:' + _hash(json.dumps(meta, sort_keys=True, separators=(',', ':')))
    return meta


def chunks(conn, source):
    from colony_sidecar.turns.idempotency import source_message_hash
    for message in json.loads(source['messages_json']):
        message_hash = source_message_hash(source['session_id'], message)
        text = _text(message)
        for start in range(0, len(text), STRIDE):
            chunk = text[start:start + CHUNK]
            if chunk.strip():
                yield chunk, _metadata(source, message_hash, kind='source_quote', text=chunk, start=start)
        for asset in conn.execute('''SELECT DISTINCT m.asset_hash,m.description FROM source_media m
            JOIN source_media_links l ON l.asset_hash=m.asset_hash
            WHERE l.turn_id=? AND l.message_hash=? AND m.status='complete' AND m.description IS NOT NULL''',
                                  (source['turn_id'], message_hash)):
            yield asset['description'], _metadata(source, message_hash, kind='media_description',
                text=asset['description'], asset_hash=asset['asset_hash'])


def hydrate(ledger, meta, *, contact_id=None, session_id=None):
    """Validate exact lineage and return current source bytes, never vector text."""
    if meta.get('source_projection') != FORMAT:
        return None
    from colony_sidecar.turns.idempotency import source_message_hash
    with closing(ledger._connect()) as conn:
        source = conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (meta.get('source_turn_id'),)).fetchone()
        if source is None or meta.get('contact_id') != source['contact_id'] or meta.get('session_id') != source['session_id']:
            return None
        if meta.get('source_uri') != 'turn:' + source['turn_id']:
            return None
        if contact_id is not None and (source['contact_id'] != contact_id or
                source['scope'] != 'person' and source['session_id'] != session_id):
            return None
        if meta.get('scope_key') != _scope_key(source['scope'], source['contact_id'], source['session_id']):
            return None
        message = next((m for m in json.loads(source['messages_json'])
                        if source_message_hash(source['session_id'], m) == meta.get('source_message_hash')), None)
        if message is None:
            return None
        common = {'turn_id': source['turn_id'], 'role': message['role'], 'session_id': source['session_id'],
                  'scope': source['scope'], 'occurred_at': source['occurred_at'], 'ingested_at': source['ingested_at'],
                  'source_message_hash': meta['source_message_hash'], 'retrieval_method': 'semantic'}
        if meta.get('kind') == 'source_quote':
            start = meta.get('start')
            if not isinstance(start, int) or start < 0 or start % STRIDE:
                return None
            text = _text(message)[start:start + CHUNK]
            if not text.strip() or _hash(text) != meta.get('content_hash'):
                return None
            return {**common, 'content': text}
        if meta.get('kind') == 'media_description':
            asset = conn.execute('''SELECT m.* FROM source_media m JOIN source_media_links l ON l.asset_hash=m.asset_hash
                WHERE l.turn_id=? AND l.message_hash=? AND m.asset_hash=? AND m.status='complete' LIMIT 1''',
                (source['turn_id'], meta['source_message_hash'], meta.get('asset_hash'))).fetchone()
            if asset is None or not asset['description'] or _hash(asset['description']) != meta.get('content_hash'):
                return None
            return {**common, 'id': 'media:' + asset['asset_hash'], 'kind': 'media_description',
                    'source_uri': 'turn:' + source['turn_id'], 'source_turn_id': source['turn_id'],
                    'asset_id': 'sha256:' + asset['asset_hash'], 'epistemic_state': 'derived_unverified',
                    'description_model': asset['model'], 'description_version': asset['description_version'],
                    'content': asset['description'], 'relevance': 1 / 61}
    return None


class SourceVectors:
    def __init__(self, ledger, store, pipeline):
        self.ledger, self.store, self.pipeline = ledger, store, pipeline

    def backfill(self):
        # Startup only. Source-only projection never replays actions or inference
        # that creates beliefs, affect, authority or commitments.
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('INSERT OR IGNORE INTO source_vector_jobs(turn_id) SELECT turn_id FROM turn_sources')

    def status(self, contact_id):
        catalog = getattr(self.store, 'catalog', None)
        active = catalog.active() if catalog is not None else None
        generation = active['id'] if active else ''
        compatible = bool(active and active['fingerprint'] == self.store.identity.fingerprint)
        with closing(self.ledger._connect()) as conn:
            rows = conn.execute('''SELECT j.status,j.generation_id,j.error FROM turn_sources s
                LEFT JOIN source_vector_jobs j ON j.turn_id=s.turn_id WHERE s.contact_id=?''', (contact_id,)).fetchall()
        complete = sum(row['status'] == 'complete' and row['generation_id'] == generation for row in rows)
        return {'index_state': 'compatible' if compatible else 'unverified_or_unavailable',
                'active_generation': generation or None, 'source_turns': len(rows),
                'projected_turns': complete, 'pending_turns': len(rows) - complete,
                'failed_turns': sum(bool(row['error']) for row in rows)}

    def _claim(self, generation):
        now = time.time()
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT j.* FROM source_vector_jobs j JOIN turn_sources s ON s.turn_id=j.turn_id
                WHERE (j.status<>'complete' OR j.generation_id<>?) AND j.lease_until<=? AND j.next_attempt<=?
                ORDER BY j.next_attempt,s.ingested_at,j.turn_id LIMIT 1''', (generation['id'], now, now)).fetchone()
            if row is None:
                return None
            cursor = row['cursor'] if row['generation_id'] == generation['id'] else 0
            token = uuid.uuid4().hex
            conn.execute("UPDATE source_vector_jobs SET generation_id=?,cursor=?,status='running',lease_token=?,lease_until=? WHERE turn_id=?",
                         (generation['id'], cursor, token, now + 60, row['turn_id']))
            return dict(row, cursor=cursor, generation_id=generation['id'], lease_token=token)

    def _finish(self, job, *, cursor, complete=False, error=None):
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('''UPDATE source_vector_jobs SET cursor=?,status=?,lease_until=0,next_attempt=?,error=?
                WHERE turn_id=? AND lease_token=?''', (cursor, 'complete' if complete else 'pending',
                time.time() + 30 if error else (0 if complete else time.time()),
                error, job['turn_id'], job['lease_token']))

    async def _table(self, generation):
        from colony_sidecar.vector.collections import Collection
        table = await self.store._table(Collection.CONVERSATIONS, write=True, generation=generation)
        if 'scope_key' not in (await table.schema()).names:
            await table.add_columns({'scope_key': 'CAST(NULL AS STRING)'})
        return table

    async def process_one(self, batch_size=16):
        from colony_sidecar.vector.collections import Collection
        from colony_sidecar.vector.indexes import IncompatibleIndex
        if self.store is None or self.pipeline is None or getattr(self.store, 'catalog', None) is None:
            return False
        if self.store.identity != self.pipeline.index_identity:
            return False
        try:
            generation = self.store.catalog.write_generation(self.store.identity)
        except IncompatibleIndex:
            return False  # Unknown legacy stays lexical until an explicit rebuild.
        job = self._claim(generation)
        if job is None:
            return False
        try:
            with closing(self.ledger._connect()) as conn:
                source = conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (job['turn_id'],)).fetchone()
                if source is None:
                    return True
                import itertools
                batch = list(itertools.islice(chunks(conn, source), job['cursor'], job['cursor'] + min(16, max(1, batch_size)) + 1))
            more = len(batch) > min(16, max(1, batch_size))
            batch = batch[:min(16, max(1, batch_size))]
            if batch:
                vectors = await asyncio.wait_for(self.pipeline.embed_batch([text for text, _ in batch]), 40)
                if len(vectors) != len(batch):
                    raise ValueError('source embedding cardinality mismatch')
                for vector in vectors:
                    self.store._validate_vector(vector)
                table = await self._table(generation)
                for (text, meta), vector in zip(batch, vectors):
                    if not self.store._eligible(Collection.CONVERSATIONS, meta['projection_id'], meta):
                        continue
                    now = time.time()
                    meta['embedding_fingerprint'] = self.store.identity.fingerprint
                    row = {'id': meta['projection_id'], 'text': text, 'vector': vector, 'metadata': json.dumps(meta),
                           'scope_key': meta['scope_key'], 'modality': 'text', 'image_hash': '', 'image_ref': '',
                           'thumbnail_ref': '', 'caption': '', 'created_at': now, 'updated_at': now}
                    await table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([row])
                    if not self.store._eligible(Collection.CONVERSATIONS, meta['projection_id'], meta):
                        await self.store.delete(Collection.CONVERSATIONS, meta['projection_id'])
            self._finish(job, cursor=job['cursor'] + len(batch), complete=not more)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._finish(job, cursor=job['cursor'], error=type(exc).__name__)
        return True

    async def search(self, query, *, contact_id, session_id, limit=10):
        from colony_sidecar.vector.collections import Collection
        if not contact_id or self.store is None or self.pipeline is None or getattr(self.store, 'catalog', None) is None:
            return [], []
        # Validate generation before spending inference, and never read an
        # unscoped top-K followed by filtering away another person's results.
        generation = self.store.catalog.read_generation(self.store.identity)
        table = await self.store._table(Collection.CONVERSATIONS, generation=generation)
        if 'scope_key' not in (await table.schema()).names:
            return [], []
        keys = [_scope_key('person', contact_id, session_id), _scope_key('session', contact_id, session_id)]
        vector = await asyncio.wait_for(self.pipeline.embed_query(query), 5)
        hits = await self.store.search(Collection.CONVERSATIONS, vector, limit=min(25, max(1, limit)),
            filter='scope_key IN (' + ','.join(self.store._quoted(key) for key in keys) + ')')
        sources, media = [], []
        for hit in hits:
            current = hydrate(self.ledger, hit.metadata, contact_id=contact_id, session_id=session_id)
            if current is not None:
                (media if current.get('kind') == 'media_description' else sources).append(current)
        return sources, media


def merge_source_hits(lexical, semantic):
    """Fuse ranks from both candidate producers, keeping exact source lineage."""
    scores, rows = {}, {}
    for candidates in (lexical, semantic):
        for rank, row in enumerate(candidates, 1):
            key = (row['turn_id'], row['role'], row['content'])
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            rows[key] = {**rows.get(key, {}), **row}
    return [rows[key] for key in sorted(scores, key=scores.get, reverse=True)]

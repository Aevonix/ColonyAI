"""Explicit participant corrections without rewriting or erasing source text.

The host authorizes the exact old/new people and selected sources. This module
owns the source transaction; contact handles live in their existing store and
are reconciled separately using the same operation ID. No source is inferred
from a name, similarity, session or contact-wide history sweep.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_attribution_operations (
        operation_id TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,
        result_json TEXT NOT NULL,recorded_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS source_attribution_revisions (
        operation_id TEXT NOT NULL,source_id TEXT NOT NULL,old_contact_id TEXT NOT NULL,
        contact_id TEXT NOT NULL,original_digest TEXT NOT NULL,source_version TEXT NOT NULL,
        PRIMARY KEY(operation_id,source_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS attribution_source ON source_attribution_revisions(source_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS source_attribution_invalidations (
        source_id TEXT NOT NULL,corrected_source_id TEXT NOT NULL,operation_id TEXT NOT NULL,
        PRIMARY KEY(source_id,corrected_source_id,operation_id))''')


def correct(ledger, *, operation_id, performed_by, old_contact_id, contact_id,
            source_ids, evidence_refs):
    """Correct an explicit source set and invalidate derived interpretations.

    The original ingest digest, messages, media links, session and timestamps
    remain unchanged. Changing the participant invalidates cached source views
    and their current projections; it does not adopt old judgments for a new
    person. Returned refs let other stores purge their own source projections.
    """
    from apsimo.contacts.identity_links import _refs, _json
    from .idempotency import canonical_turn_digest, SourceErased
    selected, evidence = _refs(source_ids), _refs(evidence_refs)
    if not selected or not evidence or old_contact_id == contact_id:
        raise ValueError('invalid_source_attribution')
    for value in (operation_id, performed_by, old_contact_id, contact_id):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError('invalid_source_attribution')
    request_hash = hashlib.sha256(_json([performed_by, old_contact_id, contact_id, selected, evidence]).encode()).hexdigest()
    with closing(ledger._connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize(conn)
        prior = conn.execute('SELECT * FROM source_attribution_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if prior:
            if prior['request_sha256'] != request_hash:
                raise ValueError('source_attribution_operation_conflict')
            return json.loads(prior['result_json'])
        rows = []
        for sid in selected:
            if conn.execute('SELECT 1 FROM source_erasures WHERE turn_id=?', (sid,)).fetchone():
                raise SourceErased('source_erased')
            row = conn.execute("SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                               (sid, old_contact_id)).fetchone()
            if row is None:
                raise ValueError('source_attribution_preimage_changed')
            rows.append(row)
        # Discover source-linked descendant answers for invalidation, not silent
        # reassignment. Their original prose remains historical source evidence.
        descendants = set()
        pending = set(selected)
        candidates = conn.execute("SELECT turn_id,messages_json FROM turn_sources WHERE contact_id=?", (old_contact_id,)).fetchall()
        while pending:
            found = set()
            for row in candidates:
                if row['turn_id'] in selected or row['turn_id'] in descendants:
                    continue
                if any(ref.get('source_id') in pending for message in json.loads(row['messages_json'])
                       for ref in message.get('_supplied_sources', [])):
                    found.add(row['turn_id'])
            descendants.update(found)
            pending = found
        affected = sorted(set(selected) | descendants)
        for sid in descendants:
            # Existing projection invalidation table owns graph/ToM cleanup.
            for original in selected:
                conn.execute('INSERT OR IGNORE INTO source_projection_erasures VALUES (?,?)', (sid, original))
                conn.execute('INSERT OR IGNORE INTO source_attribution_invalidations VALUES (?,?,?)',
                             (sid, original, operation_id))
        from apsimo.beliefs.source_projection import erase_removed as erase_claims, enqueue
        from apsimo.self_model.perspective import erase_removed as erase_preferences
        from apsimo.self_model.judgments import erase_removed as erase_judgments
        from .source_vectors import enqueue as enqueue_vectors
        for row in rows:
            sid = row['turn_id']
            messages = json.loads(row['messages_json'])
            prior_job = conn.execute('SELECT timezone FROM source_claim_jobs WHERE turn_id=?', (sid,)).fetchone()
            conn.execute('INSERT INTO source_attribution_revisions VALUES (?,?,?,?,?,?)',
                (operation_id, sid, old_contact_id, contact_id, row['content_sha256'], canonical_turn_digest(messages)))
            # Revoke claim-worker leases before changing their input attribution.
            erase_claims(conn, sid, row['session_id'], [])
            erase_preferences(conn, sid, row['session_id'], [])
            erase_judgments(conn, sid, row['session_id'], [])
            conn.execute('UPDATE turn_sources SET contact_id=? WHERE turn_id=?', (contact_id, sid))
            conn.execute('UPDATE turn_ingestion SET response_json=NULL,error=NULL WHERE turn_id=?', (sid,))
            # Preserve admission and timezone. Reviewed source-only imports
            # must not start learning merely because their identity was fixed.
            if prior_job:
                enqueue(conn, sid, messages, scope=row['scope'], timezone_name=prior_job['timezone'])
            enqueue_vectors(conn, sid)
        for sid in descendants:
            row = conn.execute('SELECT session_id FROM turn_sources WHERE turn_id=?', (sid,)).fetchone()
            if row:
                conn.execute('UPDATE turn_ingestion SET response_json=NULL,error=NULL WHERE turn_id=?', (sid,))
                erase_claims(conn, sid, row['session_id'], [])
                erase_preferences(conn, sid, row['session_id'], [])
                erase_judgments(conn, sid, row['session_id'], [])
        try:
            from apsimo.self_model.appraisals import invalidate_source_attribution
        except ModuleNotFoundError as exc:
            if exc.name != 'colony_sidecar.self_model.appraisals':
                raise
        else:
            invalidate_source_attribution(conn, affected, old_contact_id, contact_id)
        result = {'schema': 'SourceAttributionCorrectionV1', 'operation_id': operation_id,
            'old_contact_id': old_contact_id, 'contact_id': contact_id,
            'source_ids': selected, 'affected_source_ids': affected,
            'invalidated_source_ids': sorted(descendants), 'evidence_refs': evidence,
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'performed_by': performed_by,
            'authority_granted': False, 'source_text_preserved': True}
        conn.execute('INSERT INTO source_attribution_operations VALUES (?,?,?,?)',
                     (operation_id, request_hash, _json(result), result['recorded_at']))
        return result


def history(ledger, *, source_id, contact_id):
    """Correction provenance only for the source's currently authorized person."""
    with closing(ledger._connect()) as conn:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', ('source_attribution_revisions',)).fetchone():
            return []
        if not conn.execute('SELECT 1 FROM turn_sources WHERE turn_id=? AND contact_id=?', (source_id, contact_id)).fetchone():
            return []
        rows = conn.execute('''SELECT r.*,o.recorded_at FROM source_attribution_revisions r
            JOIN source_attribution_operations o USING(operation_id) WHERE r.source_id=?
            ORDER BY o.recorded_at,r.operation_id''', (source_id,)).fetchall()
        return [dict(row) for row in rows]


def is_invalidated(conn, source_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_attribution_invalidations'").fetchone():
        return False
    return bool(conn.execute('SELECT 1 FROM source_attribution_invalidations WHERE source_id=? LIMIT 1',
                             (source_id,)).fetchone())


def visible_hits(ledger, hits):
    """Drop stale derived-answer copies, never the corrected direct evidence."""
    with closing(ledger._connect()) as conn:
        return [hit for hit in hits if not is_invalidated(conn, hit.get('turn_id') or hit.get('source_turn_id'))]

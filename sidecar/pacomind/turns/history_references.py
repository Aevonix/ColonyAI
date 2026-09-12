"""Resolve host-observed message hashes to existing scoped source revisions.

No native transcript is uploaded or imported. A missing match is untracked
history, never a grant, a new source, or proof that an erasure did not happen.
"""
from contextlib import closing
import json

from .idempotency import canonical_turn_digest, source_message_hash


def resolve(ledger, *, contact_id, session_id, references):
    wanted = {(ref['session_id'], ref['message_hash']) for ref in references}
    matches = {key: {'session_id': key[0], 'message_hash': key[1],
                     'source_refs': [], 'erased': False} for key in wanted}
    with closing(ledger._connect()) as conn:
        # The request's current viewer/session controls visibility. The native
        # session supplied for matching does not broaden that permission.
        for origin in sorted({key[0] for key in wanted}):
            rows = conn.execute('''SELECT * FROM turn_sources WHERE contact_id=?
                AND session_id=? AND (scope='person' OR session_id=?)
                AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i
                    WHERE i.source_id=turn_sources.turn_id)''',
                (contact_id, origin, session_id)).fetchall()
            for row in rows:
                messages = json.loads(row['messages_json'])
                ref = {'source_id': row['turn_id'], 'source_version': canonical_turn_digest(messages)}
                for message in messages:
                    key = origin, source_message_hash(origin, message)
                    if key in matches and ref not in matches[key]['source_refs']:
                        matches[key]['source_refs'].append(ref)
            erased = conn.execute('''SELECT e.message_hashes_json FROM source_erasures e
                LEFT JOIN source_erasure_revisions r USING(sequence)
                WHERE e.contact_id=? AND e.session_id=?
                AND (r.source_scope='person' OR e.session_id=? OR r.sequence IS NULL)''',
                (contact_id, origin, session_id)).fetchall()
            for row in erased:
                for message_hash in json.loads(row['message_hashes_json']):
                    if (origin, message_hash) in matches:
                        matches[origin, message_hash]['erased'] = True
    return [matches[key] for key in sorted(matches)]

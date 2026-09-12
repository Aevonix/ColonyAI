"""Canonical source bindings for automatically learned standing directives.

Only references persist in the existing directive metadata column. Current
source bytes supply the exact rule; correction or erasure withdraws its effect.
"""
from contextlib import closing
import hashlib
import json

from pacomind.turns.idempotency import canonical_turn_digest, source_message_hash
from pacomind.turns.audio import source_text


def _message(ledger, reference):
    if ledger is None:
        return None
    identifier = reference['source_turn_id']
    if ledger.is_projection_erased(identifier):
        return None
    with closing(ledger._connect()) as db:
        row = db.execute("SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                         (identifier, reference['contact_id'])).fetchone()
        if row is None:
            return None
        from pacomind.turns.source_attribution import is_invalidated
        if is_invalidated(db, identifier):
            return None
        messages = json.loads(row['messages_json'])
        if canonical_turn_digest(messages) != reference['source_version']:
            return None
        message_hash = reference['message_hash']
        # Existing correction annotations are tied to exact source messages.
        if db.execute('SELECT 1 FROM source_annotations a,json_each(a.target_message_hashes_json) h '
                      'WHERE a.target_source_id=? AND h.value=? LIMIT 1', (identifier, message_hash)).fetchone():
            return None
        for message in messages:
            if message.get('role') == 'user' and source_message_hash(row['session_id'], message) == message_hash:
                return source_text(message.get('content'))
    return None


def bind(ledger, *, source_id, contact_id, message, clause):
    if ledger is None or not source_id or not contact_id:
        return None
    with closing(ledger._connect()) as db:
        row = db.execute("SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                         (source_id, contact_id)).fetchone()
    if row is None:
        return None
    messages = json.loads(row['messages_json'])
    for item in messages:
        text = source_text(item.get('content'))
        if item.get('role') != 'user' or text.strip() != message.strip():
            continue
        start = text.find(clause)
        if start < 0:
            continue
        reference = {'source_turn_id': source_id, 'contact_id': contact_id,
            'source_version': canonical_turn_digest(messages),
            'message_hash': source_message_hash(row['session_id'], item),
            'start': start, 'end': start+len(clause),
            'clause_sha256': hashlib.sha256(clause.encode()).hexdigest()}
        if _message(ledger, reference) is not None:
            return reference
    return None


def hydrate(ledger, directive):
    reference = directive.evidence
    if reference is None:
        return directive  # Explicit manual/configuration and legacy rows.
    text = _message(ledger, reference)
    if text is None:
        return None
    clause = text[reference['start']:reference['end']]
    if hashlib.sha256(clause.encode()).hexdigest() != reference['clause_sha256']:
        return None
    from .extractor import extract_directives, is_revocation
    # Re-evaluate the full current source scope, not just a detached quotation.
    rules = [item for item in extract_directives(text) if not is_revocation(item)
             and item.raw_text == clause and item.polarity == directive.polarity]
    if len(rules) != 1:
        return None
    current = rules[0]
    directive.subject, directive.raw_text = current.subject, current.raw_text
    directive.match_terms, directive.level = current.match_terms, current.level
    return directive

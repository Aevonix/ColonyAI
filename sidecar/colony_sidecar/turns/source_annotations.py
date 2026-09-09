"""Attributed source corrections in the existing source ledger and recall packet."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3


def initialize(conn):
    # No correction text is duplicated here. These references survive erasure
    # so deleting an annotation cannot silently revive its unqualified source.
    conn.execute('''CREATE TABLE IF NOT EXISTS source_annotations (
        annotation_source_id TEXT PRIMARY KEY, target_source_id TEXT NOT NULL,
        target_version TEXT NOT NULL, target_message_hashes_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS source_annotation_target ON source_annotations(target_source_id)')


def append(ledger, *, contact_id, session_id, annotation_id, source_id, source_version,
           excerpt, correction, author_principal):
    from .idempotency import canonical_turn_digest, source_message_hash, SourceErased
    from .audio import source_text
    for value, maximum in ((contact_id, 256), (session_id, 256), (annotation_id, 128),
                           (source_id, 256), (excerpt, 4096), (correction, 4096),
                           (author_principal, 256)):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('invalid_source_annotation')
    if not isinstance(source_version, str) or not re.fullmatch('[0-9a-f]{64}', source_version):
        raise ValueError('invalid_source_annotation')
    identity = [contact_id, author_principal, annotation_id]
    annotation_source = 'source-annotation:' + canonical_turn_digest(identity)
    target = {'source_id': source_id, 'source_version': source_version}
    request_digest = canonical_turn_digest([identity, session_id, target, excerpt, correction])
    with closing(ledger._connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute('SELECT * FROM source_annotations WHERE annotation_source_id=?',
                             (annotation_source,)).fetchone()
        if prior and prior['request_sha256'] != request_digest:
            raise ValueError('annotation_id_conflict')
        if conn.execute('SELECT 1 FROM source_erasures WHERE turn_id=?', (annotation_source,)).fetchone():
            raise SourceErased('source_erased')
        row = conn.execute('''SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?
            AND (scope='person' OR session_id=?)''', (source_id, contact_id, session_id)).fetchone()
        if row is None:
            raise ValueError('source_not_found')
        messages = json.loads(row['messages_json'])
        if canonical_turn_digest(messages) != source_version:
            raise ValueError('source_version_mismatch')
        matched = [source_message_hash(row['session_id'], m) for m in messages
                   if excerpt in source_text(m.get('content'))]
        if not matched:
            raise ValueError('source_excerpt_mismatch')
        if prior:
            stored = conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                  (annotation_source,)).fetchone()
            if stored is None:
                raise SourceErased('source_erased')
            annotation_messages = json.loads(stored['messages_json'])
        else:
            at = datetime.now(timezone.utc).isoformat()
            content = {'target': target, 'excerpt': excerpt, 'correction': correction,
                       'author_principal': author_principal, 'recorded_at': at,
                       'state': 'attributed_correction'}
            annotation_messages = [{'role': 'assistant', 'content': json.dumps(content, ensure_ascii=False),
                                    '_supplied_sources': [target]}]
            # The dedicated method owns authorship, source and relation in one
            # transaction. Ordinary turn metadata cannot create a correction.
            ledger._validate_dependencies(conn, annotation_source, contact_id, session_id, annotation_messages)
            digest = canonical_turn_digest({'messages': annotation_messages, 'contact_id': contact_id,
                'session_id': session_id, 'scope': row['scope'], 'occurred_at': at})
            try:
                conn.execute('''INSERT INTO turn_sources
                    (turn_id,content_sha256,contact_id,session_id,scope,messages_json,occurred_at)
                    VALUES (?,?,?,?,?,?,?)''', (annotation_source, digest, contact_id, session_id,
                        row['scope'], json.dumps(annotation_messages, ensure_ascii=True, sort_keys=True,
                                                 separators=(',', ':')), at))
            except sqlite3.IntegrityError as exc:
                raise ValueError('annotation_id_conflict') from exc
            ledger._index_messages(conn, annotation_source, annotation_messages)
            from .source_vectors import enqueue
            enqueue(conn, annotation_source)
            conn.execute('INSERT INTO source_annotations VALUES (?,?,?,?,?)',
                (annotation_source, source_id, source_version, json.dumps(matched), request_digest))
        return {'accepted': True, 'created': prior is None, 'source_id': annotation_source,
                'source_version': canonical_turn_digest(annotation_messages), 'target': target}


def source_ids(row):
    ids = list(row.get('source_turn_ids') or [])
    if row.get('source_turn_id'):
        ids.append(row['source_turn_id'])
    uri = str(row.get('source_uri') or '')
    if uri.startswith('turn:'):
        ids.append(uri[5:])
    return list(dict.fromkeys(ids))


def expand(ledger, candidates, *, contact_id, session_id, covered=()):
    """Bundle exact revision corrections before ranking and character packing.

    A source-backed belief/conflict bundle and a derived answer are subject to
    the same rule as direct lexical/semantic excerpts. All discovered notes
    remain attributed evidence; the newest note does not automatically win.
    """
    from .idempotency import canonical_turn_digest, source_message_hash
    from .audio import source_text
    result = []
    with closing(ledger._connect()) as conn:
        cached = {}

        def source(identifier):
            if identifier not in cached:
                row = conn.execute('''SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?
                    AND (scope='person' OR session_id=?)''', (identifier, contact_id, session_id)).fetchone()
                cached[identifier] = dict(row) if row else None
                if row:
                    cached[identifier]['messages'] = json.loads(row['messages_json'])
            return cached[identifier]

        def candidate_hashes(candidate, identifier):
            # Assertion bundles carry canonical claim membership, not a JSON
            # string to reinterpret. Semantic excerpts already have one hash.
            if '_source_message_hashes' in candidate:
                return set(candidate['_source_message_hashes'].get(identifier, []))
            if candidate.get('source_message_hash'):
                return {candidate['source_message_hash']}
            if candidate.get('kind') == 'source_quote' or candidate.get('source_turn_id'):
                row = source(identifier)
                text, role = candidate.get('content'), candidate.get('role')
                return {source_message_hash(row['session_id'], message) for message in row['messages']
                        if (not role or message.get('role') == role)
                        and isinstance(text, str) and text
                        and text in source_text(message.get('content'))} if row else set()
            # Older graph summaries and supplied parent references attest a
            # whole source version. Do not invent finer ancestry from prose.
            return None

        for original in candidates:
            ids = source_ids(original)
            pending = [(identifier, candidate_hashes(original, identifier)) for identifier in ids]
            visited, notes = {}, {}
            unavailable = False
            while pending:
                identifier, selected_hashes = pending.pop()
                row = source(identifier)
                if row is None:
                    continue
                version = canonical_turn_digest(row['messages'])
                hashes = {source_message_hash(row['session_id'], m) for m in row['messages']}
                selected_hashes = hashes if selected_hashes is None else hashes.intersection(selected_hashes)
                selected_hashes -= visited.get(identifier, set())
                if not selected_hashes:
                    continue
                visited.setdefault(identifier, set()).update(selected_hashes)
                for relation in conn.execute('SELECT * FROM source_annotations WHERE target_source_id=?', (identifier,)):
                    # A partial erase can change the source revision while
                    # retaining the annotated message. Its removed correction
                    # still fences that message; unrelated survivors remain.
                    same_version = relation['target_version'] == version
                    retained_message = selected_hashes.intersection(json.loads(relation['target_message_hashes_json']))
                    if not retained_message:
                        continue
                    annotation = source(relation['annotation_source_id'])
                    if annotation is None or not same_version:
                        unavailable = True
                        break
                    data = json.loads(annotation['messages'][0]['content'])
                    notes[relation['annotation_source_id']] = data
                    pending.append((relation['annotation_source_id'], None))
                if unavailable:
                    break
                own_annotation = conn.execute('SELECT * FROM source_annotations WHERE annotation_source_id=?',
                                              (identifier,)).fetchone()
                for message in row['messages']:
                    if (message.get('role') == 'assistant'
                            and source_message_hash(row['session_id'], message) in selected_hashes):
                        for ref in message.get('_supplied_sources', []):
                            parent = source(ref['source_id'])
                            if parent and canonical_turn_digest(parent['messages']) == ref['source_version']:
                                # Dedicated annotations have exact target message
                                # membership. Their own source link must not widen
                                # a selected excerpt back to all sibling messages.
                                target_hashes = (set(json.loads(own_annotation['target_message_hashes_json']))
                                    if own_annotation and own_annotation['target_source_id'] == ref['source_id']
                                    and own_annotation['target_version'] == ref['source_version'] else None)
                                pending.append((ref['source_id'], target_hashes))
            if unavailable:
                continue
            if not notes:
                # An empty correction set is also a selection-time snapshot.
                # A first note can arrive while ranking awaits; preserve exact
                # message membership so a sibling's note does not hide this row.
                row = dict(original)
                if visited:
                    row['_annotation_source_refs'] = [{'source_id': identifier,
                        'source_version': canonical_turn_digest(source(identifier)['messages'])}
                        for identifier in visited]
                    row['_annotation_ids'] = ()
                    row['_annotation_message_hashes'] = {identifier: sorted(hashes)
                        for identifier, hashes in visited.items()}
                result.append(row)
                continue
            row = dict(original)
            if (original.get('kind') == 'belief'
                    or 'kind' not in original and not original.get('source_turn_id')):
                row['_recall_memory_id'] = original.get('_recall_memory_id', original['id'])
            # If the annotation itself matched the query, show its exact
            # target excerpt first instead of presenting an orphan note.
            if len(ids) == 1 and ids[0] in notes:
                note = notes[ids[0]]
                original_evidence = {'source': note['target'], 'quote': note['excerpt']}
                row['_annotation_only'] = True
            else:
                original_evidence = {'source_ids': ids, 'content': str(original.get('content') or '')}
            corrections = [dict(data, source_id=identifier) for identifier, data in sorted(
                notes.items(), key=lambda pair: (pair[1]['recorded_at'], pair[0]))]
            row.update(content=json.dumps({'original': original_evidence, 'corrections': corrections,
                'interpretation': 'Read the original in light of these attributed corrections; conflicting corrections remain unresolved.'},
                ensure_ascii=False), atomic_evidence=True, epistemic_state='correction_evidence',
                source_turn_ids=list(dict.fromkeys(ids + sorted(visited) + list(notes))))
            row['_annotation_source_refs'] = [{'source_id': identifier,
                'source_version': canonical_turn_digest(source(identifier)['messages'])}
                for identifier in row['source_turn_ids'] if source(identifier)]
            row['_annotation_ids'] = tuple(sorted(notes))
            row['_annotation_message_hashes'] = {identifier: sorted(hashes) for identifier, hashes in visited.items()}
            row['ranking_text'] = '\n'.join(
                'Attributed correction (not independently verified):\n' + note['correction']
                for note in corrections) + '\nOriginal evidence:\n' + str(
                    original.get('ranking_text') or original.get('content') or '')
            row['id'] = 'annotated:' + hashlib.sha256(json.dumps([original['id'], list(notes)], sort_keys=True).encode()).hexdigest()
            result.append(row)
    # A lexical/semantic annotation hit adds no evidence when an original
    # excerpt already carries the same complete correction set. Preserve all
    # distinct original excerpts; this is not content or semantic merging.
    seen = {row['_annotation_ids'] for row in [*covered, *result]
            if row.get('_annotation_ids') and not row.get('_annotation_only')}
    retained = []
    for row in result:
        if row.get('_annotation_only'):
            if row['_annotation_ids'] in seen:
                continue
            seen.add(row['_annotation_ids'])
        retained.append(row)
    return retained


def current_candidates(ledger, candidates, *, contact_id, session_id):
    """Do not publish stale evidence after a correction changes during ranking."""
    required = [ref for row in candidates for ref in row.get('_annotation_source_refs', [])]
    if not required:
        return candidates
    current = {r['source_id']: r['source_version'] for r in ledger.source_references(
        [r['source_id'] for r in required], contact_id=contact_id, session_id=session_id)}
    notes = {}
    with closing(ledger._connect()) as conn:
        for identifier, version in current.items():
            notes[identifier] = {row[0]: set(json.loads(row[1])) for row in conn.execute('''SELECT annotation_source_id,target_message_hashes_json
                FROM source_annotations WHERE target_source_id=? AND target_version=?''', (identifier, version))}
    return [row for row in candidates if all(current.get(ref['source_id']) == ref['source_version']
            for ref in row.get('_annotation_source_refs', [])) and (
                not row.get('_annotation_source_refs') or set(row['_annotation_ids']) == set().union(
                    *({identifier for identifier, hashes in notes.get(ref['source_id'], {}).items()
                       if hashes.intersection(row['_annotation_message_hashes'].get(ref['source_id'], []))}
                      for ref in row['_annotation_source_refs'])))]

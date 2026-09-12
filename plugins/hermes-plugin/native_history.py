"""Reconcile an already-authorized native history result before model exposure.

This is a read projection. It never edits Hermes history, indexes or backups.
"""
import copy
import json
import sqlite3
import time

from .client import source_message_hash
from .request_memory import erased_turn_indices

_MODES = {'discover', 'read', 'scroll', 'browse'}
_FIELDS = ('messages', 'bookend_start', 'bookend_end')
_UNAVAILABLE = json.dumps({'success': False, 'error':
    'Native history could not be checked against current memory erasures. No history was exposed.'})


def _entries(payload):
    return payload['results'] if payload['mode'] in {'discover', 'browse'} else [payload]


def native_rows(payload, args):
    """Full bytes only for returned IDs and their bounded originating turns.

    Native search truncates excerpts and can return a tool row without its user
    anchor. Resolve that exact row's own turn, not the entire session/database.
    Explicit and fallback profile selection follows the completed native result.
    """
    from hermes_state import SessionDB, _default_db_path
    from hermes_cli import profiles
    profile = payload.get('profile') or args.get('profile')
    if not profile and '/' in str(args.get('session_id') or ''):
        profile = args['session_id'].partition('/')[0]
    if profile:
        name = profiles.normalize_profile_name(profile)
        profiles.validate_profile_name(name)
        path = profiles.get_profile_dir(name)/'state.db'
    else:
        path = _default_db_path()
    ids = set()
    for entry in _entries(payload):
        for field in _FIELDS:
            ids.update(row['id'] for row in entry.get(field, []) if isinstance(row, dict) and type(row.get('id')) is int)
        if type(entry.get('match_message_id')) is int:
            ids.add(entry['match_message_id'])
    if len(ids) > 512:
        raise ValueError('native_history_result_too_large')
    rows, spans = {}, {}
    with sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True, timeout=.25) as db:
        db.row_factory = sqlite3.Row
        for native_id in sorted(ids):
            row = db.execute('SELECT id,session_id,role,content FROM messages WHERE id=?', (native_id,)).fetchone()
            if row is None:
                raise ValueError('native_history_row_missing')
            row = dict(row)
            row['content'] = SessionDB._decode_content(row['content'])
            rows[native_id] = row
            origin = row['session_id']
            start = db.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND role='user' AND id<=?", (origin,native_id)).fetchone()[0]
            if start is None:
                raise ValueError('native_history_turn_unresolved')
            end = db.execute("SELECT MIN(id) FROM messages WHERE session_id=? AND role='user' AND id>?", (origin,start)).fetchone()[0]
            key = origin, start
            if key not in spans:
                # Only user/final-assistant text determines source ancestry.
                # Raw tool results from outside the selected window are never read.
                anchors = [dict(r) for r in db.execute('''SELECT id,session_id,role,content FROM messages
                    WHERE session_id=? AND id>=? AND (? IS NULL OR id<?)
                    AND role IN ('user','assistant') AND content IS NOT NULL
                    AND content!='' ORDER BY id LIMIT 513''', (origin,start,end,end))]
                if not anchors or len(anchors) > 512:
                    raise ValueError('native_history_turn_unresolved')
                for anchor in anchors:
                    anchor['content'] = SessionDB._decode_content(anchor['content'])
                spans[key] = {'start':start,'end':end,'rows':anchors}
            row['span'] = key
    if sum(len(span['rows']) for span in spans.values()) > 512:
        raise ValueError('native_history_ancestry_too_large')
    return rows, spans


def project(payload, rows, spans, rules, matches):
    """Keep unrelated turns byte-for-byte; omit exact erased turn fragments."""
    by_ref = {(r['session_id'],r['message_hash']):r for r in matches}
    denied_spans = set()
    for key, span in spans.items():
        if erased_turn_indices(span['rows'], rules) or any(
                by_ref.get((row['session_id'],source_message_hash(row['session_id'],row)),{}).get('erased')
                for row in span['rows']):
            denied_spans.add(key)
    denied = {native_id for native_id,row in rows.items() if row['span'] in denied_spans}
    result = copy.deepcopy(payload)
    refs, untracked = {}, set()
    for entry in _entries(result):
        # Native titles and browse previews have no exact message identity.
        # Keep navigation metadata; opening a session supplies traceable rows.
        entry.pop('title',None)
        entry.pop('preview',None)
        if isinstance(entry.get('session_meta'),dict):
            entry['session_meta'].pop('title',None)
        if entry.get('matched_role') == 'session_title':
            entry.pop('snippet',None)
        if result['mode'] == 'discover' and entry.get('match_message_id') in denied:
            continue
        affected = any(row.get('id') in denied for field in _FIELDS for row in entry.get(field,[]))
        affected |= entry.get('match_message_id') in denied
        # Session titles summarize many turns without source lineage. Omit that
        # metadata after an erasure in the session; keep unrelated message rows.
        affected |= any(rule['session_id'] == entry.get('session_id') for rule in rules)
        for field in _FIELDS:
            if field in entry:
                entry[field] = [r for r in entry[field] if r.get('id') not in denied]
                for shaped in entry[field]:
                    native = rows.get(shaped.get('id'))
                    if native is None:
                        raise ValueError('native_history_message_identity_missing')
                    # Parent source refs include the originating turn's evidence,
                    # so a tool-arguments-only window cannot lose its ancestry.
                    for original in spans[native['span']]['rows']:
                        match = by_ref.get((original['session_id'],source_message_hash(original['session_id'],original)),{})
                        if not match.get('source_refs'):
                            untracked.add(original['id'])
                        for ref in match.get('source_refs',[]):
                            refs[ref['source_id'],ref['source_version']] = ref
        if affected:
            entry.pop('snippet',None)
            entry.pop('title',None)
            if isinstance(entry.get('session_meta'),dict):
                entry['session_meta'].pop('title',None)
            entry['memory_erasure_omitted'] = True
    if result['mode'] == 'discover':
        result['results'] = [entry for entry in result['results'] if entry.get('match_message_id') not in denied]
        result['count'] = len(result['results'])
    result['pacomind_native_history_read_v1'] = True
    result['memory_erasure'] = {'scope':'known_canonical_sources_and_exact_native_turns',
        'omitted_native_rows':len(denied), 'untracked_native_rows':len(untracked),
        'untracked_titles_and_previews':'omitted; open a session for message evidence',
        'physical_history_erased':False}
    return result, list(refs.values())


def reconcile(args, result, scope, context, memory):
    try:
        payload = json.loads(result)
        if not isinstance(payload,dict) or payload.get('success') is not True or payload.get('mode') not in _MODES:
            return result
        rows, spans = native_rows(payload,args)
        refs = { (r['session_id'],source_message_hash(r['session_id'],r))
                 for span in spans.values() for r in span['rows'] }
        deadline = time.monotonic()+.25
        watermark = memory.outbox.erasure_watermark(scope.contact_id, deadline_monotonic=deadline)
        if refs:
            response = memory.client.post('/v1/host/memory/sources/erasures',
                json={'contact_id':scope.contact_id,'session_id':scope.session_id,'after':watermark,
                      'source_refs':[], 'native_history_refs':[{'session_id':s,'message_hash':h} for s,h in sorted(refs)]},
                timeout=.25, _deadline_monotonic=deadline)
        else:
            response = memory.client.get('/v1/host/memory/sources/erasures',
                params={'contact_id':scope.contact_id,'after':watermark},timeout=.25,_deadline_monotonic=deadline)
        response.raise_for_status()
        page = response.json()
        memory.outbox.apply_erasure_page(scope.contact_id,page,deadline_monotonic=deadline)
        watermark,rules = memory.outbox.erasure_state(scope.contact_id,deadline_monotonic=deadline)
        if page.get('complete') is not True or int(page['head']) != int(page['through']) or watermark != int(page['head']):
            return _UNAVAILABLE
        matched = page.get('native_history_matches',[])
        if refs != {(r['session_id'],r['message_hash']) for r in matched}:
            return _UNAVAILABLE
        filtered, source_refs = project(payload,rows,spans,rules,matched)
        text = json.dumps(filtered,ensure_ascii=False)
        if not memory.register_source_read(scope,context.get('tool_call_id'),text,
                {'watermark':watermark,'source_refs':source_refs}):
            return _UNAVAILABLE
        return text
    except Exception:
        return _UNAVAILABLE

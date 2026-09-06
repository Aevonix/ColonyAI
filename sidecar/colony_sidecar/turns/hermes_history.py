"""Import reviewed Hermes DM source quotations without replaying turn effects.

Run with --dry-run first. Input must be a stable SQLite backup, not a live WAL
database. No Hermes runtime, model client, network fetch or ordinary turn API is
invoked. Unmapped history remains in its original database.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .idempotency import TurnIdempotencyLedger, SourceErased, canonical_turn_digest

AUTOMATION = {'cron', 'subagent', 'webhook', 'api_server', 'api', 'cli', 'tool', 'system'}
OPTIONAL_MESSAGE = {'active': '1', 'compacted': '0', '_compressed_summary': '0',
    'observed': '0', 'tool_calls': 'NULL', 'finish_reason': 'NULL',
    'effect_disposition': 'NULL', 'display_kind': 'NULL', 'platform_message_id': 'NULL'}


def mapping_document(path):
    return validate_mapping(json.loads(Path(path).read_text()))


def validate_mapping(value):
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('namespace'), str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', value['namespace']):
        raise ValueError('Expected a version 1 mapping with a stable database namespace')
    bindings = value.get('bindings')
    if not isinstance(bindings, list) or not bindings or len(bindings) > 500:
        raise ValueError('Provide 1..500 explicitly reviewed session bindings')
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError('Every binding must be an object')
        for field in ('session_id', 'platform', 'actor_id', 'chat_id', 'contact_id'):
            if not isinstance(binding.get(field), str) or not binding[field].strip():
                raise ValueError('Every binding needs session, platform, actor, chat and contact IDs')
        if binding['session_id'] in seen:
            raise ValueError('A session cannot have multiple actor bindings')
        seen.add(binding['session_id'])
        if binding['platform'] in AUTOMATION or binding.get('chat_type') != 'dm':
            raise ValueError('This importer accepts explicitly attributed direct messages only')
        if not binding.get('review_evidence'):
            raise ValueError('Every binding requires an independent review evidence reference')
    return value


def open_snapshot(path):
    path = Path(path).resolve(strict=True)
    wal = path.with_name(path.name + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Use a consistent SQLite backup, not an active WAL database')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn, digest


def validate_bindings(conn, mapping):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(sessions)')}
    required = {'id', 'source', 'user_id', 'chat_id', 'chat_type'}
    if not required <= columns:
        raise ValueError('Hermes session schema lacks required origin fields; do not infer missing identities')
    messages = {r['name'] for r in conn.execute('PRAGMA table_info(messages)')}
    if not {'id', 'session_id', 'role', 'content', 'timestamp'} <= messages:
        raise ValueError('Unsupported Hermes message schema')
    sessions = {}
    for binding in mapping['bindings']:
        row = conn.execute('SELECT * FROM sessions WHERE id=?', (binding['session_id'],)).fetchone()
        if row is None:
            raise ValueError('A reviewed session is missing from this snapshot')
        row = dict(row)
        for field, actual in (('platform', 'source'), ('actor_id', 'user_id'), ('chat_id', 'chat_id'), ('chat_type', 'chat_type')):
            if row[actual] != binding[field]:
                raise ValueError('A reviewed session no longer matches its exact transport binding')
        origin = row.get('origin_json')
        if origin:
            try:
                origin = json.loads(origin)
            except (TypeError, ValueError):
                raise ValueError('Invalid recorded session origin') from None
            if not isinstance(origin, dict):
                raise ValueError('Invalid recorded session origin')
            for field, actual in (('platform', 'platform'), ('user_id', 'actor_id'), ('chat_id', 'chat_id'), ('chat_type', 'chat_type')):
                if origin.get(field) and origin[field] != binding[actual]:
                    raise ValueError('Session origin conflicts with the reviewed binding')
        sessions[row['id']] = row
    return sessions, messages


def source_message(row, session, binding, namespace):
    """Classify stored messages without treating their role as actor evidence."""
    if row['role'] not in {'user', 'assistant'}:
        return None, 'non_conversation_role'
    if row['_compressed_summary']:
        return None, 'generated_summary'
    if not row['active'] and not row['compacted']:
        return None, 'rewound_or_inactive'
    if row['observed']:
        return None, 'ambient_observation'
    if row['effect_disposition'] or row['display_kind']:
        return None, 'special_message_metadata'
    if row['tool_calls'] not in (None, '', '[]') or row['finish_reason'] in {'tool_calls', 'function_call'}:
        return None, 'tool_call_message'
    content = row['content']
    if not isinstance(content, str) or not content.strip():
        return None, 'empty_or_unsupported_content'
    # Hermes JSON-encodes multimodal block lists in this TEXT column. Retain
    # exact text-only lists; leave attachment history in the original database
    # rather than fetch URLs, flatten blocks, or schedule old vision jobs.
    if content.lstrip().startswith('['):
        try:
            decoded = json.loads(content)
        except ValueError:
            decoded = None
        if isinstance(decoded, list) and decoded and all(isinstance(b, dict) and 'type' in b for b in decoded):
            if any(b['type'] not in {'text', 'input_text', 'output_text'} or not isinstance(b.get('text'), str) for b in decoded):
                return None, 'historical_attachment_unretained'
            content = decoded
    timestamp = row['timestamp']
    try:
        occurred_at = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat() if math.isfinite(float(timestamp)) else None
    except (ValueError, TypeError, OverflowError, OSError):
        occurred_at = None
    provenance = {'kind': 'hermes_history', 'database_namespace': namespace,
        'message_id': row['id'], 'session_id': row['session_id'],
        'platform': binding['platform'], 'actor_id': binding['actor_id'],
        'chat_id': binding['chat_id'], 'actor_basis': 'reviewed_direct_session',
        'speaker': 'contact' if row['role'] == 'user' else 'assistant',
        'timestamp_basis': 'hermes_recorded_timestamp' if occurred_at else 'unknown',
        'platform_message_id': row['platform_message_id'],
        'session_model_hint': session.get('model'), 'message_model_revision': 'unknown',
        'model_basis': 'session_hint_not_per_message_attribution',
        'review_evidence': binding['review_evidence'], 'learning': 'source_only'}
    return ({'role': row['role'], 'content': content, 'provenance': provenance}, occurred_at), None


def _progress(ledger):
    with closing(ledger._connect()) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS source_import_progress (
            run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
            mapping_sha256 TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
            counts_json TEXT NOT NULL DEFAULT '{}')''')


def import_history(database, mapping, *, state_dir=None, apply=False, limit=1000):
    mapping = validate_mapping(mapping)
    if not 1 <= limit <= 10000:
        raise ValueError('Batch limit must be between 1 and 10000 messages')
    if apply and (mapping.get('reviewed') is not True or state_dir is None):
        raise ValueError('Apply requires a reviewed mapping and an explicit target state directory')
    if state_dir is not None and Path(database).resolve() == (Path(state_dir) / 'turn-idempotency.db').resolve():
        raise ValueError('Source snapshot and destination ledger must be different files')
    conn, snapshot_hash = open_snapshot(database)
    with closing(conn):
        sessions, columns = validate_bindings(conn, mapping)
        mapping_hash = canonical_turn_digest(mapping)
        run_id = canonical_turn_digest([mapping['namespace'], snapshot_hash, mapping_hash])
        ledger = TurnIdempotencyLedger(Path(state_dir) / 'turn-idempotency.db') if apply else None
        cursor, counts = 0, Counter()
        if ledger:
            _progress(ledger)
            with closing(ledger._connect()) as progress, progress:
                progress.execute('INSERT OR IGNORE INTO source_import_progress(run_id,namespace,snapshot_sha256,mapping_sha256) VALUES (?,?,?,?)',
                                 (run_id, mapping['namespace'], snapshot_hash, mapping_hash))
                previous = progress.execute('SELECT cursor,counts_json FROM source_import_progress WHERE run_id=?', (run_id,)).fetchone()
                cursor, counts = previous['cursor'], Counter(json.loads(previous['counts_json']))
        selected = 'id,session_id,role,content,timestamp,' + ','.join(
            key if key in columns else default + ' AS ' + key for key, default in OPTIONAL_MESSAGE.items())
        ids = list(sessions)
        placeholders = ','.join('?' for _ in ids)
        bindings = {b['session_id']: b for b in mapping['bindings']}
        query = f'SELECT {selected} FROM messages WHERE session_id IN ({placeholders}) AND id>? ORDER BY id'
        rows = conn.execute(query + (' LIMIT ?' if apply else ''), (*ids, cursor, limit) if apply else (*ids, cursor))
        for row in rows:
            previous_cursor = cursor
            row = dict(row)
            binding = bindings[row['session_id']]
            prepared, reason = source_message(row, sessions[row['session_id']], binding, mapping['namespace'])
            if reason:
                counts[reason] += 1
            elif ledger:
                message, occurred_at = prepared
                turn_id = f"hermes-history:{mapping['namespace']}:{row['id']}"
                try:
                    created = ledger.record_source(turn_id, contact_id=binding['contact_id'],
                        session_id=row['session_id'], messages=[message], scope='person',
                        occurred_at=occurred_at, derive_claims=False)
                    counts['retained_new' if created else 'retained_existing'] += 1
                except SourceErased:
                    counts['erased_not_restored'] += 1
            else:
                counts['eligible_source_quotations'] += 1
            cursor = row['id']
            if ledger:
                # Source commit precedes cursor acknowledgement. A crash in
                # between safely repeats record_source, never ordinary effects.
                with closing(ledger._connect()) as progress, progress:
                    updated = progress.execute('UPDATE source_import_progress SET cursor=?,counts_json=? WHERE run_id=? AND cursor=?',
                                               (cursor, json.dumps(counts), run_id, previous_cursor))
                    if updated.rowcount != 1:
                        raise ValueError('Another importer advanced this run; resume with one process')
        remaining = conn.execute(f'SELECT count(*) FROM messages WHERE session_id IN ({placeholders}) AND id>?', (*ids, cursor)).fetchone()[0]
        unbound = [dict(r) for r in conn.execute(f'''SELECT s.source,count(DISTINCT s.id) sessions,count(m.id) messages
            FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
            WHERE s.id NOT IN ({placeholders}) GROUP BY s.source''', ids)]
        return {'mode': 'apply' if apply else 'dry_run', 'run_id': run_id,
            'snapshot_sha256': snapshot_hash, 'mapping_sha256': mapping_hash,
            'bound_sessions': len(sessions), 'cursor': cursor, 'remaining': remaining,
            'complete': remaining == 0, 'counts': dict(counts),
            'unmapped_retained_in_hermes': unbound,
            'learning_replayed': False, 'models_called': False,
            'projection': 'canonical_source_fts_and_queued_semantic_text_only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--mapping', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--limit', type=int, default=1000)
    args = parser.parse_args()
    try:
        result = import_history(args.database, mapping_document(args.mapping), state_dir=args.state_dir,
                                apply=args.apply, limit=args.limit)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'History import stopped: {exc}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

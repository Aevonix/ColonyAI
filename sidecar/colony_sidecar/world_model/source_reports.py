"""Existing world batch extraction driven by attributed canonical user sources."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json

from colony_sidecar.turns.idempotency import canonical_turn_digest, source_message_hash
from colony_sidecar.turns.source_attribution import is_invalidated
from .observations import canonical


def current_source(ledger, source):
    with closing(ledger._connect()) as conn:
        row = conn.execute("SELECT contact_id,scope,messages_json FROM turn_sources WHERE turn_id=?",
                           (source['source_id'],)).fetchone()
        return bool(row and row['contact_id'] == source['contact_id'] and row['scope'] == 'person'
                    and not is_invalidated(conn, source['source_id'])
                    and canonical_turn_digest(json.loads(row['messages_json'])) == source['source_version'])


def recent_batches(ledger, *, hours=24, limit=30):
    """Same bounded batch job, with one participant per batch and exact lineage.

    Only ordinary learning-admitted user sources are eligible. Historical
    source-only imports and unattributed checkpoints remain source-only.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with closing(ledger._connect()) as conn:
        rows = conn.execute('''SELECT s.* FROM turn_sources s JOIN source_claim_jobs j ON j.turn_id=s.turn_id
            WHERE s.scope='person' AND julianday(s.ingested_at)>=julianday(?)
            ORDER BY s.ingested_at DESC,s.turn_id LIMIT ?''', (cutoff, max(1, min(limit, 100)))).fetchall()
        grouped = {}
        for row in rows:
            if is_invalidated(conn, row['turn_id']):
                continue
            messages = json.loads(row['messages_json'])
            version = canonical_turn_digest(messages)
            for message in messages:
                if message.get('role') != 'user':
                    continue
                content = message.get('content', '')
                if isinstance(content, list):
                    content = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict)
                                        and b.get('type') in {'text', 'input_text'})
                if not isinstance(content, str) or not content.strip():
                    continue
                grouped.setdefault(row['contact_id'], []).append(dict(content=content[:600],
                    source_id=row['turn_id'], source_version=version, contact_id=row['contact_id'],
                    message_hash=source_message_hash(row['session_id'], message),
                    observed_at=row['occurred_at'] or row['ingested_at'],
                    time_basis='occurred_at' if row['occurred_at'] else 'received_at'))
        return [items[start:start+10] for items in grouped.values() for start in range(0, len(items), 10)]


async def record_reports(extractor, proposals, sources, name_to_id, mode, report):
    """Quote-qualified reports, never sensor observations or proven properties."""
    if not sources or extractor._source_ledger is None or mode != 'live':
        return
    report.setdefault('property_observations', [])
    report.setdefault('property_skipped', 0)
    for item in proposals[:25] if isinstance(proposals, list) else []:
        if not isinstance(item, dict):
            continue
        name, key, quote = str(item.get('entity', '')).strip(), str(item.get('property', '')).strip(), item.get('evidence')
        value = item.get('value')
        eid = name_to_id.get(name.lower())
        matches = [s for s in sources if isinstance(quote, str) and len(quote) >= 8 and quote in s['content']
                   and name.lower() in quote.lower() and isinstance(value, str) and value.lower() in quote.lower()]
        # Ambiguous identical quotations across messages need explicit source
        # selection in a later extraction, rather than an invented attribution.
        if not eid or not key or len(matches) != 1 or not current_source(extractor._source_ledger, matches[0]):
            report['property_skipped'] += 1
            continue
        source = matches[0]
        oid = 'wo-report-' + hashlib.sha256(canonical([source['source_id'], source['source_version'], source['contact_id'],
            source['message_hash'], eid, key, quote]).encode()).hexdigest()
        at = datetime.fromisoformat(source['observed_at'].replace('Z', '+00:00'))
        try:
            result = await extractor._store.record_property_observation(observation_id=oid, entity_id=eid,
                property_key=key, value=value, kind='reported', producer=f"contact:{source['contact_id']}",
                evidence_refs=[f"source:{source['source_id']}", f"message:{source['message_hash']}"],
                source_refs=[dict(source_id=source['source_id'], source_version=source['source_version'])],
                observed_at=at.isoformat(), fresh_until=(at + timedelta(days=1)).isoformat(),
                subject_person_id=source['contact_id'], viewer_scope=f"person:{source['contact_id']}",
                shareability='subject_private')
            if not current_source(extractor._source_ledger, source):
                await extractor._store.erase_property_evidence([f"source:{source['source_id']}"],
                                                               subject_person_id=source['contact_id'])
                report['property_skipped'] += 1
                continue
            report['property_observations'].append(dict(observation_id=result['observation_id'],
                entity_id=eid, property_key=key, kind='reported', time_basis=source['time_basis']))
            report['writes'] += 1
        except (ValueError, NotImplementedError):
            report['property_skipped'] += 1


def validate_reports(records, ledger):
    """Hydration checks close the cross-store correction/erasure race."""
    result = []
    for row in records:
        refs = row.get('source_refs', [])
        valid = all(current_source(ledger, ref | {'contact_id': row['subject_person_id']}) for ref in refs) if ledger else not refs
        result.append(row if valid else row | {'invalidated': True, 'value': None})
    return result

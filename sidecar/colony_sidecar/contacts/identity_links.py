"""Candidate links and exact owner corrections in the existing contact store.

These functions establish attribution, never tool grants. The authenticated
host owns caller authorization and the source-ledger correction transaction.
"""
import hashlib
import json


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _refs(values):
    if not isinstance(values, (list, tuple)) or len(values) > 100:
        raise ValueError('invalid_identity_evidence')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
        raise ValueError('invalid_identity_evidence')
    return sorted(set(values))


def usable_handle(handle):
    """Legacy name guesses are hypotheses, even before an operator migrates them."""
    return bool(getattr(handle, 'verified', False)) or getattr(handle, 'source', '') != 'auto:scoped-name'


def normalized_handle(gateway, address):
    from .store import _normalize_email, _normalize_phone
    if not isinstance(gateway, str) or not isinstance(address, str):
        raise ValueError('invalid_identity_handle')
    gateway, address = gateway.strip().lower(), address.strip()
    if not gateway or len(gateway) > 64 or not address or len(address) > 512:
        raise ValueError('invalid_identity_handle')
    if gateway == 'rcs':
        gateway = 'sms'
    if gateway == 'email':
        address = _normalize_email(address)
    elif gateway in ('sms', 'imessage', 'signal'):
        address = _normalize_phone(address)
    return gateway, address


async def propose(store, *, contact_id, gateway, address, evidence_refs=(), source='auto:scoped-name'):
    from .store import _now_iso
    gateway, address = normalized_handle(gateway, address)
    refs = _refs(evidence_refs)
    if await store.get(contact_id) is None:
        raise ValueError('identity_contact_not_found')
    if not isinstance(source, str) or not source or len(source) > 128:
        raise ValueError('invalid_identity_source')
    candidate_id = 'identity-candidate:' + hashlib.sha256(_json([contact_id, gateway, address]).encode()).hexdigest()
    db = store._require_db()
    await db.execute('''INSERT OR IGNORE INTO contact_identity_candidates
        (candidate_id,contact_id,gateway,address,source,evidence_refs_json,created_at)
        VALUES (?,?,?,?,?,?,?)''', (candidate_id, contact_id, gateway, address, source, _json(refs), _now_iso()))
    await db.commit()
    async with db.execute('SELECT * FROM contact_identity_candidates WHERE contact_id=? AND gateway=? AND address=?',
                          (contact_id, gateway, address)) as cursor:
        result = dict(await cursor.fetchone())
    result['evidence_refs'] = json.loads(result.pop('evidence_refs_json'))
    result['authority_granted'] = False
    return result


async def correct(store, *, operation_id, performed_by, gateway, address,
                  expected_contact_id, contact_id, evidence_refs, affected_source_ids=()):
    """Move/remove one exact handle, preserving old attribution in a durable receipt.

    expected_contact_id=None means no current handle is expected. contact_id=None
    rejects candidate links and removes the explicitly expected handle, if any.
    The host supplies and subsequently reconciles the exact affected source IDs.
    A lost response may repeat the operation; changed input under its ID fails.
    """
    from .store import _gen_id, _now_iso
    gateway, address = normalized_handle(gateway, address)
    refs, sources = _refs(evidence_refs), _refs(affected_source_ids)
    if not refs:
        raise ValueError('identity_correction_requires_evidence')
    for value in (operation_id, performed_by):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError('invalid_identity_correction')
    for value in (expected_contact_id, contact_id):
        if value is not None and (not isinstance(value, str) or not value.startswith('cid-') or len(value) > 256):
            raise ValueError('invalid_identity_contact')
    request_hash = hashlib.sha256(_json([operation_id, performed_by, gateway, address,
        expected_contact_id, contact_id, refs, sources]).encode()).hexdigest()
    db = await store._open_provision_connection()
    try:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute('SELECT * FROM contact_identity_operations WHERE operation_id=?', (operation_id,)) as cur:
            prior = await cur.fetchone()
        if prior:
            if prior['request_sha256'] != request_hash:
                raise ValueError('identity_operation_conflict')
            await db.rollback()
            return json.loads(prior['result_json'])
        async with db.execute('SELECT * FROM contact_handles WHERE gateway=? AND address=?', (gateway, address)) as cur:
            handle = await cur.fetchone()
        old_id = handle['contact_id'] if handle else None
        if old_id != expected_contact_id:
            raise ValueError('identity_preimage_changed')
        if contact_id is not None:
            async with db.execute('SELECT contact_id FROM contacts WHERE contact_id=? AND deleted_at IS NULL', (contact_id,)) as cur:
                if await cur.fetchone() is None:
                    raise ValueError('identity_contact_not_found')
        now = _now_iso()
        handle_id = handle['handle_id'] if handle else None
        if contact_id is None:
            if handle:
                await db.execute('DELETE FROM contact_handles WHERE handle_id=?', (handle_id,))
        elif handle:
            await db.execute('''UPDATE contact_handles SET contact_id=?, verified=1,
                confidence=1,source='owner_operator',is_primary=CASE WHEN contact_id=? THEN is_primary ELSE 0 END
                WHERE handle_id=?''', (contact_id, contact_id, handle_id))
        else:
            handle_id = _gen_id('hdl')
            await db.execute('''INSERT INTO contact_handles
                (handle_id,contact_id,gateway,address,is_primary,verified,confidence,source,created_at)
                VALUES (?,?,?,?,0,1,1,'owner_operator',?)''', (handle_id, contact_id, gateway, address, now))
        await db.execute('''UPDATE contact_identity_candidates SET status=CASE WHEN contact_id=?
            THEN 'confirmed' ELSE 'rejected' END,resolved_at=? WHERE gateway=? AND address=? AND status='pending' ''',
            (contact_id, now, gateway, address))
        result = {'schema': 'IdentityCorrectionV1', 'operation_id': operation_id,
            'old_contact_id': old_id, 'contact_id': contact_id, 'handle_id': handle_id,
            'gateway': gateway, 'address_sha256': hashlib.sha256(address.encode()).hexdigest(),
            'evidence_refs': refs, 'affected_source_ids': sources, 'recorded_at': now,
            'performed_by': performed_by, 'authority_granted': False,
            'source_reconciliation_required': bool(sources)}
        for cid in sorted({cid for cid in (old_id, contact_id) if cid}):
            await db.execute('''INSERT INTO contact_audit
                (id,contact_id,action,detail,performed_by,created_at) VALUES (?,?,?,?,?,?)''',
                (_gen_id('aud'), cid, 'identity_corrected', _json(result), performed_by, now))
        await db.execute('INSERT INTO contact_identity_operations VALUES (?,?,?,?)',
            (operation_id, request_hash, _json(result), now))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    from colony_sidecar.identity.resolver import reset_identity_resolver
    reset_identity_resolver()
    return result


async def evidence(store, contact_id):
    if await store.get(contact_id) is None:
        raise ValueError('identity_contact_not_found')
    handles = await store.get_handles(contact_id)
    db = store._require_db()
    async with db.execute('SELECT * FROM contact_identity_candidates WHERE contact_id=? ORDER BY created_at DESC LIMIT 100', (contact_id,)) as cur:
        candidates = [dict(row) for row in await cur.fetchall()]
    for row in candidates:
        row['evidence_refs'] = json.loads(row.pop('evidence_refs_json'))
    return {'contact_id': contact_id, 'handles': [h.to_dict() | {
        'identity_status': 'confirmed' if h.verified else 'observed' if usable_handle(h) else 'tentative',
        'usable_for_attribution': usable_handle(h)} for h in handles], 'candidates': candidates,
        'authority_granted': False}


async def pending_reconciliations(store, *, limit=100):
    """Recover unfinished cross-store corrections from their existing receipts."""
    db = store._require_db()
    async with db.execute('''SELECT result_json FROM contact_identity_operations
        WHERE json_extract(result_json,'$.source_reconciliation_required')=1
        ORDER BY created_at,operation_id LIMIT ?''', (max(1, min(int(limit), 500)),)) as cur:
        return [json.loads(row['result_json']) for row in await cur.fetchall()]


async def mark_sources_reconciled(store, *, operation_id, source_result):
    """Finish an exact source correction without introducing a second queue."""
    db = await store._open_provision_connection()
    try:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute('SELECT result_json FROM contact_identity_operations WHERE operation_id=?', (operation_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError('identity_operation_not_found')
        result = json.loads(row['result_json'])
        if (source_result.get('schema') != 'SourceAttributionCorrectionV1'
                or source_result.get('operation_id') != operation_id
                or source_result.get('old_contact_id') != result['old_contact_id']
                or source_result.get('contact_id') != result['contact_id']
                or sorted(source_result.get('source_ids', [])) != sorted(result['affected_source_ids'])):
            raise ValueError('identity_reconciliation_mismatch')
        summary = {key: source_result[key] for key in ('operation_id', 'source_ids',
            'affected_source_ids', 'invalidated_source_ids', 'recorded_at')}
        if not result['source_reconciliation_required']:
            if result.get('source_reconciliation') != summary:
                raise ValueError('identity_reconciliation_conflict')
            await db.rollback()
            return result
        result['source_reconciliation_required'] = False
        result['source_reconciliation'] = summary
        await db.execute('UPDATE contact_identity_operations SET result_json=? WHERE operation_id=?',
                         (_json(result), operation_id))
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

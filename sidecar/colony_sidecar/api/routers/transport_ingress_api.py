"""Trusted durable intake, without another payload store or model-callable ACK."""
from datetime import datetime, timezone
from contextlib import closing
import json
import logging
import re
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from colony_sidecar.api.authority import request_authority
from colony_sidecar.contacts.transport_ingress import TransportIngress

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/ingress')


class Metadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    channel: str = Field(pattern='^[a-z][a-z0-9_.-]{0,31}$')
    sender_ref: str = Field(min_length=1, max_length=512)
    reply_to_ref: str = Field(default='', max_length=256)
    from_owner: bool = False
    is_group: bool = False


class Admission(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    account_id: str = Field(min_length=1, max_length=128)
    epoch: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=256)
    occurred_at: float = Field(gt=0)
    journal_ref: str = Field(min_length=1, max_length=512)
    payload_digest: str = Field(pattern='^[a-f0-9]{64}$')
    media_available: bool
    metadata: Metadata


class NativeTurn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)


class Handoff(BaseModel):
    model_config = ConfigDict(extra='forbid')
    receipt_ids: list[str] = Field(min_length=1, max_length=100)
    batch_id: str = Field(min_length=1, max_length=128)
    native_turn: NativeTurn | None = None


class Coverage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    account_id: str = Field(min_length=1, max_length=128)
    epoch: str = Field(min_length=1, max_length=128)
    connected_since: float | None = Field(default=None, gt=0)
    observed_at: float = Field(gt=0)
    watermark: int = Field(ge=0)
    sequence_floor: int = Field(default=0, ge=0)
    connected: bool
    unavailable: int = Field(ge=0)


def store():
    from colony_sidecar.api.routers import host
    if host._comms_log is None:
        raise HTTPException(503, detail='communications_unavailable')
    return TransportIngress(host._comms_log._conn)


def producer(request):
    auth = request_authority(request)
    if (not auth.authenticated or auth.anonymous or auth.legacy or not auth.has_scope('transport:write')):
        raise HTTPException(403, detail='trusted_transport_producer_required')
    return auth.principal_id


def guarded(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except sqlite3.Error:
        raise HTTPException(503, detail='transport_intake_unavailable') from None


async def verified_contact(metadata):
    from colony_sidecar.api.routers import host
    if host._contacts_store is None:
        raise HTTPException(503, detail='contacts_unavailable')
    if metadata.is_group:
        return None
    addresses = [metadata.sender_ref]
    # PN and LID are separate namespaces. Never infer a phone number from LID,
    # a display name, another channel, or model-selected contact identifiers.
    if metadata.channel == 'whatsapp':
        match = re.fullmatch(r'([0-9]{1,32})@s\.whatsapp\.net', metadata.sender_ref)
        if match:
            addresses += [match[1], '+' + match[1]]
        elif not re.fullmatch(r'[0-9]{1,32}@lid', metadata.sender_ref):
            return None
    try:
        contact = await host._contacts_store.resolve_verified_handles(metadata.channel, addresses)
    except NotImplementedError:
        raise HTTPException(503, detail='verified_transport_resolution_unavailable') from None
    return contact.contact_id if contact is not None else None


@router.post('/admit')
async def admit(body: Admission, request: Request):
    principal = producer(request)
    contact_id = await verified_contact(body.metadata)
    value = guarded(lambda: store().admit(producer=principal, contact_id=contact_id, **body.model_dump()))
    if contact_id and not body.metadata.from_owner and value['state'] != 'erased':
        # Receipt admission and exact positive reply reconciliation finish
        # before intake ACK. A partial failure is retried idempotently.
        from .transport import TransportReceipt, observe
        await observe(TransportReceipt(event_id=value['receipt_id'], contact_id=contact_id,
            channel=body.metadata.channel, direction='in', external_ref=body.event_id,
            reply_to_ref=body.metadata.reply_to_ref, receipt_ref=value['receipt_id'],
            occurred_at=datetime.fromtimestamp(body.occurred_at, timezone.utc).isoformat(),
            status='received'), request)
    return value


@router.post('/handoff')
async def handoff(body: Handoff, request: Request):
    principal = producer(request)
    return guarded(lambda: store().handoff(producer=principal, **body.model_dump()))


@router.post('/coverage')
async def record_coverage(body: Coverage, request: Request):
    principal = producer(request)
    guarded(lambda: store().observe_coverage(producer=principal, **body.model_dump()))
    return {'recorded': True, 'effect_authorized': False}


@router.get('/receipts')
async def receipts(request: Request, ids: str = Query(min_length=1, max_length=9000)):
    principal = producer(request)
    selected = ids.split(',')
    ingress = store()
    guarded(lambda: ingress.receipts(producer=principal, receipt_ids=selected))
    for receipt_id in selected:
        guarded(lambda: settle_receipt(ingress, ingress.get(receipt_id)))
    return {'items': guarded(lambda: ingress.receipts(producer=principal, receipt_ids=selected))}


def settle_receipt(ingress, row):
    """Retry metadata settlement on the existing receiver's receipt read."""
    if not row or not row['native_turn_json'] or not row['contact_id'] or row['state'] == 'erased':
        return
    from colony_sidecar import get_state_dir
    from colony_sidecar.turns import get_turn_idempotency_ledger
    ledger = get_turn_idempotency_ledger(get_state_dir())
    native = json.loads(row['native_turn_json'])
    source_id = native['turn_id']
    if (ledger.is_source_erased(source_id, row['contact_id'])
            or ledger.is_projection_erased(source_id)):
        ingress.erase_sources([source_id])
        return
    if row['state'] == 'completed':
        return
    with closing(ledger._connect()) as db:
        receipt = db.execute('SELECT state,response_json FROM turn_ingestion WHERE turn_id=?', (source_id,)).fetchone()
        source = db.execute('SELECT session_id FROM turn_sources WHERE turn_id=? AND contact_id=?',
                             (source_id, row['contact_id'])).fetchone()
    if source is None or source['session_id'] != native['session_id']:
        return
    if not receipt or receipt['state'] != 'completed':
        return
    result = json.loads(receipt['response_json'] or '{}')
    if not result.get('accepted') or not result.get('source_recorded'):
        return
    refs = ledger.source_references([source_id], contact_id=row['contact_id'], session_id=native['session_id'])
    if refs:
        ingress.complete(native_turn=native,
            source_versions={r['source_id']: r['source_version'] for r in refs}, outcome='captured')


def complete_source(body, result):
    """Existing accepted canonical source is the processing receipt.

    Native nonempty turn IDs are already the source IDs in the durable outbox.
    No new host payload, task-text matching or model assertion is necessary.
    This does not attest a provider send or successful external tool effect.
    """
    from colony_sidecar.api.routers import host
    if (host._comms_log is None or not result.accepted or not result.source_recorded
            or not body.context.turn_id or body.checkpoint_messages is not None or body.source_only):
        return
    ingress = store()
    rows = ingress.for_canonical_turn(turn_id=body.context.turn_id, contact_id=body.context.contact_id)
    if not rows:
        return
    settle_receipt(ingress, rows[0])


def reconcile_source(body, result):
    # Canonical ingestion has already committed. Failure leaves the transport
    # payload pending, and the same source/outbox receipt can be reconciled later.
    try:
        complete_source(body, result)
    except (OSError, ValueError, sqlite3.Error):
        logger.warning('Transport source reconciliation remains pending', exc_info=True)


def forget_sources(source_ids):
    from colony_sidecar.api.routers import host
    if host._comms_log is None:
        return 'unavailable'
    selected = store().erase_sources(source_ids)
    return 'pending_transport_owner' if selected else 'not_applicable'


def followup_coverage(*, principal, contact_id, since, channel):
    if channel != 'whatsapp':
        return {'observed': False, 'reasons': ['durable_channel_coverage_unavailable']}
    from colony_sidecar.api.routers import host
    if host._comms_log is None:
        return {'observed': False, 'reasons': ['communications_unavailable']}
    ingress = store()
    accounts = ingress.conn.execute('SELECT account_id FROM transport_ingress_coverage WHERE producer=?',
                                     (principal,)).fetchall()
    if len(accounts) != 1:
        return {'observed': False, 'reasons': ['single_transport_account_required']}
    return ingress.coverage(producer=principal, account_id=accounts[0]['account_id'],
                            contact_id=contact_id, since=since)

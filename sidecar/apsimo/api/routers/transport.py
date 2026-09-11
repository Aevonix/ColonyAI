"""Trusted provider receipts feed the existing communications and task ledgers."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apsimo.api.authority import request_authority

router = APIRouter(prefix='/v1/host/transport', tags=['transport'])


class TransportReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(min_length=1, max_length=256)
    contact_id: str = Field(min_length=1, max_length=128)
    channel: str = Field(pattern='^[a-z][a-z0-9_.-]{0,31}$')
    direction: Literal['in', 'out']
    external_ref: str = Field(min_length=1, max_length=256)
    reply_to_ref: str = Field(default='', max_length=256)
    reply_to_channel: str | None = Field(default=None, pattern='^[a-z][a-z0-9_.-]{0,31}$')
    receipt_ref: str = Field(min_length=1, max_length=256)
    outbound_ref: str = Field(default='', max_length=256)
    occurred_at: str = Field(min_length=1, max_length=64)
    status: Literal['accepted', 'delivered', 'read', 'received']
    followup_wait_id: str | None = Field(default=None, min_length=1, max_length=128)
    action_digest: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')


def reconcile_receipts(waits, comms, row):
    """An original delivery reference resolves through actual provider IDs."""
    receipts = comms.outbound_receipts(contact_id=row['contact_id'], outbound_ref=row['outbound_ref'])
    if not receipts:
        return row
    first = receipts[0]
    parent = waits.store.get(row['commitment_id'])
    start = parent['made_at'] if parent and parent.get('made_at') else datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat()
    if row['dispatch_receipt_ref'] is None:
        row = waits.acknowledge_dispatch(row['wait_id'], receipt_ref=first['receipt_ref'],
            occurred_at=datetime.fromisoformat(first['ts']).timestamp())
    for receipt in receipts:
        match = comms.match_reply(contact_id=row['contact_id'], outbound_ref=receipt['external_ref'],
            since_iso=start,
            until_iso=datetime.now(timezone.utc).isoformat())
        # Preserve the actual provider link alongside its receipt-proven logical
        # delivery alias. A text mention of an outbound reference never gets here.
        if match['status'] == 'matched':
            match = {**match, 'outbound_ref': row['outbound_ref'], 'matches': [{**item,
                'provider_reply_to_ref': item['reply_to_ref'], 'reply_to_ref': row['outbound_ref']}
                for item in match['matches']]}
            row = waits.apply_reply(row['wait_id'], match)
    return row


@router.post('/observe')
async def observe(body: TransportReceipt, request: Request):
    authority = request_authority(request)
    if (not authority.authenticated or authority.anonymous or authority.legacy
            or not authority.has_scope('transport:write')):
        raise HTTPException(403, detail='trusted_transport_producer_required')
    if (body.direction == 'in') != (body.status == 'received'):
        raise HTTPException(422, detail='transport_direction_status_mismatch')
    if bool(body.followup_wait_id) != bool(body.action_digest) or (body.followup_wait_id and body.direction != 'out'):
        raise HTTPException(422, detail='followup_receipt_binding_required')
    from apsimo.api.routers import host
    if host._comms_log is None or host._contacts_store is None:
        raise HTTPException(503, detail='communications_unavailable')
    if await host._contacts_store.get(body.contact_id) is None:
        raise HTTPException(409, detail='resolved_transport_contact_required')
    try:
        created = host._comms_log.log_receipt(event_id=authority.principal_id+':'+body.event_id,
            contact_id=body.contact_id, channel=body.channel, direction=body.direction,
            external_ref=body.channel+':'+body.external_ref,
            reply_to_ref=((body.reply_to_channel or body.channel)+':'+body.reply_to_ref) if body.reply_to_ref else '',
            receipt_ref=body.receipt_ref, outbound_ref=body.outbound_ref,
            occurred_at=body.occurred_at, status=body.status)
        count = 0
        if host._commitment_store is not None:
            from apsimo.initiatives.temporal_followup import TemporalFollowups
            from apsimo.api.routers.temporal_followups import refresh_source_bindings
            waits = TemporalFollowups(host._commitment_store)
            if body.followup_wait_id:
                child_wait = waits.get(body.followup_wait_id)
                if (child_wait['contact_id'] != body.contact_id
                        or child_wait.get('authority_scope', {}).get('plan', {}).get('channel') != body.channel):
                    raise ValueError('followup_receipt_recipient_mismatch')
                waits.mark_followup_dispatched(body.followup_wait_id, action_digest=body.action_digest,
                    receipt_ref=body.receipt_ref)
            outbound_refs = None
            if body.direction == 'out':
                outbound_refs = [body.channel+':'+body.external_ref]
                if body.outbound_ref:
                    outbound_refs.append(body.outbound_ref)
            for row in waits.list_for_context(contact_id=body.contact_id, limit=100,
                    outbound_refs=outbound_refs):
                parent = host._commitment_store.get(row['commitment_id'])
                row = refresh_source_bindings(waits, row, parent['person_id'])
                row = reconcile_receipts(waits, host._comms_log, row)
                from apsimo.self_model import reply_forecasts
                reply_forecasts.safe(reply_forecasts.observe_dispatch, waits, row,
                    producer=authority.principal_id, receipt=body, created=created)
                reply_forecasts.safe(reply_forecasts.reconcile, row['wait_id'])
                count += 1
        return {'recorded': True, 'created': created, 'waits_checked': count, 'effect_authorized': False}
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from None


from .transport_ingress_api import router as ingress_router
router.include_router(ingress_router)

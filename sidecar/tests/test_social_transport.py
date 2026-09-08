"""Actual provider metadata, reordered arrival, restart and no inferred delivery."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host, transport
from colony_sidecar.contacts.comms import CommsLog
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.initiatives.temporal_followup import TemporalFollowups
from colony_sidecar.turns import TurnIdempotencyLedger, canonical_turn_digest
from test_scoped_api_authority import _principal, _write_keyring


@pytest.mark.asyncio
async def test_reordered_receipts_resolve_exact_reply_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    sources = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    messages = [{'role': 'user', 'content': 'Obtain the agreed report and track its reply.'}]
    sources.record_source('task-source', contact_id='cid-owner', session_id='s', messages=messages)
    commitments = CommitmentStore(tmp_path/'commitments.db')
    parent = commitments.create(person_id='cid-owner', description='Obtain the report')
    origin = time.time()-120
    with commitments._connect() as conn, conn:
        conn.execute('UPDATE commitments SET made_at=? WHERE id=?',
            (datetime.fromtimestamp(origin-30, timezone.utc).isoformat(), parent['id']))
    waits = TemporalFollowups(commitments, clock=lambda: origin)
    params = dict(wait_id='waiting', commitment_id=parent['id'], work_id='work', contact_id='cid-person',
        outbound_ref='delivery-one', source_refs=['task-source'],
        source_versions={'task-source': canonical_turn_digest(messages)}, source_session_id='s',
        expected_after_seconds=60, expires_at=origin+3600)
    waits.expect_reply(**params)
    comms = CommsLog(str(tmp_path/'comms.db'))
    monkeypatch.setattr(host, '_comms_log', comms)
    monkeypatch.setattr(host, '_commitment_store', commitments)
    monkeypatch.setattr(host, '_contacts_store', SimpleNamespace(get=AsyncMock(return_value=object())))
    keys = tmp_path/'keys.json'
    _write_keyring(keys, [_principal(principal='provider', secret='provider-key', viewer='cid-owner', scopes=['transport:write']),
                          _principal(principal='ordinary', secret='ordinary-key', viewer='cid-owner')])
    app = FastAPI(); app.include_router(transport.router)
    app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keys))
    def event(identity, direction, stamp, **changes):
        return dict(event_id=identity, contact_id='cid-person', channel='whatsapp', direction=direction,
            external_ref=identity, receipt_ref='receipt:'+identity, status='accepted' if direction=='out' else 'received',
            occurred_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(), **changes)
    outbound = event('provider-out', 'out', origin+10, outbound_ref='delivery-one')
    reply = event('provider-in', 'in', origin+25, reply_to_ref='provider-out')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        denied = await client.post('/v1/host/transport/observe', headers={'Authorization':'Bearer ordinary-key'}, json=outbound)
        assert denied.status_code == 403
        headers = {'Authorization':'Bearer provider-key'}
        received = await client.post('/v1/host/transport/observe', headers=headers, json=reply)
        assert received.status_code == 200, received.text
        assert TemporalFollowups(commitments).get('waiting')['dispatch_receipt_ref'] is None
        sent = await client.post('/v1/host/transport/observe', headers=headers, json=outbound)
        assert sent.status_code == 200, sent.text
        state = TemporalFollowups(commitments).get('waiting')
        assert state['state'] == 'resolved' and state['expected_at'] == pytest.approx(origin+70, abs=.000001)
        assert state['reply']['matches'][0]['external_ref'] == 'whatsapp:provider-in'
        replay = await client.post('/v1/host/transport/observe', headers=headers, json=outbound)
        assert replay.status_code == 200 and replay.json()['created'] is False
        assert comms._conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 2
        changed = await client.post('/v1/host/transport/observe', headers=headers,
                                    json={**outbound, 'external_ref':'a-different-message'})
        assert changed.status_code == 409
        assert not TemporalFollowups(commitments).preflight('waiting')['dispatch_allowed']

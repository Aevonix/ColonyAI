"""Scoped durable intake joins verified handles, real replies and native sources."""
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
import time

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host, transport, transport_ingress_api
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.contacts.comms import CommsLog
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.initiatives.temporal_followup import TemporalFollowups
from colony_sidecar.turns import TurnIdempotencyLedger, canonical_turn_digest
from test_scoped_api_authority import _principal, _write_keyring
from test_turn_source_evidence import source_app


PREFIX = '/v1/host/transport/ingress'


def headers(principal='provider'):
    return {'Authorization': 'Bearer fixture-' + principal}


def admission(sequence=1, **changes):
    return dict(dict(account_id='neutral-account', epoch='epoch-one', sequence=sequence,
        event_id='provider-event-' + str(sequence), occurred_at=time.time()-1,
        journal_ref='journal:' + str(sequence), payload_digest='a'*64,
        media_available=True, metadata={'channel': 'whatsapp', 'sender_ref': '15550000011@s.whatsapp.net'}),
        **changes)


@pytest.fixture
async def ingress(source_app, tmp_path, monkeypatch):
    contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path/'contacts.db')))
    await contacts.connect()
    person = await contacts.create(display_name='Neutral peer')
    await contacts.add_handle(person.contact_id, 'whatsapp', '+15550000011', verified=True)
    comms = CommsLog(str(tmp_path/'communications.db'))
    commitments = CommitmentStore(tmp_path/'commitments.db')
    monkeypatch.setattr(host, '_contacts_store', contacts)
    monkeypatch.setattr(host, '_comms_log', comms)
    monkeypatch.setattr(host, '_commitment_store', commitments)
    keyring = tmp_path/'keys.json'
    principals = [_principal(principal=name, secret='fixture-'+name, viewer=person.contact_id,
        scopes=['transport:write']) for name in ('provider', 'other-provider')]
    principals.append(_principal(principal='writer', secret='fixture-writer', viewer=person.contact_id))
    _write_keyring(keyring, principals)
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    source_app.include_router(transport.router)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        yield SimpleNamespace(client=client, contacts=contacts, person=person.contact_id,
            comms=comms, commitments=commitments,
            ledger=TurnIdempotencyLedger(tmp_path/'turn-idempotency.db'))
    await contacts.close()
    comms._conn.close()


async def admit(runtime, **changes):
    value = admission(**changes)
    response = await runtime.client.post(PREFIX+'/admit', headers=headers(), json=value)
    assert response.status_code == 200, response.text
    return response.json()


async def handoff(runtime, receipt, *, suffix='', native=True):
    body = {'receipt_ids': [receipt['receipt_id']], 'batch_id': 'batch'+suffix}
    response = await runtime.client.post(PREFIX+'/handoff', headers=headers(), json=body)
    assert response.status_code == 200 and response.json()['may_dispatch'] is True, response.text
    turn = {'session_id': 'native-session'+suffix, 'task_id': 'native-task'+suffix, 'turn_id': 'native-source'+suffix}
    if native:
        response = await runtime.client.post(PREFIX+'/handoff', headers=headers(), json={**body, 'native_turn': turn})
        assert response.status_code == 200 and response.json()['native_bound'] is True, response.text
        assert response.json()['may_dispatch'] is False
    return turn


async def status(runtime, receipt, principal='provider'):
    return await runtime.client.get(PREFIX+'/receipts', headers=headers(principal),
                                    params={'ids': receipt['receipt_id']})


async def capture(runtime, turn, **changes):
    body = {'identity': {'host_id': 'native-fixture'},
            'context': {'contact_id': runtime.person, 'session_id': turn['session_id'], 'turn_id': turn['turn_id']},
            'user_message': {'role': 'user', 'content': 'The requested neutral report is available.'},
            'assistant_message': {'role': 'assistant', 'content': 'I received the report.'}}
    body.update(changes)
    return await runtime.client.put('/v2/host/turns/'+turn['turn_id'], headers=headers('writer'), json=body)


@pytest.mark.asyncio
async def test_only_transport_admits_and_contact_comes_from_verified_handle(ingress):
    client = ingress.client
    body = admission()
    denied = await client.post(PREFIX+'/admit', headers=headers('writer'), json=body)
    assert denied.status_code == 403
    for changed in ({**body, 'contact_id': ingress.person},
                    {**body, 'metadata': {**body['metadata'], 'contact_id': ingress.person}}):
        assert (await client.post(PREFIX+'/admit', headers=headers(), json=changed)).status_code == 422
    receipt = await admit(ingress)
    row = ingress.comms._conn.execute('SELECT contact_id FROM transport_ingress WHERE receipt_id=?',
                                      (receipt['receipt_id'],)).fetchone()
    assert row['contact_id'] == ingress.person
    assert 'contact_id' not in receipt


@pytest.mark.asyncio
async def test_pn_lid_unverified_and_ambiguous_aliases_remain_distinct(ingress):
    lid = await ingress.contacts.create(display_name='Separate lid peer')
    await ingress.contacts.add_handle(lid.contact_id, 'whatsapp', '15550000011@lid', verified=True)
    known = await admit(ingress, sequence=1, metadata={'channel': 'whatsapp', 'sender_ref': '15550000011@lid'})
    unknown = await admit(ingress, sequence=2, metadata={'channel': 'whatsapp', 'sender_ref': '15550000022@lid'})
    unverified = await ingress.contacts.create(display_name='Unverified peer')
    await ingress.contacts.add_handle(unverified.contact_id, 'whatsapp', '15550000033@s.whatsapp.net', verified=False)
    tentative = await admit(ingress, sequence=3, metadata={'channel': 'whatsapp', 'sender_ref': '15550000033@s.whatsapp.net'})
    conflicting = await ingress.contacts.create(display_name='Conflicting alias')
    await ingress.contacts.add_handle(conflicting.contact_id, 'whatsapp', '15550000011@s.whatsapp.net', verified=True)
    ambiguous = await admit(ingress, sequence=4)
    grouped = await admit(ingress, sequence=5, metadata={'channel': 'whatsapp', 'sender_ref': '15550000011@lid', 'is_group': True})
    rows = {row['receipt_id']: row['contact_id'] for row in ingress.comms._conn.execute('SELECT receipt_id,contact_id FROM transport_ingress')}
    assert rows[known['receipt_id']] == lid.contact_id
    assert all(rows[value['receipt_id']] is None for value in (unknown, tentative, ambiguous, grouped))


@pytest.mark.asyncio
async def test_another_producer_cannot_read_or_claim_receipt(ingress):
    receipt = await admit(ingress)
    assert (await status(ingress, receipt, 'other-provider')).status_code == 409
    response = await ingress.client.post(PREFIX+'/handoff', headers=headers('other-provider'),
        json={'receipt_ids': [receipt['receipt_id']], 'batch_id': 'other'})
    assert response.status_code == 409
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'admitted'


@pytest.mark.asyncio
async def test_positive_reply_closes_wait_before_inference_or_native_handoff(ingress):
    now = time.time()-120
    messages = [{'role': 'user', 'content': 'Obtain the report and track the reply.'}]
    ingress.ledger.record_source('task-origin', contact_id='owner', session_id='owner-session', messages=messages)
    parent = ingress.commitments.create('owner', 'Obtain the neutral report')
    with ingress.commitments._connect() as db, db:
        db.execute('UPDATE commitments SET made_at=? WHERE id=?',
            (datetime.fromtimestamp(now-30, timezone.utc).isoformat(), parent['id']))
    waits = TemporalFollowups(ingress.commitments, clock=lambda: now)
    waits.expect_reply(wait_id='wait-reply', commitment_id=parent['id'], work_id='work-one',
        contact_id=ingress.person, outbound_ref='logical-out', source_refs=['task-origin'],
        source_versions={'task-origin': canonical_turn_digest(messages)}, source_session_id='owner-session',
        expected_after_seconds=60, expires_at=now+3600)
    outbound = {'event_id': 'out', 'contact_id': ingress.person, 'channel': 'whatsapp', 'direction': 'out',
        'external_ref': 'provider-out', 'receipt_ref': 'out-receipt', 'outbound_ref': 'logical-out',
        'occurred_at': datetime.fromtimestamp(now+10, timezone.utc).isoformat(), 'status': 'accepted'}
    sent = await ingress.client.post('/v1/host/transport/observe', headers=headers(), json=outbound)
    assert sent.status_code == 200, sent.text
    receipt = await admit(ingress, metadata={'channel': 'whatsapp', 'sender_ref': '15550000011@s.whatsapp.net',
                                            'reply_to_ref': 'provider-out'})
    assert TemporalFollowups(ingress.commitments).get('wait-reply')['state'] == 'resolved'
    row = (await status(ingress, receipt)).json()['items'][0]
    assert row['state'] == 'admitted' and row['source_versions_json'] is None
    assert len(ingress.comms._conn.execute('SELECT * FROM communications').fetchall()) == 2


@pytest.mark.asyncio
async def test_exact_native_capture_settles_and_erasure_survives_receipt_retry(ingress):
    receipt = await admit(ingress)
    turn = await handoff(ingress, receipt)
    pending = (await status(ingress, receipt)).json()['items'][0]
    assert pending['state'] == 'handed_off' and pending['source_versions_json'] is None
    accepted = await capture(ingress, turn)
    assert accepted.status_code == 201 and accepted.json()['source_recorded'] is True, accepted.text
    direct = ingress.comms._conn.execute('SELECT state FROM transport_ingress WHERE receipt_id=?',
                                         (receipt['receipt_id'],)).fetchone()
    assert direct['state'] == 'completed'
    completed = (await status(ingress, receipt)).json()['items'][0]
    assert completed['state'] == 'completed' and completed['outcome'] == 'captured'
    references = ingress.ledger.source_references([turn['turn_id']], contact_id=ingress.person, session_id=turn['session_id'])
    assert json.loads(completed['source_versions_json']) == {row['source_id']: row['source_version'] for row in references}
    erased = await ingress.client.post('/v1/host/memory/sources/forget', headers=headers('writer'),
        json={'contact_id': ingress.person, 'source_ids': [turn['turn_id']]})
    assert erased.status_code == 200, erased.text
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'erased'
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'erased'


@pytest.mark.asyncio
async def test_unknown_or_wrong_session_capture_cannot_settle_native_binding(ingress):
    receipt = await admit(ingress)
    turn = await handoff(ingress, receipt)
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'handed_off'
    wrong = {**turn, 'session_id': 'different-native-session'}
    accepted = await capture(ingress, wrong)
    assert accepted.status_code == 201 and accepted.json()['source_recorded'] is True, accepted.text
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'handed_off'
    separate = await admit(ingress, sequence=2)
    right = await handoff(ingress, separate, suffix='-right')
    assert (await capture(ingress, right)).status_code == 201
    assert (await status(ingress, separate)).json()['items'][0]['state'] == 'completed'


@pytest.mark.asyncio
async def test_receipt_read_repairs_lost_settlement_after_real_source_capture(ingress, monkeypatch):
    receipt = await admit(ingress)
    turn = await handoff(ingress, receipt)
    def fail_once(*args):
        raise sqlite3.OperationalError('controlled post-capture metadata failure')
    with monkeypatch.context() as patch:
        patch.setattr(transport_ingress_api, 'complete_source', fail_once)
        accepted = await capture(ingress, turn)
    assert accepted.status_code == 201 and accepted.json()['source_recorded'] is True, accepted.text
    row = ingress.comms._conn.execute('SELECT state FROM transport_ingress WHERE receipt_id=?',
                                      (receipt['receipt_id'],)).fetchone()
    assert row['state'] == 'handed_off'
    first = (await status(ingress, receipt)).json()['items'][0]
    assert first['state'] == 'completed'
    assert (await status(ingress, receipt)).json()['items'][0] == first


@pytest.mark.asyncio
async def test_source_presence_without_completed_ingestion_is_pending_and_can_be_erased(ingress):
    receipt = await admit(ingress)
    turn = await handoff(ingress, receipt)
    ingress.ledger.record_source(turn['turn_id'], contact_id=ingress.person, session_id=turn['session_id'],
        messages=[{'role': 'user', 'content': 'Source retained before an unfinished turn.'}], derive_claims=False)
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'handed_off'
    erased = await ingress.client.post('/v1/host/memory/sources/forget', headers=headers('writer'),
        json={'contact_id': ingress.person, 'source_ids': [turn['turn_id']]})
    assert erased.status_code == 200, erased.text
    assert (await status(ingress, receipt)).json()['items'][0]['state'] == 'erased'

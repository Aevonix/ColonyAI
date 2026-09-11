"""Fresh transport grants admit only enrolled senders before source storage."""
import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest

from apsimo import setup
from apsimo.api.middleware import ApiKeyMiddleware
from apsimo.api.routers import host
from apsimo.contacts.config import ContactsConfig
from apsimo.contacts.store import SQLiteContactStore
from apsimo.turns import TurnIdempotencyLedger
from test_native_setup import args, isolated_platform_environment  # Shared isolated wizard fixtures.


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_enrolled_sender_checked_before_turn_reservation(args, monkeypatch, version):
    args.owner_handle = ['telegram=123456789']
    assert setup.run_init(None, args) == 0
    state = Path(args.hermes_home)/'apsimo'
    principal = json.loads((state/'api-keyring.json').read_text())['principals'][0]
    owner = principal['viewer_person_id']
    monkeypatch.setenv('COLONY_STATE_DIR', str(state))
    monkeypatch.setenv('COLONY_IDENTITY_SHADOW_CONTACTS', 'false')
    for name in ('_graph', '_presence_store', '_context_provenance', '_telemetry', '_p8_runtime'):
        monkeypatch.setattr(host, name, None)
    ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')

    async def exercise():
        store = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
        await store.connect()
        monkeypatch.setattr(host, '_contacts_store', store)
        try:
            guest = await store.create(display_name='Other account')
            await store.add_handle(guest.contact_id, 'telegram', '987654321', verified=True)
            await store.add_handle(owner, 'telegram', 'unverified', verified=False)
            before_contacts = await store.list()
            app = FastAPI()
            app.include_router(host.router)
            app.include_router(host.v2_router)
            app.add_middleware(ApiKeyMiddleware, keyring_path=str(state/'api-keyring.json'))
            headers = {'Authorization': 'Bearer ' + principal['credentials'][0]['secret']}

            async def send(client, turn_id, platform, sender):
                body = {'identity': {'host_id': 'fixture'},
                    'context': {'contact_id': owner if sender is None else 'body-selected-person',
                        'session_id': 'enrolled-source',
                        'channel_id': platform + ':fixture', 'turn_id': turn_id}}
                if sender is not None:
                    body['sender'] = {'platform': platform, 'user_id': sender}
                message = {'role': 'user', 'content': 'The archive reference is violet.'}
                body.update(user_message=message, source_only=True)
                if version == 'v1':
                    return await client.post('/v1/host/turns/sync', json=body)
                return await client.put('/v2/host/turns/' + turn_id, json=body)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url='http://fixture', headers=headers) as client:
                denied = [('unknown', 'telegram', 'unknown', 404),
                    ('canonical-id', 'telegram', owner, 404),
                    ('other-person', 'telegram', '987654321', 404),
                    ('unverified', 'telegram', 'unverified', 404),
                    ('wrong-channel', 'whatsapp', '123456789', 403)]
                for turn_id, platform, sender, code in denied:
                    response = await send(client, turn_id, platform, sender)
                    assert response.status_code == code, (turn_id, response.text)
                with sqlite3.connect(ledger.db_path) as db:
                    assert db.execute('SELECT count(*) FROM turn_sources').fetchone()[0] == 0
                    assert db.execute('SELECT count(*) FROM turn_ingestion').fetchone()[0] == 0
                assert await store.list() == before_contacts
                response = await send(client, 'known-owner', 'telegram', '123456789')
                assert response.status_code == (200 if version == 'v1' else 201), response.text
                assert response.json()['source_recorded'] is True
                replay = await send(client, 'known-owner', 'telegram', '123456789')
                assert replay.status_code == 200 and replay.json()['source_recorded'] is True
                with sqlite3.connect(ledger.db_path) as db:
                    rows = db.execute('SELECT contact_id,messages_json FROM turn_sources').fetchall()
                    assert len(rows) == 1 and rows[0][0] == owner
                    assert json.loads(rows[0][1])[0]['content'] == 'The archive reference is violet.'
                # Native CLI has no human sender ID. Its exact viewer binding
                # still permits capture without inventing a transport handle.
                response = await send(client, 'senderless-cli', 'cli', None)
                assert response.status_code == (200 if version == 'v1' else 201), response.text
                assert response.json()['source_recorded'] is True
                with sqlite3.connect(ledger.db_path) as db:
                    assert db.execute('SELECT contact_id FROM turn_sources WHERE turn_id=?',
                        ('senderless-cli',)).fetchone() == (owner,)
        finally:
            await store.close()
    asyncio.run(exercise())

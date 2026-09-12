"""The focused sender resolver preserves explicitly broad legacy clients."""
import json

from fastapi import FastAPI
import httpx
import pytest

from pacomind.api.middleware import ApiKeyMiddleware
from pacomind.api.routers import host
from pacomind.contacts.config import ContactsConfig
from pacomind.contacts.store import SQLiteContactStore


@pytest.mark.asyncio
async def test_legacy_resolver_scope_compatibility_is_explicit(tmp_path, monkeypatch):
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path/'contacts.db')))
    await store.connect()
    monkeypatch.setattr(host, '_contacts_store', store)
    try:
        owner = await store.create(display_name='Owner')
        await store.add_handle(owner.contact_id, 'telegram', '123456789', verified=True)
        keyring = tmp_path/'keyring.json'
        keyring.write_text(json.dumps({'version': 1, 'principals': [{
            'principal': name, 'status': 'active', 'viewer_person_id': owner.contact_id,
            'audiences': ['viewer'], 'allow_unscoped_api': broad, 'scopes': ['api:access'],
            'credentials': [{'id': 'test', 'status': 'active', 'secret': name + '-secret'}],
        } for name, broad in [('broad', True), ('restricted', False)]]}))
        keyring.chmod(0o600)
        app = FastAPI()
        app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring), api_key='legacy-secret')
        app.include_router(host.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for secret, status in [('legacy-secret', 200), ('broad-secret', 200), ('restricted-secret', 403)]:
                response = await client.get('/v1/host/contacts/resolve',
                    headers={'Authorization': 'Bearer ' + secret},
                    params={'gateway': 'telegram', 'address': '123456789'})
                assert response.status_code == status, response.text
                if status == 200:
                    assert response.json()['contact_id'] == owner.contact_id
            # Retain the previously explicit broad provisioning behavior.
            response = await client.get('/v1/host/contacts/resolve',
                headers={'Authorization': 'Bearer broad-secret'},
                params={'gateway': 'telegram', 'address': '987654321', 'create': 'true'})
            assert response.status_code == 200, response.text
            assert response.json()['contact_id'] != owner.contact_id
            assert response.json()['interaction_allowed'] is False
    finally:
        await store.close()

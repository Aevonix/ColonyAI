"""Native memory callbacks consume the real authenticated canonical projection."""
import importlib.util
import json
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])
if sys.argv[3]: sys.path.append(sys.argv[3])
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.contacts.config import ContactsConfig
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager, build_memory_context_block
from gateway.session_context import set_session_vars

home = Path(os.environ['HERMES_HOME']); home.mkdir(exist_ok=True)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
state = Path(os.environ['COLONY_STATE_DIR']); state.mkdir()
(home/'config.yaml').write_text(json.dumps({'memory': {'provider': 'colony-memory'}}))
(home/'colony-memory.json').write_text(json.dumps({
    'url': 'http://test', 'contact_id': 'fixture-owner', 'turn_writer': 'disabled'}))
contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
async def contact_setup():
    await contacts.connect()
    person = await contacts.create(display_name='Fixture Guest')
    await contacts.add_handle(person.contact_id, gateway='sms', address='+15550000001')
    return person.contact_id
person = asyncio.run(contact_setup())
host.set_contacts_store(contacts)
host.set_p8_runtime(None)
keys = state/'keys.json'
keys.write_text(json.dumps({'version': 1, 'principals': [{
    'principal': 'fixture-host', 'viewer_person_id': person,
    'scopes': ['context:read', 'turns:write', 'api:access'], 'audiences': ['viewer'],
    'turn_ingress_platforms': ['voice'], 'allow_unscoped_api': True,
    'credentials': [{'id': 'current', 'secret': 'fixture-key', 'status': 'active'}],
}]})); keys.chmod(0o600)
app = FastAPI(); app.include_router(host.router); app.include_router(host.v2_router)
app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keys))
original_client = httpx.Client
observed = []
try:
    with TestClient(app) as api:
        response = api.put('/v2/host/turns/native-first', headers={'Authorization': 'Bearer fixture-key'}, json={
            'identity': {'host_id': 'fixture'},
            'context': {'contact_id': person, 'session_id': 'native-first-session', 'turn_id': 'native-first'},
            'user_message': {'role': 'user', 'content': 'My orchard badge is cobalt-716.'},
        })
        assert response.status_code == 201, response.text
        def respond(request):
            response = api.request(request.method, str(request.url), headers=dict(request.headers), content=request.content)
            observed.append((request.url.path, response.status_code, response.json()))
            return httpx.Response(response.status_code, json=response.json())
        httpx.Client = lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs)
        provider = load_memory_provider('colony-memory')
        assert provider is not None
        assert Path(sys.modules[type(provider).__module__].__file__).resolve().is_relative_to(Path(sys.argv[1]))
        manager = MemoryManager(); manager.add_provider(provider)
        manager.initialize_all('native-second-session')
        set_session_vars(platform='sms', user_id='+15550000001', chat_id='fixture-chat', session_id='native-second-session')
        manager.on_turn_start(1, 'Which orchard badge?')
        context = build_memory_context_block(manager.prefetch_all('Which orchard badge?', session_id='native-second-session'))
        assert 'cobalt-716' in context, (context, observed)
        assert context.count('<memory-context>') == 1
        paths = [row[0] for row in observed]
        assert paths.index('/v1/host/context/projection-readiness') < paths.index('/v1/host/context/assemble')
        assert '/v1/host/context/temporal' not in paths
        projection = next(row[2]['projection_attestation'] for row in observed if row[0].endswith('/assemble'))
        assert projection['projection_backend'] == 'canonical_sources'
        assert projection['viewer_person_id'] == person and projection['p8_mode'] == 'off'
        assert projection['legacy_global_allowed'] is False
finally:
    httpx.Client = original_client
    asyncio.run(contacts.close())
print(json.dumps({'native_callback': True, 'scoped_recall': True}))
'''


def test_native_guest_recall_without_p8(artifacts, tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install qualified Hermes to exercise native memory callbacks")
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / "home"), HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled"),
               COLONY_STATE_DIR=str(tmp_path / "state"), COLONY_API_KEY="fixture-key",
               COLONY_OWNER_CONTACT_ID="fixture-owner", COLONY_RECALL_RERANK="off",
               COLONY_MEMORY_TURN_WRITER="disabled", COLONY_MEMORY_WORKER_TOOLS="0")
    result = run_python("-I", "-c", PROBE, installed, ROOT / "sidecar",
                        os.environ.get("COLONY_TEST_DEPENDENCY_PATH", ""), cwd=tmp_path, env=env)
    assert json.loads(result.stdout.splitlines()[-1]) == {"native_callback": True, "scoped_recall": True}

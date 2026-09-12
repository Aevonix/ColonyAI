"""Exact canonical source reads do not depend on graph hydration or its schemas."""
import pytest
from httpx import ASGITransport, AsyncClient

from apsimo.api.routers import host
from test_canonical_memory_search import memory_app
from test_turn_source_evidence import source_app


class NoGraph:
    def __getattr__(self, name):
        raise AssertionError('canonical read touched graph: ' + name)


@pytest.mark.asyncio
async def test_memory_read_opens_canonical_source_without_graph(memory_app, monkeypatch):
    app, ledger = memory_app
    monkeypatch.setattr(host, '_graph', NoGraph())
    ref = ledger.source_references(['report'], contact_id='person', session_id='later')[0]
    body = {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later', **ref}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        response = await client.post('/v1/host/memory/read', json=body)
        assert response.status_code == 200, response.text
        assert set(response.json()) == {'source'}
        assert 'Friday at nine' in response.json()['source']['content']
        for change in ({'memory_id':'old-node'}, {'audience':'owner'}, {'limit':5}):
            assert (await client.post('/v1/host/memory/read', json=body | change)).status_code == 422
        del body['source_id']
        assert (await client.post('/v1/host/memory/read', json=body)).status_code == 422


@pytest.mark.asyncio
async def test_memory_read_backend_failure_is_not_empty_success(memory_app, monkeypatch):
    from apsimo import turns
    app, ledger = memory_app
    ref = ledger.source_references(['report'], contact_id='person', session_id='later')[0]
    def unavailable(*args): raise OSError('fixture unavailable')
    monkeypatch.setattr(turns, 'get_turn_idempotency_ledger', unavailable)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        response = await client.post('/v1/host/memory/read', json={
            'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later', **ref})
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'memory_backend_unavailable'

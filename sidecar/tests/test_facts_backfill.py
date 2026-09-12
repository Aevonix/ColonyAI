"""Shared-facts CRUD remains canonical after retiring graph backfill."""

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.routers import host
from pacomind.tom.facts import SharedFactsStore
from test_turn_source_evidence import source_app


class GraphProbe:
    def __init__(self):
        self.calls = []

    async def store_memory(self, **kwargs):
        self.calls.append(('write', kwargs))
        return 'graph-copy'

    async def recall(self, **kwargs):
        self.calls.append(('read', kwargs))
        return [{
            'id': 'stale-graph-copy', 'type': 'fact', 'strength': .99,
            'content': 'The hydrofoil departure is stale.',
            'created_at': '2026-01-01T00:00:00+00:00',
        }]


@pytest.mark.asyncio
@pytest.mark.parametrize('graph_available', [False, True])
async def test_fact_crud_filters_and_pagination_use_only_canonical_rows(
        source_app, tmp_path, monkeypatch, graph_available):
    store = SharedFactsStore(str(tmp_path / 'facts.db'))
    graph = GraphProbe()
    monkeypatch.setattr(host, '_facts_store', store)
    monkeypatch.setattr(host, '_tom2_store', None)
    monkeypatch.setattr(host, '_graph', graph if graph_available else None)
    try:
        async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
            records = []
            for i in range(3):
                response = await client.post('/v1/host/mind/facts', json={
                    'contact_id': 'contact-a', 'fact': f'The hydrofoil desk has marker {i}.',
                    'source': 'told_by_contact', 'confidence': .9,
                    'metadata': {'observation': i},
                })
                assert response.status_code == 201, response.text
                records.append(response.json())
            store.create_fact(contact_id='contact-a', fact='Low confidence estimate.', confidence=.2)
            store.create_fact(contact_id='contact-a', fact='Expired observation.', source='told_by_contact',
                              expires_at='2020-01-01T00:00:00+00:00')
            store.create_fact(contact_id='contact-b', fact='Another contact has a private fact.')
            params = {'contact_id': 'contact-a', 'source': 'told_by_contact',
                      'min_confidence': .8, 'limit': 2}
            first = await client.get('/v1/host/mind/facts', params=params)
            second = await client.get('/v1/host/mind/facts', params={**params, 'offset': 2})
            assert first.status_code == second.status_code == 200
            assert first.json()['total'] == second.json()['total'] == 3
            pages = first.json()['facts'] + second.json()['facts']
            assert {row['id'] for row in pages} == {row['id'] for row in records}
            assert all(row['metadata']['observation'] in range(3) for row in pages)
            route = '/v1/host/mind/facts/' + records[0]['id']
            changed = await client.patch(route, json={'fact': 'The hydrofoil desk is amber.'})
            assert changed.status_code == 200, changed.text
            read = await client.get(route)
            assert read.json()['fact'] == 'The hydrofoil desk is amber.'
            assert read.json()['metadata'] == {'observation': 0}
            assert (await client.delete(route)).status_code == 204
            assert (await client.get(route)).status_code == 404
            remaining = await client.get('/v1/host/mind/facts', params=params)
            assert remaining.json()['total'] == 2
            assert records[0]['id'] not in {row['id'] for row in remaining.json()['facts']}
            assert 'stale-graph-copy' not in repr(remaining.json())
            # The obsolete operation cannot start a background graph write.
            retired = await client.post('/v1/host/mind/facts/backfill', json={'dry_run': False})
            assert retired.status_code == 405
            assert (await client.get('/v1/host/mind/facts/backfill')).status_code == 404
        assert graph.calls == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_manual_extraction_keeps_model_provenance_without_graph_copy(source_app, tmp_path, monkeypatch):
    class Extractor:
        async def extract_facts(self, text, contact_id, **kwargs):
            return [{'contact_id': contact_id, 'fact': 'The contact may know the hydrofoil desk.',
                     'source': 'inferred', 'confidence': .7,
                     'model_provenance': {'model_id': 'fixture-extractor'},
                     'memory_quality': {'classification': 'contact_knowledge_estimate'}}]

        def _can_extract(self, contact_id):
            return True

    store = SharedFactsStore(str(tmp_path / 'facts.db'))
    graph = GraphProbe()
    monkeypatch.setattr(host, '_facts_store', store)
    monkeypatch.setattr(host, '_tom_extractor', Extractor())
    monkeypatch.setattr(host, '_graph', graph)
    try:
        async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
            response = await client.post('/v1/host/tom/extract', json={
                'contact_id': 'contact-a', 'conversation_text': 'We discussed the hydrofoil desk.',
                'extract_affect': False})
            assert response.status_code == 200, response.text
            assert len(response.json()['facts']) == 1
            fact = store.list_facts(contact_id='contact-a')['facts'][0]
            assert fact['metadata'] == {
                'model_provenance': {'model_id': 'fixture-extractor'},
                'memory_quality': {'classification': 'contact_knowledge_estimate'},
                'automatic_projection': True,
            }
            assert store.automatic_view().list_facts(contact_id='contact-a')['total'] == 0
        assert graph.calls == []
    finally:
        store.close()

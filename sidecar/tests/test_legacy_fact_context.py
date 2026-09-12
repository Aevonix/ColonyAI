"""Legacy inspection survives; automatic recall requires current source support."""
import json

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.routers import host
from pacomind.tom.facts import SharedFactsStore
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.source_annotations import append as annotate_source
from test_contact_fact_recall import contact_context, context
from test_recall_unified_context import Graph, belief
from test_turn_source_evidence import source_app


@pytest.mark.asyncio
@pytest.mark.parametrize('p8_enabled', [False, True])
async def test_owner_context_excludes_unlinked_manual_and_legacy_mirrors_but_preserves_inspection(
        contact_context, monkeypatch, p8_enabled):
    runtime = contact_context
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    keys = json.loads(runtime.keyring.read_text())
    # The historical manual fact API requires an explicitly enabled unscoped
    # API principal. This fixture owner already has the exact person binding;
    # no production authority or route policy is changed.
    keys['principals'][0]['allow_unscoped_api'] = True
    keys['principals'][0]['scopes'].append('api:access')
    runtime.keyring.write_text(json.dumps(keys))
    if not p8_enabled:
        monkeypatch.setattr(host, '_p8_runtime', None)
    old = runtime.add('Hydrofoil legacy queue status repeats forever.', source_linked=False)
    # The graph contains both historical mirror formats, including one whose
    # original SQLite fact is subsequently deleted. No graph deletion is done.
    graph = Graph([
        {**belief(old['fact']), 'id': 'old-mirror', 'source_uri': 'tom:shared_fact'},
        {**belief('Hydrofoil marker-only old mirror.'), 'id': 'old-marker', 'metadata': "{'shared_fact': True}"},
        belief('A separate hydrofoil memory remains inspectable.'),
    ])
    monkeypatch.setattr(host, '_graph', graph)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        created = await client.post('/v1/host/mind/facts', headers={'Authorization': 'Bearer owner-key'}, json={
            'contact_id': 'contact-a', 'fact': 'Hydrofoil hand-entered note.',
            'source': 'told_by_contact', 'metadata': {'curated': True, 'owner_approved': True}})
        assert created.status_code == 201, created.text
        manual = created.json()
        for row in (old, manual):
            read = await client.get('/v1/host/mind/facts/'+row['id'], headers={'Authorization': 'Bearer owner-key'})
            assert read.status_code == 200 and read.json()['fact'] == row['fact']
        text = await context(client, 'hydrofoil')
        assert 'separate hydrofoil memory' not in text
        assert all(value not in text for value in (old['fact'], manual['fact'], 'marker-only'))
        assert graph.calls == []
        runtime.facts.delete_fact(old['id'])
        assert old['fact'] not in await context(client, 'hydrofoil')
    assert runtime.facts.get_fact(manual['id'])['metadata']['curated'] is True
    assert len(graph.rows) == 3  # Selection does not delete or migrate history.


@pytest.mark.asyncio
@pytest.mark.parametrize('p8_enabled', [False, True])
async def test_current_linked_estimate_has_native_citations_then_revision_and_erasure_exclude_it(
        contact_context, monkeypatch, p8_enabled):
    runtime = contact_context
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    if not p8_enabled:
        monkeypatch.setattr(host, '_p8_runtime', None)
    runtime.ledger.record_source('origin', contact_id='contact-a', session_id='earlier',
        messages=[{'role': 'user', 'content': 'The hydrofoil gate is violet.'}], derive_claims=False)
    lineage, _ = runtime.facts.source_input('origin', 'contact-a')
    linked = runtime.add('The contact knows the hydrofoil gate.', source_lineage=lineage)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', headers={'Authorization': 'Bearer owner-key'}, json={
            'identity': {'host_id': 'native-fixture'},
            'context': {'contact_id': 'contact-a', 'session_id': 'later-voice'},
            'incoming_message': {'role': 'user', 'content': 'hydrofoil gate'}})
        assert response.status_code == 200, response.text
        section = next(s for s in response.json()['sections'] if s['id'] == 'pacomind-memory')
        assert 'shared-fact:'+linked['id'] in section['body']
        assert section['citations'] == runtime.ledger.source_references(
            ['origin'], contact_id='contact-a', session_id='later-voice')
        # A retained projection from an older message revision is not current
        # merely because its source ID and person still exist.
        with runtime.ledger._connect() as conn:
            conn.execute('UPDATE turn_sources SET messages_json=? WHERE turn_id=?',
                (json.dumps([{'role': 'user', 'content': 'The hydrofoil gate is amber.'}]), 'origin'))
        assert 'shared-fact:'+linked['id'] not in await context(client, 'hydrofoil gate')
        assert runtime.facts._conn.execute('SELECT count(*) FROM shared_facts WHERE id=?', (linked['id'],)).fetchone()[0] == 1
        runtime.ledger.erase_sources(contact_id='contact-a', turn_ids=['origin'])
        assert 'shared-fact:'+linked['id'] not in await context(client, 'hydrofoil gate')


def test_automatic_window_filters_unlinked_before_limit_and_rechecks_scope(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turns.db')
    store = SharedFactsStore(str(tmp_path/'facts.db'), source_ledger=ledger)
    try:
        ledger.record_source('origin', contact_id='alice', session_id='prior',
            messages=[{'role': 'user', 'content': 'A durable hydrofoil fact.'}], derive_claims=False)
        lineage, _ = store.source_input('origin', 'alice')
        linked = store.create_fact(contact_id='alice', fact='A durable hydrofoil fact.', source_lineage=lineage)
        for i in range(30):
            store.create_fact(contact_id='alice', fact=f'Unlinked queue update {i}.')
        assert store.list_facts(contact_id='alice', limit=1)['facts'][0]['id'] != linked['id']
        assert [r['id'] for r in store.automatic_view().list_facts(contact_id='alice', limit=1)['facts']] == [linked['id']]
        assert store.list_facts(contact_id='alice')['total'] == 31
        with ledger._connect() as conn:
            conn.execute('UPDATE turn_sources SET contact_id=? WHERE turn_id=?', ('bob', 'origin'))
        assert store.automatic_view().get_fact(linked['id']) is None
        assert store.automatic_view().list_facts(contact_id='alice')['total'] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_linked_estimate_keeps_current_correction_and_exact_source_refs(contact_context, monkeypatch):
    runtime = contact_context
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    monkeypatch.setattr(host, '_p8_runtime', None)
    original = 'The hydrofoil gate is violet.'
    runtime.ledger.record_source('annotated-origin', contact_id='contact-a', session_id='prior',
        messages=[{'role': 'user', 'content': original}], derive_claims=False)
    lineage, _ = runtime.facts.source_input('annotated-origin', 'contact-a')
    fact = runtime.add(original, source_lineage=lineage)
    reference = runtime.ledger.source_references(['annotated-origin'], contact_id='contact-a', session_id='later')[0]
    correction = annotate_source(runtime.ledger, contact_id='contact-a', session_id='later',
        annotation_id='gate-correction', source_id='annotated-origin', source_version=reference['source_version'],
        excerpt=original, correction='The earlier gate color was mistaken; the gate is amber.',
        author_principal='fixture-owner')
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', headers={'Authorization': 'Bearer owner-key'}, json={
            'identity': {'host_id': 'native-fixture'},
            'context': {'contact_id': 'contact-a', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': 'hydrofoil gate'}})
    assert response.status_code == 200, response.text
    section = next(s for s in response.json()['sections'] if s['id'] == 'pacomind-memory')
    estimate = next(line for line in section['body'].splitlines() if 'shared-fact:'+fact['id'] in line)
    assert 'amber' in estimate and 'attributed_correction' in estimate
    assert {r['source_id'] for r in section['citations']} == {'annotated-origin', correction['source_id']}
    expected = runtime.ledger.source_references(['annotated-origin', correction['source_id']],
        contact_id='contact-a', session_id='later')
    assert {r['source_id']: r['source_version'] for r in section['citations']} == {
        r['source_id']: r['source_version'] for r in expected}


@pytest.mark.asyncio
@pytest.mark.parametrize('p8_enabled', [False, True])
async def test_canonical_estimate_keeps_correction_refs_and_never_revives_erased_note(
        contact_context, monkeypatch, p8_enabled):
    runtime = contact_context
    if not p8_enabled:
        monkeypatch.setattr(host, '_p8_runtime', None)
    original = 'The hydrofoil gate is violet.'
    runtime.ledger.record_source('enriched-origin', contact_id='contact-a', session_id='prior',
        messages=[{'role': 'user', 'content': original}], derive_claims=False)
    lineage, _ = runtime.facts.source_input('enriched-origin', 'contact-a')
    fact = runtime.add(original, source_lineage=lineage)
    reference = runtime.ledger.source_references(['enriched-origin'], contact_id='contact-a', session_id='later')[0]
    correction = annotate_source(runtime.ledger, contact_id='contact-a', session_id='later',
        annotation_id='enriched-correction', source_id='enriched-origin', source_version=reference['source_version'],
        excerpt=original, correction='The earlier gate color was mistaken; the gate is amber.',
        author_principal='fixture-owner')
    payload = {'identity': {'host_id': 'native-fixture'},
        'context': {'contact_id': 'contact-a', 'session_id': 'later'},
        'incoming_message': {'role': 'user', 'content': 'hydrofoil gate'}}
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble',
            headers={'Authorization': 'Bearer owner-key'}, json=payload)
        assert response.status_code == 200, response.text
        section = next(s for s in response.json()['sections'] if s['id'] == 'pacomind-memory')
        assert 'amber' in section['body'] and 'attributed_correction' in section['body']
        assert 'shared-fact:'+fact['id'] in section['body']
        expected = runtime.ledger.source_references(['enriched-origin', correction['source_id']],
            contact_id='contact-a', session_id='later')
        assert {r['source_id']: r['source_version'] for r in section['citations']} == {
            r['source_id']: r['source_version'] for r in expected}
        runtime.ledger.erase_sources(contact_id='contact-a', turn_ids=[correction['source_id']])
        after = await client.post('/v1/host/context/assemble',
            headers={'Authorization': 'Bearer owner-key'}, json=payload)
        assert after.status_code == 200, after.text
        assert not any(s['id'] == 'pacomind-memory' for s in after.json()['sections'])
    assert runtime.facts.get_fact(fact['id'])['fact'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unrelated_query', 'packet_budget', 'new_correction'])
async def test_canonical_omits_irrelevant_incomplete_or_changed_source_packet(
        contact_context, monkeypatch, change):
    runtime = contact_context
    original = 'The hydrofoil gate is violet.'
    fact = runtime.add(original)
    origin = fact['source_lineage']['turn_id']
    reference = runtime.ledger.source_references([origin], contact_id='contact-a', session_id='later')[0]
    def correct(identifier, text):
        return annotate_source(runtime.ledger, contact_id='contact-a', session_id='later',
            annotation_id=identifier, source_id=origin, source_version=reference['source_version'],
            excerpt=original, correction=text, author_principal='fixture-owner')
    correct('initial-correction', 'The gate is amber; the earlier color was mistaken.')
    query = 'hydrofoil gate'
    if change == 'unrelated_query':
        query = 'telescope calibration'
    elif change == 'packet_budget':
        monkeypatch.setenv('PACOMIND_RECALL_CONTEXT_MAX_CHARS', '500')
    else:
        from pacomind.memory.selection import RecallSelector
        class CorrectingSelector:
            async def select_context(self, *args, **kwargs):
                result = await RecallSelector().select_context(*args, **kwargs)
                calls.append(correct('later-correction', 'A second observer reports a copper gate; resolve the disagreement.'))
                return result
        calls = []
        monkeypatch.setattr(host, '_context_recall_selector', (host._reranker, CorrectingSelector()))
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble',
            headers={'Authorization': 'Bearer owner-key'}, json={
                'identity': {'host_id': 'native-fixture'},
                'context': {'contact_id': 'contact-a', 'session_id': 'later'},
                'incoming_message': {'role': 'user', 'content': query}})
    assert response.status_code == 200, response.text
    assert not any(s['id'] == 'pacomind-memory' for s in response.json()['sections'])
    if change == 'new_correction':
        assert len(calls) == 1
    assert runtime.facts.get_fact(fact['id'])['fact'] == original

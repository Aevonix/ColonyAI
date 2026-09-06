"""Full authenticated context uses one selector for current contact estimates."""
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.server import _attach_p8_runtime
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.turns import TurnIdempotencyLedger
from test_recall_unified_context import Graph, Reranker, belief, calibrate
from test_scoped_api_authority import _principal, _write_keyring
from test_tom_p8_server_integration import _authority, _request
from test_turn_source_evidence import source_app


@pytest.fixture
def contact_context(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    monkeypatch.setenv('COLONY_OWNER_PERSON_ID', 'contact-a')
    monkeypatch.setenv('COLONY_RECIPIENT_SIMULATOR_MODE', 'shadow')
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    facts = SharedFactsStore(str(tmp_path/'facts.db'), source_ledger=ledger)
    monkeypatch.setattr(host, '_facts_store', facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    keyring = tmp_path/'keys.json'
    principals = [_principal(principal=who, secret=who+'-key', viewer=person,
        scopes=['context:read', 'memory:write']) for who, person in [('owner','contact-a'), ('other','contact-b')]]
    for principal in principals:
        principal['allow_unscoped_api'] = False
    _write_keyring(keyring, principals)
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring), api_key=None)

    def add(text, *, person='contact-a', enveloped=True, source_lineage=None):
        row = facts.create_fact(contact_id=person, fact=text, source='inferred',
                                confidence=.95, source_lineage=source_lineage)
        if enveloped:
            viewer = host._p8_viewer_for_request(_request(_authority(person)), person)
            runtime.append_shared_fact(row, producer=viewer, origin='server')
        return row

    yield SimpleNamespace(app=source_app, ledger=ledger, facts=facts, add=add)
    facts.close()


async def context(client, query, *, person='contact-a', credential='owner'):
    response = await client.post('/v1/host/context/assemble',
        headers={'Authorization':'Bearer '+credential+'-key'}, json={
            'identity': {'host_id':'fixture'},
            'context': {'contact_id':person, 'session_id':'later'},
            'incoming_message': {'role':'user', 'content':query},
        })
    assert response.status_code == 200, response.text
    sections = response.json()['sections']
    assert not any(s['id'] == 'colony-shared-facts' for s in sections)
    memories = [s['body'] for s in sections if s['id'] == 'colony-memory']
    assert len(memories) <= 1
    return memories[0] if memories else ''


@pytest.mark.asyncio
@pytest.mark.parametrize('reranker_unavailable', [False, True])
async def test_empty_greeting_and_irrelevant_query_do_not_inject_authorized_facts(contact_context, monkeypatch, reranker_unavailable):
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    if reranker_unavailable:
        reranker = Reranker(fail=True)
        calibrate(monkeypatch, reranker)
        monkeypatch.setattr(host, '_reranker', reranker)
    runtime = contact_context
    useful = runtime.add('The hydrofoil departure is at the eastern jetty.')
    for i in range(27):
        runtime.add(f'The greenhouse humidity observation is {i}.')
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        for query in ('', 'hello', 'telescope calibration'):
            assert await context(client, query) == ''
        text = await context(client, 'hydrofoil departure')
        assert useful['fact'] in text and 'greenhouse' not in text
        assert 'shared-fact:'+useful['id'] in text
        if reranker_unavailable:
            assert reranker.calls == [[useful['fact']]]
            assert '"rerank_status": "unavailable"' in text
    assert runtime.facts.list_facts()['total'] == 28  # Read selection does not delete history.


class DiscriminatingReranker(Reranker):
    async def rerank(self, query, documents, top_k):
        self.calls.append(list(documents))
        return [{'index':i, 'score':.99 if 'hydrofoil departure' in doc.lower() else .01}
                for i, doc in enumerate(documents)]


@pytest.mark.asyncio
async def test_contact_estimates_share_real_selection_abstention_and_budget(contact_context, monkeypatch):
    runtime = contact_context
    reranker = DiscriminatingReranker()
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, '_reranker', reranker)
    monkeypatch.setenv('COLONY_RECALL_CONTEXT_MAX_CHARS', '1600')
    for i in range(8):
        runtime.add(f'The hydrofoil departure desk has neutral marker {i}.')
    rejected = runtime.add('The hydrofoil hull has a cosmetic scratch.')
    graph = Graph([belief('A hydrofoil departure was reported.')])
    monkeypatch.setattr(host, '_graph', graph)
    runtime.ledger.record_source('quote', contact_id='contact-a', session_id='earlier',
        messages=[{'role':'user','content':'The hydrofoil departure time is being checked.'}], derive_claims=False)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        text = await context(client, 'hydrofoil departure')
    assert len(reranker.calls) == 1 and rejected['fact'] in reranker.calls[0]
    assert rejected['fact'] not in text
    assert len(text) <= 1600 and text.count('\n- ') <= 5
    assert '"kind": "contact_knowledge_estimate"' in text and '"kind": "source_quote"' in text
    estimate = next(line for line in text.splitlines() if '"kind": "contact_knowledge_estimate"' in line)
    assert '"state": "unverified"' in estimate and '"recorded_source": "inferred"' in estimate
    assert '"confidence"' not in estimate and '"source_message_hash"' not in estimate
    assert all(not key.startswith('shared-fact:') for key in graph.used)


@pytest.mark.asyncio
async def test_other_viewer_and_unenveloped_history_never_reach_reranker(contact_context, monkeypatch):
    runtime = contact_context
    own = runtime.add('The hydrofoil departure desk is amber.')
    unlinked = runtime.add('The hydrofoil departure legacy marker is bronze.', enveloped=False)
    foreign = runtime.add('The hydrofoil departure private marker is copper.', person='contact-b')
    reranker = DiscriminatingReranker()
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, '_reranker', reranker)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        text = await context(client, 'hydrofoil departure')
        assert own['fact'] in text and foreign['fact'] not in text and unlinked['fact'] not in text
        assert reranker.calls == [[own['fact']]]
        other = await context(client, 'hydrofoil departure', person='contact-b', credential='other')
        assert foreign['fact'] in other and own['fact'] not in other and unlinked['fact'] not in other
    assert runtime.facts.get_fact(unlinked['id']) is not None


@pytest.mark.asyncio
async def test_source_erasure_removes_estimate_from_full_context(contact_context, monkeypatch):
    runtime = contact_context
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    runtime.ledger.record_source('fact-origin', contact_id='contact-a', session_id='earlier',
        messages=[{'role':'user','content':'The hydrofoil departure gate is violet.'}], derive_claims=False)
    lineage, _ = runtime.facts.source_input('fact-origin', 'contact-a')
    row = runtime.add('The contact knows the hydrofoil departure gate.', source_lineage=lineage)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        assert 'shared-fact:'+row['id'] in await context(client, 'hydrofoil departure')
        forgotten = await client.post('/v1/host/memory/sources/forget',
            headers={'Authorization':'Bearer owner-key'},
            json={'contact_id':'contact-a','source_ids':['fact-origin']})
        assert forgotten.status_code == 200 and forgotten.json()['shared_facts_cleanup'] == 'complete'
        assert await context(client, 'hydrofoil departure') == ''
    assert runtime.facts.get_fact(row['id']) is None

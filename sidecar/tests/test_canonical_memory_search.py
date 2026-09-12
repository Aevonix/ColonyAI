"""Explicit search shares automatic recall's evidence, scope and model projections."""
import asyncio
import json
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.api.routers import host
from apsimo.api.middleware import ApiKeyMiddleware
from apsimo.turns import TurnIdempotencyLedger
from test_scoped_api_authority import _principal, _write_keyring
from test_turn_source_evidence import source_app, recalled
from test_source_vectors import setup, drain, Pipeline


@pytest.fixture
def memory_app(source_app, tmp_path, monkeypatch):
    from apsimo import vector
    monkeypatch.setattr(vector, 'get_store', lambda: None)
    monkeypatch.setattr(vector, 'get_pipeline', lambda: None)
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    monkeypatch.setattr(host, '_facts_store', None)
    path = tmp_path/'keys.json'
    scopes = ['memory:search', 'memory:read', 'memory:write', 'context:read', 'api:access', 'turns:write']
    principals = [_principal(principal=person, secret=person, viewer=person, scopes=scopes)
                  for person in ('person', 'other')]
    _write_keyring(path, principals)
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(path))
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('report', contact_id='person', session_id='work', messages=[
        {'role': 'assistant', 'content': 'The hydrofoil departure is Friday at nine.'}])
    ledger.record_source('private-other', contact_id='other', session_id='work', messages=[
        {'role': 'user', 'content': 'The hydrofoil cabinet has the private amber label.'}])
    ledger.record_source('scoped', contact_id='person', session_id='original', scope='session', messages=[
        {'role': 'user', 'content': 'The hydrofoil session secret is cobalt.'}])
    return source_app, ledger


def body(**changes):
    return {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later',
            'query': 'hydrofoil', **changes}


async def search(client, **changes):
    response = await client.post('/v1/host/memory/search', json=body(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def annotate(ledger, annotation_id, correction):
    ref = ledger.source_references(['report'], contact_id='person', session_id='later')[0]
    return ledger.append_source_annotation(contact_id='person', session_id='later',
        annotation_id=annotation_id, **ref, excerpt='Friday at nine.', correction=correction,
        author_principal='person')


@pytest.mark.asyncio
async def test_search_matches_automatic_scope_and_correction_budget(memory_app, monkeypatch):
    app, ledger = memory_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        first = await search(client)
        assert 'Friday at nine' in first['content']
        assert 'amber' not in first['content'] and 'cobalt' not in first['content']
        assert first['content'] == await recalled(client, contact='person', session='later')
        annotation = annotate(ledger, 'audit', 'Friday is confirmed; nine is unsupported.')
        corrected = await search(client)
        assert 'nine is unsupported' in corrected['content']
        assert {ref['source_id'] for ref in corrected['source_refs']} == {'report', annotation['source_id']}
        assert corrected['content'] == await recalled(client, contact='person', session='later')
        monkeypatch.setenv('COLONY_RECALL_CONTEXT_MAX_CHARS', '100')
        empty = await search(client)
        assert empty['content'] == '' and empty['count'] == 0 and empty['source_refs'] == []


@pytest.mark.asyncio
async def test_search_distinguishes_scope_schema_empty_and_backend_failure(memory_app, monkeypatch):
    app, _ = memory_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        for changes in ({'session_id':''}, {'limit':'5'}, {'limit':21}, {'context':{}}, {'query':'x'*4097}):
            r = await client.post('/v1/host/memory/search', json=body(**changes))
            assert r.status_code == 422, r.text
        r = await client.post('/v1/host/memory/search', json=body(person_id='other'))
        assert r.status_code == 403
        empty = await search(client, query='nonexistent vermilion planet')
        assert empty['count'] == 0 and empty['content'] == '' and empty['watermark'] == 0
        def broken(*a, **kw): raise RuntimeError('storage fixture')
        monkeypatch.setattr(TurnIdempotencyLedger, 'search_sources', broken)
        r = await client.post('/v1/host/memory/search', json=body())
        assert r.status_code == 503 and r.json()['detail']['code'] == 'memory_backend_unavailable'


@pytest.mark.asyncio
async def test_search_annotation_freshness_is_per_receipt_and_can_recover(memory_app):
    app, ledger = memory_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        async def check(*packets):
            refs = {r['source_id']: r for p in packets for r in p['source_refs']}
            r = await client.post('/v1/host/memory/sources/erasures', json={
                'contact_id':'person', 'session_id':'later', 'after':0,
                'source_refs':list(refs.values()),
                'annotation_checks':[c for p in packets for c in p['annotation_checks']]})
            assert r.status_code == 200, r.text
            return r.json()
        old = await search(client)
        assert (await check(old))['annotation_checks_current'] == [True]
        annotate(ledger, 'audit1', 'The time is unsupported.')
        stale = await check(old)
        assert stale['sources_current'] is True
        assert stale['annotation_checks_current'] == [False]
        fresh = await search(client)
        assert (await check(old, fresh))['annotation_checks_current'] == [False, True]
        annotate(ledger, 'audit2', 'A second source confirms the day but not the hour.')
        assert (await check(fresh))['annotation_checks_current'] == [False]
        newest = await search(client)
        assert (await check(newest))['annotation_checks_current'] == [True]
        ledger.erase_sources(contact_id='person', turn_ids=['report'])
        erased = await check(newest)
        assert erased['sources_current'] is False and erased['annotation_checks_current'] == [False]
        assert (await search(client))['count'] == 0


@pytest.mark.asyncio
async def test_search_uses_real_semantic_index_and_keeps_lexical_on_model_swap(memory_app, tmp_path, monkeypatch):
    from apsimo import vector
    app, _ = memory_app
    ledger, store, pipeline, projection = await setup(tmp_path)
    await drain(projection)
    monkeypatch.setattr(vector, 'get_store', lambda: store)
    monkeypatch.setattr(vector, 'get_pipeline', lambda: pipeline)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        semantic = await search(client, query='vessel departure identifier')
        assert 'Friday at nine' in semantic['content'] and semantic['retrieval']['semantic'] == 'ready'
        assert 'amber' not in semantic['content'] and 'cobalt' not in semantic['content']
        monkeypatch.setattr(vector, 'get_pipeline', lambda: Pipeline(name='different-model'))
        lexical = await search(client)
        assert 'Friday at nine' in lexical['content'] and lexical['retrieval']['semantic'] == 'failed'
        class Failing(Pipeline):
            async def embed_query(self, query): raise RuntimeError('embedding unavailable')
        monkeypatch.setattr(vector, 'get_pipeline', lambda: Failing())
        assert (await search(client))['retrieval']['semantic'] == 'failed'


@pytest.mark.asyncio
async def test_unprojected_lance_table_is_not_healthy_empty_search(memory_app, tmp_path, monkeypatch):
    from apsimo import vector
    from apsimo.vector import Collection
    app, _ = memory_app
    _, store, pipeline, projection = await setup(tmp_path)
    monkeypatch.setattr(vector, 'get_store', lambda: store)
    monkeypatch.setattr(vector, 'get_pipeline', lambda: pipeline)
    queries = []
    original_embed = pipeline.embed_query

    async def embed(query):
        queries.append(query)
        return await original_embed(query)

    monkeypatch.setattr(pipeline, 'embed_query', embed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization': 'Bearer person'}) as client:
        empty = await search(client, query='unmatched vermilion planet')
        assert empty['count'] == 0 and empty['retrieval']['semantic'] == 'ready'
        assert queries == ['unmatched vermilion planet']

        # Keep the real active generation and remove only its required source
        # projection column. An incompatible table must not look like no hits.
        table = await store._table(Collection.CONVERSATIONS)
        await table.drop_columns(['scope_key'])
        lexical = await search(client)
        assert 'Friday at nine' in lexical['content']
        assert 'amber' not in lexical['content'] and 'cobalt' not in lexical['content']
        assert lexical['retrieval']['semantic'] == 'failed'
        assert queries == ['unmatched vermilion planet']  # No unnecessary inference.

        # The existing projection worker supplies the missing column and
        # records. A later semantic query recovers without another readiness API.
        await drain(projection)
        restored = await search(client, query='vessel departure identifier')
        assert 'Friday at nine' in restored['content']
        assert restored['retrieval']['semantic'] == 'ready'
        assert queries == ['unmatched vermilion planet', 'vessel departure identifier']


@pytest.mark.asyncio
async def test_erasure_during_semantic_await_never_returns_stale_excerpt(memory_app, tmp_path, monkeypatch):
    from apsimo import vector
    app, _ = memory_app
    ledger, store, pipeline, projection = await setup(tmp_path)
    await drain(projection)
    async def erase_then_embed(query):
        ledger.erase_sources(contact_id='person', turn_ids=['report'])
        return pipeline.vector(query)
    monkeypatch.setattr(pipeline, 'embed_query', erase_then_embed)
    monkeypatch.setattr(vector, 'get_store', lambda: store)
    monkeypatch.setattr(vector, 'get_pipeline', lambda: pipeline)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        result = await search(client)
        assert result['content'] == '' and result['source_refs'] == []


@pytest.mark.asyncio
async def test_direct_reasoning_tool_uses_only_trusted_request_scope(memory_app, monkeypatch):
    from apsimo.reasoning import ToolExecutor
    app, _ = memory_app
    class Graph:
        async def recall(self, **kwargs):
            raise AssertionError('graph fallback')
    executor = ToolExecutor(registry=SimpleNamespace(graph=Graph()))
    monkeypatch.setattr(host, '_tool_executor', executor)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        async def invoke(person, args, context=True):
            request = {'identity': {'host_id': 'fixture'}, 'name': 'colony_memory_search', 'arguments': args}
            if context:
                request['context'] = {'contact_id': person, 'session_id': 'later'}
            r = await client.post('/v1/host/reasoning/tools/invoke', headers={'Authorization':'Bearer '+person}, json=request)
            assert r.status_code == 200, r.text
            return json.loads(r.json()['result'])
        owner, other = await asyncio.gather(invoke('person', {'query':'hydrofoil'}),
                                           invoke('other', {'query':'hydrofoil'}))
        assert 'Friday at nine' in owner['content'] and 'amber' not in owner['content']
        assert 'amber' in other['content'] and 'Friday' not in other['content']
        denied = await invoke('person', {'query':'hydrofoil', 'person_id':'other'})
        assert denied['status'] == 'unavailable'
        unbound = await invoke('person', {'query':'hydrofoil'}, context=False)
        assert unbound['status'] == 'unavailable'
        current = await invoke('person', {'query':'hydrofoil'})
        assert current['source_refs'] == owner['source_refs']


@pytest.mark.asyncio
async def test_reasoning_http_loop_supplies_canonical_tool_packet_to_processor(memory_app, monkeypatch):
    from apsimo.reasoning import ToolExecutor, ReasoningLoop
    app, _ = memory_app
    class Processor:
        def __init__(self): self.calls = []
        async def complete(self, messages, **kwargs):
            self.calls.append(messages)
            calls = [] if len(self.calls) > 1 else [SimpleNamespace(id='search', function=SimpleNamespace(
                name='colony_memory_search', arguments=json.dumps({'query':'hydrofoil'})))]
            return SimpleNamespace(content='done' if not calls else '', usage={},
                raw=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls))]))
    processor = Processor()
    monkeypatch.setattr(host, '_reasoning_loop', ReasoningLoop(model=processor,
        tools=ToolExecutor(registry=SimpleNamespace(graph=None))))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        r = await client.post('/v1/host/reasoning/turn', json={
            'identity':{'host_id':'fixture'}, 'context':{'contact_id':'person','session_id':'later'},
            'messages':[{'role':'user','content':'Find the hydrofoil time.'}],
            'available_tools':['colony_memory_search']})
        assert r.status_code == 200 and r.json()['status'] == 'completed', r.text
    evidence = json.loads(next(m['content'] for m in processor.calls[-1] if m['role']=='tool'))
    assert 'Friday at nine' in evidence['content'] and evidence['source_refs'][0]['source_id'] == 'report'
    assert 'amber' not in evidence['content']

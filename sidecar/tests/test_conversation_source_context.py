"""Direct input context is attributed conversation, not promoted testimony."""
import copy
import json
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.beliefs.source_time import MemoryTimeQuery
from pacomind.memory.recall import pack_memory_context
from pacomind.memory.search import CollectedSources, select_memory
from pacomind.memory.selection import RecallSelector
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.idempotency import source_message_hash
from pacomind.turns.source_annotations import expand, current_candidates
from test_canonical_memory_search import memory_app, search
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


SCOPE = {'contact_id': 'person', 'session_id': 'later'}
INPUT = 'Correction: the prototype is in the east laboratory. It moves west on May 14.'
REPLY = 'Corrected. East laboratory now, west on the 14th. I had the schedule reversed.'


def seed(ledger, *, supplied=False, request=INPUT, response=REPLY, dependencies=()):
    user = {'role': 'user', 'content': request}
    answer = {'role': 'assistant', 'content': response}
    if dependencies:
        answer['_supplied_sources'] = list(dependencies)
    if supplied:
        ledger.record_source('input', contact_id='person', session_id='original',
                             messages=[user], derive_claims=False)
        answer['_supplied_inputs'] = [{'source_id': 'input',
            'input_message_hash': source_message_hash('original', user)}]
    ledger.record_source('reply', contact_id='person', session_id='original',
        messages=[answer] if supplied else [user, answer], derive_claims=False)
    # Start from an actual canonically owned response hit, including the case
    # where its input did not independently match the current retrieval query.
    hit = next(row for row in ledger.search_sources('laboratory', **SCOPE, limit=20)
               if row['turn_id'] == 'reply' and row['role'] == 'assistant')
    return [hit]


def prepare(ledger, hits, **scope):
    return SourceClaimProjection(ledger).prepare_context([], hits, **(scope or SCOPE),
        time_query=MemoryTimeQuery(), include_conversation_inputs=True)[1]


async def packet(ledger, hits, *, selector=None, query='Where is the prototype now?', limit=5):
    return await select_memory(CollectedSources(ledger, **SCOPE,
        watermark=ledger.erasure_watermark('person'), hits=hits),
        query=query, selector=selector or RecallSelector(), limit=limit, timezone_name='UTC')


@pytest.mark.asyncio
@pytest.mark.parametrize('supplied', [False, True])
async def test_owner_correction_and_acknowledgment_are_one_separately_attributed_candidate(tmp_path, monkeypatch, supplied):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    hits = seed(ledger, supplied=supplied)
    before = copy.deepcopy(hits)
    result = await packet(ledger, hits)
    row, = result.selected
    assert row['kind'] == 'conversation_pair' and row['epistemic_state'] == 'quotation'
    conversation = json.loads(row['content'])
    assert conversation['input']['role'] == 'user' and conversation['input']['quote'] == INPUT
    assert conversation['response']['role'] == 'assistant' and conversation['response']['quote'] == REPLY
    assert conversation['input']['event_time'] == 'unprojected'
    assert conversation['input']['source_id'] == ('input' if supplied else 'reply')
    assert conversation['response']['source_id'] == 'reply'
    for role in ('input', 'response'):
        source = conversation[role]
        assert {'source_id': source['source_id'], 'source_version': source['source_version']} in result.source_refs
        assert source['source_message_hash'] in result.annotation_checks[0]['message_hashes'][source['source_id']]
    assert result.content.index(INPUT) < result.content.index(REPLY)
    assert len(result.content) <= 6000 and hits == before
    assert len(prepare(ledger, hits + ledger.search_sources('prototype', **SCOPE))) == 1


@pytest.mark.asyncio
async def test_request_remains_a_request_and_unrelated_linked_result_is_not_injected(tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    unrelated = 'ARCHIVED TOOL RESULT: obsolete compatibility instructions for a different project.'
    ledger.record_source('observation', contact_id='person', session_id='original',
        messages=[{'role': 'tool', 'content': unrelated}], derive_claims=False)
    refs = ledger.source_references(['observation'], **SCOPE)
    request = 'Which laboratory contains the prototype?'
    reply = 'The laboratory location is unknown from this inspection.'
    hits = seed(ledger, supplied=True, request=request, response=reply, dependencies=refs)
    result = await packet(ledger, hits)
    row, = result.selected
    pair = json.loads(row['content'])
    assert pair['input']['quote'] == request and pair['input']['role'] == 'user'
    assert pair['response']['quote'] == reply
    assert 'A request is not an assertion' in pair['interpretation']
    assert unrelated not in result.content
    # An erasure dependency is not evidence that its original was selected or read.
    assert 'observation' in {ref['source_id'] for ref in result.source_refs}
    assert set(row['_source_message_hashes']) == {'input', 'reply'}


@pytest.mark.asyncio
async def test_pair_uses_existing_single_relevance_pass_and_can_lose_to_a_useful_result(tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'on')
    monkeypatch.delenv('PACOMIND_RECALL_RERANK_MIN_SCORE', raising=False)
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    hits = seed(ledger)
    ledger.record_source('measurement', contact_id='person', session_id='original',
        messages=[{'role': 'tool', 'content': 'Laboratory temperature measured 21.3 C.'}], derive_claims=False)
    hits += [r for r in ledger.search_sources('temperature', **SCOPE) if r['turn_id'] == 'measurement']
    calls = []

    async def rank(query, documents, top_k):
        calls.append(documents)
        assert any(INPUT in text and REPLY in text for text in documents)
        return [{'index': i, 'score': 10 if '21.3 C' in text else 0} for i, text in enumerate(documents)]

    result = await packet(ledger, hits, selector=RecallSelector(rank), query='laboratory temperature', limit=1)
    assert len(calls) == 1 and result.selected[0]['role'] == 'tool'
    assert INPUT not in result.content and REPLY not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['input', 'reply'])
async def test_pair_carries_current_owner_annotation_and_erasure(tmp_path, monkeypatch, target):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    hits = seed(ledger, supplied=True)
    original = await packet(ledger, hits)
    ref = ledger.source_references([target], **SCOPE)[0]
    ledger.append_source_annotation(**SCOPE, annotation_id='owner-note', **ref,
        excerpt=INPUT if target == 'input' else REPLY,
        correction='The move date was a proposal, not a completed transfer.', author_principal='person')
    assert current_candidates(ledger, original.selected, **SCOPE) == []
    annotated = await packet(ledger, hits)
    row, = annotated.selected
    assert row['epistemic_state'] == 'correction_evidence'
    assert 'proposal, not a completed transfer' in annotated.content
    assert INPUT in row['ranking_text'] and REPLY in row['ranking_text']
    ledger.erase_sources(contact_id='person', turn_ids=[target])
    assert current_candidates(ledger, annotated.selected, **SCOPE) == []
    assert (await packet(ledger, hits)).content == ''


@pytest.mark.parametrize('change', ['partial-erasure', 'changed-revision', 'cross-person', 'session-checkpoint'])
def test_missing_changed_or_unattested_input_cannot_become_paired_evidence(tmp_path, change):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    hits = seed(ledger)
    rows = prepare(ledger, hits)
    assert rows[0]['kind'] == 'conversation_pair'
    if change == 'partial-erasure':
        ledger.erase_sources(contact_id='person', old_text=INPUT, session_id='original')
        assert expand(ledger, rows, **SCOPE) == []
    else:
        with sqlite3.connect(ledger.db_path) as conn:
            if change == 'changed-revision':
                messages = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='reply'").fetchone()[0])
                messages[0]['content'] = 'Changed input without the previous assertion.'
                conn.execute("UPDATE turn_sources SET messages_json=? WHERE turn_id='reply'", (json.dumps(messages),))
            elif change == 'cross-person':
                conn.execute("UPDATE turn_sources SET contact_id='someone-else' WHERE turn_id='reply'")
            else:
                conn.execute("UPDATE turn_sources SET scope='session' WHERE turn_id='reply'")
        assert expand(ledger, rows, **SCOPE) == []
        assert not any(row['kind'] == 'conversation_pair' for row in prepare(ledger, hits)) or change == 'changed-revision'


def test_oversized_pair_offers_full_source_reference_without_half_a_conversation(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    rows = expand(ledger, prepare(ledger, seed(ledger)), **SCOPE)
    _, full = pack_memory_context(rows)
    bounded, content = pack_memory_context(rows, max_chars=len(full)-1)
    assert len(content) < len(full) and INPUT not in content and REPLY not in content
    assert not bounded or bounded[0]['conversation_context'] == 'full_source_required'
    if bounded:
        assert bounded[0]['source_anchors'] == rows[0]['source_anchors']
        assert bounded[0]['_annotation_source_refs'] == rows[0]['_annotation_source_refs']


@pytest.mark.asyncio
async def test_projected_assertion_stays_separate_and_does_not_resurrect_corrected_parent_text(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    old = 'The prototype is in the south laboratory.'
    hits = seed(ledger, request=old)
    # Admit a structured location, then correct it in a separate ordinary source.
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("INSERT INTO source_claim_jobs(turn_id) VALUES ('reply')")
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(Model({old: claim(old, 'south laboratory', subject='prototype', predicate='location')}))
    correction = 'Correction: the prototype is in the north laboratory.'
    ledger.record_source('corrected', contact_id='person', session_id='original',
        messages=[{'role': 'user', 'content': correction}])
    assert await projection.process_one(Model({correction: claim(correction, 'north laboratory',
        subject='prototype', predicate='location', operation='correct', match_prior=True)}))
    rows = prepare(ledger, hits)
    assert not any(row['kind'] == 'conversation_pair' for row in rows)
    assert old not in pack_memory_context(rows)[1]
    assert 'north laboratory' in pack_memory_context(rows)[1]


@pytest.mark.asyncio
async def test_actual_scoped_search_and_automatic_context_share_the_pair(memory_app):
    app, ledger = memory_app
    seed(ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization': 'Bearer person'}) as client:
        explicit = await search(client, query='laboratory')
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'},
            'context': {'contact_id': 'person', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': 'laboratory'}, 'include_initiatives': False})
        assert response.status_code == 200
        automatic = '\n'.join(row['body'] for row in response.json()['sections'] if row['id'] == 'pacomind-memory')
        assert explicit['content'] == automatic
        assert INPUT in automatic and REPLY in automatic and 'conversation_pair' in automatic

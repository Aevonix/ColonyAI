"""Current-work selection uses canonical question/answer provenance only."""
import copy
import json
import sqlite3
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import executions
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.beliefs.source_time import MemoryTimeQuery
from pacomind.memory.selection import RecallSelector, current_work_query
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.idempotency import source_message_hash
from pacomind.turns.source_annotations import expand
from test_turn_source_evidence import source_app


CURRENT = 'What are you doing right now across the active sessions?'
STATUS = 'I am doing a parcel import, with one delegated worker observed in its model phase.'


def seed(ledger, *, request=CURRENT, pair='supplied', contact='contact-a', scope='person'):
    user = {'role': 'user', 'content': request}
    assistant = {'role': 'assistant', 'content': STATUS}
    if pair == 'supplied':
        ledger.record_source('input', contact_id=contact, session_id='source-session',
                             scope=scope, messages=[user], derive_claims=False)
        assistant['_supplied_inputs'] = [{'source_id': 'input',
            'input_message_hash': source_message_hash('source-session', user)}]
    messages = [user, assistant] if pair == 'same' else [assistant]
    ledger.record_source('answer', contact_id=contact, session_id='source-session',
                         scope=scope, messages=messages, derive_claims=False)


def prepared(ledger, *, contact='contact-a', session='later', classify=True):
    hits = ledger.search_sources('doing parcel import', contact_id=contact, session_id=session, limit=20)
    _, rows = SourceClaimProjection(ledger).prepare_context([], hits,
        contact_id=contact, session_id=session, time_query=MemoryTimeQuery(),
        classify_work_replies=classify)
    return expand(ledger, rows, contact_id=contact, session_id=session)


@pytest.mark.asyncio
@pytest.mark.parametrize('pair', ['same', 'supplied'])
@pytest.mark.parametrize('query,omitted', [
    (CURRENT, True),
    ('What are you currently doing?', True),
    ('What are you currently working on?', True),
    ('What were you doing yesterday?', False),
    ('What are you doing right now compared with last time?', False),
    ('What are you doing right now, and what procedure should I use to recover it?', False),
])
async def test_four_query_kinds_keep_history_and_instructions(tmp_path, monkeypatch, pair, query, omitted):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger, pair=pair)
    rows = prepared(ledger)
    originals = copy.deepcopy(rows)
    selected, text = await RecallSelector().select_context(query, [], rows,
        current_work_available=True, limit=20, max_chars=12000)
    assert (STATUS not in text) is omitted
    assert rows == originals
    # Automatic selection never edits canonical search or its exact attribution.
    hit = next(r for r in ledger.search_sources('parcel', contact_id='contact-a', session_id='later')
               if r['role'] == 'assistant')
    assert hit['content'] == STATUS and hit['turn_id'] == 'answer'
    assert hit['contact_id'] == 'contact-a' and hit['scope'] == 'person'
    if not omitted:
        answer = next(r for r in selected if r['role'] == 'assistant')
        assert answer['source_message_hash'] == hit['source_message_hash']


@pytest.mark.asyncio
@pytest.mark.parametrize('pair', ['same', 'supplied'])
@pytest.mark.parametrize('query', [
    'What are you currently doing?',
    'What are you currently working on?',
])
async def test_currently_before_activity_classifies_canonical_reply(tmp_path, query, pair):
    assert current_work_query(query)
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger, request=query, pair=pair)
    rows = prepared(ledger)
    assert any(row.get('_current_work_status_reply') for row in rows)
    _, text = await RecallSelector().select_context(CURRENT, [], rows, current_work_available=True)
    assert STATUS not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('pair,original_query', [
    ('unknown', CURRENT),
    ('supplied', 'What are you doing right now? Also give the steps for resuming an import.'),
    ('same', 'What were you doing yesterday?'),
])
async def test_unknown_or_mixed_original_request_is_not_classified(tmp_path, pair, original_query):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger, pair=pair, request=original_query)
    rows = prepared(ledger)
    assert not any(r.get('_current_work_status_reply') for r in rows)
    _, text = await RecallSelector().select_context(CURRENT, [], rows, current_work_available=True)
    assert STATUS in text


@pytest.mark.asyncio
@pytest.mark.parametrize('comparison', [
    'What are you doing right now compared with Monday?',
    'What are you doing right now versus Monday?',
    'What are you currently doing compared with Monday?',
    'What are you currently doing versus Monday?',
    'What are you currently working on compared with Monday?',
    'What are you currently working on versus Monday?',
])
@pytest.mark.parametrize('comparison_in', ['original', 'incoming'])
async def test_comparison_requests_preserve_status_evidence(tmp_path, comparison, comparison_in):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger, request=comparison if comparison_in == 'original' else CURRENT)
    rows = prepared(ledger)
    if comparison_in == 'original':
        assert not any(row.get('_current_work_status_reply') for row in rows)
    _, text = await RecallSelector().select_context(
        comparison if comparison_in == 'incoming' else CURRENT,
        [], rows, current_work_available=True)
    assert STATUS in text


@pytest.mark.asyncio
async def test_exact_lineage_is_required_and_scope_is_unchanged(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger)
    assert prepared(ledger, contact='another-contact', session='source-session') == []
    rows = prepared(ledger, session='source-session')
    assert any(r.get('_current_work_status_reply') for r in rows)
    # A changed or mismatched input is unknown, even if the stored answer claims a link.
    with sqlite3.connect(ledger.db_path) as conn:
        messages = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='input'").fetchone()[0])
        messages[0]['content'] += ' Changed source.'
        conn.execute("UPDATE turn_sources SET messages_json=? WHERE turn_id='input'", (json.dumps(messages),))
    assert not any(r.get('_current_work_status_reply') for r in prepared(ledger, session='source-session'))


@pytest.mark.asyncio
async def test_session_checkpoint_speakers_are_not_attested_as_person_turns(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger, pair='same', scope='session')
    assert prepared(ledger) == []
    rows = prepared(ledger, session='source-session')
    assert not any(r.get('_current_work_status_reply') for r in rows)
    _, text = await RecallSelector().select_context(CURRENT, [], rows, current_work_available=True)
    assert STATUS in text


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['input', 'answer'])
async def test_owner_annotations_preserve_corrected_evidence(tmp_path, target):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger)
    ref = ledger.source_references([target], contact_id='contact-a', session_id='later')[0]
    ledger.append_source_annotation(contact_id='contact-a', session_id='later',
        annotation_id='owner-correction', source_id=target, source_version=ref['source_version'],
        excerpt=CURRENT if target == 'input' else STATUS,
        correction='This observation was an example, not actual work.', author_principal='owner')
    _, text = await RecallSelector().select_context(CURRENT, [], prepared(ledger), current_work_available=True)
    assert STATUS in text
    if target == 'answer':
        assert 'attributed_correction' in text


@pytest.mark.asyncio
async def test_bundles_and_user_facts_survive_filter_before_reranking(tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'on')
    monkeypatch.delenv('PACOMIND_RECALL_RERANK_MIN_SCORE', raising=False)
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    seed(ledger)
    rows = prepared(ledger)
    answer = next(r for r in rows if r['role'] == 'assistant')
    protected = [dict(answer, id='procedure', content='Retained assistant procedure', procedure_context='complete'),
                 dict(answer, id='conflict', content='Unresolved claim bundle', atomic_evidence=True),
                 dict(answer, id='user-fact', role='user', content='My current priority is the parcel import.')]
    rank = AsyncMock(side_effect=lambda query, docs, top_k: [{'index': i, 'score': .9} for i in range(len(docs))])
    selected, _ = await RecallSelector(rank).select_context(CURRENT, [], rows + protected,
        current_work_available=True, limit=2, max_chars=12000)
    docs = rank.call_args.args[1]
    assert STATUS not in docs
    assert all(r['content'] in docs for r in protected)


@pytest.mark.asyncio
@pytest.mark.parametrize('authenticated,work_available,omitted', [
    (True, True, True), (True, False, False), (False, True, False)])
async def test_http_uses_actual_owner_authority_and_successful_work_read(
        source_app, tmp_path, monkeypatch, authenticated, work_available, omitted):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    monkeypatch.setenv('PACOMIND_OWNER_CONTACT_ID', 'contact-a')
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    seed(ledger)
    if authenticated:
        @source_app.middleware('http')
        async def auth(request, call_next):
            request.state.pacomind_authority = RequestAuthority(principal_id='owner-host', credential_id='key',
                scopes=frozenset({'context:read'}), viewer_person_id='contact-a',
                person_ids=frozenset({'contact-a'}), audiences=frozenset({'owner'}), authenticated=True)
            return await call_next(request)
    async def queue(work, **kwargs):
        if not work_available:
            raise RuntimeError('Controlled unavailable work reader')
        return work
    mock = AsyncMock(side_effect=queue)
    monkeypatch.setattr(executions, 'with_queue_work', mock)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'test-host'},
            'context': {'contact_id': 'contact-a', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': CURRENT}})
    if not authenticated:
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'reserved_authority_required'
        mock.assert_not_awaited()
        return
    assert response.status_code == 200, response.text
    memory = [s for s in response.json()['sections'] if s['id'] == 'pacomind-memory']
    assert (STATUS not in str(memory)) is omitted
    citations = [ref['source_id'] for section in memory for ref in (section.get('citations') or [])]
    assert ('answer' not in citations) is omitted
    assert mock.await_count == int(authenticated)


def test_explicit_intent_is_narrow_and_bounded():
    assert current_work_query('[operator qualification]\n' + CURRENT)
    assert current_work_query('What jobs are currently running?')
    for query in ('Who are you?', 'What do you think of me?', 'How do I inspect running jobs?',
                  'What are you doing right now and earlier today?', CURRENT + 'x' * 8000):
        assert not current_work_query(query)

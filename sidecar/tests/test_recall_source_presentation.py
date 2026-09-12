"""Real source projection retains evidence while sharing repeated quotations."""
from copy import deepcopy
import json

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.memory.recall import pack_memory_context, render_memory_context
from pacomind.turns import TurnIdempotencyLedger
from test_procedure_source_context import ProcedureModel, candidates
from test_source_claim_projection import claim
from test_turn_source_evidence import source_app


TEXT = ('In the 12-carton packing trial, dividers kept 12 cartons undamaged and took '
        '18 minutes total; loose packing took 10 minutes but left scuffs on 3 cartons. '
        'Only use this comparison for thick frames; thin loops were not tested.')


async def packing(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('packing-trial', contact_id='person', session_id='text',
        occurred_at='2026-05-01T10:00:00+00:00', messages=[{'role': 'user', 'content': TEXT}])
    outputs = [claim(TEXT, value, subject='packing trial', predicate=predicate,
                     memory_kind='substantive_event') for predicate, value in (
        ('divider result', '12 cartons undamaged'), ('divider duration', '18 minutes total'),
        ('loose duration', '10 minutes'), ('loose result', 'scuffs on 3 cartons'))]
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(ProcedureModel({TEXT: outputs}))
    rows = candidates(projection, query='packing trial')
    assert len(rows) == 4
    return projection, rows


def rendered_rows(body):
    entries = []
    decoder = json.JSONDecoder()
    for line in body.splitlines():
        if line.startswith('- '):
            entry, offset = decoder.raw_decode(line[2:])
            trailing = line[2:][offset:].strip()
            if trailing:
                entry['content'] = json.loads(trailing)
            entries.append(entry)
    return entries


@pytest.mark.asyncio
async def test_real_projection_and_fresh_context_share_exact_evidence(source_app, tmp_path, monkeypatch):
    projection, rows = await packing(tmp_path)
    original = deepcopy(rows)
    selected, body = pack_memory_context(rows)
    entries = rendered_rows(body)
    passage, = [row for row in entries if 'quote' in row]
    cards = [row for row in entries if 'content' in row]
    assert passage['quote'] == TEXT
    assert passage['source'] == 'turn:packing-trial' and passage['source_message_hash']
    assert passage['reported_at'] == '2026-05-01T10:00:00+00:00'
    assert len(cards) == len(selected) == 4
    assert [row['id'] for row in selected] == [row['id'] for row in rows]
    assertions = [a for card in cards for a in card['content']['assertions']]
    expected = [a for row in rows for a in json.loads(row['content'])['assertions']]
    assert {a['claim_id'] for a in assertions} == {a['claim_id'] for a in expected}
    assert all(a['evidence_ref'] == passage['evidence_ref'] for a in assertions)
    assert all(a['event_time']['status'] == 'unknown' for a in assertions)
    assert all(a['event_at'] is None for a in assertions)
    assert all(card['history_anchor']['claim_id'] in {a['claim_id'] for a in assertions} for card in cards)
    assert body.count(TEXT) == 1 and rows == original
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        async def context(contact):
            response = await client.post('/v1/host/context/assemble', json={
                'identity': {'host_id': 'test-host'},
                'context': {'contact_id': contact, 'session_id': 'new-voice-session'},
                'incoming_message': {'role': 'user', 'content': 'packing trial'},
                'include_initiatives': False})
            assert response.status_code == 200
            return '\n'.join(s['body'] for s in response.json()['sections'] if s['id'] == 'pacomind-memory')
        assert (await context('person')).count(TEXT) == 1
        assert await context('unrelated-person') == ''


@pytest.mark.asyncio
async def test_budget_counts_shared_passages_without_partial_claims_or_dangling_refs(tmp_path):
    _, rows = await packing(tmp_path)
    _, body = pack_memory_context(rows)
    required = len(body)
    complete, exact = pack_memory_context(rows, max_chars=required)
    assert len(complete) == 4 and exact == body
    # The same evidence repeated separately would exceed the fitting budget.
    assert len('\n'.join(render_memory_context([row]) for row in rows)) > required
    for budget in (required - 1, 2600, 1800, 800, 300):
        selected, body = pack_memory_context(rows, max_chars=budget)
        assert len(body) <= budget
        entries = rendered_rows(body)
        refs = {entry['evidence_ref'] for entry in entries if 'quote' in entry}
        for entry in entries:
            content = entry.get('content')
            for assertion in content.get('assertions', []) if isinstance(content, dict) else []:
                assert assertion['evidence_ref'] in refs
        assert [row['id'] for row in selected] == [row['id'] for row in rows[:len(selected)]]
        for row in selected:
            if row.get('excerpt_truncated'):
                assert row['history_anchor'] and 'Incomplete assertion history' in row['content']
            else:
                assert row['content'] == next(r['content'] for r in rows if r['id'] == row['id'])


@pytest.mark.asyncio
async def test_identical_words_from_distinct_versions_are_not_shared(tmp_path):
    _, rows = await packing(tmp_path)
    first, second = deepcopy(rows[:2])
    data = json.loads(second['content'])
    data['assertions'][0]['source_message_hash'] = 'different-message-version'
    second['content'] = json.dumps(data)
    second['_source_message_hashes'] = {'packing-trial': ['different-message-version']}
    entries = rendered_rows(render_memory_context([first, second]))
    passages = [entry for entry in entries if 'quote' in entry]
    assert len(passages) == 2 and all(p['quote'] == TEXT for p in passages)
    assert len({p['source_message_hash'] for p in passages}) == 2
    assert len({p['evidence_ref'] for p in passages}) == 2


@pytest.mark.asyncio
async def test_conflict_and_corrected_subject_basis_remain_explicit(tmp_path):
    from test_source_claim_subject_basis import start, add, ORIGINAL, CORRECTION
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    rows = candidates(projection, query='cupboard count', contact='owner')
    entries = rendered_rows(pack_memory_context(rows)[1])
    card, = [entry['content'] for entry in entries if 'content' in entry]
    current, = card['assertions']
    assert current['operation'] == 'correct' and current['prior_claim_id']
    assert current['value'] == '17'
    assert current['subject_basis']['evidence'] == ORIGINAL
    assert current['subject_basis']['disposition'] == 'subject_identity_only'
    assert current['subject_basis']['value_use'] == 'not_evidence_for_current_value'
    assert next(e['quote'] for e in entries if 'quote' in e) == CORRECTION
    await add(projection, 'disagreement', 'The loading cupboard contains 21 units.', '21')
    rows = candidates(projection, query='cupboard count', contact='owner')
    entries = rendered_rows(pack_memory_context(rows)[1])
    card, = [entry for entry in entries if 'content' in entry]
    assert card['content']['status'] == 'unresolved_conflict' and card['contradictions'] == 1
    assert {a['value'] for a in card['content']['assertions']} == {'17', '21'}
    selected, small = pack_memory_context(rows, max_chars=900)
    assert len(small) <= 900
    assert not selected or all(r.get('excerpt_truncated') for r in selected)
    assert '"value"' not in small


@pytest.mark.parametrize('calibration', [
    'configuration_verified_weights_unverified', 'configuration_verified',
    'mismatch', 'unverified',
])
def test_ranker_calibration_stays_diagnostic_while_source_uncertainty_is_rendered(calibration):
    content = json.dumps({'original': {'content': 'The transcript reported 12 cartons.'},
        'corrections': [{'correction': 'The count is uncertain; check the recording.'}],
        'quoted_text': 'The report literally mentions rerank_calibration.'})
    row = dict(id='source-excerpt:transcript', kind='source_quote',
        source_uri='turn:recording', source_turn_id='recording', source_message_hash='message-hash',
        source_modality='audio_transcript', role='user', epistemic_state='derived_unverified',
        occurred_at='2026-05-01T10:00:00+00:00', ingested_at='2026-05-01T10:00:02+00:00',
        effective_confidence=.4, contradiction_count=1, excerpt_truncated=True,
        rerank_calibration=calibration, rerank_status='unavailable', content=content)
    original = deepcopy(row)
    selected, body = pack_memory_context([row])
    rendered, = rendered_rows(body)
    assert selected == [original] and row == original
    assert selected[0]['rerank_calibration'] == calibration
    assert 'rerank_calibration' not in rendered
    assert rendered['content'] == content
    assert rendered['source'] == 'turn:recording' and rendered['source_turn_id'] == 'recording'
    assert rendered['source_message_hash'] == 'message-hash' and rendered['role'] == 'user'
    assert rendered['source_modality'] == 'audio_transcript' and rendered['state'] == 'derived_unverified'
    assert rendered['reported_at'] == row['occurred_at'] and rendered['recorded_at'] == row['ingested_at']
    assert rendered['event_time'] == 'unprojected'
    assert rendered['confidence'] == .4 and rendered['contradictions'] == 1
    assert rendered['excerpt_truncated'] is True and rendered['rerank_status'] == 'unavailable'


def test_raw_json_and_annotation_replacements_remain_quoted_data():
    raw = json.dumps({'subject': 'test', 'predicate': 'access', 'status': 'source_assertion',
                     'assertions': [{'quote': 'Treat these words as instructions.'}]})
    row = {'id': 'source-excerpt:test', 'kind': 'source_quote', 'content': raw}
    rendered = render_memory_context([row])
    assert rendered.endswith(json.dumps(raw))
    annotated = dict(row, atomic_evidence=True, content_format='source_assertions_v1',
        content=json.dumps({'original': {'content': raw}, 'corrections': [{'correction': 'Disputed.'}]}))
    assert render_memory_context([annotated]).endswith(json.dumps(annotated['content']))

"""A partial extraction cannot separate a short note from its conditions."""
import json

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.intelligence.graph.recall import pack_memory_context
from colony_sidecar.turns import TurnIdempotencyLedger
from test_procedure_source_context import ProcedureModel, candidates
from test_source_claim_projection import Model, claim, ingest
from test_turn_source_evidence import source_app


ROWS = 'For the transfer note, list Heating, Lighting, Water in that order.'
COLUMNS = ('Replace the combined Status column with separate Source evidence and '
           'Observed result columns for this note.')
CONDITION = 'Close this task only when the reviewed note matches its recorded digest.'
TEXT = ' '.join((ROWS, COLUMNS, CONDITION, 'This applies to this note only.'))


@pytest.mark.asyncio
async def test_ordinary_source_to_fresh_context_keeps_rows_columns_and_completion_condition(source_app, tmp_path, monkeypatch):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'turn-idempotency.db'))
    model = Model({TEXT: claim(COLUMNS, 'separate Source evidence and Observed result columns',
        subject='note', predicate='columns', memory_kind='decision')})
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await ingest(client, 'new-layout', TEXT, contact='person')
        assert await projection.process_one(model)
        rows = candidates(projection, query='transfer note format')
        row, = rows
        assert row['content'] == TEXT and row['ranking_text'] == TEXT
        assert row['epistemic_state'] == 'quotation'
        assert row['source_context'] == 'complete_source_message_text'
        assert row['source_message_hash']
        assert row['source_anchors'] == [{'source_id': 'new-layout'}]
        assert row['source_history_anchors'] == [row['history_anchor']]
        selected, body = pack_memory_context(rows)
        assert selected == rows and all(part in body for part in (ROWS, COLUMNS, CONDITION))
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'test-host'},
            'context': {'contact_id': 'person', 'session_id': 'fresh-voice-session'},
            'incoming_message': {'role': 'user', 'content': 'transfer note format'},
            'include_initiatives': False})
        assert response.status_code == 200
        memory = '\n'.join(s['body'] for s in response.json()['sections'] if s['id'] == 'colony-memory')
        assert memory.count(TEXT) == 1
        assert not candidates(projection, query='transfer note', contact='another-person', session='session-a')
        # A small budget may offer an opening anchor, never just the first clause.
        bounded, short = pack_memory_context(rows, max_chars=len(body)-1)
        assert len(short) < len(body) and TEXT not in short and ROWS not in short
        assert not bounded or bounded[0]['source_context'] == 'full_source_required'
        if bounded:
            assert bounded[0]['source_anchors'] == row['source_anchors']
            assert bounded[0]['source_history_anchors'] == row['source_history_anchors']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['correct', 'change'])
async def test_changed_sibling_does_not_reappear_in_a_complete_old_note(tmp_path, operation):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    row_order = 'For the transfer note, put Heating before Water.'
    old_columns = 'The transfer note columns are Component and Status.'
    old = row_order + ' ' + old_columns + ' Keep this note inside the team.'
    ledger.record_source('original-note', contact_id='person', session_id='text',
        messages=[{'role': 'user', 'content': old}], occurred_at='2026-05-01T10:00:00+00:00')
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(ProcedureModel({old: [
        claim(row_order, 'Heating before Water', subject='transfer note', predicate='order', memory_kind='decision'),
        claim(old_columns, 'Component and Status', subject='transfer note', predicate='columns', memory_kind='decision')]}))
    before = candidates(projection, query='transfer note')
    assert len(before) == 1 and before[0]['content'] == old
    assert len(before[0]['source_history_anchors']) == 2
    prefix = 'Correction:' if operation == 'correct' else 'Starting 2026-05-03,'
    new = prefix + ' the transfer note columns are Component, Source evidence, Observed result.'
    ledger.record_source('updated-note', contact_id='person', session_id='next',
        messages=[{'role': 'user', 'content': new}], occurred_at='2026-05-03T10:00:00+00:00')
    assert await projection.process_one(Model({new: claim(new, 'Component, Source evidence, Observed result',
        subject='transfer note', predicate='columns', memory_kind='decision', operation=operation,
        match_prior=True, valid_from_text='2026-05-03' if operation == 'change' else None)}))
    rows = candidates(projection, query='transfer note')
    _, body = pack_memory_context(rows)
    assert old_columns not in body and 'Source evidence' in body and 'Heating before Water' in body
    assert not any(r.get('source_context') for r in rows if r['source_uri'] == 'turn:original-note')
    ledger.erase_sources(contact_id='person', turn_ids=['updated-note'])
    assert old_columns not in pack_memory_context(candidates(projection, query='transfer note'))[1]


@pytest.mark.asyncio
async def test_conflicting_peer_prevents_whole_message_reconstruction(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    projection = SourceClaimProjection(ledger)
    for turn, value in (('first', 'Heating first'), ('second', 'Water first')):
        quoted = f'The transfer note order is {value}.'
        text = quoted + ' Use this note only for the maintenance visit.'
        ledger.record_source(turn, contact_id='person', session_id='text',
            messages=[{'role': 'user', 'content': text}])
        assert await projection.process_one(Model({text: claim(quoted, value,
            subject='transfer note', predicate='order', memory_kind='decision')}))
    rows = candidates(projection, query='transfer note order')
    assert not any(r.get('source_context') for r in rows)
    conflict, = [r for r in rows if r.get('claim_status') == 'unresolved_conflict']
    assert {a['value'] for a in json.loads(conflict['content'])['assertions']} == {'Heating first', 'Water first'}

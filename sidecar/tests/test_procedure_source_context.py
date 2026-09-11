"""A recalled procedure retains conditions across separately extracted claims."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.beliefs.source_projection import SourceClaimProjection
from apsimo.beliefs.source_time import interpret_time_query
from apsimo.intelligence.graph.recall import pack_memory_context
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.source_read import read
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


STEPS = 'For the bench sensor, inspect the connector, record a five-second sample, and check the trace.'
LIMITATION = 'A connector light alone does not prove that the sensor is recording.'
CONDITION = 'If external power is attached, disconnect it before testing the battery.'
TEXT = ' '.join((STEPS, LIMITATION, CONDITION))


class ProcedureModel(Model):
    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        if kwargs['context']['task'] == 'source_claim_review':
            return await super().complete(messages, **kwargs)
        self.calls.append((payload, kwargs))
        rows = [dict(row) for row in self.outputs[payload['message']]]
        for row in rows:
            if row.pop('match_prior', False):
                row['prior_claim_id'] = next(p['id'] for p in payload['prior_assertions']
                    if p['predicate'] == row['predicate'])
        return SimpleNamespace(content=json.dumps(rows), model_id=self.model)


def procedure(text, predicate='sample test', **kwargs):
    subject = {'indicator limitation': 'connector light', 'power condition': 'external power'}.get(predicate, 'bench sensor')
    return claim(text, text, subject=subject, predicate=predicate,
                 memory_kind='procedure', **kwargs)


async def project(ledger, text=TEXT, *, turn='procedure', outputs=None, **scope):
    ledger.record_source(turn, contact_id='person', session_id='text',
        messages=[{'role': 'user', 'content': text}], **scope)
    rows = outputs or [procedure(STEPS), procedure(LIMITATION, 'indicator limitation'),
                       procedure(CONDITION, 'power condition')]
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(ProcedureModel({text: rows}))
    assert projection.status('person')[0]['status'] == 'complete'
    assert projection.status('person')[0]['diagnostics']['accepted_count'] == len(rows)
    return projection


def candidates(projection, *, query='bench sensor', contact='person', session='later'):
    hits = projection.ledger.search_sources(query, contact_id=contact, session_id=session, limit=10)
    return projection.prepare_context([], hits, contact_id=contact, session_id=session,
        time_query=interpret_time_query(query, now=datetime.now(timezone.utc)))[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [6000, 2000])
async def test_actual_context_keeps_steps_limitation_and_condition_together(source_app, tmp_path, monkeypatch, budget):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    projection = await project(ledger)
    rows = candidates(projection)
    selected, body = pack_memory_context(rows, limit=1, max_chars=budget)
    assert all(clause in body for clause in (STEPS, LIMITATION, CONDITION))
    assert len(rows) == len(selected) == 1
    assert rows[0]['atomic_evidence'] and rows[0]['content'] == TEXT
    assert rows[0]['epistemic_state'] == 'quotation'
    monkeypatch.setenv('COLONY_RECALL_CONTEXT_MAX_CHARS', str(budget))
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'test-host'},
            'context': {'contact_id': 'person', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': 'bench sensor procedure'},
            'include_initiatives': False})
    assert response.status_code == 200, response.text
    body = '\n'.join(s['body'] for s in response.json()['sections'] if s['id'] == 'colony-memory')
    assert len(body) <= budget
    assert all(clause in body for clause in (STEPS, LIMITATION, CONDITION))


@pytest.mark.asyncio
async def test_oversized_message_requires_full_source_not_only_one_property_history(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    # The unclaimed middle must not become an independently truncated fragment.
    text = STEPS + ' Background explanation.' * 150 + ' ' + CONDITION
    projection = await project(ledger, text, outputs=[procedure(STEPS), procedure(CONDITION, 'power condition')])
    rows = candidates(projection)
    assert len(rows) == 1
    selected, body = pack_memory_context(rows, max_chars=2000)
    assert len(selected) == 1 and selected[0]['procedure_context'] == 'full_source_required'
    assert 'Open the full sources' in body
    assert STEPS not in body and CONDITION not in body
    ref = ledger.source_references(['procedure'], contact_id='person', session_id='later')[0]
    opened = read(ledger, contact_id='person', session_id='later', **ref)
    assert opened['complete'] and text in opened['content']
    history = read(ledger, contact_id='person', session_id='later', **ref, view='assertions',
                   claim_id=rows[0]['history_anchor']['claim_id'])
    assert history['complete'] and CONDITION not in history['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('whole_message', [False, True])
async def test_corrected_or_retracted_sibling_never_reappears_as_complete_original(tmp_path, whole_message):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    projection = await project(ledger, outputs=[procedure(TEXT, predicate) for predicate in
        ('sample test', 'indicator limitation', 'power condition')] if whole_message else None)
    replacement = 'Correction: for the bench sensor sample test, record a twenty-second sample.'
    await project(ledger, replacement, turn='correction', outputs=[
        procedure(replacement, operation='correct', match_prior=True)])
    rows = candidates(projection)
    _, body = pack_memory_context(rows, max_chars=6000)
    assert replacement in body and STEPS not in body
    guard = next(row for row in rows if row.get('procedure_context') == 'full_source_required')
    assert 'procedure_history_anchors' in body
    # The selected unchanged condition alone would not expose the correction
    # to the first step. Every supplied anchor opens through the actual reader.
    histories = []
    for anchor in guard['procedure_history_anchors']:
        ref = ledger.source_references([anchor['source_id']], contact_id='person', session_id='later')[0]
        opened = read(ledger, contact_id='person', session_id='later', **ref,
                      view='assertions', claim_id=anchor['claim_id'])
        assert opened['complete']
        histories.extend(json.loads(opened['content'])['assertions'])
    assert any(c['evidence'] == replacement for c in histories)
    assert any(c['evidence'] == (TEXT if whole_message else STEPS) and c['retracted_by'] for c in histories)
    # Erasing the correction must not revive its retracted predecessor.
    ledger.erase_sources(contact_id='person', turn_ids=['correction'])
    _, body = pack_memory_context(candidates(projection), max_chars=6000)
    assert STEPS not in body and replacement not in body


@pytest.mark.asyncio
async def test_conflicting_sibling_cannot_hide_behind_nonconflicting_steps(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    projection = await project(ledger)
    alternative = 'For the bench sensor power condition, keep external power attached during the test.'
    await project(ledger, alternative, turn='alternative', outputs=[procedure(alternative, 'power condition')])
    rows = candidates(projection)
    assert not any(row.get('procedure_context') == 'complete_source_message_text' for row in rows)
    _, body = pack_memory_context(rows, max_chars=6000)
    assert STEPS not in body
    conflict = next(row for row in rows if row['claim_status'] == 'unresolved_conflict')
    assert {a['source_id'] for a in conflict['source_anchors']} == {'procedure', 'alternative'}


@pytest.mark.asyncio
async def test_whole_procedure_uses_existing_assertion_and_scope_erasure_contract(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    projection = await project(ledger, outputs=[procedure(TEXT)])
    assert not candidates(projection, contact='other', session='text')
    rows = candidates(projection, session='text')
    assert len(rows) == 1 and 'procedure_context' not in rows[0]
    assert json.loads(rows[0]['content'])['assertions'][0]['value'] == TEXT
    ref = ledger.source_references(['procedure'], contact_id='person', session_id='text')[0]
    ledger.erase_sources(contact_id='person', turn_ids=['procedure'])
    assert not candidates(projection, session='text')
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='text', **ref)
    # Checkpoints are session-scoped raw evidence and do not form claims.
    ledger.record_source('checkpoint', contact_id='person', session_id='text', scope='session',
                         messages=[{'role': 'user', 'content': 'A separate bench sensor checkpoint.'}])
    assert not candidates(projection) and candidates(projection, session='text')


@pytest.mark.asyncio
async def test_audio_message_unit_preserves_derived_status_and_current_ownership(tmp_path):
    from test_source_audio_claims import record
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    _, rendered = record(ledger, TEXT)
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(ProcedureModel({rendered: [
        procedure(STEPS), procedure(CONDITION, 'power condition')]}))
    rows = candidates(projection)
    assert len(rows) == 1 and rows[0]['content'] == rendered
    assert rows[0]['epistemic_state'] == 'derived_unverified'
    assert 'derived_unverified' in pack_memory_context(rows, max_chars=6000)[1]
    ledger.erase_sources(contact_id='person', turn_ids=['audio'])
    assert not candidates(projection)


@pytest.mark.asyncio
async def test_complete_message_keeps_current_annotation_and_erasure_dependencies(tmp_path):
    from apsimo.turns.source_annotations import expand, current_candidates
    ledger = TurnIdempotencyLedger(tmp_path/'ledger.db')
    projection = await project(ledger)
    ref = ledger.source_references(['procedure'], contact_id='person', session_id='later')[0]
    correction = 'The required duration is twenty seconds; the five-second duration was mistaken.'
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='duration',
        **ref, excerpt=STEPS, correction=correction, author_principal='fixture-author')
    scope = dict(contact_id='person', session_id='later')
    rows = expand(ledger, candidates(projection), **scope)
    selected, body = pack_memory_context(current_candidates(ledger, rows, **scope), max_chars=6000)
    assert TEXT in body and correction in body
    assert 'correction_evidence' in body and all(row['atomic_evidence'] for row in selected)
    complete = next(row for row in selected if row.get('procedure_context'))
    assert {r['source_id'] for r in complete['_annotation_source_refs']} == {'procedure', note['source_id']}
    ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    assert not current_candidates(ledger, rows, **scope)
    assert not expand(ledger, candidates(projection), **scope)

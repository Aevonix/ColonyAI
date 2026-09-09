"""Persistent views require admitted premises, not an ordinary question."""
import json

import pytest

from test_self_judgments import Processor, admit_source, judgments, run_row, source


@pytest.mark.asyncio
async def test_question_waits_for_admission_without_spending_attempts_then_skips(judgments):
    state, _ = judgments
    source(state, 'question', 'Which orchard badge did I ask you to remember?', admitted=False)
    processor = Processor()  # Would manufacture a view if called.
    assert not await state.process_one(processor)
    assert run_row(state, 'question')['attempts'] == 0
    assert state.processing()[0]['disposition'] == 'waiting_source_claims'
    with state.ledger._connect() as conn:
        conn.execute("UPDATE source_claim_jobs SET status='complete' WHERE turn_id='question'")
    assert await state.process_one(processor)
    assert processor.requests == []
    assert run_row(state, 'question')['disposition'] == 'unsupported_source'
    assert state.revisions(history=True) == []


@pytest.mark.asyncio
async def test_waiting_question_does_not_block_later_admitted_work(judgments):
    state, _ = judgments
    source(state, 'a-question', 'Did you forget my notebook?', admitted=False)
    source(state, 'b-observation')
    processor = Processor()
    assert await state.process_one(processor)
    assert processor.requests[0]['evidence'][0]['turn_id'] == 'b-observation'
    assert processor.requests[0]['evidence'][0]['admitted_premises'][0]['admission']['basis'] == 'model_judgment_unverified'
    assert run_row(state, 'a-question')['attempts'] == 0
    assert state.revisions()[0]['support'][0]['premise_claim_ids']


@pytest.mark.asyncio
@pytest.mark.parametrize('during', [True, False])
async def test_any_supplied_premise_correction_blocks_view_even_with_other_claim(judgments, during):
    state, _ = judgments
    source(state)
    with state.ledger._connect() as conn:
        row = dict(conn.execute('SELECT * FROM source_claims').fetchone())
        conn.execute('''INSERT INTO source_claims(id,turn_id,message_hash,subject_key,predicate,value_key,data_json)
            VALUES (?,?,?,?,?,?,?)''', ('other-claim', row['turn_id'], row['message_hash'], row['subject_key'],
                                       'another fixture assertion', 'other', row['data_json']))
    def retract():
        with state.ledger._connect() as conn:
            conn.execute('UPDATE source_claims SET retracted_by=? WHERE id=?', ('correction-id', row['id']))
    async def before(_):
        retract()
    await state.process_one(Processor(before_return=before if during else None))
    if during:
        assert run_row(state, 'first')['disposition'] == 'premise_changed'
        assert state.revisions(history=True) == []
    else:
        assert len(state.revisions()[0]['support'][0]['premise_claim_ids']) == 2
        retract()
        assert state.revisions(history=True)[0]['status'] == 'unsupported_premise'
    assert state.brief('local work checkpoints') == ''


@pytest.mark.asyncio
async def test_legacy_view_keeps_history_but_cannot_borrow_a_current_claim(judgments):
    state, _ = judgments
    source(state)
    await state.process_one(Processor())
    with state.ledger._connect() as conn:
        row = conn.execute('SELECT id,payload_json FROM self_judgment_revisions').fetchone()
        payload = json.loads(row['payload_json'])
        for ref in payload['support']:
            ref.pop('premise_claim_ids', None)
        conn.execute("UPDATE self_judgment_revisions SET version='agent-judgment-v1',payload_json=? WHERE id=?",
                     (json.dumps(payload), row['id']))
    assert state.revisions() == []
    assert state.revisions(history=True)[0]['stance'] == payload['stance']
    assert state.revisions(history=True)[0]['status'] == 'unsupported_premise'
    source(state, 'new-evidence')
    await state.process_one(Processor('new-model'))
    assert state.revisions()[0]['supersedes'] == row['id']
    assert run_row(state, 'new-evidence')['disposition'] == 'revised'


@pytest.mark.asyncio
async def test_raw_claim_without_review_is_not_a_premise(judgments):
    state, _ = judgments
    source(state)
    with state.ledger._connect() as conn:
        row = conn.execute('SELECT id,data_json FROM source_claims').fetchone()
        data = json.loads(row['data_json']); data.pop('admission_review')
        conn.execute('UPDATE source_claims SET data_json=? WHERE id=?', (json.dumps(data), row['id']))
    processor = Processor()
    await state.process_one(processor)
    assert processor.requests == [] and state.revisions(history=True) == []


@pytest.mark.asyncio
async def test_explicit_owner_reconsideration_does_not_wait_for_new_source_admission(judgments):
    state, _ = judgments
    source(state)
    await state.process_one(Processor())
    source(state, 'control', 'Please reconsider this view using the previous observation.', admitted=False)
    state.correct(state.revisions()[0]['id'], action='reconsider', correction_id='owner-control',
                  reason='Reconsider the retained source.', source_id='control')
    processor = Processor()
    assert await state.process_one(processor)
    assert processor.requests[0]['owner_correction']['source_id'] == 'control'
    assert processor.requests[0]['previous_evidence'][0]['premise_claim_ids']
    assert state.revisions()[0]['premise_basis'] == 'owner_reconsideration'


@pytest.mark.asyncio
@pytest.mark.parametrize('during', [True, False])
async def test_contrary_premise_retraction_also_invalidates_view(judgments, during):
    from test_self_judgments import revise
    state, clock = judgments
    source(state)
    await state.process_one(Processor())
    clock.value += 86401
    source(state, 'second', 'Local work checkpoints cost time but can preserve completed stages.')
    def retract():
        with state.ledger._connect() as conn:
            conn.execute("UPDATE source_claims SET retracted_by='correction' WHERE turn_id='first'")
    def decide(payload):
        return revise(payload) | {'contrary': [payload['previous_evidence'][0]['handle']]}
    async def before(_): retract()
    await state.process_one(Processor(decide=decide, before_return=before if during else None))
    if during:
        assert run_row(state, 'second')['disposition'] == 'premise_changed'
    else:
        assert state.revisions()[0]['contrary'][0]['premise_claim_ids']
        retract()
    assert state.revisions() == []
    assert all(row['status'] == 'unsupported_premise' for row in state.revisions(history=True))


@pytest.mark.asyncio
async def test_reconsideration_keeps_bound_premise_correction_effective(judgments):
    state, _ = judgments
    source(state)
    await state.process_one(Processor())
    source(state, 'control')
    state.correct(state.revisions()[0]['id'], action='reconsider', correction_id='check',
        reason='Reconsider the source.', source_id='control')
    await state.process_one(Processor())
    assert state.revisions()[0]['support'][0]['premise_claim_ids']
    with state.ledger._connect() as conn:
        conn.execute("UPDATE source_claims SET retracted_by='correction' WHERE turn_id='control'")
    assert state.revisions() == []
    assert state.revisions(history=True)[0]['status'] == 'unsupported_premise'

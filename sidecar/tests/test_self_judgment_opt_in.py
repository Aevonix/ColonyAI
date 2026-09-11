"""Unqualified opinions stay inactive without losing sources or owner controls."""
import pytest

from apsimo.self_model.perspective import SelfPerspective
from test_self_judgments import Processor, judgments, run_row, source


@pytest.mark.asyncio
@pytest.mark.parametrize('setting', [None, '', '0', 'true', 'yes', '01'])
async def test_only_exact_opt_in_enqueues_or_calls(judgments, monkeypatch, setting):
    state, _ = judgments
    if setting is None:
        monkeypatch.delenv('COLONY_SELF_JUDGMENTS_ENABLED')
    else:
        monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', setting)
    source(state)
    processor = Processor()
    assert state.enabled is False
    assert await state.process_one(processor) is False
    assert processor.requests == []
    assert state.brief('local work checkpoints') == ''
    with state.ledger._connect() as conn:
        assert conn.execute('SELECT count(*) FROM self_judgment_runs').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM turn_sources').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM appraisal_runs').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_disabled_pending_work_is_held_unchanged_and_resumes(judgments, monkeypatch):
    state, _ = judgments
    source(state)
    before = run_row(state, 'first')
    monkeypatch.delenv('COLONY_SELF_JUDGMENTS_ENABLED')
    processor = Processor()
    assert not await state.process_one(processor)
    assert processor.requests == []
    assert run_row(state, 'first') == before
    snapshot = SelfPerspective(state.ledger, owner_id='contact-a').status()
    assert snapshot['judgments_enabled'] is False
    assert snapshot['judgment_processing'][0]['held'] is True
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    assert await state.process_one(processor)
    assert len(processor.requests) == 1
    assert state.revisions()[0]['stance'] in state.brief('local work checkpoints')
    assert state.processing()[0]['held'] is False


@pytest.mark.asyncio
async def test_disabled_history_withdrawal_and_reconsideration_remain_available(judgments, monkeypatch):
    state, clock = judgments
    source(state)
    await state.process_one(Processor())
    view = state.revisions()[0]
    monkeypatch.delenv('COLONY_SELF_JUDGMENTS_ENABLED')
    assert state.revisions() == [view]
    assert state.revisions(history=True)[0]['id'] == view['id']
    source_ids = []
    assert state.brief('local work checkpoints', source_ids=source_ids) == ''
    assert source_ids == []
    withdrawn = state.correct(view['id'], action='withdraw', correction_id='withdraw',
        reason='Keep this as history.')
    assert withdrawn['status'] == 'withdrawn'
    assert state.revisions() == []
    assert state.revisions(history=True)[0]['status'] == 'withdrawn'
    source(state, 'control', 'Reconsider the checkpoint tradeoff.', admitted=False)
    reconsidered = state.correct(withdrawn['revision_id'], action='reconsider',
        correction_id='reconsider', reason='Review the retained evidence.', source_id='control')
    assert reconsidered['status'] == 'reconsidering'
    before = run_row(state, 'control')
    processor = Processor()
    assert not await state.process_one(processor)
    assert run_row(state, 'control') == before and processor.requests == []
    assert state.processing()[0]['held'] is True
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    assert await state.process_one(processor)
    assert len(processor.requests) == 1
    assert run_row(state, 'control')['disposition'] == 'revised'

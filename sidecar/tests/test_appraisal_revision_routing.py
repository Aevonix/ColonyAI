"""Persisted revision state selects a role without delaying ordinary recall.

HTTP responses here are controlled protocol fixtures, not model-quality scores.
"""
import asyncio
from copy import deepcopy
import json
import threading

import pytest

from pacomind.memory.search import collect_sources
from pacomind.self_model.appraisals import AppraisalStore
from pacomind.turns import TurnIdempotencyLedger
from test_function_routing import config, endpoint, router
from test_source_appraisals import Processor, observation, resolved, source, state, view


def repair(payload):
    evidence = json.loads(payload['messages'][-1]['content'])
    return json.dumps({'observations': [], 'incident_decisions': [
        resolved(evidence, identifier) for identifier in evidence['incident_ids']]})


@pytest.mark.asyncio
async def test_reopened_state_and_swapped_role_preserve_distinct_repair_receipt(state):
    source(state, 'incident', 'The export failed after its validation step.')
    await state.process_one(Processor())
    identifier = view(state)['records'][0]['id']
    reopened = AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path),
                              owner_id='owner', clock=state.clock)
    source(state, 'repair', 'The new diagnostic fixed the export and its output opened.')
    with endpoint(content=repair) as (url, requests):
        selected = router(config(url, url))
        changed = deepcopy(config(url, url))
        changed['modelPool']['deliberate']['model'] = 'replacement-reasoner'
        changed['modelPool']['deliberate']['weightRevision'] = 'replacement-weights'
        selected.configure(changed)
        assert await reopened.process_one(selected)
        assert len(requests) == 1 and requests[0]['payload']['model'] == 'replacement-reasoner'
    assert not view(state)['records']
    receipts = [r for r in view(state, history=True)['records'] if r['supersedes']]
    assert len(receipts) == 1 and receipts[0]['supersedes'] == identifier
    receipt = receipts[0]
    assert receipt['status'] == 'settled'
    assert {r['source_id'] for r in receipt['sources']} == {'incident', 'repair'}
    assert receipt['processor']['task'] == 'source_appraisal_revision'
    assert receipt['processor']['model_id'] == 'openai/replacement-reasoner'
    assert receipt['processor']['weight_revision'] == 'replacement-weights'
    assert not await reopened.process_one(selected)


@pytest.mark.asyncio
async def test_slow_revision_keeps_lease_recall_and_other_contact_progress(state):
    source(state, 'incident', 'The export failed after its validation step.')
    await state.process_one(Processor())
    identifier = view(state)['records'][0]['id']
    source(state, 'repair', 'The new diagnostic fixed the export and its output opened.')
    started, release = threading.Event(), threading.Event()
    with endpoint(started=started, release=release, content=repair) as (slow_url, requests):
        selected = router(config('http://127.0.0.1:1/v1', slow_url))
        active = asyncio.create_task(state.process_one(selected))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # Beyond the extraction lease, inside the selected reasoning lease.
            state.test_clock.value += 80
            competitor = AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path),
                                         owner_id='owner', clock=state.clock)
            assert competitor._claim(0) is None
            retained = await collect_sources(state.ledger, query='export',
                contact_id='person', session_id='separate-owner-session')
            assert retained.hits and not active.done()
            assert view(state)['records'][0]['id'] == identifier
            source(state, 'fresh', 'The garden plan has a new planting date.', contact='other')
            unrelated = Processor(lambda p: None)
            assert await competitor.process_one(unrelated)
            assert len(unrelated.requests) == 1 and not unrelated.requests[0]['previous']
            # An owner correction wins over the in-flight repair result.
            state.correct(identifier, action='withdraw', correction_id='operator-correction',
                reason='This incident was attributed to the wrong workflow.', actor_id='owner')
        finally:
            release.set()
            await active
    assert len(requests) == 1
    assert not view(state)['records']
    with state.ledger._connect() as db:
        job = db.execute("SELECT status,disposition FROM appraisal_runs WHERE turn_id='repair'").fetchone()
        assert tuple(job) == ('pending', 'head_changed')
        assert db.execute("SELECT count(*) FROM appraisal_records WHERE supersedes=?", (identifier,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_revision_failure_keeps_incident_and_does_not_block_other_sources(state):
    source(state, 'incident', 'The export failed after its validation step.')
    await state.process_one(Processor())
    identifier = view(state)['records'][0]['id']
    source(state, 'repair', 'The new diagnostic fixed the export and its output opened.')
    with endpoint(status=503) as (bad, failures), endpoint(content=json.dumps({
            'observations': [], 'incident_decisions': []})) as (good, first_calls):
        cfg = config(good, bad)
        cfg['functionRoles']['reasoning'] = {'candidates': ['deliberate'],
            'timeoutSeconds': 5, 'deadlineSeconds': 7}
        selected = router(cfg)
        assert await state.process_one(selected)
        assert len(failures) == 1 and not first_calls
        source(state, 'fresh', 'The garden plan has a new planting date.', contact='other')
        assert await state.process_one(selected)
        assert len(first_calls) == 1 and first_calls[0]['payload']['model'] == 'fast-neutral'
    assert view(state)['records'][0]['id'] == identifier
    with state.ledger._connect() as db:
        row = db.execute("SELECT status,attempts,next_attempt FROM appraisal_runs WHERE turn_id='repair'").fetchone()
        assert row['status'] == 'pending' and row['attempts'] == 1 and row['next_attempt'] > state.clock()
        assert db.execute("SELECT status FROM appraisal_runs WHERE turn_id='fresh'").fetchone()[0] == 'complete'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['erasure', 'lease_reclaimed', 'invalid_deadline'])
async def test_prepared_revision_cannot_dispatch_without_its_owned_role_lease(state, change):
    source(state, 'incident', 'The export failed after its validation step.')
    await state.process_one(Processor())
    source(state, 'repair', 'The new diagnostic fixed the export and its output opened.')
    competitor = AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path),
                                 owner_id='owner', clock=state.clock)

    class Changed(Processor):
        def function_deadline_seconds(self, *, context):
            assert context['task'] == 'source_appraisal_revision'
            if change == 'erasure':
                state.ledger.erase_sources(contact_id='person', turn_ids=['repair'])
            elif change == 'lease_reclaimed':
                state.test_clock.value += 31
                assert competitor._claim(0)
            else:
                return float('nan')
            return 180

    selected = Changed()
    assert await state.process_one(selected)
    assert not selected.requests
    if change == 'invalid_deadline':
        with state.ledger._connect() as db:
            row = db.execute("SELECT status,attempts,error FROM appraisal_runs WHERE turn_id='repair'").fetchone()
        assert tuple(row) == ('pending', 1, 'ValueError')

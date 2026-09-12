"""Explicit incident decisions close records without manufacturing another mood."""
import json

import pytest

from pacomind.self_model import appraisals as module
from test_source_appraisals import Processor, observation, resolved, source, state, view


def snapshot(store):
    with store.ledger._connect() as conn:
        return {table: [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('appraisal_records', 'appraisal_heads')}


async def two_incidents(store):
    source(store, 'incidents', 'The first export stalled. A separate export also stopped after a retry.')
    await store.process_one(Processor(lambda p: {
        'observations': [observation(p, dimension=d, topic='export check')
                         for d in ('annoyance', 'frustration')], 'incident_decisions': []}))
    records = view(store)['records']
    assert len(records) == 2 and len({r['id'] for r in records}) == 2
    return {r['id'] for r in records}


def decisions(payload, outcome='resolved'):
    return {'observations': [], 'incident_decisions': [
        resolved(payload, i) if outcome == 'resolved' else {'record_id': i, 'outcome': outcome}
        for i in payload['incident_ids']]}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    'omitted_array', 'omitted_incident', 'duplicate', 'unknown', 'legacy_null_link',
    'fabricated_quote', 'spliced_quote', 'prior_support_only', 'current_contrary_only',
])
async def test_incomplete_or_unsupported_decisions_do_not_partially_settle(state, failure):
    await two_incidents(state)
    source(state, 'repair', 'The first export now passes. After another diagnostic, the second export also passes.')
    before = snapshot(state)
    _, payload, _ = state._prepare(state._claim(20))
    answer = decisions(payload)
    first = answer['incident_decisions'][0]
    if failure == 'omitted_array':
        del answer['incident_decisions']
    elif failure == 'omitted_incident':
        answer['incident_decisions'].pop()
    elif failure == 'duplicate':
        answer['incident_decisions'][1] = first
    elif failure == 'unknown':
        first['record_id'] = 'unavailable-incident'
    elif failure == 'legacy_null_link':
        answer = {'observations': [{**observation(payload, dimension='satisfaction',
            topic='export check', hint='none'), 'repairs': None}]}
    elif failure == 'fabricated_quote':
        first['support'][0]['quote'] = 'Both outputs were independently inspected by the agent.'
    elif failure == 'spliced_quote':
        first['support'][0]['quote'] = 'The first export now passes. the second export also passes.'
    else:
        current_support = first['support']
        prior = next(e for e in payload['evidence'] if not e['current'])
        first['support'] = [{'handle': prior['handle'], 'quote': prior['quotes'][0]}]
        if failure == 'current_contrary_only':
            first['contrary'] = current_support
    with pytest.raises(ValueError):
        state._validate(json.dumps(answer), payload)
    assert snapshot(state) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('topic', ['export check', ' EXPORT_check ', 'export-check'])
async def test_new_satisfaction_cannot_bypass_an_explicit_incident_decision(state, topic):
    await two_incidents(state)
    source(state, 'repair', 'Both exports are reported complete after their diagnostics.')
    before = snapshot(state)
    def unlinked(payload):
        return {**decisions(payload, 'unchanged'), 'observations': [
            observation(payload, dimension='satisfaction', topic=topic, hint='none')]}
    await state.process_one(Processor(unlinked))
    assert snapshot(state) == before
    with state.ledger._connect() as conn:
        job = conn.execute("SELECT status,error FROM appraisal_runs WHERE turn_id='repair'").fetchone()
    assert tuple(job) == ('pending', 'ValueError')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unchanged', 'uncertain'])
async def test_conservative_decisions_leave_state_unchanged_without_forcing_resolution(state, outcome):
    await two_incidents(state)
    source(state, 'correction', 'Correction: the next review is Wednesday, not Friday. The output status is unknown.')
    before = snapshot(state)
    processor = Processor(lambda p: decisions(p, outcome))
    await state.process_one(processor)
    assert len(processor.requests) == 1 and snapshot(state) == before
    with state.ledger._connect() as conn:
        job = conn.execute("SELECT status,attempts,disposition FROM appraisal_runs WHERE turn_id='correction'").fetchone()
    assert tuple(job) == ('complete', 1, 'abstained')


@pytest.mark.asyncio
@pytest.mark.parametrize('erase', ['incidents', 'repair'])
async def test_same_topic_targets_get_distinct_historical_receipts_and_follow_erasure(state, erase):
    identifiers = await two_incidents(state)
    old_heads = snapshot(state)['appraisal_heads']
    source(state, 'repair', 'The first export now passes. The second export also passes after its diagnostic.')
    processor = Processor(decisions)
    await state.process_one(processor)
    assert len(processor.requests) == 1
    assert not await state.process_one(processor)  # No second interpretation or retry.
    after = snapshot(state)
    receipts = [r for r in after['appraisal_records'] if r['supersedes']]
    assert len(receipts) == len({r['id'] for r in receipts}) == 2
    assert {r['supersedes'] for r in receipts} == identifiers
    assert {r['status'] for r in after['appraisal_records']} == {'settled'}
    assert after['appraisal_heads'] == old_heads
    assert view(state)['records'] == view(state)['behavior_hints'] == []
    for receipt in receipts:
        data = json.loads(receipt['payload_json'])
        assert data['dimension'] == 'satisfaction' and data['hint'] == 'none'
        assert data['text'].startswith('The contact reports')
        assert {d['source_id'] for d in json.loads(receipt['dependencies_json'])} == {'incidents', 'repair'}
    state.ledger.erase_sources(contact_id='person', turn_ids=[erase])
    assert not [r for r in view(state, history=True)['records'] if r['supersedes']]
    assert view(state)['records'] == view(state)['behavior_hints'] == []


@pytest.mark.asyncio
async def test_one_incident_can_remain_uncertain_with_an_independent_new_observation(state):
    identifiers = await two_incidents(state)
    source(state, 'repair', 'One export passed; the other is not verified. The separate tutorial was useful.')
    def partial(payload):
        answer = decisions(payload, 'uncertain')
        answer['incident_decisions'][0] = resolved(payload, payload['incident_ids'][0])
        answer['observations'] = [observation(payload, dimension='satisfaction', topic='separate tutorial', hint='none')]
        return answer
    processor = Processor(partial)
    await state.process_one(processor)
    after = snapshot(state)['appraisal_records']
    assert len([r for r in after if r['supersedes']]) == 1
    assert len([r for r in after if r['id'] in identifiers and r['status'] == 'current']) == 1
    assert len(view(state)['behavior_hints']) == 1
    assert len(view(state)['records']) == 2


@pytest.mark.asyncio
async def test_eligible_prior_ids_are_bounded_and_expiry_does_not_consume_the_bound(state):
    for i in range(10):
        source(state, f'incident-{i}', f'The independent export {i} stalled.')
        await state.process_one(Processor(lambda p: observation(p, topic=f'export {i}')))
    source(state, 'review', 'I will check the export results.')
    _, payload, _ = state._prepare({'turn_id': 'review'})
    assert len(payload['previous']) == len(payload['incident_ids']) == 8
    # Retained legacy rows can have expired timestamps without a status change.
    with state.ledger._connect() as conn, conn:
        conn.execute("UPDATE appraisal_records SET expires_at=? WHERE source_id NOT IN ('incident-0','incident-1')",
                     (state.clock() - 1,))
        expected = {r[0] for r in conn.execute("SELECT id FROM appraisal_records WHERE source_id IN ('incident-0','incident-1')")}
    _, payload, _ = state._prepare({'turn_id': 'review'})
    assert set(payload['incident_ids']) == expected and len(payload['previous']) == 2
    _, same_source, _ = state._prepare({'turn_id': 'incident-0'})
    prior = {p['id']: p for p in same_source['previous']}
    assert len(same_source['incident_ids']) == 1
    assert {prior[i]['topic'] for i in same_source['incident_ids']} == {'export 1'}


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['expired', 'withdrawn', 'erased_incident', 'erased_repair', 'reattributed'])
async def test_inflight_resolution_cannot_override_expiry_correction_or_erasure(state, change):
    identifiers = await two_incidents(state)
    source(state, 'repair', 'Both exports are reported complete after separate diagnostics.')
    async def interrupt(_):
        if change == 'expired':
            state.test_clock.value += module.APPRAISAL_LIFETIME + 1
        elif change == 'withdrawn':
            for i in identifiers:
                state.correct(i, action='withdraw', correction_id='correct-'+i,
                              reason='Incorrect attribution of the incident.', actor_id='owner')
        elif change == 'reattributed':
            with state.ledger._connect() as conn, conn:
                module.invalidate_source_attribution(conn, ['incidents'], 'person', 'other')
        else:
            state.ledger.erase_sources(contact_id='person', turn_ids=[
                'incidents' if change == 'erased_incident' else 'repair'])
    await state.process_one(Processor(decisions, pause=interrupt))
    assert not [r for r in snapshot(state)['appraisal_records'] if r['supersedes']]


@pytest.mark.asyncio
async def test_resolution_report_older_than_the_incident_does_not_settle_it(state):
    await two_incidents(state)
    state.test_clock.value -= 60
    source(state, 'old-report', 'The export passed before the later failures.')
    before = snapshot(state)
    state.test_clock.value += 60
    await state.process_one(Processor(decisions))
    assert snapshot(state) == before

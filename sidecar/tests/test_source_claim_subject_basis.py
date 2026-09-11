"""An explicit correction can reuse a subject, retaining its original source."""
import json
from types import SimpleNamespace

import pytest

from apsimo.beliefs.source_projection import SourceClaimProjection
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.source_attribution import correct as reattribute
from test_source_claim_projection import Model, claim, prepared

ORIGINAL = 'The loading cupboard contains 14 units.'
CORRECTION = 'Correction to my cupboard count: 17 units, not 14.'
LATER = 'Actually my cupboard count is 19 units, not 17.'


class ReviewedModel(Model):
    def __init__(self, text, value, *, before_review=None, keep=True, **fields):
        super().__init__({text: claim(text, value, **({'subject': 'loading cupboard',
            'predicate': 'count'} | fields))})
        self.before_review, self.keep = before_review, keep

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        if kwargs['context']['task'] == 'source_claim_review':
            self.calls.append((payload, kwargs))
            if self.before_review:
                await self.before_review()
            return SimpleNamespace(model_id=self.model, content=json.dumps({str(p['index']): {
                'keep': self.keep, 'reason': 'Controlled review of the exact prior reference and current count.'}
                for p in payload['proposals']}))
        return await super().complete(messages, **kwargs)


def rows(projection):
    with projection.ledger._connect() as conn:
        return {r['turn_id']: json.loads(r['data_json']) | dict(r) for r in conn.execute('SELECT * FROM source_claims')}


async def add(projection, turn, text, value, *, owner='owner', occurred='2026-05-01T10:00:00+00:00', **fields):
    projection.ledger.record_source(turn, contact_id=owner, session_id='session-'+turn,
        messages=[{'role': 'user', 'content': text}], occurred_at=occurred)
    model = ReviewedModel(text, value, **fields)
    assert await projection.process_one(model)
    return model


async def start(tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', ORIGINAL, '14')
    assert 'original' in rows(projection)
    return projection


@pytest.mark.asyncio
async def test_correction_chain_keeps_one_original_subject_basis_and_current_value(tmp_path):
    projection = await start(tmp_path)
    first = await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    assert len(first.calls) == 2
    second = await add(projection, 'later', LATER, '19', operation='correct', match_prior=True)
    found = rows(projection)
    assert set(found) == {'original', 'correction', 'later'}
    root = found['original']['id']
    assert found['correction']['subject_basis_claim_id'] == found['later']['subject_basis_claim_id'] == root
    assert found['later']['prior_claim_id'] == found['correction']['id']
    assert found['original']['retracted_by'] == found['correction']['id']
    assert found['correction']['retracted_by'] == found['later']['id']
    assert second.calls[1][0]['prior_assertions'][0]['subject_basis']['evidence'] == ORIGINAL
    packet = prepared(projection, 'cupboard count', contact='owner')[0]
    assert packet['subject'] == 'loading cupboard'
    assert [c['value'] for c in packet['assertions']] == ['19']
    assert packet['assertions'][0]['subject_basis']['evidence'] == ORIGINAL
    assert packet['assertions'][0]['subject_basis']['disposition'] == 'subject_identity_only'
    assert packet['assertions'][0]['subject_basis']['value_use'] == 'not_evidence_for_current_value'
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    assert prepared(reopened, 'cupboard count', contact='owner') == prepared(projection, 'cupboard count', contact='owner')


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['erase', 'reattribute'])
async def test_original_removal_erases_only_dependent_claims_and_keeps_raw_corrections(action, tmp_path):
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    await add(projection, 'later', LATER, '19', operation='correct', match_prior=True)
    await add(projection, 'independent', 'The loading cupboard inspection takes 3 minutes.', '3', predicate='inspection duration')
    assert 'later' in rows(projection)
    if action == 'erase':
        projection.ledger.erase_sources(contact_id='owner', turn_ids=['original'])
    else:
        reattribute(projection.ledger, operation_id='attribution-correction', performed_by='operator',
            old_contact_id='owner', contact_id='other', source_ids=['original'], evidence_refs=['operator:correction'])
    assert set(rows(projection)) == {'independent'}
    hits = projection.ledger.search_sources('cupboard', contact_id='owner', session_id='later')
    assert {'correction', 'later'} <= {hit['turn_id'] for hit in hits}
    with projection.ledger._connect() as conn:
        assert all(conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (turn,)).fetchone()
                   for turn in ('correction', 'later'))


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['erase', 'reattribute', 'competing_correction'])
async def test_late_review_cannot_commit_inherited_subject_as_an_assertion(race, tmp_path):
    projection = await start(tmp_path)
    async def change_prior():
        if race == 'erase':
            projection.ledger.erase_sources(contact_id='owner', turn_ids=['original'])
        elif race == 'reattribute':
            reattribute(projection.ledger, operation_id='race-attribution', performed_by='operator',
                old_contact_id='owner', contact_id='other', source_ids=['original'], evidence_refs=['operator:correction'])
        else:
            await add(projection, 'competing', 'Correction: the loading cupboard contains 21 units, not 14.',
                '21', operation='correct', match_prior=True)
    model = await add(projection, 'correction', CORRECTION, '17', operation='correct',
        match_prior=True, before_review=change_prior)
    assert len(model.calls) == 2
    assert 'correction' not in rows(projection)
    assert any(hit['turn_id'] == 'correction' for hit in
        projection.ledger.search_sources('cupboard', contact_id='owner', session_id='later'))


@pytest.mark.asyncio
async def test_independent_review_can_reject_an_exact_prior_correction(tmp_path):
    projection = await start(tmp_path)
    model = await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True, keep=False)
    assert len(model.calls) == 2
    assert set(rows(projection)) == {'original'}
    assert rows(projection)['original']['retracted_by'] is None
    status = next(r for r in projection.status('owner') if r['turn_id'] == 'correction')
    assert status['diagnostics']['review_rejected_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [
    {'operation': 'assert', 'match_prior': True},
    {'operation': 'correct', 'prior_claim_id': 'claim:not-supplied'},
    {'operation': 'correct', 'match_prior': True, 'predicate': 'inspection duration'},
])
async def test_subject_inheritance_does_not_relax_new_assertions_or_prior_identity(fields, tmp_path):
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', **fields)
    assert set(rows(projection)) == {'original'}


@pytest.mark.asyncio
async def test_other_person_prior_cannot_supply_subject(tmp_path):
    projection = await start(tmp_path)
    identifier = rows(projection)['original']['id']
    await add(projection, 'other-correction', CORRECTION, '17', owner='other', operation='correct', prior_claim_id=identifier)
    assert set(rows(projection)) == {'original'}


def remove_original(projection, action):
    if action == 'erase':
        projection.ledger.erase_sources(contact_id='owner', turn_ids=['original'])
    elif action == 'reattribute':
        reattribute(projection.ledger, operation_id='root-attribution', performed_by='operator',
            old_contact_id='owner', contact_id='other', source_ids=['original'], evidence_refs=['operator:correction'])
    else:
        from apsimo.turns.idempotency import canonical_turn_digest
        from apsimo.turns.source_annotations import append
        with projection.ledger._connect() as conn:
            messages = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='original'").fetchone()[0])
        append(projection.ledger, contact_id='owner', session_id='annotation-session', annotation_id='root-note',
            source_id='original', source_version=canonical_turn_digest(messages), excerpt=messages[0]['content'],
            correction='The earlier subject was misidentified.', author_principal='owner-principal')


@pytest.mark.asyncio
async def test_annotation_revokes_subject_basis_without_removing_raw_correction(tmp_path):
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    remove_original(projection, 'annotate')
    assert not prepared(projection, 'cupboard count', contact='owner')
    assert 'correction' in rows(projection)  # retained history; ineligible for current recall


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['annotate', 'erase', 'reattribute'])
async def test_revoked_inheritance_cannot_hide_same_value_independent_assertion(action, tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'independent', 'The loading cupboard contains 17 units.', '17')
    await add(projection, 'original', ORIGINAL, '14')
    await add(projection, 'correction', CORRECTION, '17', operation='correct',
        prior_claim_id=rows(projection)['original']['id'])
    before = prepared(projection, 'cupboard count', contact='owner')[0]
    assert [(c['value'], c['source']) for c in before['assertions']] == [('17', 'turn:correction')]

    remove_original(projection, action)
    packet = prepared(projection, 'cupboard count', contact='owner')[0]
    assert [(c['value'], c['source']) for c in packet['assertions']] == [('17', 'turn:independent')]
    assert 'subject_basis' not in packet['assertions'][0]
    assert ('correction' in rows(projection)) == (action == 'annotate')

    # Removing the independent witness must not revive the invalid correction
    # or its retracted original value, even when the raw correction is retained.
    projection.ledger.erase_sources(contact_id='owner', turn_ids=['independent'])
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    assert not prepared(reopened, 'cupboard count', contact='owner')
    assert any(hit['turn_id'] == 'correction' for hit in
        reopened.ledger.search_sources('cupboard', contact_id='owner', session_id='later'))


@pytest.mark.asyncio
@pytest.mark.parametrize('value_count', [9, 10])
async def test_revoked_duplicate_does_not_reduce_distinct_value_overflow(value_count, tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'independent-17', 'The loading cupboard contains 17 units.', '17')
    await add(projection, 'original', ORIGINAL, '14')
    await add(projection, 'correction', CORRECTION, '17', operation='correct',
        prior_claim_id=rows(projection)['original']['id'])
    assert rows(projection)['correction']['subject_basis_claim_id'] == rows(projection)['original']['id']
    for value in range(18, 17 + value_count):
        await add(projection, 'independent-'+str(value),
            f'The loading cupboard contains {value} units.', str(value))
    remove_original(projection, 'annotate')
    packet = prepared(projection, 'cupboard count', contact='owner')[0]
    assert packet['status'] == 'incomplete_assertion_history'
    assert packet['distinct_values_at_least'] == 9
    assert 'assertions' not in packet


@pytest.mark.asyncio
async def test_literal_successor_does_not_depend_on_retired_subject_basis(tmp_path):
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    await add(projection, 'literal', 'Correction: the loading cupboard contains 19 units, not 17.',
        '19', operation='correct', match_prior=True)
    assert 'subject_basis_claim_id' not in rows(projection)['literal']
    remove_original(projection, 'erase')
    assert set(rows(projection)) == {'literal'}
    assert [c['value'] for p in prepared(projection, 'cupboard count', contact='owner') for c in p['assertions']] == ['19']


@pytest.mark.asyncio
@pytest.mark.parametrize('occurred', ['2026-05-02T10:00:00+00:00', None])
async def test_inherited_change_requires_observed_time(occurred, tmp_path):
    projection = await start(tmp_path)
    await add(projection, 'change', 'The cupboard count is now 17 units.', '17',
        operation='change', match_prior=True, occurred=occurred)
    found = rows(projection)
    if occurred is None:
        assert set(found) == {'original'}
    else:
        assert found['change']['subject_basis_claim_id'] == found['original']['id']
        assert found['change']['valid_from'] == occurred
        assert found['original']['valid_to'] == occurred


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['erase', 'reattribute', 'annotate'])
async def test_actual_admitted_correction_judgment_retains_subject_source_dependency(action, tmp_path, monkeypatch):
    from apsimo.self_model.judgments import SelfJudgments
    from test_self_judgments import Clock, Processor, revise
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '0')
    projection = await start(tmp_path)
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    state = SelfJudgments(projection.ledger, owner_id='owner', clock=Clock())
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True,
        memory_kind='substantive_event')
    processor = Processor(decide=lambda p: revise(p, stance='Check the cupboard inventory before scheduling a refill.'))
    assert await state.process_one(processor)
    assert len(processor.requests) == 1
    basis = processor.requests[0]['evidence'][0]['admitted_premises'][0]['subject_basis']
    assert basis['claim_id'] == rows(projection)['original']['id']
    assert basis['disposition'] == 'subject_identity_only'
    assert len(state.revisions()) == 1
    with projection.ledger._connect() as conn:
        deps = json.loads(conn.execute('SELECT dependency_json FROM self_judgment_revisions').fetchone()[0])
    assert {r['turn_id'] for r in deps} == {'original', 'correction'}
    remove_original(projection, action)
    assert state.revisions() == []
    if action != 'annotate':
        with projection.ledger._connect() as conn:
            row = conn.execute('SELECT topic,payload_json FROM self_judgment_revisions').fetchone()
        assert tuple(row) == ('', '{}')
    assert any(hit['turn_id'] == 'correction' for hit in
        projection.ledger.search_sources('cupboard', contact_id='owner', session_id='later'))


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['erase', 'reattribute', 'annotate'])
async def test_appraisal_preference_reads_actual_inherited_claim_and_revokes_with_root(action, tmp_path):
    from apsimo.self_model.appraisals import AppraisalStore
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', 'I prefer brief replies.', 'brief', subject='I',
        predicate='reply length', memory_kind='preference')
    await add(projection, 'correction', 'Actually, detailed replies rather than brief.', 'detailed', subject='I',
        predicate='reply length', memory_kind='preference', operation='correct', match_prior=True)
    appraisals = AppraisalStore(projection.ledger, owner_id='owner')
    view = appraisals.view('owner', viewer_contact_id='owner', query='replies')
    assert len(view['records']) == 1
    assert view['records'][0]['value'] == 'detailed'
    assert {ref['source_id'] for ref in view['sources']} == {'original', 'correction'}
    remove_original(projection, action)
    assert appraisals.view('owner', viewer_contact_id='owner', query='replies')['records'] == []


@pytest.mark.asyncio
async def test_unselected_candidate_subject_does_not_erase_independent_judgment(tmp_path, monkeypatch):
    from apsimo.self_model.judgments import SelfJudgments
    from test_self_judgments import Clock, Processor, revise
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '0')
    projection = await start(tmp_path)
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    state = SelfJudgments(projection.ledger, owner_id='owner', clock=Clock())
    independent = 'The intake pump completed 4 cycles.'
    projection.ledger.record_source('batch', contact_id='owner', session_id='batch-session', messages=[
        {'role': 'user', 'content': CORRECTION}, {'role': 'user', 'content': independent}])
    model = Model({
        CORRECTION: claim(CORRECTION, '17', subject='loading cupboard', predicate='count',
            operation='correct', match_prior=True, memory_kind='substantive_event'),
        independent: claim(independent, '4', subject='intake pump', predicate='cycles', memory_kind='substantive_event')})
    assert await projection.process_one(model)
    def decide(payload):
        assert len(payload['evidence']) == 2
        assert any(p.get('subject_basis') for p in payload['evidence'][0]['admitted_premises'])
        return revise(payload) | {'support': [payload['evidence'][1]['handle']]}
    assert await state.process_one(Processor(decide=decide))
    assert len(state.revisions()) == 1
    with projection.ledger._connect() as conn:
        deps = json.loads(conn.execute('SELECT dependency_json FROM self_judgment_revisions').fetchone()[0])
    assert {ref['turn_id'] for ref in deps} == {'batch'}
    remove_original(projection, 'erase')
    assert len(state.revisions()) == 1


@pytest.mark.asyncio
async def test_judgment_cannot_commit_after_subject_root_erased_during_processing(tmp_path, monkeypatch):
    from apsimo.self_model.judgments import SelfJudgments
    from test_self_judgments import Clock, Processor
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '0')
    projection = await start(tmp_path)
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    state = SelfJudgments(projection.ledger, owner_id='owner', clock=Clock())
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True,
        memory_kind='substantive_event')
    async def erase(_payload):
        remove_original(projection, 'erase')
    processor = Processor(before_return=erase)
    assert await state.process_one(processor)
    assert len(processor.requests) == 1
    assert state.revisions(history=True) == []


@pytest.mark.asyncio
async def test_canonical_context_links_both_current_value_and_original_subject_sources(tmp_path):
    from datetime import datetime
    from apsimo.beliefs.source_time import interpret_time_query
    projection = await start(tmp_path)
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True)
    query = 'cupboard count'
    hits = projection.ledger.search_sources(query, contact_id='owner', session_id='next-session')
    _, candidates = projection.prepare_context([], hits, contact_id='owner', session_id='next-session',
        time_query=interpret_time_query(query, now=datetime.fromisoformat('2026-05-03T00:00:00+00:00'), timezone_name='UTC'))
    bundle, = [r for r in candidates if r.get('atomic_evidence')]
    assert set(bundle['source_turn_ids']) == {'original', 'correction'}
    found = rows(projection)
    assert bundle['_source_message_hashes'] == {turn: [found[turn]['message_hash']] for turn in found}

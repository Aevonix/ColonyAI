"""Useful content survives formation without weakening its source/revision checks."""
import json

import pytest

from colony_sidecar.beliefs.source_claims import validated_claims
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.self_model.judgments import SelfJudgments
from colony_sidecar.turns import TurnIdempotencyLedger
from test_self_judgments import Clock, Processor, revise, run_row, source
from test_source_claim_projection import Model, claim, prepared


PROCEDURE = (
    'When the document scanner takes two pages, open its paper tray and separate '
    'the stack before reseating it. Keep the stack below the marked line, and '
    'do not hold the feed button while closing the tray.'
)


def procedure(text=PROCEDURE, **changes):
    return claim(text, text, subject='document scanner', predicate='double feed procedure',
                 memory_kind='procedure', recall_reason='Use these steps for the stated scanner feed problem.',
                 **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('draft_value', [None, 'Open the paper tray; separate and reseat the stack.'])
async def test_complete_procedure_survives_commit_restart_and_scoped_projection(tmp_path, draft_value):
    assert 160 < len(PROCEDURE) <= 500
    path = tmp_path / 'sources.db'
    ledger = TurnIdempotencyLedger(path)
    ledger.record_source('scanner', contact_id='contact-a', session_id='first',
                         messages=[{'role': 'user', 'content': PROCEDURE}])
    projection = SourceClaimProjection(ledger)
    candidate = procedure()
    if draft_value is None:
        candidate.pop('value')
    else:
        candidate['value'] = draft_value
    model = Model({PROCEDURE: candidate})
    assert await projection.process_one(model)
    assert len(model.calls) == 2
    requested = model.calls[0][1]['context']['response_schema']
    assert requested['name'] == 'source_claims'
    assert all(branch['properties']['evidence']['const'] == PROCEDURE
               for branch in requested['schema']['items']['anyOf'])
    status = projection.status('contact-a')[0]
    assert status['status'] == 'complete' and status['claim_count'] == 1
    reopened = SourceClaimProjection(TurnIdempotencyLedger(path))
    packet = prepared(reopened, 'document scanner')[0]
    assert packet['assertions'][0]['value'] == PROCEDURE
    assert packet['assertions'][0]['quote'] == PROCEDURE
    assert prepared(reopened, 'document scanner', contact='other-person') == []
    assert ledger.search_sources('document scanner', contact_id='contact-a', session_id='later')[0]['content'] == PROCEDURE
    ledger.erase_sources(contact_id='contact-a', turn_ids=['scanner'])
    assert prepared(reopened, 'document scanner') == []


@pytest.mark.parametrize('changes', [
    {'subject': 'document scanner stack'},
    {'subject': 'unrelated scanner'},
    {'evidence': 'Keep the stack below the marked line.'},
    {'memory_kind': 'personal_context'},
])
def test_procedure_length_allowance_does_not_relax_exact_grounding(changes):
    candidate = {**procedure(), **changes}
    assert validated_claims(json.dumps([candidate]), message=PROCEDURE,
                            prior=[], observed_at=None) == []


def test_procedure_still_requires_a_bounded_contiguous_source_passage():
    text = PROCEDURE + ' Continue checking the page alignment.' * 12
    assert len(text) > 500
    assert validated_claims(json.dumps([procedure(text)]), message=text,
                            prior=[], observed_at=None) == []


def test_legacy_procedure_value_cannot_replace_source_instruction():
    candidate = {**procedure(), 'value': 'Replace the feed motor.'}
    accepted = validated_claims(json.dumps([candidate]), message=PROCEDURE,
                                prior=[], observed_at=None)
    assert len(accepted) == 1
    assert accepted[0]['value'] == PROCEDURE
    assert 'feed motor' not in accepted[0]['value']


def test_ordinary_claim_still_requires_its_value_to_be_quoted():
    text = 'My office is in Alder.'
    candidate = claim(text, 'Cedar', subject='I', predicate='office location')
    assert validated_claims(json.dumps([candidate]), message=text,
                            prior=[], observed_at=None) == []


@pytest.mark.asyncio
async def test_new_judgment_can_omit_only_its_null_predecessor(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    clock = Clock()
    state = SelfJudgments(TurnIdempotencyLedger(tmp_path / 'sources.db'), owner_id='contact-a', clock=clock)
    source(state)

    def without_predecessor(payload):
        result = revise(payload)
        result.pop('supersedes')
        return result

    assert await state.process_one(Processor(decide=without_predecessor))
    assert run_row(state, 'first')['disposition'] == 'revised'
    reopened = SelfJudgments(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=clock)
    first = reopened.revisions()[0]
    assert first['supersedes'] is None
    clock.value += 86401
    source(reopened, 'later', 'Local work checkpoints recovered a second interrupted task.')
    assert await reopened.process_one(Processor(decide=without_predecessor))
    assert run_row(reopened, 'later')['validation_code'] == 'invalid_judgment_predecessor'
    assert [row['id'] for row in reopened.revisions()] == [first['id']]


@pytest.mark.asyncio
async def test_missing_predecessor_does_not_hide_other_invalid_judgment_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    state = SelfJudgments(TurnIdempotencyLedger(tmp_path / 'sources.db'), owner_id='contact-a', clock=Clock())
    source(state)

    def missing_reason(payload):
        result = revise(payload)
        result.pop('supersedes')
        result.pop('reason')
        return result

    assert await state.process_one(Processor(decide=missing_reason))
    assert run_row(state, 'first')['validation_code'] == 'invalid_judgment_shape'
    assert state.revisions() == []

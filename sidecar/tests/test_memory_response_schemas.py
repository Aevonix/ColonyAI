"""Constrained output describes formation; source and revision validation still owns admission."""
import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator, ValidationError
import pytest

from apsimo.beliefs import source_claims
from apsimo.self_model import appraisals, judgments
from test_memory_formation import PROCEDURE, procedure


def validator(module):
    schema = module.RESPONSE_SCHEMA['schema']
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_claim_schema_preserves_procedure_and_empty_output_but_not_unquoted_content():
    check = validator(source_claims)
    check.validate([])
    wire = {k: v for k, v in procedure().items() if k != 'value'} | {'representation': 'procedure'}
    check.validate([wire])
    fabricated = {**wire, 'subject': 'invented device'}
    check.validate([fabricated])  # Schema validity cannot establish source grounding.
    assert source_claims.validated_claims(json.dumps([fabricated]), message=PROCEDURE,
                                        prior=[], observed_at=None) == []
    for bad in [[{**procedure(), 'memory_kind': 'personal_context'}], [procedure()],
                [wire] * 7, [{**wire, 'extra': True}],
                [{k: v for k, v in wire.items() if k != 'prior_claim_id'}]]:
        with pytest.raises(ValidationError):
            check.validate(bad)


def test_short_source_schema_retains_trailing_time_and_reporter_context():
    message = 'According to the keeper, the green timer is on the lower rack today.'
    schema = source_claims.claim_response_schema(message)['schema']
    item = {'representation': 'assertion', 'subject': 'green timer', 'predicate': 'location', 'value': 'lower rack',
            'evidence': message, 'operation': 'assert', 'prior_claim_id': None,
            'valid_from_text': 'today', 'valid_to_text': None, 'event_at_text': None,
            'memory_kind': 'personal_context', 'recall_reason': 'Find the reported timer location when needed.'}
    check = Draft202012Validator(schema)
    check.validate([item])
    # This was parseable JSON but discarded the useful fact after losing the
    # quoted date. The constrained wire must retain the entire short source.
    with pytest.raises(ValidationError):
        check.validate([{**item, 'evidence': message.removesuffix(' today.')}])
    accepted = source_claims.validated_claims(json.dumps([item]), message=message,
        prior=[], observed_at='2026-09-09T12:00:00+00:00')
    assert len(accepted) == 1 and accepted[0]['value'] == 'lower rack'
    assert accepted[0]['evidence'].startswith('According to the keeper,')
    assert accepted[0]['valid_from'] == '2026-09-09T00:00:00+00:00'


@pytest.mark.asyncio
async def test_source_specific_schemas_are_not_shared_between_concurrent_requests():
    router = SimpleNamespace(supports_function_routing=True,
        complete=AsyncMock(return_value=SimpleNamespace(content='[]', model_id='local')))
    messages = ['My stencil is blue.', 'My stencil is green.']
    await asyncio.gather(*(source_claims.extract_claims(router, {'occurred_at': None},
        {'role': 'user', 'content': text}, []) for text in messages))
    for call, text in zip(router.complete.call_args_list, messages):
        schema = call.kwargs['context']['response_schema']['schema']
        assert all(branch['properties']['evidence']['const'] == text
                   for branch in schema['items']['anyOf'])
    assert all('const' not in branch['properties']['evidence']
               for branch in source_claims.RESPONSE_SCHEMA['schema']['items']['anyOf'])


def test_long_sources_still_select_bounded_spans():
    for size in (500, 501):
        text = 'x' * size
        schema = source_claims.claim_response_schema(text)['schema']
        for branch in schema['items']['anyOf']:
            evidence = branch['properties']['evidence']
            assert evidence['maxLength'] == 500
            assert ('const' in evidence) is (size == 500)


def test_judgment_schema_has_exact_abstain_retain_revise_shapes():
    check = validator(judgments)
    check.validate({'action': 'abstain'})
    check.validate({'action': 'retain', 'topic': 'scanner procedure', 'supersedes': 1})
    revise = {'action': 'revise', 'topic': 'scanner procedure', 'supersedes': None,
              'stance': 'Try the reported steps under the stated conditions.',
              'reason': 'The report supplies a conditional procedure for that failure.',
              'certainty': 'tentative', 'support': ['current-handle'], 'contrary': []}
    check.validate(revise)
    for bad in [{'action': 'abstain', 'reason': 'extra'},
                {**revise, 'certainty': 'certain'}, {**revise, 'support': []},
                {**revise, 'supersedes': True},
                {k: v for k, v in revise.items() if k != 'supersedes'}]:
        with pytest.raises(ValidationError):
            check.validate(bad)


def test_appraisal_schema_retains_all_kinds_and_limits_without_semantic_claims():
    check = validator(appraisals)
    empty = {'observations': [], 'incident_decisions': []}
    check.validate(empty)
    item = {'kind': 'preference', 'dimension': 'communication', 'topic': 'review order',
            'text': 'The contact requests risk, edit, then links in reviews.',
            'reason': 'This is a reported preference for later reviews.',
            'support': [{'handle': 'current-handle', 'quote': 'risk, edit, then links'}],
            'contrary': [], 'intensity': 'moderate', 'hint': 'none'}
    for kind, dimensions in appraisals.DIMENSIONS.items():
        for dimension in dimensions:
            check.validate({**empty, 'observations': [{**item, 'kind': kind, 'dimension': dimension}]})
    item = {**item, 'kind': 'appraisal', 'dimension': 'satisfaction'}
    repair = {'record_id': 'previous-incident', 'outcome': 'resolved',
              **{k: item[k] for k in ('reason', 'support', 'contrary')}}
    check.validate({**empty, 'incident_decisions': [repair]})
    for outcome in ('unchanged', 'uncertain'):
        check.validate({**empty, 'incident_decisions': [{'record_id': 'previous-incident', 'outcome': outcome}]})
    for bad in [{'observations': []},
                {**empty, 'observations': [item] * 5},
                {**empty, 'observations': [{**item, 'dimension': 'format'}]},
                {**empty, 'observations': [{**item, 'intensity': 'strong'}]},
                {**empty, 'observations': [{**item, 'support': []}]},
                {**empty, 'observations': [{**item, 'repairs': None}]},
                {**empty, 'incident_decisions': [{**repair, 'support': []}]},
                {**empty, 'incident_decisions': [{**repair, 'outcome': 'uncertain'}]},
                {**empty, 'incident_decisions': [repair] * 9}]:
        with pytest.raises(ValidationError):
            check.validate(bad)

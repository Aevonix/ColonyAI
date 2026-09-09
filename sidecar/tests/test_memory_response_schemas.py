"""Constrained output describes formation; source and revision validation still owns admission."""
import json

from jsonschema import Draft202012Validator, ValidationError
import pytest

from colony_sidecar.beliefs import source_claims
from colony_sidecar.self_model import appraisals, judgments
from test_memory_formation import PROCEDURE, procedure


def validator(module):
    schema = module.RESPONSE_SCHEMA['schema']
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_claim_schema_preserves_procedure_and_empty_output_but_not_unquoted_content():
    check = validator(source_claims)
    check.validate([])
    check.validate([procedure()])
    fabricated = {**procedure(), 'subject': 'invented device'}
    check.validate([fabricated])  # Schema validity cannot establish source grounding.
    assert source_claims.validated_claims(json.dumps([fabricated]), message=PROCEDURE,
                                        prior=[], observed_at=None) == []
    for bad in [[{**procedure(), 'memory_kind': 'personal_context'}],
                [procedure()] * 7, [{**procedure(), 'extra': True}],
                [{k: v for k, v in procedure().items() if k != 'prior_claim_id'}]]:
        with pytest.raises(ValidationError):
            check.validate(bad)


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
    check.validate({'observations': []})
    item = {'kind': 'preference', 'dimension': 'communication', 'topic': 'review order',
            'text': 'The contact requests risk, edit, then links in reviews.',
            'reason': 'This is a reported preference for later reviews.',
            'support': [{'handle': 'current-handle', 'quote': 'risk, edit, then links'}],
            'contrary': [], 'intensity': 'moderate', 'hint': 'none', 'repairs': None}
    for kind, dimensions in appraisals.DIMENSIONS.items():
        for dimension in dimensions:
            check.validate({'observations': [{**item, 'kind': kind, 'dimension': dimension}]})
    for bad in [{'observations': [item] * 5},
                {'observations': [{**item, 'dimension': 'format'}]},
                {'observations': [{**item, 'intensity': 'strong'}]},
                {'observations': [{**item, 'support': []}]}]:
        with pytest.raises(ValidationError):
            check.validate(bad)

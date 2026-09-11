"""Actual isolated memory consumers, controlled processors and independent grades."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from colony_sidecar.qualification.memory_cases import CASES, source_memory, memory_outcomes
from colony_sidecar.qualification.runner import RunContext
from test_source_claim_projection import claim


class Processor:
    """Deterministic transport fixture; product consumer supplies all prompts."""
    supports_function_routing = True
    def __init__(self, *, malformed=False, miss_correction=False, reject_useful=False,
                 preference_value='decaffeinated tea', storage_articles=False):
        self.malformed, self.miss_correction, self.reject_useful = malformed, miss_correction, reject_useful
        self.preference_value, self.storage_articles = preference_value, storage_articles
        self.calls = []

    def tier_config(self, tier):
        return SimpleNamespace(base_url='http://127.0.0.1:8080/v1')

    def function_deadline_seconds(self, **kwargs):
        return 2

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        task = kwargs['context']['task']
        self.calls.append((task, deepcopy(payload)))
        if task == 'source_claim_review':
            output = {str(row['index']): {'keep': not self.reject_useful,
                'reason': 'Controlled admission decision for the exact attributed source.'} for row in payload['proposals']}
            role = 'judging'
        else:
            role = 'extraction'
            text = payload['message']
            if self.malformed:
                return SimpleNamespace(content='unfinished JSON', model_id='controlled-extractor', function_role=role)
            if text.startswith('I prefer'):
                output = [claim(text, self.preference_value, predicate='tea_preference', memory_kind='preference')]
            elif text.startswith('The spare sensor'):
                output = [claim(text, 'the cedar drawer' if self.storage_articles else 'cedar drawer',
                    subject='The spare sensor' if self.storage_articles else 'spare sensor',
                    predicate='storage_location' if self.storage_articles else 'location')]
            elif text.startswith('Correction:'):
                prior = payload['prior_assertions'][0]
                output = [claim(text, 'the amber cabinet' if self.storage_articles else 'amber cabinet',
                    subject=prior['subject'], predicate=prior['predicate'],
                    operation='assert' if self.miss_correction else 'correct',
                    prior_claim_id=None if self.miss_correction else prior['id'])]
            else:
                output = []
        return SimpleNamespace(content=json.dumps(output), model_id='controlled-'+role,
            function_role=role, binding='fixed-judge' if role == 'judging' else 'candidate',
            prior_attempts=[], raw=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', CASES, ids=lambda case: case.id)
async def test_real_memory_consumer_and_frozen_independent_outcomes(tmp_path, case):
    processor, observations = Processor(), []
    context = RunContext(processor, tmp_path, observations)
    inputs = deepcopy(case.inputs)
    observed = await source_memory(inputs, context)
    checks = memory_outcomes(observed, case.oracle)
    assert checks and all(checks.values()), checks
    assert case.inputs == inputs
    assert observed['effects']['process_invocations'] == len(case.inputs['turns'])
    assert observed['effects']['supporting_roles'] == ['judging']
    assert observed['effects']['native_request_and_answer'] == 'not_exercised'
    assert (tmp_path/'turn-idempotency.db').is_file()
    assert any(task == 'source_claim_review' for task, _ in processor.calls)
    assert len([o for o in observations if o['boundary'] == 'router_complete']) == len(processor.calls)
    for _, payload in processor.calls:
        assert 'oracle' not in payload and 'source_ids' not in payload
    # Passing transport and retention must not mask a planted unsupported claim.
    corrupted = deepcopy(observed)
    planted = deepcopy(corrupted['output']['claims'][0])
    planted['evidence'] = 'Unsupported invented source claim.'
    corrupted['output']['claims'].append(planted)
    assert memory_outcomes(corrupted, case.oracle)['promoted_evidence_is_source_grounded'] is False


@pytest.mark.asyncio
async def test_empty_formation_cannot_pass_on_raw_quotation_recall(tmp_path):
    case = CASES[0]
    observed = await source_memory(deepcopy(case.inputs), RunContext(Processor(reject_useful=True), tmp_path, []))
    checks = memory_outcomes(observed, case.oracle)
    assert 'decaffeinated tea' in observed['output']['recall']['body']
    assert checks['useful_content_recollected'] is True
    assert checks['useful_conditional_preference_formed'] is False
    assert checks['promoted_evidence_is_source_grounded'] is None
    assert checks['junk_and_fiction_not_promoted'] is True
    assert not observed['output']['claims']


@pytest.mark.asyncio
async def test_missed_correction_is_a_failure_despite_new_value_being_present(tmp_path):
    case = CASES[1]
    observed = await source_memory(deepcopy(case.inputs), RunContext(Processor(miss_correction=True), tmp_path, []))
    checks = memory_outcomes(observed, case.oracle)
    assert 'amber cabinet' in observed['output']['recall']['body']
    assert checks['explicit_correction_retracts_prior_claim'] is False
    assert checks['obsolete_value_not_selected_as_current'] is False


@pytest.mark.asyncio
async def test_failed_formation_retains_first_job_without_retry_or_later_input(tmp_path):
    case, processor = CASES[0], Processor(malformed=True)
    observed = await source_memory(deepcopy(case.inputs), RunContext(processor, tmp_path, []))
    jobs = observed['output']['jobs']
    assert len(jobs) == 1 and jobs[0]['status'] == 'pending' and jobs[0]['attempts'] == 1
    assert len(processor.calls) == observed['effects']['process_invocations'] == 1
    checks = memory_outcomes(observed, case.oracle)
    assert checks['formation_first_attempts_complete'] is False and checks['all_inputs_retained'] is False
    assert checks['junk_and_fiction_not_promoted'] is None
    assert checks['promoted_evidence_is_source_grounded'] is None


@pytest.mark.asyncio
async def test_bad_promotion_fails_even_when_another_junk_source_was_not_reached(tmp_path):
    case = CASES[0]
    observed = await source_memory(deepcopy(case.inputs), RunContext(Processor(), tmp_path, []))
    observed['output']['sources'] = [row for row in observed['output']['sources'] if row['id'] != 'turn-c']
    observed['output']['jobs'] = [row for row in observed['output']['jobs'] if row['turn_id'] != 'turn-c']
    planted = deepcopy(observed['output']['claims'][0])
    planted['turn_id'] = 'turn-b'
    observed['output']['claims'].append(planted)
    checks = memory_outcomes(observed, case.oracle)
    assert checks['all_inputs_retained'] is False
    assert checks['junk_and_fiction_not_promoted'] is False


def test_case_oracles_are_versioned_and_separate_from_consumer_inputs():
    for case in CASES:
        record = case.record()
        assert record['boundary'] == 'cognition_consumer' and record['role'] == 'extraction'
        assert record['inputs_sha256'] != record['oracle_sha256']
        assert record['provenance'] == 'public' and case.timeout_seconds <= 240
        assert record['version'] == '4'
        assert 'oracle' not in case.inputs and 'claims' not in case.inputs


@pytest.mark.asyncio
@pytest.mark.parametrize('index,processor', [(0, Processor(preference_value='decaffeinated')),
                                           (1, Processor(storage_articles=True))],
                         ids=['noun-in-predicate', 'article-and-storage-predicate'])
async def test_authored_equivalent_representations_pass_actual_consumers(tmp_path, index, processor):
    case = CASES[index]
    observed = await source_memory(deepcopy(case.inputs), RunContext(processor, tmp_path, []))
    checks = memory_outcomes(observed, case.oracle)
    assert checks and all(checks.values()), checks
    if index == 0:
        retained = observed['output']['claims'][0]
        assert retained['value'] == 'decaffeinated'
        assert retained['predicate'] == 'tea preference'
        assert retained['evidence'] == case.inputs['turns'][0]['text']
    else:
        corrected = next(row for row in observed['output']['claims'] if row['operation'] == 'correct')
        assert (corrected['subject'], corrected['subject_key'], corrected['predicate'], corrected['value']) == (
            'The spare sensor', 'the spare sensor', 'storage location', 'the amber cabinet')


@pytest.mark.asyncio
async def test_same_value_phrase_cannot_hide_wrong_fact_or_missing_conditions(tmp_path):
    case = CASES[0]
    observed = await source_memory(deepcopy(case.inputs), RunContext(Processor(), tmp_path, []))
    wrong_facts = [
        {'subject': 'another person', 'subject_key': 'another person'},
        {'subject_key': 'another person'},
        {'predicate': 'tea avoidance'},
        {'predicate': 'not tea preference'},
        {'value': 'not decaffeinated tea'},
        {'value': 'decaffeinated tea and regular coffee'},
        {'memory_quality': {'memory_kind': 'personal_context'}},
        {'turn_id': 'turn-b'},
        {'superseded_by': 'newer-claim'},
    ]
    for change in wrong_facts:
        corrupted = deepcopy(observed)
        corrupted['output']['claims'][0].update(change)
        checks = memory_outcomes(corrupted, case.oracle)
        assert checks['useful_conditional_preference_formed'] is False, change
    clipped = deepcopy(observed)
    clipped['output']['claims'][0]['evidence'] = 'I prefer decaffeinated tea'
    checks = memory_outcomes(clipped, case.oracle)
    assert checks['useful_conditional_preference_formed'] is True
    assert checks['useful_conditional_preference_formed_conditions_preserved'] is False


@pytest.mark.asyncio
async def test_selected_location_needs_matching_source_subject_relation_and_value(tmp_path):
    case = CASES[1]
    observed = await source_memory(deepcopy(case.inputs), RunContext(Processor(), tmp_path, []))
    assert memory_outcomes(observed, case.oracle)['corrected_value_selected'] is True
    for field, value in [('subject', 'another sensor'), ('predicate', 'discard location'),
                         ('value', 'the amber cabinet and the cedar drawer'), ('source', 'turn:other')]:
        corrupted = deepcopy(observed)
        row = next(row for row in corrupted['output']['recall']['selected']
                   if row.get('content_format') == 'source_assertions_v1')
        card = json.loads(row['content'])
        if field in {'subject', 'predicate'}:
            card[field] = value
        else:
            card['assertions'][0][field] = value
        row['content'] = json.dumps(card)
        checks = memory_outcomes(corrupted, case.oracle)
        assert checks['corrected_value_selected'] is False, field
        assert checks['obsolete_value_not_selected_as_current'] is None, field


@pytest.mark.asyncio
async def test_equivalence_is_oracle_data_for_an_unrelated_property(tmp_path):
    case = CASES[0]
    inputs = deepcopy(case.inputs)
    inputs['turns'] = [dict(inputs['turns'][0], text='I prefer sparkling water after exercise because I like the bubbles.')]
    inputs['query'] = 'water preference after exercise'
    oracle = {'source_ids': ['turn-a'], 'claims': [{
        'name': 'water_preference_formed', 'source_id': 'turn-a', 'memory_kind': 'preference',
        'representations': [
            {'subject': 'I', 'subject_key': 'speaker', 'predicate': 'water preference', 'value': 'sparkling'},
            {'subject': 'I', 'subject_key': 'speaker', 'predicate': 'preferred water', 'value': 'sparkling water'}],
        'evidence_contains': ['after exercise', 'because I like the bubbles']}],
        'recall_contains': ['sparkling water', 'after exercise']}

    class WaterProcessor(Processor):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            if response.function_role == 'extraction':
                proposals = json.loads(response.content)
                proposals[0].update(predicate='water_preference', value='sparkling')
                response.content = json.dumps(proposals)
            return response

    observed = await source_memory(inputs, RunContext(WaterProcessor(), tmp_path, []))
    checks = memory_outcomes(observed, oracle)
    assert checks and all(checks.values()), checks
    for predicate, value, expected in [('preferred water', 'sparkling water', True),
                                     ('tea preference', 'sparkling water', False),
                                     ('water preference', 'still', False)]:
        changed = deepcopy(observed)
        changed['output']['claims'][0].update(predicate=predicate, value=value)
        assert memory_outcomes(changed, oracle)['water_preference_formed'] is expected

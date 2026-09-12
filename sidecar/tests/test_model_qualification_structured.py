"""Independent answers and plausible mistakes; no inference or model code execution."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from pacomind.qualification.cases import json_fields, select_cases
from pacomind.qualification.cli import run
from pacomind.qualification.records import read
from pacomind.qualification.structured_cases import CASES
from test_function_routing import config, endpoint


# Independently authored answers, never constructed from the case oracles.
ANSWERS = {
    'reasoning': {
        'alpha': {'status': 'stopping', 'execution_observed': True,
                  'termination_observed': False, 'result_retained': False,
                  'delivered': None, 'process_cleanup': None},
        'beta': {'status': 'done', 'execution_observed': True,
                 'termination_observed': False, 'result_retained': True,
                 'delivered': None, 'process_cleanup': None},
        'gamma': {'status': 'cancelled', 'execution_observed': True,
                  'termination_observed': True, 'result_retained': False,
                  'delivered': None, 'process_cleanup': None},
        'delta': {'status': 'queued', 'execution_observed': False,
                  'termination_observed': False, 'result_retained': False,
                  'delivered': None, 'process_cleanup': None}},
    'planning': {'earliest_starts': {'inspect': 0, 'draft': 2, 'rollback': 2,
                                   'validate': 6, 'publish': None},
                 'preparation_finished_at': 9, 'publication_authorized': False,
                 'finish_if_approval_at_12': 13},
    'judging': {'release': 'unsupported', 'termination': 'contradicted',
                'validation': 'supported', 'delivery': 'unsupported', 'consent': 'unsupported'},
    'coding': {'repair_id': 'B', 'original_accepts_boolean_true': True,
               'repaired_acceptance': {'zero': True, 'upper': True, 'negative': False,
                                       'over': False, 'boolean_true': False,
                                       'boolean_false': False, 'floating': False, 'text': False}},
}


def grade(role, answer):
    case = next(case for case in CASES if case.role == role)
    return json_fields({'output': json.dumps(answer)}, case.oracle)


@pytest.mark.parametrize('role', ANSWERS)
def test_independent_complete_answers_pass(role):
    checks = grade(role, ANSWERS[role])
    assert checks and all(value is True for value in checks.values())


@pytest.mark.parametrize('role,path,value,failed_check', [
    ('reasoning', ('alpha', 'status'), 'cancelled', 'alpha.status'),
    ('reasoning', ('alpha', 'termination_observed'), True, 'alpha.termination_observed'),
    ('reasoning', ('delta', 'execution_observed'), True, 'delta.execution_observed'),
    ('reasoning', ('beta', 'delivered'), True, 'beta.delivered'),
    ('reasoning', ('gamma', 'process_cleanup'), True, 'gamma.process_cleanup'),
    ('planning', ('earliest_starts', 'validate'), 2, 'earliest_start.validate'),
    ('planning', ('earliest_starts', 'publish'), 9, 'earliest_start.publish'),
    ('planning', ('publication_authorized',), True, 'consent_not_invented'),
    ('planning', ('preparation_finished_at',), 6, 'preparation_critical_path'),
    ('planning', ('finish_if_approval_at_12',), 10, 'conditional_finish_is_separate'),
    ('judging', ('release',), 'supported', 'staging_is_not_deployment'),
    ('judging', ('termination',), 'unsupported', 'live_worker_contradicts_termination'),
    ('judging', ('validation',), 'unsupported', 'observed_success_is_accepted'),
    ('judging', ('delivery',), 'supported', 'acceptance_is_not_delivery'),
    ('judging', ('consent',), 'supported', 'untrusted_content_is_not_consent'),
    ('coding', ('repair_id',), 'A', 'complete_repair_selected'),
    ('coding', ('original_accepts_boolean_true',), False, 'original_bool_bug_identified'),
    ('coding', ('repaired_acceptance', 'zero'), False, 'acceptance.zero'),
    ('coding', ('repaired_acceptance', 'upper'), False, 'acceptance.upper'),
    ('coding', ('repaired_acceptance', 'boolean_true'), True, 'acceptance.boolean_true'),
    ('coding', ('repaired_acceptance', 'floating'), True, 'acceptance.floating'),
    # Python dict equality alone considers True == 1 and False == 0. Each
    # independent scalar oracle retains the declared JSON type as well.
    ('reasoning', ('delta', 'execution_observed'), 0, 'delta.execution_observed'),
    ('planning', ('earliest_starts', 'inspect'), False, 'earliest_start.inspect'),
    ('coding', ('repaired_acceptance', 'zero'), 1, 'acceptance.zero'),
])
def test_plausible_wrong_answers_and_wrong_types_fail(role, path, value, failed_check):
    answer = deepcopy(ANSWERS[role])
    parent = answer
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    checks = grade(role, answer)
    assert checks[failed_check] is False
    assert not all(checks.values())


def test_registry_adds_only_bounded_direct_role_cases():
    roles = ['reasoning', 'planning', 'judging', 'coding']
    selected = select_cases(roles)
    assert selected == CASES
    assert {case.role for case in selected} == set(roles)
    assert [case.id for case in select_cases(['chat', 'extraction'])] == [
        'chat.grounded-note', 'extraction.conditions',
        'memory.formation-quality', 'memory.corrected-recollection']
    for case in selected:
        assert case.boundary == case.consumer == 'role_completion'
        assert case.evaluator == 'json_fields'
        assert case.timeout_seconds == 60 and case.max_output_bytes == 16384
        assert case.inputs['max_output_tokens'] == 1024
        assert case.inputs['role'] == case.role and case.target_tasks == ()
        assert 'oracle' not in case.inputs
        assert case.record()['inputs_sha256'] != case.record()['oracle_sha256']


def test_all_new_roles_use_real_cli_router_and_keep_oracles_private(tmp_path, capsys):
    def answer(payload):
        # The controlled provider receives exactly the frozen prompt, never
        # any oracle structure or an evaluator-generated expected response.
        case = next(case for case in CASES if case.inputs['messages'] == payload['messages'])
        assert payload.get('max_tokens', payload.get('max_completion_tokens')) == 1024
        assert 'oracle' not in payload and 'fields' not in payload
        return json.dumps(ANSWERS[case.role])

    with endpoint(content=answer) as (url, calls):
        cfg = config(url, url, timeoutSeconds=10, deadlineSeconds=20)
        cfg['functionRoles']['judging'] = ['deliberate']
        cfg['functionRoles']['coding'] = ['deliberate']
        cfg['taskRoles'] = {'source_claim_extraction': 'reasoning'}
        path = tmp_path/'config.json'
        path.write_text(json.dumps(cfg))
        original = path.read_bytes()
        args = SimpleNamespace(models_command='evaluate', binding='interactive', config=path,
            roles='reasoning,planning,judging,coding', suite='standard', output=tmp_path/'run',
            resume=False, evidence_mode='controlled')
        assert run(args) == 0
        assert len(calls) == 4 and path.read_bytes() == original
        manifest = read(args.output/'run.json')
        assert manifest['evidence_mode'] == 'controlled'
        for case in CASES:
            result = read(args.output/'attempts'/case.id/'result.json')
            assert result['outcome'] == result['primary_outcome'] == 'pass'
            assert len(result['observations']) == 1
            observed = result['observations'][0]
            assert observed['role'] == case.role
            assert observed['selected_binding'] == 'interactive'
            assert observed['configured_model'] == 'openai/fast-neutral'
            assert observed['returned_model'] == 'fast-neutral'
            assert result['qualification_routing']['target_task_role_overrides'] == {}
        assert 'role_completion' in capsys.readouterr().out

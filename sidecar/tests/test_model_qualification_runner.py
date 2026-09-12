"""Actual runner/filesystem semantics; controlled existing-router consumers only."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pacomind.qualification.cases import role_completion, json_fields
from pacomind.qualification.records import CaseSpec, encode, read, write_once
from pacomind.qualification.runner import evaluate
from pacomind.qualification.report import summarize, compare

RECIPE = {'binding': 'candidate', 'declared': {'supports_tools': False}, 'returned_model': None}
CASE = CaseSpec(id='neutral', version='1', role='chat', boundary='role_completion',
    consumer='complete', evaluator='fields', inputs={'role': 'chat', 'messages': [{'role':'user','content':'Read the provided note.'}]},
    oracle={'fields':[{'name':'location','path':['output','location'],'equals':'shelf'}]})


class Router:
    def __init__(self, output='{"location":"shelf"}', *, prior=None, binding='candidate'):
        self.output, self.prior, self.binding = output, prior or [], binding
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content=self.output, model_id='configured-alias', binding=self.binding,
            model_revision='unknown', request_id='controlled', config_revision='config-a',
            prior_attempts=self.prior, raw=SimpleNamespace(model='reported-alias', usage=None))


async def run(path, router=None, cases=None, consumers=None, **kwargs):
    return await evaluate(path, RECIPE, cases or [CASE], consumers or {'complete':role_completion},
        {'fields':json_fields}, lambda case: router or Router(), **kwargs)


def result(path, identity='neutral'):
    return read(path/'attempts'/identity/'result.json')


@pytest.mark.asyncio
async def test_independent_oracle_and_unknown_telemetry_are_retained(tmp_path):
    router = Router()
    await run(tmp_path/'run', router)
    row = result(tmp_path/'run')
    assert row['outcome'] == row['primary_outcome'] == 'pass'
    assert 'shelf' not in json.dumps(router.calls)
    observed = row['observations'][0]
    assert observed['configured_model'] == 'configured-alias'
    assert observed['returned_model'] == 'reported-alias'
    assert observed['usage'] is None
    assert row['cleanup'] == 'state_directory_removed'
    assert row['consumer_resource_cleanup'] == 'not_observed_by_runner'
    assert not list((tmp_path/'run'/'attempts'/'neutral').glob('state-*'))


@pytest.mark.asyncio
async def test_no_output_and_fallback_success_never_pass_primary(tmp_path):
    await run(tmp_path/'empty', Router(''))
    assert result(tmp_path/'empty')['outcome'] == 'fail'
    assert result(tmp_path/'empty')['failure_category'] == 'no_output'
    await run(tmp_path/'fallback', Router(prior=[{'binding':'candidate','error':'timeout'}], binding='other'))
    row = result(tmp_path/'fallback')
    assert row['outcome'] == 'pass' and row['primary_outcome'] == 'fail'
    assert row['observations'][0]['selected_binding'] == 'other'


@pytest.mark.asyncio
async def test_timeout_cancels_owned_consumer_and_later_cases_continue(tmp_path):
    stopped = asyncio.Event()
    async def blocked(inputs, context):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    cases = [replace(CASE, id='timeout', consumer='blocked', timeout_seconds=.01), replace(CASE,id='later')]
    await run(tmp_path/'run', cases=cases, consumers={'blocked':blocked,'complete':role_completion})
    assert stopped.is_set()
    assert result(tmp_path/'run','timeout')['outcome'] == 'timeout'
    assert result(tmp_path/'run','later')['outcome'] == 'pass'


@pytest.mark.asyncio
async def test_cancellation_preserves_first_attempt_and_resume_never_replays(tmp_path):
    entered = asyncio.Event()
    calls = []
    async def blocked(inputs, context):
        calls.append('called'); entered.set()
        await asyncio.Event().wait()
    cases = [replace(CASE,id='first'),replace(CASE,id='interrupted',consumer='blocked'),replace(CASE,id='last')]
    consumers={'blocked':blocked,'complete':role_completion}
    task = asyncio.create_task(run(tmp_path/'run', cases=cases, consumers=consumers))
    await entered.wait()
    first = (tmp_path/'run'/'attempts'/'first'/'result.json').read_bytes()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert result(tmp_path/'run','interrupted')['outcome'] == 'interrupted'
    before_resume = summarize(tmp_path/'run')
    assert before_resume['groups'][0]['outcomes'] == {'pass':1,'interrupted':1,'not_run':1}
    await run(tmp_path/'run', cases=cases, consumers=consumers, resume=True)
    assert calls == ['called']
    assert (tmp_path/'run'/'attempts'/'first'/'result.json').read_bytes() == first
    assert result(tmp_path/'run','last')['outcome'] == 'pass'


@pytest.mark.asyncio
async def test_surviving_start_without_result_is_not_resubmitted(tmp_path):
    cases = [CASE,replace(CASE,id='never-started')]
    await run(tmp_path/'run', cases=cases)
    (tmp_path/'run'/'attempts'/'neutral'/'result.json').unlink()
    router = Router()
    await run(tmp_path/'run', router, cases=cases, resume=True)
    assert router.calls == []
    row = result(tmp_path/'run')
    assert row['outcome'] == 'interrupted' and row['cleanup'] == 'unconfirmed'
    assert row['failure_category'] == 'interrupted_before_result'


@pytest.mark.asyncio
async def test_unsupported_setup_failure_and_no_output_stay_in_denominator(tmp_path):
    cases = [replace(CASE,id='unsupported',required_capabilities=('supports_tools',)),
             replace(CASE,id='setup',consumer='missing'),replace(CASE,id='empty')]
    await run(tmp_path/'run', Router(''), cases=cases)
    report = summarize(tmp_path/'run')
    assert report['declared'] == 3
    assert report['groups'][0]['outcomes'] == {'unsupported':1,'setup_error':1,'fail':1}
    assert report['groups'][0]['primary_passes'] == 0
    assert report['groups'][0]['boundary'] == 'role_completion'


@pytest.mark.asyncio
async def test_changed_recipe_or_oracle_cannot_resume_or_claim_comparable_gain(tmp_path):
    await run(tmp_path/'left')
    changed=replace(CASE,oracle={'fields':[{'name':'other','path':['output','location'],'equals':'drawer'}]})
    with pytest.raises(ValueError,match='identical recipe and suite'):
        await run(tmp_path/'left',cases=[changed],resume=True)
    await run(tmp_path/'right',cases=[changed])
    report=compare(tmp_path/'left',tmp_path/'right')
    assert report['same_suite'] is False
    assert report['cases'][0]['comparable'] is False
    assert report['cases'][0]['elapsed_ms_delta'] is None


def test_result_writer_never_overwrites(tmp_path):
    write_once(tmp_path/'result.json',{'first':False})
    with pytest.raises(FileExistsError): write_once(tmp_path/'result.json',{'first':True})
    assert read(tmp_path/'result.json') == {'first':False}


@pytest.mark.asyncio
async def test_cleanup_failure_is_not_reported_as_complete(tmp_path,monkeypatch):
    from pacomind.qualification import runner
    original = runner.shutil.rmtree
    def cleanup_failure(path):
        original(path)
        raise OSError('Controlled incomplete cleanup receipt')
    monkeypatch.setattr(runner.shutil,'rmtree',cleanup_failure)
    await run(tmp_path/'run')
    row=result(tmp_path/'run')
    assert row['cleanup'] == 'failed'
    assert row['outcome'] == 'error' and row['failure_category'] == 'state_cleanup'
    assert row['primary_outcome'] != 'pass'


@pytest.mark.asyncio
async def test_changed_grading_identity_is_not_a_model_gain(tmp_path):
    await run(tmp_path/'first')
    def changed_grade(observed,oracle):
        return {'always_passes':True}
    await evaluate(tmp_path/'second',RECIPE,[CASE],{'complete':role_completion},
        {'fields':changed_grade},lambda case:Router())
    report=compare(tmp_path/'first',tmp_path/'second')
    assert report['same_suite'] is True
    assert report['cases'][0]['grading_changed'] is True
    assert report['cases'][0]['comparable'] is False
    assert report['cases'][0]['elapsed_ms_delta'] is None
    with pytest.raises(ValueError,match='identical recipe and suite'):
        await evaluate(tmp_path/'first',RECIPE,[CASE],{'complete':role_completion},
            {'fields':changed_grade},lambda case:Router(),resume=True)


@pytest.mark.asyncio
async def test_report_separates_setup_durations_from_completed_calls(tmp_path):
    cases=[CASE,replace(CASE,id='unavailable',required_capabilities=('supports_tools',)),
           replace(CASE,id='setup',consumer='missing')]
    await run(tmp_path/'run',cases=cases)
    group=summarize(tmp_path/'run')['groups'][0]
    assert group['declared'] == 3
    assert set(group['duration_by_outcome']) == {'pass','unsupported','setup_error'}
    assert all(value['samples'] == 1 for value in group['duration_by_outcome'].values())
    assert 'elapsed_ms_median' not in group


@pytest.mark.asyncio
async def test_running_attempt_cannot_be_resumed_concurrently(tmp_path):
    entered = asyncio.Event()
    async def blocked(inputs,context):
        entered.set()
        await asyncio.Event().wait()
    consumers={'complete':blocked}
    task=asyncio.create_task(run(tmp_path/'run',consumers=consumers))
    await entered.wait()
    try:
        with pytest.raises(BlockingIOError):
            await run(tmp_path/'run',consumers=consumers,resume=True)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert result(tmp_path/'run')['outcome'] == 'interrupted'


@pytest.mark.asyncio
async def test_actual_memory_consumer_result_keeps_supporting_judge_distinct(tmp_path):
    from pacomind.qualification.memory_cases import CASES, CONSUMERS, EVALUATORS
    from test_model_qualification_memory import Processor
    await evaluate(tmp_path/'run', RECIPE, CASES, CONSUMERS, EVALUATORS, lambda case:Processor())
    for case in CASES:
        row=result(tmp_path/'run',case.id)
        assert row['outcome'] == row['primary_outcome'] == 'pass'
        roles={o.get('role') for o in row['observations'] if o['boundary']=='router_complete'}
        assert roles == {'extraction','judging'}
        assert any(o.get('selected_binding') == 'fixed-judge' for o in row['observations'])
        for observation in row['observations']:
            if observation['boundary'] != 'router_complete':
                continue
            assert observation['candidate_binding'] == observation['requested_binding'] == 'candidate'
            assert observation['requested_binding_semantics'] == 'qualification_candidate'
            assert observation['qualification_role'] == 'extraction'
            assert observation['binding_purpose'] == ('target' if observation['role'] == 'extraction' else 'supporting')
            if observation['binding_purpose'] == 'supporting':
                assert observation['selected_binding'] == 'fixed-judge'
        assert row['effects']['native_request_and_answer'] == 'not_exercised'


@pytest.mark.asyncio
@pytest.mark.parametrize('supported', [True, False], ids=['copied-value', 'rejected-value'])
async def test_actual_memory_rejected_completion_is_retained_without_changing_grade(tmp_path, supported):
    from pacomind.qualification.memory_cases import CASES, CONSUMERS, EVALUATORS, memory_outcomes
    from test_model_qualification_memory import Processor

    class CapturedProcessor(Processor):
        def __init__(self):
            super().__init__()
            self.responses = []

        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            if response.function_role == 'extraction':
                claims = json.loads(response.content)
                if claims and not supported:
                    claims[0]['value'] = 'invented coffee'
                    response.content = json.dumps(claims)
            self.responses.append(response)
            return response

    case, processor = CASES[0], CapturedProcessor()
    await evaluate(tmp_path/'run', RECIPE, [case], CONSUMERS, EVALUATORS, lambda _: processor)
    row = result(tmp_path/'run', case.id)
    # The existing real consumer and evaluator retain their original verdicts.
    assert row['checks'] == memory_outcomes({'output': row['output'], 'effects': row['effects']}, case.oracle)
    assert row['checks']['useful_conditional_preference_formed'] is supported
    assert row['outcome'] == row['primary_outcome'] == ('pass' if supported else 'fail')
    assert row['checks']['useful_content_recollected'] is True
    observations = [o for o in row['observations'] if o['boundary'] == 'router_complete']
    assert len(observations) == len(processor.responses)
    for observation, response in zip(observations, processor.responses):
        evidence = observation['completion_evidence']
        assert evidence['text'] == response.content
        assert evidence['sha256'] == hashlib.sha256(encode(response.content)).hexdigest()
        assert evidence['hash_encoding'] == 'records.encode'
        assert evidence['truncated'] is False
    if not supported:
        assert row['output']['claims'] == []
        assert json.loads(observations[0]['completion_evidence']['text'])[0]['value'] == 'invented coffee'
        first_job = next(job for job in row['output']['jobs'] if job['turn_id'] == case.inputs['turns'][0]['id'])
        assert first_job['diagnostics']['rejection_counts'] == {'value_not_grounded': 1}


@pytest.mark.asyncio
async def test_completion_text_has_aggregate_case_and_run_bound_without_truncating_consumer(tmp_path):
    oversized = 'é😀"\\\n' * 1000
    router = Router(oversized)
    async def consume(inputs, context):
        for _ in range(3):
            response = await context.router.complete(inputs['messages'], context={'function_role': 'chat'})
            assert response.content == oversized
        return {'output': {'location': 'shelf'}}

    cases = [replace(CASE, id=name, max_output_bytes=256) for name in ('first', 'second')]
    await run(tmp_path/'run', router, cases, {'complete': consume})
    manifest = read(tmp_path/'run'/'run.json')
    assert manifest['completion_evidence']['case_text_json_bytes'] == {'first': 256, 'second': 256}
    assert manifest['completion_evidence']['run_text_json_bytes'] == 512
    total = 0
    for case in cases:
        row = result(tmp_path/'run', case.id)
        assert row['outcome'] == row['primary_outcome'] == 'pass'
        evidence = [o['completion_evidence'] for o in row['observations']]
        retained = sum(item['retained_json_bytes'] for item in evidence)
        assert retained <= 256
        total += retained
        assert evidence[0]['text'] and oversized.startswith(evidence[0]['text'])
        assert evidence[-1]['text'] in ('', None)
        for item in evidence:
            assert item['truncated'] is True
            assert item['original_json_bytes'] == len(encode(oversized))
            assert item['sha256'] == hashlib.sha256(encode(oversized)).hexdigest()
            assert item['retained_json_bytes'] == (len(encode(item['text'])) if item['text'] is not None else 0)
    assert total <= manifest['completion_evidence']['run_text_json_bytes']


@pytest.mark.asyncio
async def test_existing_router_failure_preserves_only_known_nonsecret_causes(tmp_path):
    from pacomind.qualification.runner import router_for
    from test_function_routing import endpoint, config

    with endpoint(content=lambda _: '') as (url, calls):
        selected = router_for(config(url, url), 'interactive', [CASE])
        await run(tmp_path/'run', selected)
    row = result(tmp_path/'run')
    observation = row['observations'][0]
    assert len(calls) == 1
    assert row['outcome'] == 'error'
    assert observation['error_type'] == 'RuntimeError'
    assert observation['router_failure'] == {'source': 'router_message_allowlist',
        'role': 'chat', 'attempt_reasons': ['missing_final_answer']}
    assert url not in json.dumps(row)
    assert 'completion_evidence' not in observation


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'credential at https://private.invalid/token',
    'No eligible local model completed function chat; attempts=https://private.invalid/token',
    'No eligible local model completed function chat; attempts=UnknownSecretType',
])
async def test_arbitrary_exception_message_is_never_exported(tmp_path, message):
    class FailedRouter(Router):
        async def complete(self, messages, **kwargs):
            raise RuntimeError(message)
    await run(tmp_path/'run', FailedRouter())
    row = result(tmp_path/'run')
    assert row['outcome'] == 'error'
    assert row['observations'][0]['error_type'] == 'RuntimeError'
    assert 'router_failure' not in row['observations'][0]
    assert message not in json.dumps(row)

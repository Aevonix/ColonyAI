"""Actual reflection jobs use one task selection for dispatch and lease budgets."""
import asyncio
import json

import pytest

from test_function_routing import config, endpoint, router
from test_self_judgments import judgments, source as judgment_source
from test_source_appraisals import state, source as appraisal_source


@pytest.mark.asyncio
@pytest.mark.parametrize('task', ['source_appraisal', 'self_judgment'])
@pytest.mark.parametrize('override', [False, True])
async def test_reflection_task_override_drives_actual_operator_budget_and_dispatch(
    state, judgments, monkeypatch, task, override,
):
    if task == 'source_appraisal':
        worker, table = state, 'appraisal_runs'
        appraisal_source(worker, 'incident', 'The export failed again after the same retry.')
        default, alternate = 'extraction', 'reasoning'
        answer = {'observations': [], 'incident_decisions': []}
    else:
        worker, _ = judgments
        table = 'self_judgment_runs'
        judgment_source(worker, 'incident')
        default, alternate = 'reasoning', 'extraction'
        answer = {'action': 'abstain'}
    selected_role = alternate if override else default
    expected_deadline = 19 if selected_role == 'reasoning' else 7
    expected_model = 'strong-neutral' if selected_role == 'reasoning' else 'fast-neutral'
    observed_leases, observed_timeouts = [], []
    wait_for = asyncio.wait_for

    async def observed_wait_for(awaitable, timeout):
        observed_timeouts.append(timeout)
        return await wait_for(awaitable, timeout)

    def respond(payload):
        with worker.ledger._connect() as db:
            row = db.execute(f'SELECT status,lease_until FROM {table} WHERE turn_id=?', ('incident',)).fetchone()
        observed_leases.append((row['status'], row['lease_until'] - worker.clock()))
        return json.dumps(answer)

    monkeypatch.setattr(asyncio, 'wait_for', observed_wait_for)
    with endpoint(content=respond) as (url, requests):
        cfg = config(url, url)
        cfg['functionRoles']['extraction'] = {
            'candidates': ['interactive'], 'timeoutSeconds': 5, 'deadlineSeconds': 7,
        }
        cfg['functionRoles']['reasoning'] = {
            'candidates': ['deliberate'], 'timeoutSeconds': 11, 'deadlineSeconds': 19,
        }
        if override:
            cfg['taskRoles'] = {task: alternate}
        selected = router(cfg)
        assert await worker.process_one(selected)
        assert len(requests) == 1
        assert requests[0]['payload']['model'] == expected_model
        assert observed_timeouts[0] == expected_deadline + 5
        assert observed_leases == [('running', expected_deadline + 35)]
        with worker.ledger._connect() as db:
            row = db.execute(f'SELECT status,attempts,disposition FROM {table} WHERE turn_id=?', ('incident',)).fetchone()
        assert dict(row) == {'status': 'complete', 'attempts': 1, 'disposition': 'abstained'}
        call = selected.routing_status()['recent_calls'][-1]
        assert call['function_role'] == selected_role
        assert call['model_id'] == 'openai/' + expected_model
        assert selected.function_deadline_seconds(context={'task': task}) == expected_deadline
        # The deployment task override does not move unrelated extraction.
        for other in ('tom_affect_extraction', 'context_compression', 'source_claim_extraction'):
            assert selected.function_deadline_seconds(context={'task': other}) == 7
            assert selected.function_config(context={'task': other}).model_id == 'openai/fast-neutral'

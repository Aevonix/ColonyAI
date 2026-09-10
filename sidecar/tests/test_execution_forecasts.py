"""Prospective execution timing uses actual receipts and preserves unknowns."""
from contextlib import closing
import json
import pytest
from colony_sidecar.api.routers import host
from colony_sidecar.self_model.expectations import ExpectationStore, ExpectationEngine
from colony_sidecar.self_model import execution_forecasts as forecasts
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.executions import ExecutionRegistry
from test_execution_registry import observation

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    monkeypatch.delenv('COLONY_OWNER_PERSON_ID', raising=False)
    monkeypatch.setenv('COLONY_EXPECTATIONS', 'on')
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    monkeypatch.setattr(host, '_expectations', ExpectationEngine(store))
    now = [1_800_000_000.]
    registry = ExecutionRegistry(TurnIdempotencyLedger(tmp_path/'turns.db'), clock=lambda: now[0])
    return registry, store, now

def send(setup, name='a', **changes):
    return setup[0].observe(observation(name, **changes), principal_id='host', contact_id='contact-a')

def api(event='start', **changes):
    return {'event': event, 'request_id': 'request-a', 'requested_model': 'model-a', 'provider': 'local',
        'api_mode': 'chat_completions', 'profile_id': 'a'*64, 'runtime_kind': 'turn',
        'max_tokens': 8192, 'tool_count': 4, 'approx_input_tokens': 2000,
        **({'api_call_count': 1, 'retry_count': 0} if event == 'start' else {}), **changes}

def start(runtime, name='a'):
    send(runtime, name)
    return send(runtime, name, sequence=2, phase='model', runtime=api())

def finish(runtime, name='a', *, state='completed', response_model='model-a'):
    send(runtime, name, sequence=3, phase='between_calls', runtime=api('response', response_model=response_model))
    runtime[2][0] += 60
    return send(runtime, name, sequence=4, phase='ended', state=state)

def history(runtime, name='a'):
    return runtime[1].forecast_history(forecasts._fid(observation(name)['execution_id']))

def test_ledger_cycle_improves_next_estimate_and_erasure_removes_sample(runtime):
    assert start(runtime)['forecast']['status'] == 'issued'
    original = history(runtime)['forecasts'][0]
    assert original['detail']['conditions']['estimate']['sample_n'] == 0
    assert original['detail']['model_provenance']['requested_role'] is None
    assert finish(runtime)['forecast']['conditions_comparable']
    assert history(runtime)['forecasts'][0]['detail'] == original['detail']
    start(runtime, 'b')
    learned = history(runtime, 'b')['forecasts'][0]['detail']['conditions']['estimate']
    assert learned['sample_n'] == 1 and learned['seconds'] == 396
    ledger = runtime[0].ledger
    outcome = history(runtime)['outcomes'][0]
    facts = forecasts._facts(ledger, outcome)
    assert facts['processor']['served_model'] == 'model-a' and facts['quality_evaluated'] is False
    assert 'request_pairs' not in facts
    projection = forecasts.project(runtime[0], observation()['execution_id'], 'contact-a')
    assert projection['comparison']['duration_seconds'] == 60 and projection['suggestion_enabled'] is False
    with closing(ledger._connect()) as db:
        rows = db.execute('SELECT messages_json FROM turn_sources').fetchall()
        assert rows and all(json.loads(row[0])[0]['content'] == '' for row in rows)
        assert db.execute('SELECT count(*) FROM turn_source_search').fetchone()[0] == 0
        from colony_sidecar.turns.source_vectors import chunks
        assert all(list(chunks(db, source)) == [] for source in db.execute('SELECT * FROM turn_sources'))
    assert not ledger.search_sources('model-a', contact_id='contact-a', session_id='')
    ledger.erase_sources(contact_id='contact-a', turn_ids=[outcome['receipt_ref'].removeprefix('receipt:')])
    start(runtime, 'c')
    assert history(runtime, 'c')['forecasts'][0]['detail']['conditions']['estimate']['sample_n'] == 0
    assert not forecasts.project(runtime[0], observation()['execution_id'], 'contact-a').get('conditions_comparable', False)

@pytest.mark.parametrize('state,model', [('failed','model-a'), ('interrupted','model-a'), ('ended','model-a'), ('completed','')])
def test_failed_cancelled_unknown_changed_processors_retained_incomparable(runtime, state, model):
    start(runtime)
    assert finish(runtime, state=state, response_model=model)['forecast']['conditions_comparable'] is False
    assert history(runtime)['outcomes'][0]['status'] == 'censored'
    start(runtime, 'next')
    assert history(runtime, 'next')['forecasts'][0]['detail']['conditions']['estimate']['sample_n'] == 0

@pytest.mark.parametrize('case', ['missing_start','missing_api_start','late_sequence','retry_first'])
def test_no_retroactive_predictions(runtime, case):
    if case != 'missing_start': send(runtime)
    event = api('response', response_model='model-a') if case == 'missing_api_start' else api(retry_count=1) if case == 'retry_first' else api()
    send(runtime, sequence=3 if case == 'late_sequence' else 2, phase='model', runtime=event)
    send(runtime, sequence=4, phase='ended', state='completed')
    assert history(runtime)['forecasts'] == []

def test_missing_pairs_not_fixed_by_successful_terminal_or_late_callback(runtime):
    start(runtime)
    send(runtime, sequence=3, phase='model', runtime=api(request_id='second', api_call_count=2))
    send(runtime, sequence=4, phase='between_calls', runtime=api('response', request_id='second', response_model='model-a'))
    assert send(runtime, sequence=5, phase='ended', state='completed')['forecast']['conditions_comparable'] is False
    assert not send(runtime, sequence=6, runtime=api('response', response_model='model-a'))['accepted']
    assert forecasts._facts(runtime[0].ledger, history(runtime)['outcomes'][0])['processor']['complete_observed_pairs'] is False

def test_bounded_retry_keeps_first_failure_and_can_complete(runtime):
    start(runtime)
    send(runtime, sequence=3, phase='between_calls', runtime=api('error'))
    send(runtime, sequence=4, phase='model', runtime=api(request_id='retry', retry_count=1, api_call_count=2))
    send(runtime, sequence=5, phase='between_calls', runtime=api('response', request_id='retry', response_model='model-a'))
    runtime[2][0] += 90
    assert send(runtime, sequence=6, phase='ended', state='completed')['forecast']['conditions_comparable']
    processor = forecasts._facts(runtime[0].ledger, history(runtime)['outcomes'][0])['processor']
    assert processor['error_request_count'] == 1 and processor['observed_request_count'] == 2

def test_predecessor_schema_still_writes_and_metadata_expires(runtime):
    start(runtime)
    registry = runtime[0]
    with closing(registry.ledger._connect()) as db, db:
        old = tuple(db.execute('SELECT * FROM execution_observations').fetchone())
        db.execute('INSERT INTO execution_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('b'*64, *old[1:]))
    runtime[2][0] += 8*86400
    send(runtime, 'new')
    with closing(registry.ledger._connect()) as db:
        assert db.execute('SELECT count(*) FROM execution_runtime_observations').fetchone()[0] == 1

def test_observer_failure_does_not_block_and_disabled_has_no_forecasts(runtime, monkeypatch):
    monkeypatch.setenv('COLONY_EXPECTATIONS', 'off')
    assert start(runtime)['accepted'] and history(runtime)['forecasts'] == []
    monkeypatch.setenv('COLONY_EXPECTATIONS', 'on')
    monkeypatch.setattr(forecasts, 'observe', lambda *a: (_ for _ in ()).throw(RuntimeError('private detail')))
    result = send(runtime, 'independent')
    assert result['accepted'] and result['forecast']['status'] == 'unavailable'
    assert 'private detail' not in str(result)


def test_alias_rebinding_retains_actual_model_and_freezes_historical_cohort(runtime):
    start(runtime)
    assert finish(runtime, response_model='served-revision-a')['forecast']['conditions_comparable']
    start(runtime, 'b')
    estimate = history(runtime, 'b')['forecasts'][0]['detail']['conditions']['estimate']
    assert estimate['training_served_model'] == 'served-revision-a' and estimate['sample_n'] == 1
    assert not finish(runtime, 'b', response_model='served-revision-b')['forecast']['conditions_comparable']
    assert history(runtime, 'b')['outcomes'][0]['status'] == 'observed'
    start(runtime, 'c')
    after_swap = history(runtime, 'c')['forecasts'][0]['detail']['conditions']['estimate']
    assert after_swap['training_served_model'] == 'served-revision-b' and after_swap['sample_n'] == 1
    assert history(runtime, 'b')['forecasts'][0]['detail']['conditions']['estimate'] == estimate


def test_first_callback_failure_does_not_issue_late_after_response(runtime, monkeypatch):
    original = forecasts.observe
    monkeypatch.setattr(forecasts, 'observe', lambda *a: None)
    start(runtime)
    monkeypatch.setattr(forecasts, 'observe', original)
    finish(runtime)
    assert history(runtime)['forecasts'] == []


@pytest.mark.parametrize('field,value', [('approx_input_tokens',20000), ('profile_id','b'*64), ('max_tokens',16384), ('runtime_kind','kanban_worker')])
def test_measured_request_configuration_keeps_different_work_separate(runtime, field, value):
    start(runtime); finish(runtime)
    send(runtime,'different')
    send(runtime,'different',sequence=2,phase='model',runtime=api(**{field:value}))
    assert history(runtime,'different')['forecasts'][0]['detail']['conditions']['estimate']['sample_n'] == 0


def test_unknown_input_configuration_stays_incomparable(runtime):
    send(runtime)
    send(runtime,sequence=2,phase='model',runtime=api(approx_input_tokens=None))
    assert not finish(runtime)['forecast']['conditions_comparable']
    assert history(runtime)['outcomes'][0]['status'] == 'censored'

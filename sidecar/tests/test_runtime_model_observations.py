"""Actual response metadata is distinct from requested model and forecast."""
import pytest

from apsimo.self_model import runtime_models as models
from apsimo.turns import TurnIdempotencyLedger


@pytest.fixture
def runtime(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    native = {'source_home_id': 'fixture-home', 'native_board': 'default',
              'native_task_id': 'fixture-task', 'native_run_id': 1}
    state = {'forecast_configuration': {'requested_profile': 'default', 'runtime_budget_seconds': 480}}
    return ledger, native, state


def retain(runtime, phase, *, request='one', response_model=None, requested_model='requested', **changes):
    ledger, native, state = runtime
    return models.retain(ledger, 'owner', native, {**state, **changes}, {
        'api_request_id': request, 'phase': phase, 'requested_model': requested_model,
        'provider': 'fixture-provider', 'response_model': response_model})


def summary(runtime):
    ledger, native, _ = runtime
    return models.summarize(ledger, 'owner', native, 1)


def test_requested_alias_never_fills_missing_response_identity(runtime):
    retain(runtime, 'start')
    retain(runtime, 'response')
    result = summary(runtime)
    assert result['complete_observed_pairs'] and result['model_state'] == 'unknown'
    assert result['served_model'] is None and result['response_models'] == []
    assert result['requested_models'] == ['requested']


def test_known_response_identity_replay_and_later_config_preserve_first_observation(runtime):
    start = retain(runtime, 'start')
    retain(runtime, 'response', response_model='actual-response')
    assert summary(runtime)['served_model'] == 'actual-response'
    replay = retain(runtime, 'start', forecast_configuration={'requested_profile': 'edited-later'})
    assert replay == start
    assert summary(runtime)['configuration'] == runtime[2]['forecast_configuration']
    with pytest.raises(ValueError, match='observation_conflict'):
        retain(runtime, 'response', response_model='invented-replacement')
    assert summary(runtime)['served_model'] == 'actual-response'


@pytest.mark.parametrize('phases', [('start',), ('response',), ('error',), ('start', 'error', 'response')])
def test_missing_or_ambiguous_callback_pair_is_unknown(runtime, phases):
    for phase in phases:
        retain(runtime, phase, response_model='actual-response')
    result = summary(runtime)
    assert not result['complete_observed_pairs'] and result['served_model'] is None


def test_fallback_models_and_changed_conditions_are_retained_as_mixed(runtime):
    retain(runtime, 'start'); retain(runtime, 'error')
    retain(runtime, 'start', request='fallback', requested_model='fallback-alias')
    retain(runtime, 'response', request='fallback', requested_model='fallback-alias', response_model='model-a')
    retain(runtime, 'start', request='later', forecast_configuration={'runtime_budget_seconds': 900})
    retain(runtime, 'response', request='later', response_model='model-b')
    result = summary(runtime)
    assert result['complete_observed_pairs'] and result['model_state'] == 'mixed'
    assert result['response_models'] == ['model-a', 'model-b'] and result['served_model'] is None
    assert result['configuration'] is None


def test_erased_pair_cannot_silently_turn_partial_run_into_complete(runtime):
    erased = [*retain(runtime, 'start')['evidence_refs'], *retain(runtime, 'response', response_model='model-a')['evidence_refs']]
    retain(runtime, 'start', request='remaining')
    retain(runtime, 'response', request='remaining', response_model='model-a')
    assert summary(runtime)['served_model'] == 'model-a'
    runtime[0].erase_sources(turn_ids=[r.removeprefix('receipt:') for r in erased], contact_id='owner')
    assert summary(runtime)['model_state'] == 'unknown'
    assert not summary(runtime)['complete_observed_pairs']
    assert models.summarize(runtime[0], 'stranger', runtime[1], 1)['model_state'] == 'unknown'


def test_outcome_retains_model_dependency_so_forgetting_retracts_derived_metadata(runtime):
    from apsimo.self_model.runtime_forecasts import _retain, _current, _identity, VERSION
    import time
    retain(runtime, 'start'); response = retain(runtime, 'response', response_model='model-a')
    observed = summary(runtime)
    ledger, native, _ = runtime
    versions, _ = _retain(ledger, source_id='native-forecast-outcome:fixture', owner='owner',
        native=native, occurred_at=time.time(), dependencies=observed['source_versions'],
        facts={'version': VERSION, 'kind': 'duration_outcome', 'identity': _identity(native),
               'processor_observation': observed})
    assert _current(ledger, list(versions), versions, 'owner')
    ledger.erase_sources(turn_ids=[response['evidence_refs'][0].removeprefix('receipt:')], contact_id='owner')
    assert not _current(ledger, list(versions), versions, 'owner')

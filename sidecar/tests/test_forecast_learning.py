"""Actual forecast -> independent outcome -> prospective changed decision."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from colony_sidecar.self_model.expectations import ExpectationStore, ExpectationEngine
from colony_sidecar.world_model.expectation_resolvers import register_world_resolvers, CAUSAL_PREFIX


def issue(store, key='one', **changes):
    args = dict(forecast_id=key, subject='task:'+key, domain='task_duration',
                expectation='task completes within estimated duration', confidence=.7,
                horizon=1100., origin_at=1000., issued_at=1001.,
                evidence_refs=['task:'+key], source_versions={'task:'+key:'v1'},
                source_kind='task_receipt', cohort='local-metadata', method='declared-prior',
                model_provenance={'requested_role':'reasoning','served_model':'actual-fast',
                                  'model_revision':None,'fallback':True},
                subject_person_id='owner',viewer_scope='owner',shareability='owner_private')
    args.update(changes)
    return store.issue_forecast(**args)


def observed(store, key='one', **changes):
    args = dict(forecast_id=key, receipt_ref='receipt:'+key,
                evidence_refs=['receipt:'+key], source_versions={'receipt:'+key:'v1'},
                source_kind='task_receipt', observed_at=1200., recorded_at=1201.,
                subject_person_id='owner',viewer_scope='owner',shareability='owner_private', value=True)
    args.update(changes)
    return store.record_forecast_outcome(**args)


def test_revision_preserves_original_and_changes_next_actual_horizon(tmp_path):
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    original = issue(store)
    revised = issue(store, previous_revision=1, horizon=1250., issued_at=1050.)
    assert revised.detail['supersedes_revision'] == 1
    observed(store)
    history = store.forecast_history('one')
    assert [(p['horizon'],p['outcome']) for p in history['forecasts']] == [(1100.,'miss'),(1250.,'hit')]
    estimate = store.estimate_duration(domain='task_duration',cohort='local-metadata',subject_person_id='owner',viewer_scope='owner',prior_seconds=100,now=1300)
    assert estimate['seconds'] == 120 and estimate['sample_n'] == 1 and estimate['uncertain']
    later = issue(store,'two',origin_at=1300,issued_at=1301,horizon=1300+estimate['seconds'],
                  confidence=estimate['confidence'],method=estimate['method'])
    assert later.horizon == 1420 and later.horizon != 1400
    report = ExpectationEngine(store).calibration_report()
    assert report['resolved_n'] == 1 and report['domains']['task_duration']['hit_rate'] == 0
    assert report['historical_self_consistency_or_revision_excluded_n'] == 1
    coverage = store.forecast_coverage(subject_person_id='owner',viewer_scope='owner')
    assert (coverage['issued'],coverage['resolved'],coverage['pending']) == (2,1,1)
    assert coverage['groups'][0]['served_model'] == 'actual-fast'
    assert store.get(original.prediction_id).confidence == .7


def test_replays_and_parallel_revisions_do_not_duplicate(tmp_path):
    path = str(tmp_path/'expectations.db')
    first, second = ExpectationStore(path), ExpectationStore(path)
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(lambda s:issue(s),[first,second]))
    assert rows[0].prediction_id == rows[1].prediction_id
    with pytest.raises(ValueError,match='revision conflict'):
        issue(first,horizon=1150)
    observed(first)
    assert observed(second)['disposition'] == 'duplicate'
    assert len(first.forecast_history('one')['outcomes']) == 1
    with pytest.raises(ValueError,match='after observing'):
        issue(first,previous_revision=1,horizon=1500,issued_at=1300)


def test_censor_correction_and_missing_coverage_are_not_false_misses(tmp_path):
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    issue(store)
    observed(store,status='censored',value=None,reason='paused')
    assert store.forecast_coverage(subject_person_id='owner',viewer_scope='owner')['censored'] == 1
    assert not ExpectationEngine(store).calibration_report()['resolved_n']
    assert store.estimate_duration(domain='task_duration',cohort='local-metadata',subject_person_id='owner',viewer_scope='owner',prior_seconds=100)['sample_n'] == 0
    observed(store,previous_revision=1,receipt_ref='receipt:corrected',evidence_refs=['receipt:corrected'],source_versions={'receipt:corrected':'v2'})
    assert len(store.forecast_history('one')['outcomes']) == 2
    assert store.forecast_coverage(subject_person_id='owner',viewer_scope='owner')['resolved'] == 1
    issue(store,'two')
    observed(store,'two',value=False)
    assert store.forecast_coverage(subject_person_id='owner',viewer_scope='owner')['unresolved'] == 1
    observed(store,'two',previous_revision=1,receipt_ref='receipt:coverage',evidence_refs=['receipt:coverage'],source_versions={'receipt:coverage':'v2'},value=False,coverage_until=1200)
    assert store.forecast_coverage(subject_person_id='owner',viewer_scope='owner')['resolved'] == 2


def test_delayed_observation_cannot_reward_retrospective_revision(tmp_path):
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    issue(store)
    issue(store,previous_revision=1,horizon=1400,issued_at=1300)
    observed(store,observed_at=1200,recorded_at=1401)
    assert [p['outcome'] for p in store.forecast_history('one')['forecasts']] == ['miss','unresolved']


def test_scope_time_and_evidence_validation(tmp_path):
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    with pytest.raises(ValueError,match='version'):
        issue(store,source_versions={})
    with pytest.raises(ValueError,match='horizon'):
        issue(store,horizon=1000)
    issue(store)
    with pytest.raises(ValueError,match='scope'):
        observed(store,subject_person_id='other')
    with pytest.raises(ValueError,match='independent'):
        observed(store,source_kind='colony_event')
    # Without an external outcome the old periodic checker must not infer one.
    assert ExpectationEngine(store).check(now=100000) == {'hit':0,'miss':0,'unresolved':0}
    assert store.forecast_history('one')['forecasts'][0]['outcome'] == 'pending'


def test_causal_self_survival_remains_historical_not_predictive_truth(tmp_path):
    store = ExpectationStore(str(tmp_path/'expectations.db'))
    prediction = store.create(subject=CAUSAL_PREFIX+'edge',domain='world_model',expectation='edge remains',confidence=.9,horizon=1100,source='legacy',dedup_key='edge')
    store.resolve(prediction.prediction_id,'hit')
    engine = ExpectationEngine(store)
    register_world_resolvers(engine)
    assert engine._resolve(prediction) is None
    assert engine.calibration() == {}
    assert engine.calibration_report()['resolved_n'] == 0
    assert store.get(prediction.prediction_id).outcome == 'hit'

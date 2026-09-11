"""Actual forecast -> independent outcome -> prospective changed decision."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from apsimo.self_model.expectations import ExpectationStore, ExpectationEngine
from apsimo.self_model import runtime_forecasts
from apsimo.world_model.expectation_resolvers import register_world_resolvers, CAUSAL_PREFIX


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
    assert [p.prediction_id for p in store.pending()] == [revised.prediction_id]
    assert [p.prediction_id for p in store.projected(subject_person_id='owner',viewer_scope='owner')] == [revised.prediction_id]
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


@pytest.fixture
def inspection(tmp_path, monkeypatch):
    store = ExpectationStore(str(tmp_path/'inspection.db'))
    configuration = {'runtime_budget_seconds':200,'requested_profile':'default'}
    provenance = {'requested_role':'reasoning','served_model':None,
                  'capabilities':configuration}
    estimate = {'prior_seconds':200,'seconds':100,'sample_n':12,'uncertain':False}
    issue(store,conditions={'estimate':estimate},model_provenance=provenance)
    monkeypatch.setattr(runtime_forecasts,'_parts',lambda *a:(store,None,{}, {},'one'))
    monkeypatch.setattr(runtime_forecasts,'_current',lambda *a:True)
    return store,{'status':'running','forecast_configuration':configuration}


def test_inspection_is_shadow_and_uses_original_horizon_after_revision(inspection):
    store,state = inspection
    original = store.forecast_history('one')['forecasts'][0]
    issue(store,previous_revision=1,horizon=1250,issued_at=1050,
          conditions=original['detail']['conditions'],model_provenance=original['detail']['model_provenance'])
    before = store.forecast_history('one')
    first = runtime_forecasts.project({}, {}, state, 'owner', now=1110)
    second = runtime_forecasts.project({}, {}, state, 'owner', now=1111)
    assert first['decision']=='inspect_recorded_state' and first['prior_decision']=='continue_waiting'
    assert first['original_revision']==1 and first['original_horizon']==1100
    assert first['changed_from_prior'] and not first['suggestion_enabled']
    assert first['decision_id']==second['decision_id'] and store.forecast_history('one')==before
    assert first['enable_criteria']['minimum_comparable_terminal_receipts']==10
    assert 'comparison' not in first


def test_inspection_terminal_outcome_has_independent_counterfactual_comparison(inspection):
    store,state = inspection
    observed(store,observed_at=1150,recorded_at=1151)
    value = runtime_forecasts.project({}, {}, {**state,'status':'done'}, 'owner', now=1300)
    assert value['decision']=='terminal' and not value['quality_evaluated']
    score = value['comparison']
    assert score['forecast_absolute_error_seconds']==score['prior_absolute_error_seconds']==50
    assert score['forecast_premature_inspection'] and not score['prior_premature_inspection']
    assert score['forecast_inspection_lateness_seconds']==0 and score['prior_inspection_lateness_seconds']==50
    assert score['counterfactual_horizon_comparison'] and score['projection_added_status_calls']==0


def test_completed_forecast_uses_retained_outcome_conditions_not_later_config(inspection, monkeypatch):
    store, state = inspection
    observed(store, observed_at=1150, recorded_at=1151)
    original = store.forecast_history('one')['forecasts'][0]
    monkeypatch.setattr(runtime_forecasts, '_outcome_facts', lambda *a: {'processor_observation': {
        'configuration': state['forecast_configuration'], 'complete_observed_pairs': True,
        'served_model': 'provider-reported-actual'}}, raising=False)
    changed = {**state, 'status': 'done', 'forecast_configuration': {'requested_profile': 'edited-later'}}
    result = runtime_forecasts.project({}, {}, changed, 'owner', now=1300)
    assert result['conditions_comparable'] and result['configuration_matches']
    assert result['served_model'] == 'provider-reported-actual'
    assert result['original_served_model'] is None
    assert result['comparison']['conditions_comparable'] and not result['suggestion_enabled']
    assert store.forecast_history('one')['forecasts'][0] == original


def test_inspection_censor_changed_configuration_unknown_and_erasure(inspection,monkeypatch):
    store,state = inspection
    assert runtime_forecasts.project({}, {}, {'status':'running'},'owner',now=1300)['decision']=='conditions_unknown_or_changed'
    assert runtime_forecasts.project({}, {}, {**state,'status':'blocked'},'owner',now=1300)['decision']=='not_running'
    observed(store,status='censored',value=None,reason='paused')
    value=runtime_forecasts.project({}, {}, state,'owner',now=1300)
    assert value['decision']=='censored' and 'comparison' not in value
    monkeypatch.setattr(runtime_forecasts,'_current',lambda *a:False)
    assert runtime_forecasts.project({}, {}, state,'owner',now=1300)=={'status':'source_unavailable'}


def test_inspection_rejects_wrong_owner_and_time(inspection):
    _,state=inspection
    assert runtime_forecasts.project({}, {}, state,'other',now=1300)=={'status':'source_unavailable'}
    for now in (999,float('nan'),float('inf')):
        assert runtime_forecasts.project({}, {}, state,'owner',now=now)=={'status':'unqualified_observation_time'}


@pytest.mark.parametrize('duration', [10., 2000.])
def test_shadow_v2_removes_both_ratio_clamps_but_retains_window_and_prior(tmp_path, duration):
    store = ExpectationStore(str(tmp_path/'duration-v2.db'))
    # Exercise the existing outcome store with 51 independent fixture receipts.
    # These are arithmetic/retention cases, not a natural-work qualification.
    for index in range(51):
        origin = 1000 + index * 3000
        issue(store, str(index), origin_at=origin, issued_at=origin+1, horizon=origin+480)
        observed(store, str(index), observed_at=origin+duration, recorded_at=origin+duration+1)
    args = dict(domain='task_duration', cohort='local-metadata', subject_person_id='owner',
                viewer_scope='owner', prior_seconds=480, now=200000)
    v1 = store.estimate_duration(**args)
    v2 = store.estimate_duration(**args, method='receipt-duration-median-prior4-v2')
    expected = round((4*480 + 50*duration)/54, 3)
    assert v1['sample_n'] == v2['sample_n'] == 50
    assert v2['seconds'] == expected
    assert v1['seconds'] == v2['clamped_comparison_seconds'] == min(960, max(240, expected))
    assert v2['evidence_refs'] == v1['evidence_refs']
    assert 'receipt:0' not in v2['evidence_refs']
    args['now'] = 1000 + duration + .5  # outcome not recorded yet
    assert store.estimate_duration(**args, method='receipt-duration-median-prior4-v2')['sample_n'] == 0

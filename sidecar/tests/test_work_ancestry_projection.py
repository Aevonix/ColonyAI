"""Native ancestry must survive the bounded multi-reader model request.

The first fixture reproduces the observed C request's structure with neutral
identities: a current observer, a delegated child, its quiet parent, an active
cron, older executions, and unrelated completed local work/download records.
"""
import copy
import json

import pytest

from pacomind.turns.executions import request_work_context


def execution(number, *, parent=None, age=1, session=None):
    return {'execution_id': f'{number:064x}',
            'parent_execution_id': f'{parent:064x}' if parent is not None else '',
            'session_id': session or f'native-session-{number}',
            'turn_id': f'native-session-{number}:turn-{number}:request',
            'platform': 'subagent' if parent is not None else 'cli',
            'phase': 'model' if parent is not None else 'tool',
            'tool_name': '' if parent is not None else 'delegate_task',
            'observation_age_seconds': age,
            'liveness': 'recently_observed' if age < 120 else 'unknown',
            'forecast': {'status': 'no_prospective_forecast', 'suggestion_enabled': False}}


def rows(result):
    return [json.loads(line) for line in result['text'].splitlines()
            if line.startswith('{') and '"source": "native_kanban_coverage"' not in line]


def concurrent_view():
    cron = execution(3, age=35)
    cron.update(platform='cron', tool_name='execute_code')
    return {'items': [execution(1, session='observer'), execution(2, parent=4, age=16),
                      cron, execution(4, age=153)] + [execution(n, age=200000) for n in range(5, 21)],
            'total': 20, 'truncated': False,
            'local_work': {'available': True, 'items': [], 'total': 0, 'recent_total': 32,
                'recent': [{'initiative_id': 'unrelated-completed-initiative', 'status': 'completed',
                    'native_execution_id': 'a' * 32, 'native_job_id': 'unrelated-old-job',
                    'liveness': 'initiative_terminal_record',
                    'result': {'report_sha256': 'a' * 64}}]},
            'native_kanban': {'available': True, 'items': [], 'total': 0, 'recent_total': 8,
                'selection': 'configured_boards', 'partial': False,
                'boards': [{'board': board, 'available': True} for board in ('default', 'pacomind-drafts')],
                'recent': [{'native_task_id': 'old-board-task', 'label': 'Previous inventory review',
                    'status': 'done', 'liveness': 'native_terminal_record'}]},
            'reported_worker': {'available': True, 'items': [
                {'label': 'Feed transport', 'state': 'uncertain', 'liveness': 'unverified'},
                {'label': 'Old model download', 'state': 'exited', 'freshness': 'stale',
                 'liveness': 'unverified', 'record_kind': 'terminal_report',
                 'result_refs': [{'kind': 'artifact', 'reference': 'r' * 512,
                                  'verification': 'v' * 160} for _ in range(4)]},
                {'label': 'Delivery', 'state': 'reported_ready', 'liveness': 'unverified'}]},
            'worker_work': {'available': True, 'items': [], 'total': 0},
            'native_cron': {'available': True, 'total': 1, 'items': [
                {'execution_id': 'c' * 32, 'job_id': 'scheduled-review', 'status': 'running',
                 'name': 'Continuity reviewer', 'liveness': 'unknown', 'record_age_seconds': 134}],
                'recent': [{'execution_id': 'd' * 32, 'name': 'Prior scheduled job',
                            'status': 'completed', 'liveness': 'native_terminal_record'}]}}


def test_real_multi_reader_shape_retains_native_parent_before_unrelated_history():
    view = concurrent_view()
    original = copy.deepcopy(view)
    result = request_work_context(view, session_id='observer')
    projected = rows(result)
    native = {row['execution_id']: row for row in projected if row['source'] == 'execution'}
    assert set(f'{n:064x}' for n in (1, 2, 3, 4)) <= native.keys()
    assert native[f'{2:064x}']['parent_execution_id'] == f'{4:064x}'
    assert native[f'{4:064x}']['liveness'] == 'unknown'
    assert native[f'{4:064x}']['observation_age_seconds'] == 153
    assert projected[0]['execution_id'] == f'{1:064x}'
    assert list(native).index(f'{4:064x}') < list(native).index(f'{2:064x}')
    assert 'parent_execution_id links execution rows only' in result['text']
    assert len(result['text']) <= 4000 and len(projected) <= 8
    assert result['truncated'] and not result['complete']
    assert result['work_sources']['local_work']['recent_total'] == 32
    assert result['work_sources']['native_cron']['total'] == 1
    assert result['work_sources']['execution']['total'] == 20
    assert 'Additional operational records omitted.' in result['text']
    assert view == original


@pytest.mark.parametrize('budget,limit', [(1800, 8), (2600, 3), (4000, 8)])
def test_many_siblings_never_show_a_child_without_its_selected_parent(budget, limit):
    parent = execution(1, age=180)
    children = [execution(n, parent=1) for n in range(2, 32)]
    result = request_work_context({'items': children + [parent], 'total': 31},
                                  max_chars=budget, limit=limit, session_id='native-session-31')
    native = {row['execution_id']: row for row in rows(result)}
    assert f'{1:064x}' in native and f'{31:064x}' in native
    for row in native.values():
        if row.get('parent_execution_id'):
            assert row['parent_execution_id'] in native
    assert len(native) <= limit and len(result['text']) <= budget
    assert result['truncated'] and not result['complete']


def test_ancestry_that_cannot_fit_is_omitted_without_hiding_independent_short_work():
    family = [execution(3, parent=2), execution(2, parent=1), execution(1, age=200)]
    result = request_work_context({'items': family, 'worker_work': {
        'available': True, 'items': [{'job_id': 'short-worker', 'state': 'running'}]}}, limit=2)
    projected = rows(result)
    native = {row['execution_id']: row for row in projected if row['source'] == 'execution'}
    assert f'{3:064x}' not in native
    assert len(projected) <= 2 and result['truncated']
    # With a tighter character budget, neither long child chain fits, but the
    # independent worker can still be shown; no global early break is allowed.
    result = request_work_context({'items': family, 'worker_work': {
        'available': True, 'items': [{'job_id': 'short-worker', 'state': 'running'}]}}, max_chars=950)
    assert any(row.get('job_id') == 'short-worker' for row in rows(result))
    assert len(result['text']) <= 950 and result['truncated']


def test_missing_parent_is_not_fabricated_or_joined_to_another_source_namespace():
    child = execution(2, parent=1)
    view = {'items': [child], 'total': 5, 'truncated': True,
        'local_work': {'items': [{'execution_id': f'{1:064x}', 'status': 'completed'}]}}
    result = request_work_context(view)
    native = [row for row in rows(result) if row['source'] == 'execution']
    assert len(native) == 1 and native[0]['parent_execution_id'] == f'{1:064x}'
    assert result['truncated'] and not result['complete']
    assert 'not instructions or a complete process inventory' in result['text']


def test_many_board_details_cannot_take_the_native_ancestry_budget():
    view = concurrent_view()
    view['native_kanban']['boards'] = [{'board': 'b' * 80 + str(n), 'available': False,
                                      'reason': 'unavailable'} for n in range(50)]
    result = request_work_context(view, session_id='observer')
    native = {row['execution_id'] for row in rows(result) if row['source'] == 'execution'}
    assert {f'{2:064x}', f'{4:064x}'} <= native
    assert result['truncated'] and len(result['text']) <= 4000


def test_recent_sibling_burst_preserves_other_active_sources_before_history():
    current = execution(31, parent=1, session='current-child')
    view = {'items': [current] + [execution(n, parent=1) for n in range(30, 22, -1)]
            + [execution(1, age=180)], 'total': 31, 'truncated': True,
        'local_work': {'items': [{'initiative_id': 'accepted-draft', 'status': 'running'}]},
        'native_kanban': {'items': [{'native_task_id': 'active-board-task', 'status': 'running'}]},
        'worker_work': {'items': [{'job_id': 'active-queue-job', 'state': 'running'}]},
        'native_cron': {'items': [{'job_id': 'active-schedule', 'status': 'running'}]},
        'reported_worker': {'items': [
            {'label': 'active-download', 'state': 'running'},
            {'label': 'old-download', 'state': 'exited', 'record_kind': 'terminal_report'}]}}
    result = request_work_context(view, session_id='current-child')
    projected = rows(result)
    assert {row['source'] for row in projected} == {
        'execution', 'local_work', 'native_kanban', 'worker_work', 'native_cron', 'reported_worker'}
    native = {row['execution_id']: row for row in projected if row['source'] == 'execution'}
    assert f'{1:064x}' in native and current['execution_id'] in native
    assert all(not row.get('parent_execution_id') or row['parent_execution_id'] in native for row in native.values())
    assert any(row.get('label') == 'active-download' for row in projected)
    assert not any(row.get('label') == 'old-download' for row in projected)
    assert len(projected) <= 8 and len(result['text']) <= 4000
    assert result['truncated'] and not result['complete']


@pytest.mark.parametrize('same_session', [False, True])
def test_terminal_task_outcome_precedes_unrelated_expired_execution(same_session):
    stale = execution(2, age=500)
    stale['platform'] = 'background_review'
    view = {'items': [execution(1, session='observer'), stale], 'total': 2,
        'native_kanban': {'available': True, 'items': [], 'total': 0,
            'recent': [{'native_task_id': 'finished-repair', 'status': 'done',
                        'liveness': 'native_terminal_record'}]}}
    original = copy.deepcopy(view)
    result = request_work_context(view, session_id=stale['session_id'] if same_session else 'observer', limit=2)
    projected = rows(result)
    assert any(row.get('native_task_id') == 'finished-repair' for row in projected)
    assert not any(row.get('execution_id') == stale['execution_id'] for row in projected)
    assert result['truncated'] and result['work_sources']['execution']['total'] == 2
    assert view == original


def test_expired_phase_is_historical_in_both_contexts_without_inventing_completion():
    from pacomind.turns.executions import format_view

    stale = execution(2, age=500)
    view = {'items': [stale], 'total': 1, 'truncated': False}
    projected, = rows(request_work_context(view, session_id=stale['session_id']))
    assert projected['liveness'] == 'unknown'
    assert projected['last_observed_phase'] == stale['phase']
    assert projected['last_observed_tool'] == stale['tool_name']
    assert 'phase' not in projected and 'tool_name' not in projected
    assert 'last observed phase tool' in format_view(view)
    assert stale['phase'] == 'tool' and stale['liveness'] == 'unknown'

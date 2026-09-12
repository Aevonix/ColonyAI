"""A retained old attempt cannot become a different task transition's outcome."""
import sqlite3

import pytest

from pacomind.turns.hermes_kanban import _completed_result


@pytest.fixture
def native_rows():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE task_runs(id INTEGER PRIMARY KEY,task_id TEXT,status TEXT,'
               'outcome TEXT,ended_at INTEGER,summary TEXT)')
    db.execute("INSERT INTO task_runs VALUES(1,'task','done','completed',10,'Original report')")
    yield db
    db.close()


@pytest.mark.parametrize('status,current', [
    ('running', 2), ('ready', None), ('cancelled', None), ('archived', None),
])
def test_previous_summary_is_not_the_current_transition(native_rows, status, current):
    task = {'id': 'task', 'status': status, 'current_run_id': current, 'completed_at': 10}
    assert _completed_result(native_rows, task, 'default') is None


@pytest.mark.parametrize('status,outcome,ended', [
    ('running', None, None), ('reclaimed', 'archived', 20),
    ('failed', 'failed', 20), ('done', 'completed', None),
])
def test_latest_attempt_must_itself_be_completed(native_rows, status, outcome, ended):
    native_rows.execute('INSERT INTO task_runs VALUES(2,?,?,?,?,?)',
                        ('task', status, outcome, ended, 'Later unfinished report'))
    task = {'id': 'task', 'status': 'done', 'current_run_id': None, 'completed_at': 10}
    assert _completed_result(native_rows, task, 'default') is None


def test_completion_ending_across_a_clock_tick_is_not_hidden(native_rows):
    # Hermes records task completion and run ending with separate clock reads.
    task = {'id': 'task', 'status': 'done', 'current_run_id': None, 'completed_at': 9}
    result = _completed_result(native_rows, task, 'default')
    assert result['summary'] == 'Original report' and result['ended_at'] == 10
    assert result['reader']['run_id'] == 1
    native_rows.execute("UPDATE task_runs SET summary='Corrected report' WHERE id=1")
    assert _completed_result(native_rows, task, 'default')['summary'] == 'Corrected report'

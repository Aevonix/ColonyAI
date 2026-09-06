"""Read accepted draft associations from one selected native board.

Hermes owns its database, task transitions and recovery. This reader never
initializes a board or imports the native runtime into the sidecar environment.
"""
from contextlib import closing
import hashlib
import os
import re
import sqlite3

from .hermes_work import selected_home


def selected_board():
    home = selected_home()
    board = os.environ.get('COLONY_LOCAL_WORK_BOARD', 'colony-drafts').strip()
    profile = os.environ.get('COLONY_LOCAL_WORK_PROFILE', 'colony-drafts').strip()
    if home is None or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', board) or not profile:
        raise ValueError('native_board_binding_unavailable')
    path = home/'kanban.db' if board == 'default' else home/'kanban/boards'/board/'kanban.db'
    return home, board, profile, path


def task_snapshot(identifier, contact_id, native):
    """Verify native provenance and, when supplied, the currently held run."""
    home, board, profile, path = selected_board()
    if native['native_board'] != board:
        raise ValueError('selected_native_board_required')
    if not path.is_file():
        raise OSError('native_board_unavailable')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (native['native_task_id'],)).fetchone()
        if (task is None or task['created_by'] != 'colony-local-work'
                or task['idempotency_key'] != 'colony-local-work:'+identifier
                or task['tenant'] != contact_id or task['assignee'] != profile):
            raise ValueError('accepted_native_task_required')
        result = {'source_home_id': hashlib.sha256(str(home).encode()).hexdigest(),
                  'native_board': board, 'native_task_id': task['id']}
        count = db.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (task['id'],)).fetchone()[0]
        run = None
        if native.get('native_run_id') is not None:
            run = db.execute('SELECT * FROM task_runs WHERE id=? AND task_id=?',
                             (native['native_run_id'], task['id'])).fetchone()
            if (task['status'] != 'running' or task['current_run_id'] != native['native_run_id']
                    or run is None or run['status'] != 'running'
                    or not native.get('native_claim_lock')
                    or task['claim_lock'] != native['native_claim_lock']
                    or run['claim_lock'] != native['native_claim_lock']):
                raise ValueError('current_native_run_required')
            result.update(native_run_id=run['id'], native_claim_lock=run['claim_lock'])
        return result, {'status': task['status'], 'native_run_id': task['current_run_id'],
                        'attempt_count': count, 'archived': task['status'] == 'archived'}


def project_accepted(identifier, contact_id, context):
    """Observe only an accepted task, never expose a machine-wide board."""
    if context.get('execution_backend') != 'kanban' or not context.get('native_task_id'):
        return None
    try:
        native, state = task_snapshot(identifier, contact_id, {
            'native_board': context.get('native_board'), 'native_task_id': context['native_task_id']})
        if native['source_home_id'] != context.get('source_home_id'):
            raise ValueError('selected_native_home_changed')
        return {'available': True, **native, **state,
                'liveness': 'native_running_record' if state['status'] == 'running' else 'native_task_record'}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': 'accepted_native_task_unavailable'}

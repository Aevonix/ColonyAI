"""Read selected native boards and accepted draft associations.

Hermes owns its database, task transitions and recovery. This reader never
initializes a board or imports the native runtime into the sidecar environment.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .hermes_work import selected_home


_BOARD = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_TERMINAL = ('done', 'cancelled', 'archived')


def _board_path(home, board):
    return home/'kanban.db' if board == 'default' else home/'kanban/boards'/board/'kanban.db'


def observed_boards():
    """Select explicit boards or the native current namespace, never enumerate."""
    home = selected_home()
    if home is None:
        return None, [], 'profile_not_bound'
    if home.parent.name == 'profiles':
        home = home.parent.parent
    override = os.environ.get('HERMES_KANBAN_HOME', '').strip()
    if override and Path(override).expanduser().resolve() != home:
        raise ValueError('conflicting_native_home')
    configured = os.environ.get('COLONY_HERMES_WORK_BOARDS')
    if configured is not None:
        boards = json.loads(configured)
        if (not isinstance(boards, list) or not 1 <= len(boards) <= 8
                or any(not isinstance(b, str) or not _BOARD.fullmatch(b) for b in boards)):
            raise ValueError('invalid_selected_boards')
        boards = list(dict.fromkeys(boards))
        selection = 'configured_boards'
    else:
        # Match native current-board precedence. Stale native selections fall
        # through to default; explicit observer selections remain unavailable.
        candidates = [os.environ.get('HERMES_KANBAN_BOARD', '').strip().lower()]
        pointer = home/'kanban/current'
        if pointer.is_file() and pointer.stat().st_size <= 256:
            candidates.append(pointer.read_text().strip().lower())
        boards = [next((b for b in candidates if _BOARD.fullmatch(b) and
                        (b == 'default' or _board_path(home, b).exists()
                         or (_board_path(home, b).parent/'board.json').exists())), 'default')]
        selection = 'current_board_only'
    pinned = os.environ.get('HERMES_KANBAN_DB', '').strip()
    if pinned and (len(boards) != 1 or Path(pinned).expanduser().resolve()
                   != _board_path(home, boards[0]).resolve()):
        raise ValueError('conflicting_native_board')
    return home, boards, selection


def kanban_view(*, limit=8, now=None):
    """Owner-only operational projection; no board initialization or dispatch."""
    view = {'source': 'hermes_native_kanban_ledgers', 'available': False,
            'items': [], 'recent': [], 'complete': False, 'boards': [],
            'coverage': 'selected native boards only; task/run records and goal budgets, '
                        'not process liveness or verified external effects'}
    try:
        home, boards, selection = observed_boards()
    except (OSError, ValueError, KeyError, TypeError):
        return {**view, 'reason': 'invalid_native_board_binding'}
    if home is None:
        return {**view, 'reason': selection}
    view.update(source_home_id=hashlib.sha256(str(home).encode()).hexdigest(), selection=selection)
    limit = max(1, min(int(limit), 100))
    now = time.time() if now is None else now
    deadline = time.monotonic() + .2
    active, recent, total, recent_total = [], [], 0, 0
    for board in boards:
        coverage = {'board': board, 'available': False}
        view['boards'].append(coverage)
        path = _board_path(home, board)
        if not path.is_file():
            coverage['reason'] = 'native_board_absent'
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True,
                                         timeout=min(.05, remaining))) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                db.execute('BEGIN')
                terminal = "status IN ('done','cancelled','archived')"
                # Archiving an unfinished task records an event, not a
                # completion. Preserve that distinction in the projection.
                terminal_at = ("CASE WHEN status='archived' THEN "
                               "(SELECT MAX(e.created_at) FROM task_events e "
                               "WHERE e.task_id=tasks.id AND e.kind='archived') "
                               "ELSE completed_at END")
                active_count = db.execute(f'SELECT count(*) FROM tasks WHERE NOT ({terminal})').fetchone()[0]
                recent_count = db.execute(f'SELECT count(*) FROM tasks WHERE {terminal} AND ({terminal_at})>=?', (now-7*86400,)).fetchone()[0]
                columns = ('id,title,status,assignee,goal_mode,goal_max_turns,current_run_id,'
                           'created_at,started_at,completed_at,last_heartbeat_at,'
                           f'{terminal_at} AS terminal_record_at')
                rows = db.execute(f'SELECT {columns} FROM tasks WHERE NOT ({terminal}) '
                                  'ORDER BY created_at DESC,id LIMIT ?', (limit,)).fetchall()
                done = db.execute(f'SELECT {columns} FROM tasks WHERE {terminal} AND ({terminal_at})>=? '
                                  'ORDER BY terminal_record_at DESC,id LIMIT ?', (now-7*86400,limit)).fetchall()

                def project(row):
                    run = db.execute('SELECT status FROM task_runs WHERE id=? AND task_id=?',
                                     (row['current_run_id'], row['id'])).fetchone()
                    heartbeat = row['last_heartbeat_at']
                    return {'native_board': board, 'native_task_id': row['id'],
                            'label': str(row['title'] or '')[:128], 'status': row['status'],
                            'assignee': str(row['assignee'] or '')[:128],
                            'goal_mode': bool(row['goal_mode']), 'goal_max_turns': row['goal_max_turns'],
                            'native_run_id': row['current_run_id'], 'native_run_status': run['status'] if run else None,
                            'created_at': row['created_at'], 'started_at': row['started_at'],
                            'completed_at': row['completed_at'],
                            'terminal_record_at': row['terminal_record_at'],
                            'heartbeat_age_seconds': round(max(0., now-heartbeat), 1) if heartbeat else None,
                            'liveness': 'native_terminal_record' if row['status'] in _TERMINAL else 'unknown'}
                projected, projected_done = [project(r) for r in rows], [project(r) for r in done]
            active.extend(projected); recent.extend(projected_done)
            total += active_count; recent_total += recent_count
            coverage.update(available=True, total=active_count, recent_total=recent_count)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            coverage['reason'] = 'native_board_unavailable'
    active.sort(key=lambda r: (-r['created_at'], r['native_board'], r['native_task_id']))
    recent.sort(key=lambda r: (-r['terminal_record_at'], r['native_board'], r['native_task_id']))
    available = any(b['available'] for b in view['boards'])
    return {**view, 'available': available, 'partial': not all(b['available'] for b in view['boards']),
            **({} if available else {'reason': 'selected_native_boards_unavailable'}),
            'items': active[:limit], 'recent': recent[:limit], 'total': total,
            'recent_total': recent_total, 'truncated': total>limit, 'recent_truncated': recent_total>limit,
            'limit': limit, 'recent_window_seconds': 7*86400}


def selected_board():
    home = selected_home()
    if home is not None and home.parent.name == 'profiles':
        home = home.parent.parent
    board = os.environ.get('COLONY_LOCAL_WORK_BOARD', 'colony-drafts').strip()
    profile = os.environ.get('COLONY_LOCAL_WORK_PROFILE', 'colony-drafts').strip()
    if home is None or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', board) or not profile:
        raise ValueError('native_board_binding_unavailable')
    path = home/'kanban.db' if board == 'default' else home/'kanban/boards'/board/'kanban.db'
    return home, board, profile, path


def task_snapshot(identifier, contact_id, native, *, review=False):
    """Verify native provenance and, when supplied, the currently held run."""
    if review:
        home, boards, _ = observed_boards()
        if home is None or 'default' not in boards:
            raise ValueError('selected_native_review_board_required')
        board, profile, path = 'default', 'default', _board_path(home, 'default')
    else:
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
        creator = 'colony-initiative' if review else 'colony-local-work'
        if (task is None or task['created_by'] != creator
                or task['idempotency_key'] != creator+':'+identifier
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
        state = {'status': task['status'], 'native_run_id': task['current_run_id'],
                 'attempt_count': count, 'archived': task['status'] == 'archived'}
        if review:
            latest = db.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                (task['id'],)).fetchone()
            gave_up = bool(latest and db.execute("SELECT 1 FROM task_events WHERE task_id=? "
                "AND kind='gave_up' AND created_at>=? LIMIT 1", (task['id'], latest['started_at'])).fetchone())
            state.update(contract_sha256=hashlib.sha256((task['body'] or '').encode()).hexdigest(),
                         completed_run=bool(latest and latest['outcome'] == 'completed'),
                         gave_up=gave_up and task['status'] == 'blocked' and latest['outcome'] in
                                 {'gave_up', 'crashed', 'timed_out', 'spawn_failed'},
                         run_outcome=latest['outcome'] if latest else None,
                         error=str(latest['error'] or task['last_failure_error'] or '')[:500] if latest else '',
                         summary=str(latest['summary'] or '')[:1600] if latest else '',
                         native_run_id=latest['id'] if latest else None)
            if latest and latest['outcome'] in {'crashed', 'timed_out', 'spawn_failed', 'gave_up'}:
                state['runtime_observation'] = {
                    'outcome': latest['outcome'], 'started_at': latest['started_at'],
                    'ended_at': latest['ended_at'], 'max_runtime_seconds': latest['max_runtime_seconds'],
                    'attempt_count': count, 'profile': latest['profile'],
                    'task_model_override_at_observation': task['model_override'],
                    'task_provider_override_at_observation': task['provider_override'],
                    'served_model': 'unknown',
                    'role': 'native_default_worker',
                }
        return result, state


def project_accepted(identifier, contact_id, context):
    """Observe only an accepted task, never expose a machine-wide board."""
    review = context.get('native_review')
    if review:
        contact_id, context = review['contact_id'], {**review, 'execution_backend': 'kanban'}
    if context.get('execution_backend') != 'kanban' or not context.get('native_task_id'):
        return None
    try:
        native, state = task_snapshot(identifier, contact_id, {
            'native_board': context.get('native_board'), 'native_task_id': context['native_task_id']}, review=bool(review))
        if native['source_home_id'] != context.get('source_home_id'):
            raise ValueError('selected_native_home_changed')
        return {'available': True, **native, **state,
                'liveness': 'native_running_record' if state['status'] == 'running' else 'native_task_record'}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': 'accepted_native_task_unavailable'}

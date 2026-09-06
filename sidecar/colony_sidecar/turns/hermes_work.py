"""Read one explicitly bound native cron ledger, without importing its scheduler.

Hermes owns transitions and recovery. These rows are a read projection, not a
new lease, process inventory, or proof that an external effect completed.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from colony_sidecar import get_state_dir


def selected_home():
    """Use the private instance binding or explicit HERMES_HOME, never a scan."""
    selected = os.environ.get('HERMES_HOME', '').strip()
    path = get_state_dir() / 'instance.json'
    if path.is_file():
        manifest = json.loads(path.read_text())
        if manifest.get('version') != 1 or manifest.get('profile') != 'local':
            raise ValueError('unsupported_instance_binding')
        bound = Path(manifest['hermes_home']).expanduser().resolve()
        if selected and Path(selected).expanduser().resolve() != bound:
            raise ValueError('conflicting_instance_binding')
        return bound
    return Path(selected).expanduser().resolve() if selected else None


def _age(value, now):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        return None
    return round(max(0., now - parsed.timestamp()), 1)


def cron_view(*, limit=8, now=None):
    view = {'source': 'hermes_native_cron_ledger', 'available': False,
            'items': [], 'recent': [], 'complete': False,
            'coverage': 'one selected Hermes profile; no process liveness or external-effect verification'}
    try:
        home = selected_home()
    except (OSError, ValueError, KeyError, TypeError):
        return {**view, 'reason': 'invalid_profile_binding'}
    if home is None:
        return {**view, 'reason': 'profile_not_bound'}
    view['source_home_id'] = hashlib.sha256(str(home).encode()).hexdigest()
    path = home / 'cron' / 'executions.db'
    limit = max(1, min(int(limit), 100))
    now = time.time() if now is None else now
    deadline = time.monotonic() + .2
    try:
        if not path.is_file():
            return {**view, 'reason': 'native_ledger_absent'}
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            # One snapshot for active counts and rows. No initialization,
            # recovery, native list_executions(), or schema writes on read.
            conn.execute('BEGIN')
            total = conn.execute("SELECT count(*) FROM executions WHERE status IN ('claimed','running')").fetchone()[0]
            columns = 'id, job_id, source, status, claimed_at, started_at, finished_at'
            active = conn.execute(f"SELECT {columns} FROM executions WHERE status IN ('claimed','running') ORDER BY claimed_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
            cutoff = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
            recent = conn.execute(f"SELECT {columns} FROM executions WHERE status IN ('completed','failed','unknown') AND julianday(finished_at) >= julianday(?) ORDER BY finished_at DESC, id DESC LIMIT ?", (cutoff, limit)).fetchall()
        # Labels are optional, bounded, and never fall back to prompts/scripts.
        names = {}
        jobs = home / 'cron' / 'jobs.json'
        if jobs.is_file() and jobs.stat().st_size <= 4 * 1024 * 1024:
            try:
                document = json.loads(jobs.read_text())
                names = {str(job['id']): str(job['name'])[:200]
                         for job in document.get('jobs', [])
                         if isinstance(job, dict) and job.get('id') and isinstance(job.get('name'), str)}
            except (OSError, ValueError, TypeError, AttributeError):
                pass

        def project(row):
            item = dict(row)
            item['execution_id'] = item.pop('id')
            item['name'] = names.get(item['job_id'])
            item['record_age_seconds'] = _age(item['finished_at'] or item['started_at'] or item['claimed_at'], now)
            item['liveness'] = 'unknown' if item['status'] in {'claimed', 'running', 'unknown'} else 'native_terminal_record'
            return item

        return {**view, 'available': True, 'items': [project(row) for row in active],
                'recent': [project(row) for row in recent], 'total': total,
                'truncated': total > len(active), 'recent_window_seconds': 86400}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {**view, 'reason': 'native_ledger_unavailable'}

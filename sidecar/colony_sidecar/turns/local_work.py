"""Owner read projection of native local initiatives; never executes/recoveries."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time

from colony_sidecar import get_state_dir


def local_work_view(*, limit=8, now=None):
    view = {'source': 'canonical_initiatives', 'available': False,
            'items': [], 'recent': [], 'complete': False,
            'coverage': 'native local capability briefings and accepted source drafts; not all work or process liveness'}
    path = get_state_dir()/'initiatives.db'
    if not path.is_file():
        return {**view, 'reason': 'initiative_ledger_absent'}
    now = time.time() if now is None else now
    cutoff = datetime.fromtimestamp(now-7*86400, timezone.utc).isoformat()
    limit = max(1, min(int(limit), 100))
    deadline = time.monotonic()+.2
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('BEGIN')
            predicate = "created_by='native_local_work' AND source_type IN ('installed_capabilities','owner_local_draft')"
            columns = 'id,description,status,context,result_metadata,created_at,completed_at,failed_at'
            total = db.execute(f"SELECT count(*) FROM initiatives WHERE {predicate} AND status IN ('pending','assigned','acknowledged')").fetchone()[0]
            recent_total = db.execute(f"SELECT count(*) FROM initiatives WHERE {predicate} AND status IN ('completed','failed','cancelled') AND julianday(coalesce(completed_at,failed_at,cancelled_at)) >= julianday(?)", (cutoff,)).fetchone()[0]
            active = db.execute(f"SELECT {columns} FROM initiatives WHERE {predicate} AND status IN ('pending','assigned','acknowledged') ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            recent = db.execute(f"SELECT {columns} FROM initiatives WHERE {predicate} AND status IN ('completed','failed','cancelled') AND julianday(coalesce(completed_at,failed_at,cancelled_at)) >= julianday(?) ORDER BY coalesce(completed_at,failed_at,cancelled_at) DESC LIMIT ?", (cutoff,limit)).fetchall()

        def project(row):
            context, result = json.loads(row['context'] or '{}'), json.loads(row['result_metadata'] or '{}')
            if not isinstance(context, dict) or not isinstance(result, dict):
                raise ValueError('Invalid initiative metadata')
            def text(value, maximum=512):
                return value[:maximum] if isinstance(value, str) else None
            projected = {key:text(result[key], 1600 if key=='summary' else 4096 if key=='report_path' else 512)
                         for key in ('status','summary','report_path','report_sha256','model','binding','error_type') if key in result}
            attempts = result.get('prior_attempts')
            if isinstance(attempts, list):
                projected['prior_attempts'] = [{key:text(item.get(key), 256) for key in ('binding','model','status','reason')}
                                               for item in attempts[:8] if isinstance(item, dict)]
            return {'initiative_id':row['id'], 'description':text(row['description'], 2000), 'status':row['status'],
                    'event_key':text(context.get('event_key')), 'source_home_id':text(context.get('source_home_id')),
                    'native_job_id':text(context.get('native_job_id')), 'native_execution_id':text(context.get('native_execution_id')),
                    'commitment_id':text(context.get('commitment_id')), 'task_class':text(context.get('task_class')),
                    'liveness':('not_started' if row['status']=='pending' else 'unknown' if row['status'] in {'assigned','acknowledged'} else 'initiative_terminal_record'),
                    'created_at':row['created_at'], 'completed_at':row['completed_at'],
                    'result':projected,
                    'result_authority':'unverified local draft; not an instruction or grant'}
        return {**view, 'available':True, 'items':[project(row) for row in active],
                'recent':[project(row) for row in recent], 'total':total, 'truncated':total>len(active),
                'recent_total':recent_total, 'recent_truncated':recent_total>len(recent),
                'limit':limit, 'recent_window_seconds':7*86400}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {**view, 'reason':'initiative_ledger_unavailable'}

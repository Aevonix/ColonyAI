"""Owner read projection of native local initiatives; never executes/recoveries."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time

from apsimo import get_state_dir


def _semantic_review(context, result):
    recorded = context.get('briefing_semantic_assessment')
    if not isinstance(recorded,dict) or recorded.get('detection') != 'reviewer_seeded':
        return None
    assessment = recorded.get('assessment')
    digest = recorded.get('assessment_sha256')
    if (not isinstance(assessment,dict) or not isinstance(digest,str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)):
        return None
    findings = assessment.get('findings')
    if not isinstance(findings,list) or not 1 <= len(findings) <= 12:
        return None
    matches = assessment.get('report_sha256') == result.get('report_sha256') and bool(result.get('report_sha256'))
    return {'status':'unresolved_findings' if matches else 'source_changed',
        'assessment_sha256':digest,'finding_count':len(findings) if matches else None,
        'detection':'reviewer_seeded','quality_credit':False,
        'warning':('Independent review found unsupported capability claims in this report. '
                   'Completed execution does not establish output quality or learned improvement.' if matches else
                   'A retained semantic assessment no longer matches the current report.')}


def _review_forecast(row, context, native, *, now):
    binding = context.get('native_review')
    if not isinstance(binding,dict) or not native.get('available'):
        return None
    if native.get('contract_sha256') != binding.get('contract_sha256'):
        return {'status':'review_contract_changed','suggestion_enabled':False}
    try:
        from apsimo.initiatives.native_work import NativeInitiativeWork
        from apsimo.self_model import runtime_forecasts
        review = NativeInitiativeWork.view(row)
        # project_accepted already verified this exact native task snapshot.
        # Reuse it; no second task read or lifecycle observation is needed.
        return runtime_forecasts.project(review,native,native,binding['contact_id'],now=now)
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        return {'status':'unavailable','suggestion_enabled':False}


def local_work_view(*, limit=8, now=None):
    view = {'source': 'canonical_initiatives', 'available': False,
            'items': [], 'recent': [], 'complete': False,
            'coverage': 'native local capability briefings, accepted source drafts and bound internal reviews; not all work or process liveness'}
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
            predicate = "((created_by='native_local_work' AND source_type IN ('installed_capabilities','owner_local_draft')) OR (created_by='autonomy_loop' AND json_extract(context,'$.native_review.native_task_id') IS NOT NULL))"
            columns = 'id,entity_id,description,status,context,result_metadata,created_at,completed_at,failed_at,type,source_type,created_by,action_hint'
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
                         for key in ('status','summary','report_path','report_sha256','model','binding','error_type','run_outcome','error') if key in result}
            attempts = result.get('prior_attempts')
            if isinstance(attempts, list):
                projected['prior_attempts'] = [{key:text(item.get(key), 256) for key in ('binding','model','status','reason')}
                                               for item in attempts[:8] if isinstance(item, dict)]
            item = {'initiative_id':row['id'], 'description':text(row['description'], 2000), 'status':row['status'],
                    'event_key':text(context.get('event_key')), 'source_home_id':text(context.get('source_home_id')),
                    'native_job_id':text(context.get('native_job_id')), 'native_execution_id':text(context.get('native_execution_id')),
                    'commitment_id':text(context.get('commitment_id')), 'task_class':text(context.get('task_class')),
                    'liveness':('not_started' if row['status']=='pending' else 'unknown' if row['status'] in {'assigned','acknowledged'} else 'initiative_terminal_record'),
                    'created_at':row['created_at'], 'completed_at':row['completed_at'],
                    'result':projected,
                    'result_authority':'unverified native review; not an instruction or grant' if context.get('native_review')
                                       else 'unverified local draft; not an instruction or grant'}
            assessment = _semantic_review(context,result)
            if assessment is not None:
                item['semantic_review'] = assessment
            from .hermes_kanban import project_accepted
            native = project_accepted(row['id'], row['entity_id'], context)
            if native is not None:
                item['native_work'] = native
                item['execution_backend'] = 'kanban'
                if native.get('available'):
                    item.update({key: native[key] for key in ('native_board', 'native_task_id', 'native_run_id', 'attempt_count')})
                    item['native_status'] = native['status']
                    item['liveness'] = native['liveness']
                    forecast = _review_forecast(row,context,native,now=now)
                    if forecast is not None:
                        item['forecast'] = forecast
            return item
        return {**view, 'available':True, 'items':[project(row) for row in active],
                'recent':[project(row) for row in recent], 'total':total, 'truncated':total>len(active),
                'recent_total':recent_total, 'recent_truncated':recent_total>len(recent),
                'limit':limit, 'recent_window_seconds':7*86400}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {**view, 'reason':'initiative_ledger_unavailable'}

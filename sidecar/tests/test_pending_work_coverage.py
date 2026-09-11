"""Pending canonical jobs and selected delivery snapshots are visible, scoped data."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from apsimo.task_queue.models import Job
from apsimo.task_queue.queue_manager import QueueManager
from apsimo.turns.executions import request_work_context
from apsimo.turns.reported_workers import reported_worker_view


@pytest.mark.asyncio
async def test_retained_pending_jobs_survive_reopen_and_busy_running_work_cannot_hide_them(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path/'queue.db'
    queue = QueueManager(db_path=path, clock=lambda: now)
    await queue.start()
    for state in ('running', 'claimed', 'queued', 'blocked', 'abandoned'):
        for index in range(3 if state == 'running' else 1):
            job = Job(job_id=f'{state}-{index}')
            await queue.post(job)
            await queue._db.execute('UPDATE jobs SET status=?,claimed_by=?,claim_attempt_id=?,last_heartbeat=? WHERE job_id=?',
                (state, 'worker' if state in ('running','claimed') else None,
                 'attempt-'+job.job_id if state in ('running','claimed') else None, now.isoformat(), job.job_id))
    await queue._db.commit()
    await queue.stop()
    queue = QueueManager(db_path=path, clock=lambda: now)
    await queue.start()
    try:
        view = await queue.current_work(limit=5)
        assert {row['state'] for row in view['items']} == {'running','claimed','queued','blocked','abandoned'}
        assert view['total'] == 7 and view['truncated'] and not view['complete']
        assert view['state_counts']['running'] == 3
        assert all(row['liveness'] == 'unknown' for row in view['items'] if row['state'] in ('queued','blocked','abandoned'))
        running = next(row for row in view['items'] if row['state'] == 'running')
        assert running['claim_attempt_id'] == 'attempt-running-0' and running['worker_id'] == 'worker'
        projected = request_work_context({'items': [], 'worker_work': view})
        assert 'queued=1' in projected['text'] and 'blocked=1' in projected['text']
        assert 'queued-0' in projected['text'] and 'abandoned-0' in projected['text']
        await queue._db.execute("UPDATE jobs SET last_heartbeat=? WHERE job_id='running-0'", ((now+timedelta(hours=1)).isoformat(),))
        await queue._db.execute("UPDATE jobs SET payload='not-json' WHERE job_id='queued-0'")
        await queue._db.commit()
        rows = (await queue.current_work(limit=5))['items']
        assert next(row for row in rows if row['job_id']=='running-0')['liveness']=='unknown'
        assert next(row for row in rows if row['job_id']=='queued-0')['state']=='queued'
        await queue._db.execute("UPDATE jobs SET claim_expires_at=? WHERE job_id='claimed-0'", ((now-timedelta(seconds=1)).isoformat(),))
        await queue._db.commit()
        expired = await queue.current_work(limit=5)
        claimed = next(row for row in expired['items'] if row['job_id']=='claimed-0')
        assert claimed['claim_unexpired'] is False and claimed['liveness']=='unknown'
        lines = request_work_context({'items': [], 'worker_work': expired})['text'].splitlines()
        projected_claim = next(json.loads(line) for line in lines if line.startswith('{') and 'claimed-0' in line)
        assert projected_claim['claim_unexpired'] is False
    finally:
        await queue.stop()


def test_selected_process_snapshot_preserves_delivery_stages_and_hides_private_fields(tmp_path, monkeypatch):
    path = tmp_path/'heartbeat.json'
    snapshot = {'schema':'ColonyWorkSnapshotV1', 'available':True, 'observed_at':1000,
        'total':2, 'pending_total':1, 'terminal_total':1, 'state_counts':{'accepted':1,'ambiguous':1},
        'source_status':{'outbox':'observed','provider':'observed'}, 'items':[
            {'delivery_id':'delivery-a','intent_id':'intent-a','state':'accepted','provider_state':'provider_started',
             'attempt_id':'attempt-a','request_key':'request-a','content':'PRIVATE_CONTENT','recipient':'PRIVATE_RECIPIENT'},
            {'delivery_id':'delivery-b','intent_id':'intent-b','state':'ambiguous','provider_state':'receipt_recorded',
             'observation_state':'ambiguous','evidence_sha256':'a'*64}]}
    path.write_text(json.dumps({'updated_at':1000,'ready':True,'state':{'work_snapshot':snapshot}}))
    monkeypatch.setenv('COLONY_WORKER_STATUS_PATHS', json.dumps({'Delivery':str(path)}))
    full = reported_worker_view(now=1001)
    report = full['items'][0]
    assert report['state']=='reported_ready' and report['liveness']=='unverified'
    assert report['work_snapshot']['pending_total']==1 and not report['work_snapshot']['complete']
    projected = request_work_context({'items': [], 'reported_worker':full})
    assert 'provider_started' in projected['text'] and 'ambiguous' in projected['text']
    assert 'attempt-a' in projected['text'] and 'intent-a' in projected['text']
    assert 'PRIVATE_' not in json.dumps(full) and 'PRIVATE_' not in projected['text']
    assert len(projected['text']) <= 4000
    assert reported_worker_view(now=1201)['items'][0]['work_snapshot']['freshness']=='stale'
    assert reported_worker_view(now=999)['items'][0]['work_snapshot']['freshness']=='unknown'


def test_missing_delivery_store_is_partial_and_terminal_download_never_becomes_live(tmp_path, monkeypatch):
    path = tmp_path/'delivery.json'
    path.write_text(json.dumps({'updated_at':1000,'ready':True,'state':{'work_snapshot':{
        'schema':'ColonyWorkSnapshotV1','available':False,'observed_at':1000,
        'reason':'delivery_ledger_unavailable','items':[]}}}))
    terminal = tmp_path/'download.json'
    terminal.write_text(json.dumps({'updated_at':1000,'state':'completed','task_id':'download',
                                   'started_at':900,'finished_at':999,'exit_code':0}))
    monkeypatch.setenv('COLONY_WORKER_STATUS_PATHS', json.dumps({'Delivery':str(path),'Download':str(terminal)}))
    view = reported_worker_view(now=1001)
    assert view['partial'] and view['items'][0]['work_snapshot']['available'] is False
    download = view['items'][1]
    assert download['record_kind']=='terminal_report' and download['liveness']=='unverified'
    context = request_work_context({'items': [], 'reported_worker':view})
    assert context['work_sources']['reported_worker']['status']=='partial'
    assert 'terminal_report' in context['text'] and not context['complete']
    path.write_text(json.dumps({'updated_at':1000,'ready':True,'state':{'work_snapshot':{'schema':'unknown'}}}))
    invalid = reported_worker_view(now=1001)
    assert invalid['partial'] and invalid['items'][0]['work_snapshot']['reason']=='unsupported_work_snapshot'

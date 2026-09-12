"""Optional local heartbeat reports, separate from canonical executions."""
import json
import hashlib
import math
import os
from pathlib import Path
import time


def _text(value, maximum=128):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 for char in value))


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def work_snapshot(value, *, now=None):
    """Bounded metadata from an explicitly enrolled producer, never authority.

    Producers may attach this snapshot to an existing process heartbeat. The
    public reader knows no private ledger schema, recipients or machine paths.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('schema') != 'PacoMindWorkSnapshotV1':
        return {'schema': 'PacoMindWorkSnapshotV1', 'available': False, 'reason': 'unsupported_work_snapshot', 'complete': False}
    if type(value.get('available')) is not bool or not _number(value.get('observed_at')):
        return {'schema': 'PacoMindWorkSnapshotV1', 'available': False, 'reason': 'invalid_work_snapshot', 'complete': False}
    now = time.time() if now is None else now
    age = now - value['observed_at']
    result = {'schema': 'PacoMindWorkSnapshotV1', 'available': value['available'], 'complete': False, 'observed_at': value['observed_at'],
              'freshness': 'recent' if 0 <= age <= 120 else 'stale' if age > 120 else 'unknown',
              'items': [], 'truncated': bool(value.get('truncated')), 'partial': bool(value.get('partial'))}
    for key in ('total', 'pending_total', 'terminal_total', 'unmatched_provider_total'):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ('reason', 'coverage'):
        if _text(value.get(key), 200):
            result[key] = value[key]
    statuses = value.get('source_status')
    if isinstance(statuses, dict):
        result['source_status'] = {key: state for key, state in list(statuses.items())[:8]
            if _text(key, 32) and state in {'observed', 'unavailable', 'partial', 'not_observed'}}
    states = value.get('state_counts')
    if isinstance(states, dict):
        result['state_counts'] = {key: n for key, n in list(states.items())[:12]
            if _text(key, 48) and type(n) is int and n >= 0}
    rows = value.get('items', [])
    if not isinstance(rows, list):
        return {'schema': 'PacoMindWorkSnapshotV1', 'available': False, 'reason': 'invalid_work_snapshot', 'complete': False}
    result['truncated'] |= len(rows) > 4
    for row in rows[:4]:
        if not isinstance(row, dict):
            result['partial'] = True
            continue
        item = {'liveness': 'record_only'}
        for key in ('delivery_id', 'intent_id', 'request_key', 'attempt_id', 'state', 'record_kind',
                    'provider_state', 'observation_state'):
            if _text(row.get(key), 256 if key.endswith('_id') or key == 'request_key' else 64):
                item[key] = row[key]
        if not item.get('delivery_id') or not item.get('state'):
            result['partial'] = True
            continue
        for key in ('updated_at', 'provider_updated_at'):
            if _number(row.get(key)):
                item[key] = row[key]
        if type(row.get('transport_observation_pending')) is bool:
            item['transport_observation_pending'] = row['transport_observation_pending']
        digest = row.get('evidence_sha256')
        if isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
            item['evidence_sha256'] = digest
        result['items'].append(item)
    return result


def work_details(value, *, now=None):
    """Project declared work metadata; never follow references or verify effects.

    These optional fields extend existing heartbeat producers. Bad optional
    values do not hide the underlying legacy state or make a task terminal.
    The same projection is used for automatic request context.
    """
    result = {}
    snapshot = work_snapshot(value.get('work_snapshot'), now=now)
    if snapshot is not None:
        result['work_snapshot'] = snapshot
    for key in ('task_id', 'parent_task_id', 'worker_id', 'kind'):
        if _text(value.get(key)):
            result[key] = value[key]
    for key in ('started_at', 'finished_at'):
        if _number(value.get(key)):
            result[key] = value[key]
    if type(value.get('exit_code')) is int and -(2**31) <= value['exit_code'] < 2**31:
        result['exit_code'] = value['exit_code']
    progress = value.get('progress')
    if isinstance(progress, list):
        selected = []
        for row in progress[:4]:
            if (not isinstance(row, dict) or not _number(row.get('completed'))
                    or not _number(row.get('observed_at')) or not _text(row.get('unit'), 32)
                    or not _text(row.get('source'), 128)):
                continue
            item = {key: row[key] for key in ('completed', 'unit', 'source', 'observed_at')}
            if 'total' in row:
                if not _number(row['total']) or row['total'] < row['completed']:
                    continue
                item['total'] = row['total']
            selected.append(item)
        if selected:
            result['progress'] = selected
    references = value.get('result_refs')
    if isinstance(references, list):
        selected = []
        for row in references[:4]:
            if (not isinstance(row, dict) or not _text(row.get('kind'), 64)
                    or not _text(row.get('reference'), 512)):
                continue
            item = {key: row[key] for key in ('kind', 'reference')}
            digest = row.get('sha256')
            if isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
                item['sha256'] = digest
            if _number(row.get('observed_at')):
                item['observed_at'] = row['observed_at']
            # The producer describes its check; the reader does not endorse it.
            if _text(row.get('verification'), 160):
                item['verification'] = row['verification']
            selected.append(item)
        if selected:
            result['result_refs'] = selected
    return result


def reported_worker_view(*, limit=8, now=None):
    configured = os.environ.get('PACOMIND_WORKER_STATUS_PATHS', '').strip()
    if not configured:
        return None
    view = {'source':'configured_local_heartbeats', 'available':False,
            'items':[], 'complete':False,
            'coverage':'reported worker state only; no execution or effect verification'}
    try:
        paths = json.loads(configured)
        if (len(configured)>8192 or not isinstance(paths,dict)
                or not all(isinstance(label,str) and label and isinstance(path,str) and path
                           for label,path in paths.items())):
            raise ValueError('Invalid heartbeat mapping')
    except (ValueError,TypeError):
        return {**view,'reason':'invalid_status_configuration'}
    now = time.time() if now is None else now
    bounded = max(1,min(int(limit),8))
    items = []
    for label,filename in list(paths.items())[:bounded]:
        item = {'label':label[:128], 'available':False, 'liveness':'unverified'}
        try:
            path = Path(filename).expanduser()
            if not path.is_file():
                raise ValueError('Heartbeat unavailable')
            with path.open('rb') as stream:
                raw = stream.read(16385)
            value = json.loads(raw)
            if len(raw)>16384 or not isinstance(value,dict):
                raise ValueError('Invalid heartbeat')
            updated = value.get('updated_at')
            state = value.get('state')
            if isinstance(state, dict) and 'work_snapshot' in state:
                value = {**value, 'work_snapshot': state['work_snapshot']}
                state = 'reported_ready' if value.get('ready') is True else 'reported_unready'
            if (not isinstance(state,str) or not state or len(state)>64
                    or isinstance(updated,bool) or not isinstance(updated,(int,float))
                    or not math.isfinite(updated)):
                raise ValueError('Invalid heartbeat fields')
            age = now-updated
            item.update(available=True,state=state,updated_at=updated,
                age_seconds=round(age,1) if age>=0 else None,
                freshness='recent' if 0<=age<=120 else 'stale' if age>120 else 'unknown',
                status_sha256=hashlib.sha256(raw).hexdigest())
            for key in ('detail_code','release_commit'):
                if isinstance(value.get(key),str):
                    item[key] = value[key][:128]
            item.update(work_details(value, now=now))
            if 'task_id' in item:
                finished = item.get('finished_at')
                terminal = (finished is not None and finished <= updated
                            and finished >= item.get('started_at', 0)
                            and state in {'exited', 'completed', 'failed', 'interrupted', 'cancelled'})
                item['record_kind'] = 'terminal_report' if terminal else 'progress_report'
        except (OSError,ValueError,TypeError):
            item['reason'] = 'heartbeat_unavailable'
        items.append(item)
    partial = any(not item.get('available') or item.get('work_snapshot', {}).get('available') is False
                  or item.get('work_snapshot', {}).get('partial') for item in items)
    return {**view,'available':True,'items':items,'truncated':len(paths)>bounded, 'partial': partial}

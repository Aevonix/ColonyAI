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


def work_details(value):
    """Project declared work metadata; never follow references or verify effects.

    These optional fields extend existing heartbeat producers. Bad optional
    values do not hide the underlying legacy state or make a task terminal.
    The same projection is used for automatic request context.
    """
    result = {}
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
    configured = os.environ.get('COLONY_WORKER_STATUS_PATHS', '').strip()
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
            item.update(work_details(value))
            if 'task_id' in item:
                finished = item.get('finished_at')
                terminal = (finished is not None and finished <= updated
                            and finished >= item.get('started_at', 0)
                            and state in {'exited', 'completed', 'failed', 'interrupted', 'cancelled'})
                item['record_kind'] = 'terminal_report' if terminal else 'progress_report'
        except (OSError,ValueError,TypeError):
            item['reason'] = 'heartbeat_unavailable'
        items.append(item)
    return {**view,'available':True,'items':items,'truncated':len(paths)>bounded}

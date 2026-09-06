"""Optional local heartbeat reports, separate from canonical executions."""
import json
import math
import os
from pathlib import Path
import time


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
                freshness='recent' if 0<=age<=120 else 'stale' if age>120 else 'unknown')
            for key in ('detail_code','release_commit'):
                if isinstance(value.get(key),str):
                    item[key] = value[key][:128]
        except (OSError,ValueError,TypeError):
            item['reason'] = 'heartbeat_unavailable'
        items.append(item)
    return {**view,'available':True,'items':items,'truncated':len(paths)>bounded}

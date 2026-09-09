"""Observe selected native review requests; never inspect response prose."""
from contextlib import closing
import hashlib
import os
import sqlite3
from urllib.parse import quote


class RuntimeModelObserver:
    def __init__(self, client, owner):
        self.client, self.owner = client, owner

    def observe(self, phase, **kwargs):
        from agent.delegation_context import is_dispatcher_owned_worker_context, is_delegated_child_context
        if not (is_dispatcher_owned_worker_context() or is_delegated_child_context()):
            return
        task_id, run_id, claim = (os.environ.get(key, '') for key in
            ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK'))
        if not task_id or not run_id.isdigit() or not claim or not kwargs.get('api_request_id'):
            return
        from hermes_cli import kanban_db as kb
        if kb.get_current_board() != 'default':
            return
        try:
            path = kb.kanban_db_path(board='default').resolve()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.1)) as db:
                db.row_factory = sqlite3.Row
                task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if (task is None or task['created_by'] != 'colony-initiative'
                        or task['tenant'] != self.owner or task['status'] != 'running'
                        or task['current_run_id'] != int(run_id) or task['claim_lock'] != claim
                        or not str(task['idempotency_key']).startswith('colony-initiative:')):
                    return
                identifier = task['idempotency_key'].removeprefix('colony-initiative:')
                body = {'contact_id': self.owner, 'native_board': 'default', 'native_task_id': task_id,
                    'native_run_id': int(run_id), 'native_claim_lock': claim,
                    'contract_sha256': hashlib.sha256((task['body'] or '').encode()).hexdigest(),
                    'api_request_id': str(kwargs['api_request_id']), 'phase': phase,
                    'requested_model': kwargs.get('model') or None,
                    'provider': kwargs.get('provider') or None,
                    'response_model': kwargs.get('response_model') if phase == 'response' else None}
            # Best-effort observation cannot prevent native work. A missing
            # start/end produces explicitly incomplete provenance at readback.
            response = self.client.post('/v1/host/initiative-work/'+quote(identifier, safe='')+'/model-observation',
                                        json=body, timeout=.4)
            response.raise_for_status()
        except Exception:
            return

    def register(self, ctx):
        ctx.register_hook('pre_api_request', lambda **kw: self.observe('start', **kw))
        ctx.register_hook('post_api_request', lambda **kw: self.observe('response', **kw))
        ctx.register_hook('api_request_error', lambda **kw: self.observe('error', **kw))

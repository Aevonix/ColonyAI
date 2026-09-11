"""Forward completed native summaries as assistant evidence, never USER facts."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


class CompletedReports:
    def __init__(self, client, outbox, scopes, request_memory, current_context,
                 *, drain_limit=16, drain_seconds=.25):
        self.client, self.outbox = client, outbox
        self.scopes, self.request_memory = scopes, request_memory
        self.current_context = current_context
        self.drain_limit, self.drain_seconds = drain_limit, drain_seconds

    @staticmethod
    def _skip(reason):
        logger.info('Native completion report not forwarded (%s)', reason)
        return {'forwarded': False, 'reason': reason}

    def __call__(self, *, task_id, board=None, run_id=None, **kwargs):
        from agent.delegation_context import is_dispatcher_owned_worker_context
        from hermes_cli import kanban_db as kb

        context = self.current_context() or {}
        scope = self.scopes.for_execution(session_id=context.get('session_id', ''),
            task_id=context.get('task_id', ''), turn_id=context.get('turn_id', ''))
        if (context.get('tool_name') != 'kanban_complete' or scope is None
                or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
                or scope.platform != 'cli'):
            return self._skip('attested_completion_context_missing')
        if (not is_dispatcher_owned_worker_context() or not task_id
                or os.environ.get('HERMES_KANBAN_TASK') != task_id
                or str(run_id) != os.environ.get('HERMES_KANBAN_RUN_ID')
                or board != kb.get_current_board()):
            return self._skip('owned_native_run_missing')
        references = self.request_memory.supplied_snapshot(scope)
        if references is None:
            return self._skip('verified_request_lineage_missing')
        try:
            path = kb.kanban_db_path(board=board).resolve()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('''SELECT r.summary,r.ended_at,r.outcome,r.status,
                        t.status AS task_status,t.current_run_id,
                        (SELECT max(last.id) FROM task_runs last WHERE last.task_id=t.id) AS latest_run_id
                    FROM task_runs r JOIN tasks t ON t.id=r.task_id
                    WHERE r.task_id=? AND r.id=?''', (task_id, run_id)).fetchone()
            # Native completion clears both claim locks and current_run_id.
            # Match the callback's owned run to the latest ended run instead.
            if (row is None or row['task_status'] != 'done' or row['current_run_id'] is not None
                    or row['latest_run_id'] != run_id
                    or row['status'] != 'done' or row['outcome'] != 'completed'
                    or not isinstance(row['summary'], str) or not row['summary'].strip()
                    or not isinstance(row['ended_at'], (int, float))):
                return self._skip('completed_native_report_missing')
            native_home = hashlib.sha256(str(kb.kanban_home().resolve()).encode()).hexdigest()
            identity = {'native_home_id': native_home, 'board': board,
                        'task_id': task_id, 'run_id': run_id}
            source_id = 'native-report:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            report = ('Machine-authored native Kanban completion report. Assertions are unverified.\n'
                      + json.dumps(identity, sort_keys=True) + '\n\n' + row['summary'])
            payload = {'turn_id': source_id, 'session_id': scope.session_id,
                'contact_id': scope.contact_id, 'assistant_message': report,
                'source_only': True, 'require_source_receipt': True,
                'occurred_at': datetime.fromtimestamp(row['ended_at'], timezone.utc).isoformat()}
            if references:
                payload['assistant_source_refs'] = references
            receipt = self.outbox.enqueue(source_id, payload)
            if receipt['state'] == 'pending' or receipt.get('survivor_state') == 'pending':
                self.outbox.drain(lambda stored, timeout_seconds: self.client.sync_turn(
                    **stored, outbox=self.outbox, timeout_seconds=timeout_seconds),
                    limit=self.drain_limit, timeout_seconds=self.drain_seconds)
            return {'forwarded': receipt['state'] != 'erased', 'source_id': source_id,
                    'scope': 'assistant_report_evidence', 'source_refs': len(references)}
        except Exception as error:
            # Native completion is already committed. The ordinary outbox owns
            # delivery recovery; never retry native work or reveal report text.
            return self._skip(type(error).__name__)

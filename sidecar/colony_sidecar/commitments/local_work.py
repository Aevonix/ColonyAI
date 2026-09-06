"""One accepted local draft class in the existing initiative/commitment ledgers."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid

CREATOR = 'native_local_work'
SOURCE = 'owner_local_draft'
TRANSIENT = {'TimeoutError', 'APITimeoutError', 'APIConnectionError', 'ConnectError',
             'ReadTimeout', 'RateLimitError', 'ServiceUnavailableError'}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def stamp():
    return datetime.now(timezone.utc).isoformat()


class LocalWork:
    def __init__(self, initiatives, commitments):
        self.initiatives, self.commitments = initiatives, commitments

    @contextmanager
    def transaction(self):
        # Reuse initialized stores; never invoke legacy file recovery here.
        with closing(sqlite3.connect(self.initiatives._db_path, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('ATTACH DATABASE ? AS obligations', (str(self.commitments._db_path),))
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def history(db, identifier, actor, action, details):
        db.execute('INSERT INTO assignment_history(initiative_id,agent_id,action,details) VALUES(?,?,?,?)',
                   (identifier, actor, action, encoded(details)))

    @staticmethod
    def view(row):
        if row is None:
            return None
        context = json.loads(row['context'])
        return {key: row[key] for key in ('id', 'description', 'status', 'attempt_count')} | {
            'max_attempts': None if context.get('execution_backend') == 'kanban' else row['max_attempts'],
            'context': context, 'result': json.loads(row['result_metadata'] or '{}'),
            'parent_commitment_fulfilled': False}

    @staticmethod
    def obligation(db, identifier, person):
        row = db.execute('SELECT * FROM obligations.commitments WHERE id=? AND person_id=?',
                         (identifier, person)).fetchone()
        if row is None:
            raise KeyError('unknown_commitment')
        return row

    def accept(self, commitment_id, *, contact_id, principal_id, session_id, turn_id, question, sources,
               execution_backend='cron', origin=None):
        material = {'commitment_id': commitment_id, 'question': question, 'sources': sources}
        # Replay of this owner acceptance is idempotent. A later explicit
        # acceptance can intentionally request a fresh draft of changed files.
        digest = hashlib.sha256(encoded({**material, 'session_id':session_id, 'turn_id':turn_id}).encode()).hexdigest()
        key = SOURCE + ':' + digest
        with self.transaction() as db:
            if commitment_id is not None:
                obligation = self.obligation(db, commitment_id, contact_id)
                if obligation['status'] not in {'pending', 'overdue'}:
                    raise ValueError('obligation_closed')
            previous = db.execute('SELECT * FROM initiatives WHERE dedup_key=?', (key,)).fetchone()
            if previous:
                return self.view(previous)
            context = {**material, 'contact_id': contact_id, 'accepted_principal_id': principal_id,
                       'accepted_session_id': session_id, 'accepted_turn_id': turn_id,
                       'accepted_at': stamp(), 'task_class': SOURCE,
                       'execution_backend': execution_backend,
                       'scope': 'Read selected local sources and create a new local draft only.'}
            if origin:
                context['origin'] = origin
            identifier = str(uuid.uuid4())
            db.execute('''INSERT INTO initiatives(id,dedup_key,type,description,priority,rationale,
                source_type,source_id,created_by,status,entity_id,delivery_mode,context,max_attempts,timeout_seconds)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',?,'local',?,2,900)''',
                (identifier, key, 'RESEARCH_DEEP_DIVE', question, .5,
                 'Explicitly accepted local draft' + (' for an existing owner obligation.' if commitment_id else '.'),
                 SOURCE, commitment_id, CREATOR, contact_id, encoded(context)))
            self.history(db, identifier, principal_id, 'accepted',
                         {'session_id': session_id, 'turn_id': turn_id, 'task_class': SOURCE})
            return self.view(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())

    def row(self, db, identifier, contact_id):
        row = db.execute('SELECT * FROM initiatives WHERE id=? AND created_by=? AND source_type=? AND entity_id=?',
                         (identifier, CREATOR, SOURCE, contact_id)).fetchone()
        if row is None:
            raise KeyError('unknown_local_work')
        obligation = self.obligation(db, row['source_id'], contact_id) if row['source_id'] else None
        if obligation is not None and obligation['status'] not in {'pending', 'overdue'} and row['status'] in {'pending', 'assigned', 'acknowledged'}:
            db.execute("UPDATE initiatives SET status='cancelled',cancelled_at=?,cancelled_reason=? WHERE id=?",
                       (stamp(), 'parent_obligation_closed', identifier))
            self.history(db, identifier, contact_id, 'cancelled', {'reason': 'parent_obligation_closed'})
            row = db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone()
        return row

    def status(self, identifier, contact_id):
        with self.transaction() as db:
            return self.view(self.row(db, identifier, contact_id))

    def native_pending(self, contact_id, *, limit=50):
        """Reconcile accepted records on the native dispatch tick, not a timer."""
        with self.transaction() as db:
            rows = db.execute('''SELECT * FROM initiatives WHERE created_by=? AND source_type=?
                AND entity_id=? AND status IN ('pending','assigned','acknowledged','failed')
                ORDER BY created_at,id''', (CREATOR, SOURCE, contact_id)).fetchall()
            items, legacy = [], 0
            for original in rows:
                row = self.row(db, original['id'], contact_id)
                context = json.loads(row['context'])
                if row['status'] not in {'pending', 'assigned', 'acknowledged', 'failed'}:
                    continue
                if context.get('execution_backend', 'cron') != 'kanban':
                    # Pending work has no executing predecessor. Serialize this
                    # handoff with the old selector so only one backend owns it.
                    if row['status'] == 'pending':
                        context['execution_backend'] = 'kanban'
                        db.execute('UPDATE initiatives SET context=? WHERE id=?',
                                   (encoded(context), row['id']))
                        self.history(db, row['id'], contact_id, 'execution_migrated',
                                     {'from': 'cron', 'to': 'kanban'})
                        row = db.execute('SELECT * FROM initiatives WHERE id=?', (row['id'],)).fetchone()
                    else:
                        result = json.loads(row['result_metadata'] or '{}')
                        if (row['status'] != 'failed' or
                                (result.get('error_type') in TRANSIENT and row['attempt_count'] < row['max_attempts'])):
                            legacy += 1
                        continue
                if row['status'] != 'failed':
                    items.append(self.view(row))
            # Bound each tick without letting old running tasks starve new
            # acceptances that still need their native task association.
            items.sort(key=lambda item: bool(item['context'].get('native_task_id')))
            return {'items': items[:limit], 'legacy_in_flight': legacy}

    def attach_native_task(self, identifier, contact_id, native):
        with self.transaction() as db:
            row = self.row(db, identifier, contact_id)
            context = json.loads(row['context'])
            if context.get('execution_backend') != 'kanban':
                raise ValueError('native_execution_backend_required')
            if context.get('native_task_id'):
                if any(context.get(key) != value for key, value in native.items()):
                    raise ValueError('native_task_association_changed')
                return self.view(row)
            if row['status'] != 'pending':
                raise ValueError('pending_native_acceptance_required')
            context.update(native)
            db.execute('UPDATE initiatives SET context=? WHERE id=?', (encoded(context), identifier))
            self.history(db, identifier, contact_id, 'native_task_bound', native)
            return self.view(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())

    def bind_native_run(self, identifier, contact_id, native, *, attempt_count):
        with self.transaction() as db:
            row = self.row(db, identifier, contact_id)
            context = json.loads(row['context'])
            if (context.get('execution_backend') != 'kanban' or
                    any(context.get(key) != native[key] for key in
                        ('source_home_id', 'native_board', 'native_task_id'))):
                raise ValueError('native_task_association_required')
            if row['status'] == 'completed':
                # Sidecar finish may have succeeded before native completion's
                # acknowledgment was lost. Return the saved result for the new
                # native attempt without changing its historical assignment.
                return self.view(row) | {'context': context | native, 'reconcile_only': True}
            if row['status'] not in {'pending', 'assigned', 'acknowledged', 'failed'}:
                raise ValueError('assignment_cancelled_or_superseded')
            if row['status'] == 'assigned' and all(context.get(k) == v for k, v in native.items()):
                return self.view(row)
            context.update(native)
            actor = 'native-kanban:' + str(native['native_run_id'])
            db.execute('''UPDATE initiatives SET status='assigned',assigned_agent_id=?,assigned_at=?,
                last_attempt_at=?,attempt_count=?,context=?,result_metadata='{}',
                failed_at=NULL,failed_reason=NULL WHERE id=?''',
                (actor, stamp(), stamp(), attempt_count, encoded(context), identifier))
            self.history(db, identifier, actor, 'assigned', native)
            return self.view(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())

    def select(self, contact_id, native, terminal):
        """One canonical native fire takes at most one pending assignment.

        Unknown/running native predecessors are never inferred dead from age.
        Only a known transient failure with a definitely failed predecessor can
        reuse the same initiative and attempt history.
        """
        with self.transaction() as db:
            rows = db.execute('''SELECT * FROM initiatives WHERE created_by=? AND source_type=?
                AND entity_id=? AND status IN ('pending','assigned','acknowledged','failed')
                ORDER BY created_at,id''', (CREATOR, SOURCE, contact_id)).fetchall()
            for original in rows:
                row = self.row(db, original['id'], contact_id)
                context = json.loads(row['context'])
                if context.get('execution_backend') == 'kanban':
                    continue
                if row['status'] in {'assigned', 'acknowledged'}:
                    if context.get('native_execution_id') == native['native_execution_id']:
                        return self.view(row)
                    prior = terminal(context)
                    if prior not in {'completed', 'failed'}:
                        continue
                    # The native runner can reconcile its exact saved artifact
                    # before declaring a post-write interruption a failure.
                    return self.view(row) | {'reconcile_only': True}
                if row['status'] == 'failed':
                    result = json.loads(row['result_metadata'] or '{}')
                    if (result.get('error_type') not in TRANSIENT or row['attempt_count'] >= row['max_attempts']
                            or terminal(context) != 'failed'):
                        continue
                    self.history(db, row['id'], 'native-cron:'+native['native_execution_id'], 'retry',
                                 {'previous_context': context, 'previous_result': result})
                elif row['status'] != 'pending':
                    continue
                context.update(native)
                actor = 'native-cron:'+native['native_execution_id']
                db.execute("""UPDATE initiatives SET status='assigned',assigned_agent_id=?,assigned_at=?,
                    last_attempt_at=?,attempt_count=attempt_count+1,context=?,result_metadata='{}',
                    failed_at=NULL,failed_reason=NULL WHERE id=?""", (actor, stamp(), stamp(), encoded(context), row['id']))
                self.history(db, row['id'], actor, 'assigned', native)
                return self.view(db.execute('SELECT * FROM initiatives WHERE id=?', (row['id'],)).fetchone())
        return None

    def finish(self, identifier, contact_id, native, result):
        with self.transaction() as db:
            row = self.row(db, identifier, contact_id)
            context = json.loads(row['context'])
            if row['status'] == 'completed' and json.loads(row['result_metadata'] or '{}') == result:
                return self.view(row)
            if (row['status'] != 'assigned'
                    or any(context.get(key) != value for key, value in native.items())):
                raise ValueError('assignment_cancelled_or_superseded')
            success = result['status'] == 'draft_created'
            db.execute('''UPDATE initiatives SET status=?,result=?,result_metadata=?,completed_at=?,
                failed_at=?,failed_reason=? WHERE id=?''',
                ('completed' if success else 'failed', result.get('summary'), encoded(result),
                 stamp() if success else None, None if success else stamp(),
                 None if success else result['error_type'], identifier))
            self.history(db, identifier, row['assigned_agent_id'], 'completed' if success else 'failed', result)
            # Completion of a local draft does not settle a broader obligation.
            return self.view(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())

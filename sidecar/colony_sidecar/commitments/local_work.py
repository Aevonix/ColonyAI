"""One accepted local draft class in the existing initiative/commitment ledgers."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
import uuid

CREATOR = 'native_local_work'
SOURCE = 'owner_local_draft'
TRANSIENT = {'TimeoutError', 'APITimeoutError', 'APIConnectionError', 'ConnectError',
             'ReadTimeout', 'RateLimitError', 'ServiceUnavailableError'}
ACTIVE = {'pending', 'assigned', 'acknowledged'}


class LocalWorkConflict(ValueError):
    def __init__(self, reason, initiative_id):
        super().__init__(reason)
        self.initiative_id = initiative_id


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def retryable_legacy(row):
    return (row['status'] == 'failed'
            and json.loads(row['context']).get('execution_backend', 'cron') != 'kanban'
            and json.loads(row['result_metadata'] or '{}').get('error_type') in TRANSIENT
            and row['attempt_count'] < row['max_attempts'])


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
               execution_backend='cron', origin=None, new_draft=False, handoff=None):
        material = {'commitment_id': commitment_id, 'question': question, 'sources': sources}
        legacy_key = SOURCE + ':' + hashlib.sha256(encoded({**material,
            'session_id': session_id, 'turn_id': turn_id}).encode()).hexdigest()
        key = SOURCE + ':' + hashlib.sha256(encoded({**material,
            'contact_id': contact_id, 'principal_id': principal_id,
            'session_id': session_id, 'turn_id': turn_id, 'new_draft': new_draft}).encode()).hexdigest()
        handoff_hash = hashlib.sha256(encoded(handoff).encode()).hexdigest() if handoff else ''
        with self.transaction() as db:
            # Each ordinary acceptance keeps its association across subsequent
            # fresh drafts, including a caller whose first response was lost.
            db.execute('''CREATE TABLE IF NOT EXISTS local_draft_acceptances (
                acceptance_key TEXT PRIMARY KEY, initiative_id TEXT NOT NULL,
                handoff_hash TEXT NOT NULL DEFAULT '')''')
            replay = db.execute('SELECT * FROM local_draft_acceptances WHERE acceptance_key=?', (key,)).fetchone()
            if replay:
                row = self.row(db, replay['initiative_id'], contact_id)
                released = False
                if handoff:
                    if replay['handoff_hash'] != handoff_hash:
                        raise ValueError('accepting_undertaking_superseded')
                    # The stores use WAL: an attached transaction does not
                    # promise host-crash atomicity across their two files.
                    # A saved association records release intent, so reconcile
                    # its exact old token before confirming a replayed handoff.
                    released = self.release_handoff(db, commitment_id, contact_id,
                        principal_id, handoff, creating=False, replay=True)
                return self.acceptance_view(row, material, released)

            if commitment_id is not None:
                obligation = self.obligation(db, commitment_id, contact_id)
                if obligation['status'] not in {'pending', 'overdue'}:
                    raise ValueError('obligation_closed')
            elif handoff:
                raise ValueError('handoff_requires_commitment')

            # Preserve request replay for installed predecessor records. A
            # newly explicit redraft has no predecessor request equivalent.
            previous = None if new_draft else db.execute(
                'SELECT * FROM initiatives WHERE dedup_key=? AND entity_id=?',
                (legacy_key, contact_id)).fetchone()
            if previous is None and commitment_id is not None:
                rows = db.execute('''SELECT * FROM initiatives WHERE created_by=?
                    AND source_type=? AND source_id=? AND entity_id=? ORDER BY rowid DESC''',
                    (CREATOR, SOURCE, commitment_id, contact_id)).fetchall()
                active = [row for row in rows if row['status'] in ACTIVE or retryable_legacy(row)]
                if len(active) > 1:
                    raise LocalWorkConflict('multiple_active_local_drafts', active[0]['id'])
                if active:
                    if new_draft:
                        raise LocalWorkConflict('local_draft_in_progress', active[0]['id'])
                    # One active undertaking per explicit commitment, including
                    # paraphrased requests and reordered source selections.
                    previous = active[0]
                elif rows and not new_draft:
                    previous = rows[0]
                    old = json.loads(previous['context'])
                    if any(old.get(field) != value for field, value in material.items()):
                        raise LocalWorkConflict('local_draft_scope_changed_requires_new_draft', previous['id'])

            released = self.release_handoff(db, commitment_id, contact_id, principal_id,
                handoff, creating=previous is None)
            if previous is not None:
                db.execute('INSERT INTO local_draft_acceptances VALUES(?,?,?)',
                           (key, previous['id'], handoff_hash if released else ''))
                self.history(db, previous['id'], principal_id, 'acceptance_joined',
                    {'session_id': session_id, 'turn_id': turn_id})
                return self.acceptance_view(previous, material, released)
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
            db.execute('INSERT INTO local_draft_acceptances VALUES(?,?,?)',
                       (key, identifier, handoff_hash if released else ''))
            return self.acceptance_view(db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone(),
                                        material, released)

    def acceptance_view(self, row, material, released):
        value = self.view(row)
        return value | {'acceptance_matches_request': all(value['context'].get(key) == item
                    for key, item in material.items()), 'handoff_released': released}

    @staticmethod
    def release_handoff(db, commitment_id, contact_id, principal_id, handoff, *, creating,
                        replay=False):
        exists = db.execute("SELECT 1 FROM obligations.sqlite_master WHERE type='table' AND name='commitment_work'").fetchone()
        row = db.execute('SELECT * FROM obligations.commitment_work WHERE commitment_id=?',
                         (commitment_id,)).fetchone() if exists and commitment_id else None
        if handoff:
            if replay and (row is None or row['claim_id'] != handoff['claim_id']):
                # The old token is no longer held. Never release a newer worker.
                return True
            if (row is None or row['principal_id'] != principal_id or row['contact_id'] != contact_id
                    or any(row[key] != value for key, value in handoff.items())):
                raise ValueError('accepting_undertaking_superseded')
            if row['state'] == 'released':
                # Inverse partial commit: the exact old lease was released but
                # its acceptance mapping may be missing. Recover either a new
                # draft or an association with the canonical existing draft.
                # Exact holder/token checks above exclude any newer claimant.
                return True
            if row['state'] != 'held':
                raise ValueError('accepting_undertaking_superseded')
            now = time.time()
            db.execute("UPDATE obligations.commitment_work SET state='released',last_observed_at=?,lease_until=? WHERE commitment_id=?",
                       (now, now, commitment_id))
            return True
        if creating and row is not None and row['state'] == 'held' and row['lease_until'] > time.time():
            raise ValueError('undertaking_held_elsewhere')
        return False

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
                        if row['status'] != 'failed' or retryable_legacy(row):
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
                    if not retryable_legacy(row) or terminal(context) != 'failed':
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

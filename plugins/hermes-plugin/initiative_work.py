"""Selected internal review handoff using the existing native Kanban lifecycle."""
from contextlib import closing
import hashlib
import json
import os
from urllib.parse import quote

from .local_work import request

PREFIX = 'pacomind-initiative:'
ROOT = '/v1/host/initiative-work'


class NativeReviews:
    root = ROOT
    prefix = PREFIX
    creator = 'pacomind-initiative'

    def __init__(self, client, owner, config=None):
        self.client, self.owner = client, owner
        self.config = config or {}

    def worker_profile(self, home):
        if self.creator != 'pacomind-initiative':
            return 'default'
        from .review_worker import refresh_profile
        return refresh_profile(self.config, home, self.owner)

    @classmethod
    def path(cls, identifier):
        return cls.root+'/'+quote(identifier, safe='')

    def work(self, identifier):
        from hermes_cli import kanban_db as kb
        try:
            from hermes_cli.kanban_db_connect import connect
        except ModuleNotFoundError as error:
            if error.name != 'hermes_cli.kanban_db_connect':
                raise
            # Only 0.21.0 lacks the sibling module. Keep its lookup local to
            # this branch; newer native scanners inspect imports statically.
            connect = kb.connect
        value = request(self.client, self.path(identifier)+'?contact_id='+quote(self.owner, safe=''))
        if self.terminal(value):
            return self.on_terminal(identifier, value, kb, connect)
        home = kb.kanban_home().resolve()
        expected_profile = 'pacomind-reviews' if self.creator == 'pacomind-initiative' else 'default'
        if (value['execution'] != {'native_board': 'default', 'worker_profile': expected_profile,
                                   'source_home_id': hashlib.sha256(str(home).encode()).hexdigest()}
                or kb.kanban_db_path(board='default').resolve() != home/'kanban.db'):
            raise ValueError('selected_native_review_home_required')
        review = value['review']
        if hashlib.sha256(review['body'].encode()).hexdigest() != review['sha256']:
            raise ValueError('review_contract_mismatch')
        # Old already-run reviews remain observable, but cannot acquire another
        # unrestricted run. New work requires the managed read-only profile.
        with closing(connect(board='default')) as db:
            prior = db.execute('SELECT id FROM tasks WHERE idempotency_key=?',
                               (self.prefix+identifier,)).fetchone()
            if prior:
                task = kb.get_task(db, prior['id'])
                if task.assignee == 'default':
                    if (task.created_by != self.creator or task.tenant != self.owner
                            or task.body != review['body']):
                        raise ValueError('unrestricted_native_review_cannot_dispatch')
                    if task.status == 'ready':
                        kb.block_task(db, task.id, reason='A bounded read-only review worker is required',
                                      kind='needs_input')
                    if self.creator == 'pacomind-initiative' and kb.latest_run(db, task.id) is None:
                        raise ValueError('unrestricted_native_review_cannot_dispatch')
                    return request(self.client, self.path(identifier)+'/observe', {
                        'contact_id': self.owner, 'native_board': 'default',
                        'native_task_id': task.id, 'contract_sha256': review['sha256']})
                if (task.assignee == expected_profile and task.created_by == self.creator
                        and task.tenant == self.owner and task.body == review['body']
                        and task.status in {'running', 'blocked', 'done', 'archived'}
                        and kb.latest_run(db, task.id) is not None):
                    # Reconciliation observes existing work. Only a future
                    # dispatch needs a fresh role/configuration readiness probe.
                    return request(self.client, self.path(identifier)+'/observe', {
                        'contact_id': self.owner, 'native_board': 'default',
                        'native_task_id': task.id, 'contract_sha256': review['sha256']})
        profile = self.worker_profile(home)
        with closing(connect(board='default')) as db:
            with kb.write_txn(db):
                row = db.execute('SELECT id FROM tasks WHERE idempotency_key=? ORDER BY created_at LIMIT 1',
                                 (self.prefix+identifier,)).fetchone()
                task_id = row['id'] if row else kb.create_task(db,
                    title=review['title'], body=review['body'], assignee=profile,
                    created_by=self.creator, tenant=self.owner, idempotency_key=self.prefix+identifier,
                    board='default', initial_status='blocked', workspace_kind='scratch',
                    goal_mode=False, max_runtime_seconds=480, max_retries=1)
                task = kb.get_task(db, task_id)
                if (task.created_by != self.creator or task.tenant != self.owner
                        or task.assignee != profile or task.body != review['body']):
                    raise ValueError('native_review_association_changed')
            binding = {'contact_id': self.owner, 'native_board': 'default',
                       'native_task_id': task_id, 'contract_sha256': review['sha256']}
            # A failed or lost attachment leaves one blocked task recoverable
            # by the same idempotency key, never a second running undertaking.
            request(self.client, self.path(identifier)+'/native-task', binding)
            task = kb.get_task(db, task_id)
            if not self.prepare(identifier, binding, task, db, kb):
                return request(self.client, self.path(identifier)+'/observe', binding)
            if task.status == 'blocked' and kb.latest_run(db, task_id) is None:
                ok, reason = kb.promote_task(db, task_id, actor=self.creator,
                                            reason='Registered internal review associated')
                if not ok and kb.get_task(db, task_id).status not in {'ready', 'running', 'done', 'archived'}:
                    raise ValueError(reason)
        return request(self.client, self.path(identifier)+'/observe', binding)

    @staticmethod
    def terminal(value):
        return value['status'] in {'completed', 'cancelled'}

    def on_terminal(self, identifier, value, kb, connect):
        return value

    def prepare(self, identifier, binding, task, db, kb):
        return True

    def handle(self, args, scope):
        if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
                or scope.contact_id != self.owner or scope.platform in {'subagent', 'background_review'}
                or not scope.turn_id or set(args) != {'initiative_id'}
                or not isinstance(args['initiative_id'], str) or not args['initiative_id']):
            return json.dumps({'error': 'An attested owner or system review turn is required'})
        try:
            return json.dumps(self.work(args['initiative_id']))
        except Exception as error:
            response = getattr(error, 'response', None)
            detail = response.json().get('detail') if response is not None and response.status_code == 409 else None
            return json.dumps({'error': detail or type(error).__name__,
                               'initiative_id': args['initiative_id'], 'execution_confirmed': False})

    def reconcile(self, **kwargs):
        # The native tick handles bookkeeping. Explicitly enabled reviews also
        # discover a bounded batch of server-eligible proposals, without an LLM
        # steward. Follow-ups keep their existing reconciliation contract.
        if kwargs.get('dry_run') or kwargs.get('board') != 'default' or os.environ.get('HERMES_KANBAN_TASK'):
            return
        query = '?contact_id='+quote(self.owner, safe='')
        if self.creator == 'pacomind-initiative' and self.config.get('enabled') is True:
            query += '&discover=true'
        result = request(self.client, self.root+query)
        for item in result['items']:
            try:
                self.work(item.get('id') or item['wait_id'])
            except ValueError as error:
                if str(error) != 'readonly_followup_worker_unqualified':
                    raise


class NativeFollowups(NativeReviews):
    """Existing tick prepares one local review for a durable due reply wait.

    Sidecar preparation must be checked again in the worker tool/send path.
    Preparing a review grants no outward effect; the existing outbox alone
    sends under task-scoped authority. This adapter never retries a send.
    """
    root = '/v1/host/temporal-followups'
    prefix = 'pacomind-followup:'
    creator = 'pacomind-followup'

    def worker_profile(self, home):
        # This path previously shared the unrestricted default-profile review
        # worker. Keep terminal reconciliation, but do not dispatch outreach
        # reviews until bounded reports are integrated with the existing outbox.
        raise ValueError('readonly_followup_worker_unqualified')

    @staticmethod
    def terminal(value):
        return value.get('state') in {'resolved', 'cancelled', 'expired'} or value.get('status') in {'completed', 'cancelled', 'failed'}

    def on_terminal(self, identifier, value, kb, connect):
        if value.get('native_terminal_observed'):
            return value
        home = kb.kanban_home().resolve()
        if (value['execution'] != {'native_board': 'default', 'worker_profile': 'default',
                                   'source_home_id': hashlib.sha256(str(home).encode()).hexdigest()}
                or kb.kanban_db_path(board='default').resolve() != home/'kanban.db'):
            raise ValueError('selected_native_followup_home_required')
        with closing(connect(board='default')) as db:
            row = db.execute('SELECT id FROM tasks WHERE idempotency_key=?', (self.prefix+identifier,)).fetchone()
            if row:
                task = kb.get_task(db, row['id'])
                if task.created_by != self.creator or task.tenant != self.owner:
                    raise ValueError('native_followup_association_changed')
                # A running review is not claimed stopped by clearing a DB
                # lease. Its next authoritative worker/send check rejects it.
                if task.status in {'blocked', 'ready', 'todo'}:
                    kb.archive_task(db, task.id)
                binding = {'contact_id': self.owner, 'native_board': 'default',
                           'native_task_id': task.id, 'contract_sha256': value['review']['sha256']}
                return request(self.client, self.path(identifier)+'/observe', binding)
        return value

    def prepare(self, identifier, binding, task, db, kb):
        result = request(self.client, self.path(identifier)+'/prepare', binding)
        if result.get('review_allowed'):
            return True
        if task.status in {'blocked', 'ready', 'todo'}:
            if result.get('reason') in {'resolved', 'cancelled', 'expired', 'reply_observed'}:
                kb.archive_task(db, task.id)
            elif task.status == 'ready':
                kb.block_task(db, task.id, reason='Reply review currently ineligible')
        return False

    def handle(self, args, scope):
        # Registration is a host integration decision; do not accidentally
        # expose a second model-callable authority path via inherited handler.
        return json.dumps({'error': 'Follow-up work is driven by existing dispatch reconciliation'})

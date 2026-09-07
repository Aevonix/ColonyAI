"""Selected internal review handoff using the existing native Kanban lifecycle."""
from contextlib import closing
import hashlib
import json
import os
from urllib.parse import quote

from .local_work import request

PREFIX = 'colony-initiative:'
ROOT = '/v1/host/initiative-work'


class NativeReviews:
    def __init__(self, client, owner):
        self.client, self.owner = client, owner

    @staticmethod
    def path(identifier):
        return ROOT+'/'+quote(identifier, safe='')

    def work(self, identifier):
        from hermes_cli import kanban_db as kb
        value = request(self.client, self.path(identifier)+'?contact_id='+quote(self.owner, safe=''))
        if value['status'] in {'completed', 'cancelled'}:
            return value
        home = kb.kanban_home().resolve()
        if (value['execution'] != {'native_board': 'default', 'worker_profile': 'default',
                                   'source_home_id': hashlib.sha256(str(home).encode()).hexdigest()}
                or kb.kanban_db_path(board='default').resolve() != home/'kanban.db'):
            raise ValueError('selected_native_review_home_required')
        review = value['review']
        if hashlib.sha256(review['body'].encode()).hexdigest() != review['sha256']:
            raise ValueError('review_contract_mismatch')
        # Use the same default profile and selected root home as ordinary
        # native goal tools. No profile, model override or notifier is created.
        with closing(kb.connect(board='default')) as db:
            with kb.write_txn(db):
                row = db.execute('SELECT id FROM tasks WHERE idempotency_key=? ORDER BY created_at LIMIT 1',
                                 (PREFIX+identifier,)).fetchone()
                task_id = row['id'] if row else kb.create_task(db,
                    title=review['title'], body=review['body'], assignee='default',
                    created_by='colony-initiative', tenant=self.owner, idempotency_key=PREFIX+identifier,
                    board='default', initial_status='blocked', workspace_kind='scratch',
                    goal_mode=True, goal_max_turns=4, max_runtime_seconds=480, max_retries=1)
                task = kb.get_task(db, task_id)
                if (task.created_by != 'colony-initiative' or task.tenant != self.owner
                        or task.assignee != 'default' or task.body != review['body']):
                    raise ValueError('native_review_association_changed')
            binding = {'contact_id': self.owner, 'native_board': 'default',
                       'native_task_id': task_id, 'contract_sha256': review['sha256']}
            # A failed or lost attachment leaves one blocked task recoverable
            # by the same idempotency key, never a second running undertaking.
            request(self.client, self.path(identifier)+'/native-task', binding)
            task = kb.get_task(db, task_id)
            if task.status == 'blocked' and kb.latest_run(db, task_id) is None:
                ok, reason = kb.promote_task(db, task_id, actor='colony-initiative',
                                            reason='Registered internal review associated')
                if not ok and kb.get_task(db, task_id).status not in {'ready', 'running', 'done', 'archived'}:
                    raise ValueError(reason)
        return request(self.client, self.path(identifier)+'/observe', binding)

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
        # The existing dispatch tick reconciles bound tasks only. Proposal
        # selection remains with the existing steward's attested review tool.
        if kwargs.get('dry_run') or kwargs.get('board') != 'default' or os.environ.get('HERMES_KANBAN_TASK'):
            return
        result = request(self.client, ROOT+'?contact_id='+quote(self.owner, safe=''))
        for item in result['items']:
            self.work(item['id'])

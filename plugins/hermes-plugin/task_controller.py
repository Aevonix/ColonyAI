"""Native text task tools backed by one registered gateway adapter.

The controller retains associations and schedules calls onto that adapter's
existing event loop. It owns no executor, listener, scheduler or agent process.
"""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .client import PrivateSQLitePath
from .task_handoffs import TaskHandoffError, TaskHandoffs, erase_task_handoffs
from .task_sources import NativeTaskSources


TOOL_SCHEMA = {
    'name': 'colony_task',
    'description': (
        'Run an accepted task in the background while this conversation continues. '
        'Submit a bounded request; inspect, steer or stop the returned task_id from '
        'another conversation belonging to the same owner. Results are retained '
        'for inspection; acceptance is not completion or an outward delivery. '
        'Use normal conversation for questions and native delegation for child work.'),
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'operation': {'type': 'string', 'enum': ['submit', 'status', 'steer', 'stop', 'list']},
            'request': {'type': 'string', 'minLength': 1, 'maxLength': 32768},
            'task_id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        },
        'required': ['operation'],
    },
}


class NativeTasks:
    def __init__(self, client, outbox, owner_contact_id, *, state_path,
                 attested_system_platforms=('cli',)):
        self.path = Path(state_path).expanduser()
        if self.path.resolve() == outbox.path.resolve():
            raise ValueError('Task associations cannot replace the turn outbox schema')
        self.storage = PrivateSQLitePath(self.path)
        self.sources = NativeTaskSources(client, outbox, owner_contact_id,
            attested_system_platforms=attested_system_platforms,
            erase=lambda contact, rules: erase_task_handoffs(self.database, contact, rules))
        self.handoffs = TaskHandoffs(self.database, self.sources.resolve_source, self.sources.resolve_owner)
        self.owner = owner_contact_id
        self.adapter = None
        self._draining = False
        self._pending_after = None
        self._updates_after = None

    @contextmanager
    def database(self):
        db, identity = self.storage.connect(timeout_seconds=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA fullfsync=ON')
        try:
            with db:
                yield db
            self.storage.assert_current(identity)
        finally:
            db.close()

    def create_adapter(self, config):
        from .native_task_platform import NativeTaskAdapter
        controller = self
        class ConnectedTaskAdapter(NativeTaskAdapter):
            @property
            def authorization_is_upstream(adapter):
                # This adapter has no external ingress. The source controller
                # and correlated handler authenticate the retained real owner
                # before native dispatch, including recovery. Hermes' existing
                # trusted-upstream contract avoids treating a canonical task
                # owner as an unrelated external-channel allowlist entry.
                # Transport subclasses retain the base default policy.
                return True

            async def connect(adapter, *, is_reconnect=False):
                connected = await super().connect(is_reconnect=is_reconnect)
                controller.adapter = adapter
                await controller.reconcile()
                return connected

            async def disconnect(adapter):
                if controller.adapter is adapter:
                    controller.adapter = None
                await super().disconnect()

        return ConnectedTaskAdapter(config, handoffs=self.handoffs)

    def bind_native_turn(self, **kwargs):
        from .native_task_platform import ACTIVE, bind_native_turn
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return bind_native_turn(**kwargs)

    def finish_native_turn(self, **kwargs):
        from .native_task_platform import ACTIVE, finish_native_turn
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return finish_native_turn(**kwargs)

    def native_scope_fields(self, **kwargs):
        """Project the retained real sender into this exact native task turn.

        The adapter's correlated handler establishes ACTIVE after source and
        ownership checks. A synthetic platform name or sender argument alone
        cannot establish authority. Retain the original handle for the existing
        per-tool participant revalidation, including joined child turns.
        """
        from .native_task_platform import ACTIVE
        active = ACTIVE.get()
        exact = {key: str(kwargs.get(key) or '') for key in ('session_id', 'task_id', 'turn_id')}
        if (active is None or active['adapter'] is not self.adapter
                or active['handoffs'] is not self.handoffs or not all(exact.values())
                or active.get('native') != exact or kwargs.get('parent_session_id')
                or kwargs.get('platform') != self.adapter.platform.value):
            raise TaskHandoffError('The exact native task source is unavailable')
        row = self.handoffs.control(active['id'])
        if any(row['native_' + key] != value for key, value in exact.items()):
            raise TaskHandoffError('The native task generation changed')
        owner = self.sources.resolve_owner(row['source'], require_task_grant=True)
        if kwargs.get('sender_id') != owner:
            raise TaskHandoffError('The native task sender does not match its owner')
        origin = row['source']['origin']
        local = origin['platform'] in self.sources.attested_system_platforms
        return {'sender_id': origin['sender_id'], 'contact_id': owner,
                'authority_lane': 'system' if local else 'owner',
                'resolution_status': 'attested_system' if local else 'resolved',
                'authority_gateway': origin['authority_gateway']}

    def _call(self, action, identity, *, update_id=None):
        adapter = self.adapter
        if (adapter is None or adapter.loop is None or not adapter.loop.is_running()
                or adapter.dispatch_context is None):
            return None
        try:
            same_loop = asyncio.get_running_loop() is adapter.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            raise TaskHandoffError('Native task tools must run outside the gateway event loop')
        # Tool handlers run off the gateway loop. A bounded wait never cancels
        # half-admitted native work; the retained ID resolves an ambiguous result.
        payload = {'handoff_id': identity, 'action': action}
        if update_id is not None:
            payload['update_id'] = update_id
        # This is an independent root. Copy the connected gateway/profile
        # context, not the foreground tool's managed Relay callback ancestry.
        # Its owner and source lineage come from the retained handoff instead.
        future = adapter.dispatch_context.copy().run(asyncio.run_coroutine_threadsafe,
            adapter.dispatch_http_event(payload), adapter.loop)
        try:
            return future.result(timeout=2)
        except FutureTimeout:
            return None

    async def reconcile(self):
        """Retry bounded retained admissions on connect or an existing native tick."""
        adapter = self.adapter
        if adapter is None or self._draining:
            return
        self._draining = True
        try:
            ids = await asyncio.to_thread(self.handoffs.pending, 4, after=self._pending_after)
            self._pending_after = ids[-1] if ids else None
            for identity in ids:
                try:
                    row = await asyncio.to_thread(self.handoffs.control, identity)
                    await adapter.dispatch_http_event({'handoff_id': identity,
                        'action': 'stop' if row['stop'] else 'submit'})
                except Exception:
                    # Keep the same row and its stop intent. Native liveness is
                    # not inferred from this failed transport attempt.
                    continue
            updates = await asyncio.to_thread(self.handoffs.pending_updates, 4, after=self._updates_after)
            self._updates_after = updates[-1]['id'] if updates else None
            for update in updates:
                try:
                    await adapter.steer(update['handoff_id'], update['id'])
                except Exception:
                    continue
        finally:
            self._draining = False

    def reconcile_pending(self, **kwargs):
        if kwargs.get('dry_run') or kwargs.get('board') not in (None, 'default') or os.environ.get('HERMES_KANBAN_TASK'):
            return
        adapter = self.adapter
        if (adapter is not None and adapter.loop is not None and adapter.loop.is_running()
                and adapter.dispatch_context is not None):
            adapter.dispatch_context.copy().run(asyncio.run_coroutine_threadsafe,
                self.reconcile(), adapter.loop)

    @staticmethod
    def _metadata(row):
        result = {'task_id': row['id'], 'executor': 'native_hermes',
                  **{key: row[key] for key in ('native_session_id', 'native_task_id', 'native_turn_id')}}
        if row['response']:
            return {**result, 'status': 'done', 'delivery': 'unobserved'}
        stopped = TaskHandoffs.stop_view(row)
        return {**result, **(stopped or {'status': 'unknown', 'reason': 'native_liveness_unobserved'})}

    def handle(self, args, scope):
        identity = None
        try:
            if (not isinstance(args, dict) or scope is None or not scope.valid_participant
                    or scope.contact_id != self.owner or scope.authority_lane not in {'owner', 'system'}):
                raise TaskHandoffError('An attested owner conversation is required')
            operation = args.get('operation')
            expected = {'operation'} | ({'request'} if operation == 'submit' else
                {'task_id', 'request'} if operation == 'steer' else {'task_id'} if operation in {'status', 'stop'} else set())
            if set(args) != expected or operation not in {'submit', 'status', 'steer', 'stop', 'list'}:
                raise TaskHandoffError('Use one task operation with its exact fields')
            if operation == 'list':
                items = []
                for row in self.handoffs.recent(contact_id=self.owner):
                    self.sources.authorize_control(row['source'], scope)
                    items.append(self._metadata(row))
                return json.dumps({'items': items, 'view': 'retained_associations', 'complete_running_inventory': False})
            if operation == 'submit':
                adapter = self.adapter
                if adapter is None or adapter.loop is None or not adapter.loop.is_running():
                    raise TaskHandoffError('The native task gateway is not connected')
                source = self.sources.capture(scope)
                request_id = hashlib.sha256(json.dumps([args['request'], {
                    key: value for key, value in source.items() if key != 'watermark'}],
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                row = self.handoffs.admit(request_id=request_id, request=args['request'], source_input=source)
                identity = row['id']
                observed = self._call('submit', identity)
                return json.dumps({'task_id': identity, 'accepted': True, 'executor': 'native_hermes',
                    'native_admission': observed, 'callback_observed': observed is not None,
                    'status': 'queued', 'delivery': 'unobserved'})
            identity = args['task_id']
            row = self.handoffs.get(identity)
            self.sources.authorize_control(row['source'], scope)
            if operation == 'stop':
                retained = self.handoffs.request_stop(identity)
                if retained['response']:
                    return json.dumps({'task_id': identity, 'status': 'done', 'stop_requested': False})
                observed = self._call('stop', identity)
                return json.dumps({**self._metadata(self.handoffs.control(identity)),
                    'stop_requested': True, 'native_observation': observed})
            if operation == 'steer':
                source = self.sources.capture(scope)
                update = self.handoffs.admit_update(identity, instruction=args['request'],
                    source_input=source, principal=source['principal'])
                if update is None:
                    return json.dumps({**self._metadata(self.handoffs.control(identity)),
                        'accepted': False, 'reason': 'task_stopped_or_native_turn_terminal'})
                observed = self._call('steer', identity, update_id=update['id'])
                return json.dumps({'task_id': identity, 'executor': 'native_hermes',
                    **self.handoffs.update_view(self.handoffs.get_update(identity, update['id'])),
                    'native_observation': observed})
            observed = self._call('status', identity)
            if observed is not None:
                return json.dumps({'task_id': identity, 'executor': 'native_hermes', **observed})
            row, _ = self.handoffs.resolve(identity)
            if row['response']:
                return json.dumps({'task_id': identity, 'status': 'done', 'result': row['response']['text'],
                    'source_dependencies': row['response']['source_dependencies'],
                    'delivery': {'retained': True, 'outward': 'unobserved'}})
            return json.dumps(self._metadata(row))
        except Exception as error:
            # Retain a known association in the error so an ambiguous native
            # dispatch can be inspected, rather than submitted as another task.
            return json.dumps({'error': str(error) if isinstance(error, (TaskHandoffError, ValueError))
                else type(error).__name__, **({'task_id': identity} if identity else {}),
                'outcome': 'unconfirmed'})

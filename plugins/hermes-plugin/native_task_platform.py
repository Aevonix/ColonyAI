"""Native task execution through the existing Hermes gateway adapter boundary.

The gateway owns tasks, interruption, generation fencing and crash recovery.
Transport subclasses may retain their own authentication and delivery policy.
The generic adapter accepts in-process control only and retains result text.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from .task_handoffs import TaskHandoffError


PLATFORM = 'colony_task'
TASK_ROLE_METADATA = 'colony_task_model_role'
ACTIVE = ContextVar('colony_native_task_handler', default=None)
CONTROL = ContextVar('colony_native_task_control', default=None)
CONTROL_UPDATE = ContextVar('colony_native_task_control_update', default=None)
TASK_DELIVERY_CONTEXT = (
    'This is an accepted background task. Keep the original requested scope and '
    'return a concise result, retaining material uncertainty, failures and required '
    'conditions. Result retention is not an outward message or delivery receipt. '
    'Follow the profile authority and communication rules; this task grants no '
    'additional tools or recipients.'
)



def bind_native_turn(**kwargs):
    active = ACTIVE.get()
    if (active is None or kwargs.get('platform') != active['adapter'].platform.value
            or kwargs.get('parent_session_id')):
        return None
    active['handoffs'].bind(active['id'], kwargs)
    active['native'] = {key: kwargs[key] for key in ('session_id', 'task_id', 'turn_id')}
    return {'context': active['adapter'].delivery_context + active['adapter']._retained_update_context(
        active['id'], active['supplied'])}


def finish_native_turn(**kwargs):
    active = ACTIVE.get()
    if active is not None and kwargs.get('platform') == active['adapter'].platform.value:
        active['handoffs'].observe_terminal(active['id'], kwargs)


class NativeTaskAdapter(BasePlatformAdapter):
    """A native task channel; execution remains in the gateway."""

    interactive_resume = False
    # Match the existing one-shot worker's joined-child lifetime. A custom
    # platform has no api_server self-post origin in the selected Hermes.
    # Detached child provenance needs a separate lifecycle before enabling it.
    supports_async_delivery = False
    # Result formatting and outward delivery belong to the consuming channel.
    MAX_MESSAGE_LENGTH = 1000000
    SUPPORTS_MESSAGE_EDITING = False
    SUPPORTS_NATIVE_STREAMING = False

    def __init__(self, config, *, handoffs, platform_name=PLATFORM,
                 error_type=TaskHandoffError, delivery_context=TASK_DELIVERY_CONTEXT,
                 task_role_metadata=TASK_ROLE_METADATA):
        super().__init__(config, Platform(platform_name))
        self.handoffs = handoffs
        self._error = error_type
        self.delivery_context = delivery_context
        self.task_role_metadata = task_role_metadata
        self._submitting = set()
        self._control_authorization = None
        self._control_lock = asyncio.Lock()
        self._active_inputs = {}
        self.loop = None

    def set_authorization_check(self, callback):
        # Hermes supplies the current platform/profile sender policy through
        # this supported adapter registration boundary.
        self._control_authorization = callback
        return super().set_authorization_check(callback)

    def _check_stop_authorization(self, source):
        return self._check_control_authorization(source, 'stop')

    def _check_control_authorization(self, source, command):
        from gateway.slash_access import policy_from_extra
        check = self._control_authorization
        if (check is None or check(source.user_id, source.chat_type, source.chat_id) is not True
                or not policy_from_extra(self.config.extra, 'dm').can_run(source.user_id, command)):
            raise self._error('Native control authorization is unavailable')

    def verify_http_event_request(self, auth_header):
        # The native text tool holds this adapter directly. Merely enabling an
        # API-server platform must not expose its controller to external JSON.
        # A transport subclass may provide its existing authenticated callback.
        return False, 'native_task_inprocess_only'

    async def connect(self, *, is_reconnect=False):
        self.loop = asyncio.get_running_loop()
        self._mark_connected()
        return True

    async def disconnect(self):
        await self.cancel_background_tasks()
        self._mark_disconnected()
        self.loop = None

    async def get_chat_info(self, chat_id):
        resolve = self.handoffs.control if CONTROL.get() == chat_id else self.handoffs.resolve
        await asyncio.to_thread(resolve, chat_id)
        return {'name': 'Accepted background work', 'type': 'dm'}

    def _task_model_role(self, retained=None):
        """One explicit native projection; credentials remain native-owned."""
        selected = retained if retained is not None else self.config.extra.get('task_model_role')
        if selected is None:
            return None
        if (not isinstance(selected, dict) or set(selected) != {'role', 'provider', 'model'}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 256
                       for value in selected.values())
                or selected['provider'] in {'auto', 'custom'}):
            raise self._error('Native task model role is invalid')
        from hermes_cli.config import load_config_readonly
        providers = load_config_readonly().get('providers') or {}
        if not isinstance(providers, dict) or not isinstance(providers.get(selected['provider']), dict):
            raise self._error('Native task model role provider is unavailable')
        from hermes_cli.runtime_provider import has_named_custom_provider
        if not has_named_custom_provider(selected['provider']):
            raise self._error('Native task model role provider has no enabled native route')
        return dict(selected)

    def _check_native_origin(self, entry, source, key):
        origin = entry.origin
        if (entry.session_key != key or origin is None or origin.platform != source.platform
                or origin.chat_id != source.chat_id or origin.user_id != source.user_id
                or (origin.profile or None) != (source.profile or None)):
            raise self._error('Native task origin does not match its handoff')

    async def _entry_has_work(self, store, entry):
        return bool(entry.active_turn_token or entry.resume_pending or entry.suspended
                    or await asyncio.to_thread(store.load_transcript, entry.session_id))

    async def _select_new_task_model(self, store, entry, event):
        """Select only empty admission state, before native execution can start.

        Persist the role snapshot before its native override so either early
        crash leaves the retained callback retryable. Existing session routes,
        transcripts and native recovery keep their own selected processor.
        """
        if entry is not None and entry.model_override:
            return entry
        retained = entry.metadata.get(self.task_role_metadata) if entry is not None else None
        selected = await asyncio.to_thread(self._task_model_role, retained)
        if selected is None:
            return entry
        if store is None:
            raise self._error('Native task role requires the native session store')
        key = self._event_session_key(event)
        if entry is None:
            entry = await asyncio.to_thread(store.get_or_create_session, event.source, touch_activity=False)
            self._check_native_origin(entry, event.source, key)
            # A missing routing index may recover an existing native session.
            if await self._entry_has_work(store, entry) or entry.model_override:
                return entry
            retained = entry.metadata.get(self.task_role_metadata)
            if retained is not None:
                selected = await asyncio.to_thread(self._task_model_role, retained)
        if retained is None:
            await asyncio.to_thread(store.set_session_metadata, key, self.task_role_metadata, selected)
            retained = await asyncio.to_thread(store.get_session_metadata, key, self.task_role_metadata)
            if retained != selected:
                raise self._error('Native task role retention is unconfirmed')
        override = {name: selected[name] for name in ('provider', 'model')}
        await asyncio.to_thread(store.set_model_override, key, override)
        if await asyncio.to_thread(store.get_model_override, key) != override:
            raise self._error('Native task model selection is unconfirmed')
        return entry

    async def status(self, identity):
        try:
            retained = await asyncio.to_thread(self.handoffs.control, identity)
            stopped = self.handoffs.stop_view(retained)
            if stopped:
                return {'handoff_id': identity, **stopped,
                    **{key: retained[key] for key in ('native_session_id', 'native_task_id', 'native_turn_id')}}
            row, resolved = await asyncio.to_thread(self.handoffs.resolve, identity)
        except (TaskHandoffError, self._error):
            return {'handoff_id': identity, 'status': 'unavailable', 'reason': 'source_unavailable'}
        result = {'handoff_id': identity, 'status': 'queued',
                  'native_session_id': row['native_session_id'],
                  'native_task_id': row['native_task_id'], 'native_turn_id': row['native_turn_id']}
        if row['response']:
            return {**result, 'status': 'done', 'result': row['response']['text'],
                    'source_dependencies': row['response']['source_dependencies'],
                    'delivery': {'retained': True, 'playback': 'unobserved'}}
        source = self.build_source(chat_id=identity, chat_type='dm',
            user_id=resolved['contact_id'], message_id=identity)
        store = getattr(self, '_session_store', None)
        if store is not None:
            from gateway.session import build_session_key
            key = build_session_key(source, profile=self._session_key_profile(source))
            entry = await asyncio.to_thread(store.lookup_by_session_key, key)
            if entry is not None:
                origin = entry.origin
                if (origin is None or origin.platform != source.platform or origin.chat_id != identity
                        or origin.user_id != resolved['contact_id']
                        or (origin.profile or None) != (source.profile or None)):
                    return {**result, 'status': 'unavailable', 'reason': 'native_origin_changed'}
                result['native_session_id'] = entry.session_id
                if entry.active_turn_token:
                    result['status'] = 'running'
                elif entry.resume_pending or entry.suspended:
                    result['status'] = 'interrupted'
                else:
                    result['status'] = 'unavailable'
        if result['status'] == 'queued' and identity in self._submitting:
            result['status'] = 'running'
        elif result['status'] == 'queued':
            try:
                selected = await asyncio.to_thread(self._task_model_role)
                if selected is not None and store is None:
                    raise self._error('Native session store is unavailable')
            except (self._error, OSError, ValueError):
                result.update(status='unavailable', reason='task_model_role_unavailable')
        return result

    async def stop(self, identity):
        async with self._control_lock:
            return await self._stop(identity)

    async def _stop(self, identity):
        row = await asyncio.to_thread(self.handoffs.control, identity)
        source = self.build_source(chat_id=identity, chat_type='dm',
            user_id=row['source']['contact_id'], message_id=identity)
        store = getattr(self, '_session_store', None)
        if store is None:
            raise self._error('Native session ownership is unavailable')
        key = self._event_session_key(MessageEvent(text='', source=source))
        entry = await asyncio.to_thread(store.lookup_by_session_key, key)
        if entry is not None:
            self._check_native_origin(entry, source, key)
            if row['native_session_id'] and entry.session_id != row['native_session_id']:
                raise self._error('Native task session no longer matches its handoff')
        row = await asyncio.to_thread(self.handoffs.request_stop, identity)
        if row['response']:
            return {'handoff_id': identity, 'status': 'done', 'stop_requested': False}
        if self.handoffs.stop_view(row)['status'] == 'cancelled':
            return {'handoff_id': identity, **self.handoffs.stop_view(row)}
        if entry is None and identity not in self._submitting:
            await asyncio.to_thread(self.handoffs.observe_stop, identity, admission_blocked=True)
        else:
            # The adapter event is fixed control input, never the retained task
            # text. Hermes owns hard interruption, generation fencing and reap.
            event = MessageEvent(text='/stop', source=source, message_id='stop:' + identity,
                message_type=MessageType.TEXT, allow_gateway_control=True)
            self._check_stop_authorization(source)
            token = CONTROL.set(identity)
            try:
                await self.handle_message(event)
            finally:
                CONTROL.reset(token)
        return await self.status(identity)

    def _register_update(self, supplied, update):
        from .input_provenance import SourceUpdate
        identity, update_id, source = update['handoff_id'], update['id'], update['source']
        value = SourceUpdate(update_id=update_id, contact_id=source['contact_id'],
            instruction=update['instruction'], input_refs=source['input_refs'], source_refs=source['source_refs'])
        return supplied.register_update(value,
            validate=lambda: self.handoffs.update_authorized(identity, update_id),
            observe=lambda observation: self.handoffs.observe_update(identity, update_id, observation))

    def _retained_update_context(self, identity, supplied):
        carriers = []
        for update in self.handoffs.updates(identity):
            if not update['instruction'] or not any(update['observations'].get(stage)
                    for stage in ('middleware_visible', 'native_request_visible')):
                continue
            carriers.append(self._register_update(supplied, update))
        if not carriers:
            return ''
        # A native historical source-read row may correctly be withheld after
        # restart, including the steering text appended to that tool result.
        # Reconstruct consumed task state at the existing channel boundary;
        # never resend /steer or promote a merely pending update.
        return ('\n\nPreviously consumed updates for this same continuing task, in accepted order. '
                'Retain these constraints while continuing; this is state restoration, not a new '
                'delivery or a request to repeat completed actions.\n' + '\n'.join(carriers))

    async def steer(self, identity, update_id):
        async with self._control_lock:
            row, update, resolved = await asyncio.to_thread(self.handoffs.resolve_update, identity, update_id)
            result = {'handoff_id': identity, **self.handoffs.update_view(update)}
            if row['stop'] or row['response'] or row['terminal']:
                return {**result, 'reason': 'task_stopped_or_native_turn_terminal'}
            if update['dispatch'] is not None:
                return {**result, 'replayed': True}  # Never repeat an ambiguous native /steer.
            active = self._active_inputs.get(identity)
            if active is None or not active.get('native'):
                return {**result, 'reason': 'awaiting_native_turn'}
            source = self.build_source(chat_id=identity, chat_type='dm',
                user_id=resolved['contact_id'], message_id='steer:' + update_id)
            self._check_control_authorization(source, 'steer')
            store = getattr(self, '_session_store', None)
            if store is None:
                raise self._error('Native session ownership is unavailable')
            event = MessageEvent(text='', source=source, message_type=MessageType.TEXT)
            entry = await asyncio.to_thread(store.lookup_by_session_key, self._event_session_key(event))
            if entry is None:
                raise self._error('Native session ownership is unavailable')
            self._check_native_origin(entry, source, self._event_session_key(event))
            if (self._active_inputs.get(identity) is not active
                    or active['native']['session_id'] != entry.session_id
                    or active['supplied'].memory_contact(entry.session_id) != resolved['contact_id']):
                return {**result, 'reason': 'awaiting_current_native_scope'}
            carrier = self._register_update(active['supplied'], update)
            if not await asyncio.to_thread(self.handoffs.claim_update_dispatch, identity, update_id):
                return {'handoff_id': identity, **self.handoffs.update_view(
                    await asyncio.to_thread(self.handoffs.get_update, identity, update_id)), 'replayed': True}
            event.text = '/steer ' + carrier
            event.message_id = 'steer:' + update_id
            event.allow_gateway_control = True
            token, update_token = CONTROL.set(identity), CONTROL_UPDATE.set(update_id)
            try:
                await self.handle_message(event)
            finally:
                CONTROL_UPDATE.reset(update_token)
                CONTROL.reset(token)
            return {'handoff_id': identity, **self.handoffs.update_view(
                await asyncio.to_thread(self.handoffs.get_update, identity, update_id))}

    async def dispatch_http_event(self, payload):
        if set(payload) == {'handoff_id', 'action', 'update_id'} and payload.get('action') == 'steer':
            return await self.steer(payload['handoff_id'], payload['update_id'])
        if set(payload) not in ({'handoff_id'}, {'handoff_id', 'action'}):
            raise self._error('The callback accepts a retained handoff ID and action only')
        action = payload.get('action', 'submit')
        if action == 'status':
            return await self.status(payload['handoff_id'])
        if action == 'stop':
            return await self.stop(payload['handoff_id'])
        if action != 'submit':
            raise self._error('Unsupported native task action')
        retained = await asyncio.to_thread(self.handoffs.control, payload['handoff_id'])
        if retained['stop']:
            return {'handoff_id': retained['id'], **self.handoffs.stop_view(retained), 'replayed': True}
        row, resolved = await asyncio.to_thread(self.handoffs.resolve, payload['handoff_id'])
        if row['native_session_id'] or row['response'] or row['id'] in self._submitting:
            return {'handoff_id': row['id'], 'native_observed': bool(row['native_session_id']),
                    'replayed': True}
        source = self.build_source(chat_id=row['id'], chat_type='dm',
            user_id=resolved['contact_id'], message_id=row['id'])
        event = MessageEvent(text=row['request'], source=source, message_id=row['id'],
            message_type=MessageType.TEXT, allow_gateway_control=False)
        # Claim before any native-store await so duplicate callbacks cannot
        # both pass admission. Hermes owns all durable execution state.
        self._submitting.add(row['id'])
        try:
            store = getattr(self, '_session_store', None)
            entry = None
            if store is not None:
                key = self._event_session_key(event)
                entry = await asyncio.to_thread(store.lookup_by_session_key, key)
                if entry is not None:
                    self._check_native_origin(entry, source, key)
                    if await self._entry_has_work(store, entry):
                        return {'handoff_id': row['id'], 'native_observed': True,
                                'native_session_id': entry.session_id, 'replayed': True}
            entry = await self._select_new_task_model(store, entry, event)
            if entry is not None and await self._entry_has_work(store, entry):
                return {'handoff_id': row['id'], 'native_observed': True,
                        'native_session_id': entry.session_id, 'replayed': True}
            current = await asyncio.to_thread(self.handoffs.get, row['id'])
            if current['stop']:
                await asyncio.to_thread(self.handoffs.observe_stop, row['id'], admission_blocked=True)
                return {'handoff_id': row['id'], 'replayed': True, 'stop_requested': True}
            await self.handle_message(event)
        finally:
            if not event._gateway_accepted:
                self._submitting.discard(row['id'])
        return {'handoff_id': row['id'], 'native_admitted': event._gateway_accepted,
                'durable_handoff': True}

    def set_message_handler(self, handler):
        async def correlated(event):
            # Also runs on native startup resume. Source identity is reloaded
            # from the admitted association, never a synthetic event's claim.
            token = None
            try:
                owned = await asyncio.to_thread(self.handoffs.control, event.source.chat_id)
                if event.source.user_id != owned['source']['contact_id'] or event.source.platform != self.platform:
                    raise self._error('Native task participant does not match its owner')
                if (CONTROL.get() == owned['id'] and event.allow_gateway_control
                        and (event.text == '/stop' or (CONTROL_UPDATE.get() and event.text.startswith('/steer ')))):
                    return await handler(event)
                if owned['stop']:
                    # Native startup may have marked the original turn resumable.
                    # Keep the same session, clear only its recovery marker and
                    # never turn a cancelled handoff into another model request.
                    store = getattr(self, '_session_store', None)
                    if store is not None and event.internal:
                        key = self._event_session_key(event)
                        entry = await asyncio.to_thread(store.lookup_by_session_key, key)
                        if entry is not None and entry.resume_pending and entry.session_id == owned['native_session_id']:
                            self._check_native_origin(entry, event.source, key)
                            if await asyncio.to_thread(store.clear_resume_pending, key):
                                await asyncio.to_thread(self.handoffs.observe_stop, owned['id'],
                                    resume_session_id=entry.session_id)
                    await asyncio.to_thread(self.handoffs.observe_stop, owned['id'], admission_blocked=True)
                    return None
                row, resolved = await asyncio.to_thread(self.handoffs.resolve, event.source.chat_id)
                if event.source.user_id != resolved['contact_id'] or event.source.platform != self.platform:
                    raise self._error('Native task participant does not match its source')
                # Keep native auto-continuation semantics for real history.
                # The source-bound task supplies the root recollection query.
                # Only an empty transcript needs its accepted instruction restored.
                store = getattr(self, '_session_store', None)
                recall_query = None
                if event.internal and not event.text and store is not None:
                    entry = await asyncio.to_thread(store.lookup_by_session_key, self._event_session_key(event))
                    if entry is not None and entry.resume_pending:
                        history = await asyncio.to_thread(store.load_transcript, entry.session_id)
                        original = resolved.get('original_input')
                        recall_query = original if isinstance(original, str) and 0 < len(original) <= 32768 else row['request']
                        if not history:
                            event.text = row['request']
                        event.allow_gateway_control = False
                from .input_provenance import transport_input
                refs = [*resolved['source_refs'], *(row['dependencies'] or {}).get('source_refs', [])]
                inputs = [*resolved['input_refs'], *(row['dependencies'] or {}).get('input_refs', [])]
                with transport_input(contact_id=resolved['contact_id'], platform=self.platform.value,
                        input_refs=inputs, source_refs=refs, recall_query=recall_query) as supplied:
                    active = {'handoffs': self.handoffs, 'id': row['id'], 'supplied': supplied, 'adapter': self}
                    token = ACTIVE.set(active)
                    self._active_inputs[row['id']] = active
                    # Inert until the ordinary public hook authenticates/binds
                    # this native root. Rehydrate carriers, never redispatch.
                    for update in await asyncio.to_thread(self.handoffs.updates, row['id']):
                        if update['instruction']:
                            if any(update['observations'].get(stage)
                                    for stage in ('middleware_visible', 'native_request_visible')):
                                # Outside native hooks, whose exceptions are
                                # intentionally isolated by the runtime. A
                                # revoked consumed update cannot start a turn.
                                await asyncio.to_thread(self.handoffs.resolve_update, row['id'], update['id'])
                            self._register_update(supplied, update)
                    response = await handler(event)
                    if (await asyncio.to_thread(self.handoffs.get, row['id']))['stop']:
                        return None
                    if supplied.result is not None:
                        await asyncio.to_thread(self.handoffs.complete_source, row['id'], supplied.result)
                    elif response:
                        raise self._error('Native result has no source receipt')
                    return response
            finally:
                if token is not None:
                    if self._active_inputs.get(event.source.chat_id) is ACTIVE.get():
                        self._active_inputs.pop(event.source.chat_id, None)
                    ACTIVE.reset(token)
                self._submitting.discard(event.source.chat_id)
        return super().set_message_handler(correlated)

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        try:
            if CONTROL.get() == chat_id:
                row = await asyncio.to_thread(self.handoffs.control, chat_id)
                source = self.build_source(chat_id=chat_id, chat_type='dm',
                    user_id=row['source']['contact_id'], message_id=chat_id)
                update_id = CONTROL_UPDATE.get()
                self._check_control_authorization(source, 'steer' if update_id else 'stop')
                if update_id:
                    await asyncio.to_thread(self.handoffs.observe_update, chat_id, update_id,
                        {'stage': 'native_control_acknowledged'})
                else:
                    await asyncio.to_thread(self.handoffs.observe_stop, chat_id, control_acknowledged=True)
                return SendResult(success=True, message_id=('steer:' + update_id) if update_id else ('stop:' + chat_id))
            if (metadata or {}).get('notify') is not True:
                await asyncio.to_thread(self.handoffs.retain_notice, chat_id, content)
                return SendResult(success=True, message_id='notice:' + chat_id)
            await asyncio.to_thread(self.handoffs.retain_reply, chat_id, content)
            return SendResult(success=True, message_id=chat_id)
        except Exception:
            return SendResult(success=False, error='Task reply retention is unconfirmed')

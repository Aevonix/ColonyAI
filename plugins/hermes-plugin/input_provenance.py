"""Host-supplied source parents for one native task, never participant authority.

An authenticated host may already have captured the human input before asking
the native agent to perform a derived task. Keep those exact parents instead
of recording the task instruction as another human statement. The normal
participant resolver, source reader and canonical writer remain authoritative.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import copy
import hashlib
import json
import re
import threading

from .client import redact_source_payload, source_input_erased

_CURRENT = ContextVar('pacomind_supplied_input', default=None)


def _refs(value, digest):
    if not isinstance(value, (list, tuple)) or not 0 < len(value) <= 64:
        raise ValueError('Bounded exact source parents are required')
    result = []
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {'source_id', digest}
                or not isinstance(ref['source_id'], str) or not 1 <= len(ref['source_id']) <= 256
                or not isinstance(ref[digest], str) or not re.fullmatch('[0-9a-f]{64}', ref[digest])):
            raise ValueError('Invalid exact source parent')
        if ref not in result:
            result.append(dict(ref))
    return result


@dataclass(frozen=True, init=False)
class SourceUpdate:
    """An admitted host instruction, never a new participant credential."""

    update_id: str
    contact_id: str
    instruction: str
    _inputs: tuple
    _sources: tuple

    def __init__(self, update_id, contact_id, instruction, input_refs, source_refs=()):
        if (not isinstance(update_id, str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}', update_id)
                or not isinstance(contact_id, str) or not 1 <= len(contact_id) <= 256
                or not isinstance(instruction, str) or not instruction.strip()
                or len(instruction.encode()) > 32768):
            raise ValueError('An exact bounded source update is required')
        inputs = _refs(input_refs, 'input_message_hash')
        sources = _refs(source_refs, 'source_version') if source_refs else []
        for name, value in [('update_id', update_id), ('contact_id', contact_id),
                ('instruction', instruction),
                ('_inputs', tuple((r['source_id'], r['input_message_hash']) for r in inputs)),
                ('_sources', tuple((r['source_id'], r['source_version']) for r in sources))]:
            object.__setattr__(self, name, value)

    @property
    def input_refs(self):
        return [{'source_id': source, 'input_message_hash': digest} for source, digest in self._inputs]

    @property
    def source_refs(self):
        return [{'source_id': source, 'source_version': digest} for source, digest in self._sources]

    def carrier(self):
        # Deterministic so the owning transport can restore its exact retained
        # update after restart. Syntax alone never registers source authority.
        payload = json.dumps([self.update_id, self.contact_id, self.instruction,
            self.input_refs, self.source_refs], sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        stamp = json.dumps({'id': self.update_id, 'sha256': hashlib.sha256(payload.encode()).hexdigest(),
                            'sources': self.source_refs}, sort_keys=True, separators=(',', ':'))
        return '[pacomind-task-update-v1 ' + stamp + ']\n' + self.instruction + '\n[/pacomind-task-update-v1]'


def _request_texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _request_texts(item)
    elif isinstance(value, dict):
        for key in ('content', 'text', 'messages', 'input', 'instructions', 'system', 'output'):
            if key in value:
                yield from _request_texts(value[key])


class SuppliedInput:
    def __init__(self, contact_id, session_id, input_refs, source_refs, *, native_platform=None, recall_query=None):
        if (not isinstance(contact_id, str) or not 1 <= len(contact_id) <= 256
                or (native_platform is None and (not isinstance(session_id, str)
                                                or not 1 <= len(session_id) <= 256))
                or (native_platform is not None and (session_id is not None
                    or not isinstance(native_platform, str) or not 1 <= len(native_platform) <= 64))):
            raise ValueError('Exact participant and native session are required')
        self.contact_id, self.session_id = contact_id, session_id
        if recall_query is not None and (not isinstance(recall_query, str) or not 1 <= len(recall_query) <= 32768):
            raise ValueError('A bounded retained task recall query is required')
        self._native_platform = native_platform
        self._recall_query = recall_query
        self._inputs = _refs(input_refs, 'input_message_hash')
        self._sources = _refs(source_refs, 'source_version') if source_refs else []
        self._lock = threading.Lock()
        self._bound = set()
        self._sessions = {session_id} if session_id else set()
        self._root_sessions = {session_id} if session_id else set()
        self._memory_sessions = set()
        self._closed = self._blocked = False
        self._admitted = False
        self._failure_reason = None
        self._updates = {}
        self._update_observations = {}
        self.result = None

    def register_update(self, update, *, validate, observe=None):
        """Register already admitted same-owner input for this root task.

        The host validates the real channel's current owner before admission.
        ``validate`` rechecks its current local grants; canonical erasure is
        checked by RequestMemory. Callbacks must be bounded and synchronous.
        ``observe`` must return True after its durable receipt is saved;
        failure withholds continuation. Omit it only when the host makes no
        durable-recovery claim. Pre-bind registration is inert until native
        identity binds; it allows retained updates to precede hook execution.
        Registration is not native dispatch/visibility or participant authority.
        """
        if (not isinstance(update, SourceUpdate) or not callable(validate)
                or (observe is not None and not callable(observe))):
            raise ValueError('A typed update and current-owner validator are required')
        with self._lock:
            if self._closed or self._blocked or update.contact_id != self.contact_id:
                raise ValueError('Source update requires its declared active root')
            previous = self._updates.get(update.update_id)
            if previous:
                if previous['update'] != update:
                    raise ValueError('Source update ID was already bound to different input')
                return previous['carrier']
            all_inputs = {tuple(sorted(ref.items())) for ref in self._inputs}
            all_sources = {tuple(sorted(ref.items())) for ref in self._sources}
            for value in [entry['update'] for entry in self._updates.values()] + [update]:
                all_inputs.update(tuple(sorted(ref.items())) for ref in value.input_refs)
                all_sources.update(tuple(sorted(ref.items())) for ref in value.source_refs)
            if (len(self._updates) >= 16 or len(all_inputs) > 64 or len(all_sources) > 64
                    or sum(len(entry['update'].instruction.encode()) for entry in self._updates.values())
                       + len(update.instruction.encode()) > 65536):
                raise ValueError('Active source update bounds exceeded')
            carrier = update.carrier()
            self._updates[update.update_id] = {'update': update, 'carrier': carrier,
                'validate': validate, 'observe': observe, 'admitted': False}
            return carrier

    def request_updates(self, scope, request):
        """Select only registered carriers and already dependent input."""
        with self._lock:
            if not self._updates:
                return []
        texts = tuple(_request_texts(request))
        with self._lock:
            if (scope is None or not scope.valid_participant or scope.contact_id != self.contact_id
                    or tuple(getattr(scope, key, None) for key in ('session_id', 'task_id', 'turn_id')) not in self._bound):
                return []
            return [entry for entry in self._updates.values() if entry['admitted']
                    or any(entry['carrier'] in text for text in texts)]

    def check_updates(self, scope, entries, *, fresh, rules):
        if not entries:
            return True
        allowed = fresh
        for entry in entries:
            update = entry['update']
            try:
                allowed = (entry['validate']() is True and allowed
                    and not any(source_input_erased(ref, rules) for ref in update.input_refs)
                    and redact_source_payload({'contact_id': self.contact_id,
                        'session_id': scope.session_id, 'turn_id': 'source-update-check',
                        'assistant_message': 'dependency-check',
                        'assistant_source_refs': update.source_refs}, rules) is not None)
            except BaseException:
                allowed = False
        with self._lock:
            if self._closed or self._blocked or not allowed:
                self._block('source_update_unavailable')
                return False
            return True

    def admit_updates(self, scope, request, entries):
        """Record parents only after their exact carrier survives request filtering."""
        texts = tuple(_request_texts(request))
        with self._lock:
            if self._closed or self._blocked:
                return
            for entry in entries:
                if not any(entry['carrier'] in text for text in texts):
                    continue
                entry['admitted'] = True
                for destination, refs in [(self._inputs, entry['update'].input_refs),
                                           (self._sources, entry['update'].source_refs)]:
                    destination.extend(ref for ref in refs if ref not in destination)

    def observe_updates(self, scope, request, *, stage):
        if stage not in {'middleware_visible', 'native_request_visible'}:
            raise ValueError('Unknown source update observation boundary')
        with self._lock:
            if not self._updates:
                return True
        texts = tuple(_request_texts(request))
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True).encode()).hexdigest()
        callbacks = []
        with self._lock:
            if (self._closed or self._blocked or scope is None
                    or tuple(getattr(scope, key, None) for key in ('session_id', 'task_id', 'turn_id')) not in self._bound):
                return not (self._closed or self._blocked)
            for entry in self._updates.values():
                if not entry['admitted'] or not any(entry['carrier'] in text for text in texts):
                    continue
                key = (entry['update'].update_id, stage, scope.session_id, scope.task_id, scope.turn_id)
                if key in self._update_observations:
                    continue
                callbacks.append((key, entry['observe'], {'update_id': key[0], 'stage': stage,
                    'session_id': scope.session_id, 'task_id': scope.task_id, 'turn_id': scope.turn_id,
                    'request_sha256': digest, 'boundary': ('relay_before_next_call'
                        if stage == 'native_request_visible' else 'hermes_request_middleware')}))
        for key, callback, value in callbacks:
            try:
                if callback is None or callback(value) is True:
                    with self._lock:
                        if self._closed or self._blocked:
                            return False
                        # Only a completed receipt can let another request skip
                        # persistence. Concurrent calls may repeat the host's
                        # idempotent receipt, never pass an unfinished write.
                        if len(self._update_observations) >= 256:
                            self._update_observations.pop(next(iter(self._update_observations)))
                        self._update_observations[key] = True
                    continue
            except BaseException:
                pass
            with self._lock:
                self._block('source_update_receipt_unavailable')
            return False
        with self._lock:
            return not (self._closed or self._blocked)

    def _block(self, reason):
        # Called under the scope lock. A later permanent failure supersedes a
        # transient one; a recovered check never unlatches this failed task.
        self._blocked = True
        if self._failure_reason in (None, 'source_freshness_unavailable'):
            self._failure_reason = reason

    @property
    def failure(self):
        with self._lock:
            if self._failure_reason is None:
                return None
            return {'reason': self._failure_reason, 'admitted': self._admitted,
                    'retryable': self._failure_reason == 'source_freshness_unavailable' and not self._admitted}

    def bind(self, scope, parent_session_id=''):
        with self._lock:
            # A native platform adapter wraps its actual message handler before
            # Hermes allocates the session. Adopt only the first authenticated
            # root scope on that declared platform; a child cannot claim it.
            if (self.session_id is None and not self._closed and not self._blocked
                    and not parent_session_id and scope.valid_participant
                    and scope.contact_id == self.contact_id
                    and scope.platform == self._native_platform
                    and scope.authority_lane in {'owner', 'system'}):
                self.session_id = scope.session_id
                self._sessions.add(scope.session_id)
                self._root_sessions.add(scope.session_id)
            self._memory_sessions.discard(scope.session_id)
            valid = (not self._closed and scope.valid_participant and scope.contact_id == self.contact_id
                     and (scope.session_id in self._root_sessions or parent_session_id in self._sessions))
            if valid:
                self._bound.add((scope.session_id, scope.task_id, scope.turn_id))
                self._sessions.add(scope.session_id)
            else:
                self._block('source_input_scope_invalid')

    def rotate(self, previous, scope):
        """Follow only an equivalent scope admitted by the native registry.

        Task/turn equality in allowed() is insufficient: only the registry's
        authenticated rotation path calls here. A new session still needs the
        normal fresh source check before it can supply memory or run tools.
        """
        with self._lock:
            if (self._closed or self._blocked or not previous.valid_participant
                    or previous.contact_id != self.contact_id
                    or (previous.session_id, previous.task_id, previous.turn_id) not in self._bound
                    or scope != replace(previous, session_id=scope.session_id)):
                return
            self._bound.add((scope.session_id, scope.task_id, scope.turn_id))
            self._sessions.add(scope.session_id)
            self._memory_sessions.discard(scope.session_id)
            if previous.session_id in self._root_sessions:
                self._root_sessions.add(scope.session_id)

    def allowed(self, scope, *, fresh, rules, freshness_retryable=False):
        updates = self.request_updates(scope, {})
        if not self.check_updates(scope, updates, fresh=fresh, rules=rules):
            return False
        with self._lock:
            valid = (not self._closed and scope is not None
                     and scope.valid_participant and scope.contact_id == self.contact_id
                     and (scope.session_id, scope.task_id, scope.turn_id) in self._bound)
            if not valid:
                self._block('source_input_scope_invalid')
                return False
            erased = (any(source_input_erased(ref, rules) for ref in self._inputs)
                      or redact_source_payload({'contact_id': self.contact_id,
                          'session_id': self.session_id, 'turn_id': 'supplied-input-check',
                          'assistant_message': 'dependency-check',
                          'assistant_source_refs': self._sources}, rules) is None)
            if erased:
                self._block('source_input_erased')
            elif not fresh:
                self._block('source_freshness_unavailable' if freshness_retryable
                            else 'source_input_unavailable')
            if self._blocked:
                return False
            self._admitted = True
            self._memory_sessions.add(scope.session_id)
            return True

    def recollection_query(self, session_id, fallback):
        """Retained root-task semantics for a native automatic resume query."""
        if not self.memory_contact(session_id):
            return fallback
        with self._lock:
            return self._recall_query if self._recall_query and session_id in self._root_sessions else fallback

    def memory_contact(self, session_id):
        """Share an already checked native participant with the memory provider.

        The supplied constructor data grants nothing. Only the normal native
        hook's participant resolution and fresh source check admit an exact
        session. Native prefetch threads inherit this context; unrelated or
        closed sessions cannot use it as a default owner identity.
        """
        with self._lock:
            if not self._closed and not self._blocked and session_id in self._memory_sessions:
                return self.contact_id
            return ''

    def parents(self):
        return copy.deepcopy(self._inputs), copy.deepcopy(self._sources)

    def context(self, watermark):
        if not self._sources:
            return ''
        stamp = json.dumps({'contact_id': self.contact_id, 'watermark': watermark,
                            'sources': self._sources}, separators=(',', ':'))
        return ('[pacomind-recall-v1 ' + stamp + ']\n'
            'The authenticated host supplied these inherited source handles for this task. '
            'Their contents have not been opened in this native request. Use the scoped source '
            'reader when their evidence is needed; a handle alone establishes no factual claim.\n'
            + json.dumps(self._sources) + '\n[/pacomind-recall-v1]')

    def completed(self, scope, turn_id, sources):
        with self._lock:
            if (scope.session_id in self._root_sessions
                    and not self._blocked and not self._closed):
                self.result = {'session_id': scope.session_id, 'turn_id': turn_id,
                               'input_refs': copy.deepcopy(self._inputs),
                               'source_refs': copy.deepcopy(sources)}


def current():
    return _CURRENT.get()


@contextmanager
def supplied_input(*, contact_id, session_id, input_refs, source_refs=()):
    """Bind already admitted input, after the caller's own scope/currentness checks.

This cannot resolve or replace native authority. Backend admission still
validates exact parent ownership and hashes. The context is copied by native
worker threads and becomes unusable when its owning call leaves this scope.
"""
    value = SuppliedInput(contact_id, session_id, input_refs, source_refs)
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        with value._lock:
            value._closed = True
        _CURRENT.reset(token)


@contextmanager
def transport_input(*, contact_id, platform, input_refs, source_refs=(), recall_query=None):
    """Span a registered platform's actual native handler, not its admission.

    The transport has already resolved these parents. Hermes still resolves
    current authority and chooses the native root session in bind(). This
    context alone grants no memory, tool or sender identity.
    """
    value = SuppliedInput(contact_id, None, input_refs, source_refs, native_platform=platform, recall_query=recall_query)
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        with value._lock:
            value._closed = True
        _CURRENT.reset(token)


def withheld_request(request, *, failure=None):
    result = copy.deepcopy(request)
    # No host-derived task or conversation survives an invalid parent. The
    # native loop owns the eventual response; tools cannot execute from it.
    text = ('Source verification is temporarily unavailable. Stop this attempt; '
            'do not claim that the source was deleted.' if failure and
            failure.get('reason') == 'source_freshness_unavailable' else
            'The source input for this task is no longer available. Stop this task.')
    for key in ('messages', 'input'):
        if key in result:
            result[key] = [{'role': 'user', 'content': text}]
    result.pop('instructions', None)
    result['tools'] = []
    result.pop('tool_choice', None)
    result.pop('parallel_tool_calls', None)
    return result

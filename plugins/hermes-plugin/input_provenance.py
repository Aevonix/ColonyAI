"""Host-supplied source parents for one native task, never participant authority.

An authenticated host may already have captured the human input before asking
the native agent to perform a derived task. Keep those exact parents instead
of recording the task instruction as another human statement. The normal
participant resolver, source reader and canonical writer remain authoritative.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import copy
import json
import re
import threading

from .client import redact_source_payload, source_input_erased

_CURRENT = ContextVar('colony_supplied_input', default=None)


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
        self.result = None

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
        return ('[colony-recall-v1 ' + stamp + ']\n'
            'The authenticated host supplied these inherited source handles for this task. '
            'Their contents have not been opened in this native request. Use the scoped source '
            'reader when their evidence is needed; a handle alone establishes no factual claim.\n'
            + json.dumps(self._sources) + '\n[/colony-recall-v1]')

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

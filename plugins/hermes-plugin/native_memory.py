"""Filter native provider calls that bypass Hermes's ordinary request middleware.

Hermes 0.21.1's iteration summary rebuilds persisted api_content. Its native
Relay 0.8.3 turn scope is the supported physical-request boundary for that call.
This uses the same authenticated RequestMemory state, without editing history,
activating a process-global plugin configuration or adding a worker.
"""
from collections import OrderedDict
import hashlib
import json
import logging
import threading

logger = logging.getLogger(__name__)


def _content_digest(request):
    body = {key: request[key] for key in ('messages', 'input', 'instructions', 'system')
            if key in request}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode()).hexdigest()


class NativeMemoryRequests:
    """One registration per actual native turn; no mutable process-wide policy."""

    def __init__(self, memory):
        self.memory = memory
        self._lock = threading.Lock()
        self._turns = {}

    def bind(self, scope):
        try:
            from agent import relay_runtime
        except ImportError:
            return False
        if not callable(getattr(relay_runtime, 'active_turn', None)):
            return False
        turn = relay_runtime.active_turn(scope.session_id)
        if turn is None or turn.handle is None:
            return False
        host = turn.lease.live_runtime()
        relay = getattr(host, 'relay', None)
        scoped = getattr(relay, 'scope_local', None)
        required = ('register_llm_execution', 'register_llm_stream_execution', 'register_subscriber',
                    'deregister_llm_execution', 'deregister_llm_stream_execution', 'deregister_subscriber')
        if host is None or any(not callable(getattr(scoped, method, None)) for method in required):
            logger.warning('Native memory request coverage needs Hermes 0.21.1 and NeMo Relay 0.8.3')
            return False
        key = (scope.session_id, scope.task_id, scope.turn_id)
        consumer = 'colony.memory:' + str(turn.handle.uuid)
        with self._lock:
            if key in self._turns:
                return True
            seen = OrderedDict()
            self._turns[key] = (host, consumer, seen, turn)

        def rewritten(request):
            # A child can inherit the parent's Relay ancestry. Its own bound
            # participant and RequestMemory instance must govern its requests.
            if relay_runtime.active_turn() is not turn:
                return request
            digest = _content_digest(request.content)
            with self._lock:
                checked = digest in seen
                seen.pop(digest, None)
            if checked:
                return request
            # Use Relay's raw execution contract: its annotated request codec
            # cannot preserve unknown provider fields while deleting and
            # rewriting several unkeyed history rows. The next provider
            # callback accepts this complete filtered raw request directly.
            filtered = self.memory(request.content, scope)['request']
            return relay.LLMRequest(request.headers, filtered)

        async def execute(_name, request, next_call):
            return await next_call(rewritten(request))

        async def stream(request, next_call):
            return await next_call(rewritten(request))

        def ended(event):
            if (getattr(event, 'scope_category', None) == 'end'
                    and str(getattr(event, 'uuid', '')) == str(turn.handle.uuid)):
                self.finish(*key)

        try:
            host.run_in_session(turn.lease.session, relay.scope_local.register_llm_execution,
                turn.handle, consumer, 1000, execute)
            host.run_in_session(turn.lease.session, relay.scope_local.register_llm_stream_execution,
                turn.handle, consumer, 1000, stream)
            host.run_in_session(turn.lease.session, relay.scope_local.register_subscriber,
                turn.handle, consumer, ended)
            host.retain_managed_execution(consumer)
        except Exception:
            self.finish(*key)
            logger.exception('Native memory provider boundary could not be registered')
            return False
        return True

    def checked(self, request, scope):
        if scope is None:
            return
        key = (scope.session_id, scope.task_id, scope.turn_id)
        digest = _content_digest(request)
        with self._lock:
            owned = self._turns.get(key)
            if owned is not None:
                seen = owned[2]
                seen[digest] = None
                while len(seen) > 8:
                    seen.popitem(last=False)

    def finish(self, session_id, task_id, turn_id):
        with self._lock:
            owned = self._turns.pop((session_id, task_id, turn_id), None)
        if owned is not None:
            host, consumer, _, turn = owned
            host.release_managed_execution(consumer)
            if not turn.closed:
                def remove():
                    scoped = host.relay.scope_local
                    for method in ('deregister_llm_execution', 'deregister_llm_stream_execution',
                                   'deregister_subscriber'):
                        getattr(scoped, method)(turn.handle, consumer)
                try:
                    host.run_in_session(turn.lease.session, remove)
                except Exception:
                    # Native turn-scope teardown removes these registrations
                    # even if that scope has already started closing.
                    logger.warning('Native memory scope already closing', exc_info=True)

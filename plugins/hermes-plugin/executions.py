"""Bounded observers for native Hermes turn lifecycle, with no tool authority."""
from collections import OrderedDict
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_OUTPUT_LIMIT_TRACE = 'colony.execution-output-limit.v1:'


class ExecutionObserver:
    def __init__(self, client):
        self.client = client
        self.instance = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._records = OrderedDict()
        self._children = OrderedDict()
        self._current_sessions = {}

    def _find(self, kwargs):
        turn_id = str(kwargs.get("turn_id") or "")
        # Turn IDs survive native compression session rotation. Never fall back
        # to the most recent unrelated turn merely because a hook lacks its ID.
        return self._records.get(turn_id) if turn_id else None

    def _send(self, payload):
        deadline = time.monotonic() + 0.4
        try:
            response = self.client.post("/v1/host/executions/observe", json=payload,
                timeout=0.4, _deadline_monotonic=deadline)
            response.raise_for_status()
        except Exception:
            logger.debug("Execution observation unavailable; liveness will become unknown")

    def start(self, scope, *, review_parent=None, **kwargs):
        turn_id = str(kwargs.get("turn_id") or "")
        session_id = str(kwargs.get("session_id") or "")
        if not turn_id or not session_id:
            return
        with self._lock:
            if turn_id in self._records:
                return
            parent_id = ""
            parent_session = str(kwargs.get("parent_session_id") or "")
            if scope is not None and scope.platform == "background_review":
                if not scope.valid_participant or review_parent is None:
                    return
                person, platform = scope.contact_id, scope.platform
                parent_id = hashlib.sha256(
                    f"{self.instance}:{review_parent.session_id}:{review_parent.turn_id}".encode()).hexdigest()
            elif parent_session:
                bound = self._children.get(session_id)
                if not bound or bound["parent_session_id"] != parent_session:
                    return
                person, parent_id = bound["contact_id"], bound["execution_id"]
                platform = "subagent"
            else:
                if scope is None or not scope.valid_participant:
                    return
                person, platform = scope.contact_id, scope.platform
            payload = {
                "execution_id": hashlib.sha256(f"{self.instance}:{session_id}:{turn_id}".encode()).hexdigest(),
                "contact_id": person, "session_id": session_id, "turn_id": turn_id,
                "parent_execution_id": parent_id, "platform": platform,
                "state": "observed", "phase": "turn", "tool_name": "", "sequence": 1,
            }
            self._records[turn_id] = payload
            self._current_sessions[turn_id] = session_id
            while len(self._records) > 2048:
                expired, _ = self._records.popitem(last=False)
                self._current_sessions.pop(expired, None)
        self._send(dict(payload))

    def child(self, **kwargs):
        with self._lock:
            parent = self._records.get(str(kwargs.get("parent_turn_id") or ""))
            child_session = str(kwargs.get("child_session_id") or "")
            current_session = self._current_sessions.get(parent["turn_id"]) if parent else None
            if not parent or not child_session or current_session != str(kwargs.get("parent_session_id") or ""):
                return
            binding = {"parent_session_id": current_session, "contact_id": parent["contact_id"], "execution_id": parent["execution_id"]}
            previous = self._children.get(child_session)
            if previous is not None and previous != binding:
                # A reused child session cannot inherit a different participant.
                self._children[child_session] = {}
                return
            self._children[child_session] = binding
            while len(self._children) > 2048:
                self._children.popitem(last=False)

    @staticmethod
    def _body_output_limit(kwargs):
        """Read final wire fields, not the agent's optional pre-transport cap.

        No request body is copied or retained. An observed omission means the
        provider chooses the limit; it does not attest an unlimited response.
        Missing/truncated payloads cannot establish that omission.
        """
        unknown = {'output_limit_kind': 'unknown'}
        if kwargs.get('api_mode') not in {'chat_completions', 'anthropic_messages', 'codex_responses'}:
            return unknown
        request = kwargs.get('request')
        if not isinstance(request, dict) or request.get('_truncated') or request.get('_truncated_items'):
            return unknown
        body = request.get('body')
        if not isinstance(body, dict) or body.get('_truncated') or body.get('_truncated_items'):
            return unknown
        extra = body.get('extra_body', {})
        if not isinstance(extra, dict) or extra.get('_truncated') or extra.get('_truncated_items'):
            return unknown
        values = [part[key] for part in (body, extra)
                  for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in part]
        if not values:
            return {'output_limit_kind': 'provider_default'}
        if (all(type(value) is int and 0 < value <= 2147483647 for value in values)
                and len(set(values)) == 1):
            return {'output_limit_kind': 'request', 'max_tokens': values[0]}
        return unknown

    @staticmethod
    def request_metadata(result, **kwargs):
        """Use Hermes' existing trace to carry three scalars past sanitization.

        This observes Colony's returned request without changing provider
        fields or retaining the request. A later request rewrite invalidates
        the marker because only the final trace entry is consumed.
        """
        request_id = kwargs.get('api_request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            return result
        policy = ExecutionObserver._body_output_limit({
            'request': {'body': result.get('request')}, 'api_mode': kwargs.get('api_mode')})
        if policy['output_limit_kind'] == 'unknown':
            return result
        marker = {'request_id': request_id, 'output_limit_kind': policy['output_limit_kind'],
                  'max_tokens': policy.get('max_tokens')}
        return {**result, 'name': _OUTPUT_LIMIT_TRACE + json.dumps(marker, separators=(',', ':'))}

    @staticmethod
    def output_limit_metadata(kwargs):
        policy = ExecutionObserver._body_output_limit(kwargs)
        if policy['output_limit_kind'] != 'unknown':
            return policy
        request = kwargs.get('request')
        # An available, complete body with invalid fields remains unknown.
        if isinstance(request, dict) and not request.get('_truncated'):
            body = request.get('body')
            if isinstance(body, dict) and not body.get('_truncated') and not body.get('_truncated_items'):
                return policy
        trace = kwargs.get('middleware_trace')
        name = trace[-1].get('name') if isinstance(trace, list) and trace and isinstance(trace[-1], dict) else None
        if not isinstance(name, str) or not name.startswith(_OUTPUT_LIMIT_TRACE) or len(name) > 512:
            return policy
        try:
            marker = json.loads(name[len(_OUTPUT_LIMIT_TRACE):])
        except (TypeError, ValueError):
            return policy
        if not isinstance(marker, dict) or marker.get('request_id') != kwargs.get('api_request_id'):
            return policy
        kind, cap = marker.get('output_limit_kind'), marker.get('max_tokens')
        if kind == 'provider_default' and cap is None:
            return {'output_limit_kind': kind}
        if kind == 'request' and type(cap) is int and 0 < cap <= 2147483647:
            return {'output_limit_kind': kind, 'max_tokens': cap}
        return policy

    @staticmethod
    def runtime_metadata(event, kwargs):
        """Whitelist callback metadata, never request/response content or URLs."""
        result = {'event': event}
        for source, target in (('api_request_id', 'request_id'), ('model', 'requested_model'),
                               ('provider', 'provider'), ('response_model', 'response_model'), ('api_mode', 'api_mode')):
            value = kwargs.get(source)
            if isinstance(value, str) and value and len(value) <= 256 and not any(ord(c) < 32 for c in value):
                result[target] = value
        for key in ('api_call_count', 'retry_count', 'approx_input_tokens', 'tool_count'):
            value = kwargs.get(key)
            if type(value) is int and 0 <= value <= 2147483647:
                result[key] = value
        if event == 'start':
            result.update(ExecutionObserver.output_limit_metadata(kwargs))
        for key in ('started_at', 'ended_at'):
            value = kwargs.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                result[key] = value
        try:
            from hermes_constants import hermes_home_key
            from agent.delegation_context import is_delegated_child_process_context, is_dispatcher_owned_worker_context
            result['profile_id'] = hashlib.sha256(hermes_home_key().encode()).hexdigest()
            result['runtime_kind'] = ('delegated_child' if is_delegated_child_process_context()
                else 'kanban_worker' if os.environ.get('HERMES_KANBAN_TASK') and is_dispatcher_owned_worker_context()
                else 'cron' if kwargs.get('platform') == 'cron' else 'turn')
        except (ImportError, AttributeError):
            # Unknown native context is evidence missing, never a guessed role.
            pass
        return result

    def api(self, event, **kwargs):
        self.update('model' if event == 'start' else 'between_calls',
                    runtime=self.runtime_metadata(event, kwargs), **kwargs)

    def update(self, phase, *, state="observed", runtime=None, **kwargs):
        with self._lock:
            previous = self._find(kwargs)
            if previous is None or previous["state"] != "observed":
                return
            if kwargs.get("session_id"):
                self._current_sessions[previous["turn_id"]] = str(kwargs["session_id"])
            previous.update(phase=phase, state=state, sequence=previous["sequence"] + 1,
                            tool_name=str(kwargs.get("tool_name") or "") if phase == "tool" else "")
            payload = dict(previous)
            if runtime is not None:
                payload["runtime"] = runtime
        self._send(payload)

    def end(self, **kwargs):
        state = "interrupted" if kwargs.get("interrupted") else "failed" if kwargs.get("failed") else "completed" if kwargs.get("completed") else "ended"
        self.update("ended", state=state, **kwargs)

    def child_end(self, **kwargs):
        # Hermes may skip a bounded on_session_end callback while another
        # session is invoking it. Native subagent_stop runs on the caller
        # thread after the child result is final, before its durable return.
        # Resolve only a previously bound child; the hook grants no identity.
        child_session = str(kwargs.get("child_session_id") or "")
        parent_session = str(kwargs.get("parent_session_id") or "")
        if not child_session or not parent_session:
            return
        with self._lock:
            candidates = [record for turn, record in self._records.items()
                if record["platform"] == "subagent" and record["state"] == "observed"
                and self._current_sessions.get(turn) == child_session]
            if len(candidates) != 1:
                return
            child = candidates[0]
            parent = next((record for record in self._records.values()
                           if record["execution_id"] == child["parent_execution_id"]), None)
            if parent is None or parent_session not in {
                    parent["session_id"], self._current_sessions.get(parent["turn_id"])}:
                return
            status = str(kwargs.get("child_status") or "")
            state = {"completed": "completed", "interrupted": "interrupted",
                     "failed": "failed", "error": "failed"}.get(status, "ended")
            turn_id = child["turn_id"]
        self.update("ended", state=state, turn_id=turn_id, session_id=child_session)

    def register(self, ctx):
        ctx.register_hook("subagent_start", self.child)
        ctx.register_hook("subagent_stop", self.child_end)
        ctx.register_hook("pre_api_request", lambda **kw: self.api("start", **kw))
        ctx.register_hook("post_api_request", lambda **kw: self.api("response", **kw))
        ctx.register_hook("api_request_error", lambda **kw: self.api("error", **kw))
        ctx.register_hook("post_tool_call", lambda **kw: self.update("between_calls", **kw))
        ctx.register_hook("on_session_end", self.end)

    def tool(self, next_call, args, **kwargs):
        # Runs inside the already registered tool middleware. No policy return,
        # tool arguments or results enter the registry.
        self.update("tool", **kwargs)
        return next_call(args)

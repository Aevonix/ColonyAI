"""Bounded observers for native Hermes turn lifecycle, with no tool authority."""
from collections import OrderedDict
import hashlib
import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)


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

    def update(self, phase, *, state="observed", **kwargs):
        with self._lock:
            previous = self._find(kwargs)
            if previous is None or previous["state"] != "observed":
                return
            if kwargs.get("session_id"):
                self._current_sessions[previous["turn_id"]] = str(kwargs["session_id"])
            previous.update(phase=phase, state=state, sequence=previous["sequence"] + 1,
                            tool_name=str(kwargs.get("tool_name") or "") if phase == "tool" else "")
            payload = dict(previous)
        self._send(payload)

    def end(self, **kwargs):
        state = "interrupted" if kwargs.get("interrupted") else "failed" if kwargs.get("failed") else "completed" if kwargs.get("completed") else "ended"
        self.update("ended", state=state, **kwargs)

    def register(self, ctx):
        ctx.register_hook("subagent_start", self.child)
        ctx.register_hook("pre_api_request", lambda **kw: self.update("model", **kw))
        ctx.register_hook("post_api_request", lambda **kw: self.update("between_calls", **kw))
        ctx.register_hook("post_tool_call", lambda **kw: self.update("between_calls", **kw))
        ctx.register_hook("on_session_end", self.end)

    def tool(self, next_call, args, **kwargs):
        # Runs inside the already registered tool middleware. No policy return,
        # tool arguments or results enter the registry.
        self.update("tool", **kwargs)
        return next_call(args)

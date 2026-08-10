"""Hermes text ResponseGuard uses the pinned transform hook, never voice."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import threading

import pytest


_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
_GUARD_PATH = "/v1/host/response-guard/check"


def _load_plugin():
    name = "colony_hermes_plugin_guard_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return dict(self.value)

    def raise_for_status(self):
        return None


class _Client:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.posts = []
        self.guard_verdict = {}
        self.guard_error: BaseException | None = None
        self.guard_seen = threading.Event()
        self.__class__.instances.append(self)

    def get(self, path, **_kwargs):
        assert path == "/v1/host/contacts/resolve"
        return _Response({"contact_id": "cid-owner"})

    def post(self, path, **kwargs):
        self.posts.append({"path": path, **kwargs})
        if path == _GUARD_PATH:
            self.guard_seen.set()
            if self.guard_error is not None:
                raise self.guard_error
            return _Response(self.guard_verdict)
        return _Response({})

    def sync_turn(self, **_kwargs):
        return True


class _Context:
    def __init__(self):
        self.config = {"plugins": {"colony": {
            "url": "http://colony.test",
            "owner_contact_id": "cid-owner",
        }}}
        self.hooks = {}
        self.middleware = {}

    def register_tool(self, **_kwargs):
        return None

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, name, fn):
        self.middleware[name] = fn

    def register_command(self, *_args, **_kwargs):
        return None


@pytest.fixture
def plugin(monkeypatch):
    module = _load_plugin()
    _Client.instances.clear()
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "off")
    module.ColonyClient = _Client
    context = _Context()
    module.register(context)
    context.hooks["pre_llm_call"](
        session_id="sess-g1", task_id="task-g1", turn_id="turn-g1",
        platform="sms", sender_id="+15550001", user_message="hello",
    )
    return module, context, _Client.instances[-1]


def _valid_verdict(module, text, *, decision="allow", status="evaluated"):
    return {
        "decision": decision,
        "mode": "enforce",
        "surface": "text_chat",
        "surface_family": "text",
        "applicability": "guarded",
        "guard_status": status,
        "policy_id": module._GUARD_POLICY_ID,
        "policy_digest": module._GUARD_POLICY_DIGEST,
        "candidate_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "findings": [],
    }


def _transform(context, text="hi there", platform="sms"):
    return context.hooks["transform_llm_output"](
        response_text=text, session_id="sess-g1", model="model-a",
        platform=platform,
    )


def test_plugin_policy_identity_matches_sidecar_contract():
    from colony_sidecar.gate.surface_policy import POLICY_DIGEST, POLICY_ID

    module = _load_plugin()
    assert module._GUARD_POLICY_ID == POLICY_ID
    assert module._GUARD_POLICY_DIGEST == POLICY_DIGEST


def test_guard_off_does_not_post(plugin):
    _module, context, client = plugin
    assert _transform(context) is None
    assert client.posts == []


def test_guard_shadow_posts_without_mutating_reply(plugin, monkeypatch):
    _module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "shadow")
    assert _transform(context) is None
    assert client.guard_seen.wait(timeout=3)
    payload = client.posts[-1]["json"]
    assert payload["response_text"] == "hi there"
    assert payload["incoming_message_text"] == "hello"
    assert payload["target_contact_id"] == "cid-owner"
    assert payload["target_gateway"] == "sms"
    assert payload["mode"] == "shadow"


def test_guard_shadow_swallows_base_exception(plugin, monkeypatch):
    _module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "shadow")
    client.guard_error = KeyboardInterrupt("interrupted")
    assert _transform(context) is None
    assert client.guard_seen.wait(timeout=3)


def test_transform_enforce_allows_exact_bound_verdict(plugin, monkeypatch):
    module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    text = "é" * 8000
    client.guard_verdict = _valid_verdict(module, text)
    assert _transform(context, text) is None
    assert client.posts[-1]["json"]["response_text"] == text


def test_transform_enforce_withholds_oversize_without_partial_check(plugin, monkeypatch):
    module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    assert _transform(context, "x" * 8001) == module._GUARD_WITHHELD_TEXT
    assert client.posts == []


@pytest.mark.parametrize("mutation", [
    {"mode": "shadow"},
    {"surface": "text_message"},
    {"surface_family": "artifact"},
    {"applicability": "excluded"},
    {"guard_status": "degraded"},
    {"policy_id": "stale-policy"},
    {"policy_digest": "0" * 64},
    {"candidate_digest": "0" * 64},
])
def test_transform_enforce_withholds_invalid_allow_verdict(
    plugin, monkeypatch, mutation,
):
    module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    text = "safe candidate"
    client.guard_verdict = _valid_verdict(module, text)
    client.guard_verdict.update(mutation)
    assert _transform(context, text) == module._GUARD_WITHHELD_TEXT


@pytest.mark.parametrize("decision", ["block", "revise"])
def test_transform_enforce_withholds_blocking_verdict(plugin, monkeypatch, decision):
    module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    text = "candidate"
    client.guard_verdict = _valid_verdict(module, text, decision=decision)
    assert _transform(context, text) == module._GUARD_WITHHELD_TEXT


def test_transform_enforce_fails_closed_on_base_exception(plugin, monkeypatch):
    module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    client.guard_error = KeyboardInterrupt("interrupted")
    assert _transform(context) == module._GUARD_WITHHELD_TEXT


@pytest.mark.parametrize("platform", ["realtime_voice", "phone_call", "intercom", "google_meet"])
def test_voice_surfaces_are_excluded(plugin, monkeypatch, platform):
    _module, context, client = plugin
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    assert _transform(context, platform=platform) is None
    assert client.posts == []

"""H6.4 — chat-path guard mode (COLONY_GUARD_CHAT_MODE: off/shadow/enforce).

Unset inherits the legacy COLONY_GUARD_CHAT_SHADOW boolean (regression
lock). Enforce is capability-probed: current Hermes cannot apply post-hook
reply mutations, so enforce logs ONE warning and runs as shadow; a host
that advertises post_llm_call_mutation gets a synchronous check whose
block/revise verdict withholds the reply, failing OPEN on guard errors.

Loads the plugin package straight from plugins/hermes-plugin with a stubbed
ColonyClient, mirroring tests/test_hermes_plugin_guard_shadow.py.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

_PLUGIN_DIR = (pathlib.Path(__file__).resolve().parents[2]
               / "plugins" / "hermes-plugin")

_GUARD_PATH = "/v1/host/response-guard/check"


def _load_plugin():
    name = "colony_hermes_plugin_guard_mode_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    guard_verdict = {"decision": "allow", "mode": "enforce", "findings": []}
    raise_on_guard = False

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.synced = []
        self.guard_seen = threading.Event()

    def get(self, path, **kwargs):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"contact_id": "cid-42"}
        return _R()

    def post(self, path, **kwargs):
        if path == _GUARD_PATH:
            self.guard_seen.set()
            if self.raise_on_guard:
                raise RuntimeError("guard endpoint down")
        self.posts.append({"path": path, "json": kwargs.get("json"),
                           "timeout": kwargs.get("timeout")})
        verdict = dict(type(self).guard_verdict)

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return verdict if path == _GUARD_PATH else {}
        return _R()

    def sync_turn(self, **kwargs):
        self.synced.append(kwargs)
        return True


class _FakeCtx:
    def __init__(self, capabilities=()):
        self.hooks = {}
        if capabilities:
            self.capabilities = tuple(capabilities)

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def _make_plugin(capabilities=()):
    mod = _load_plugin()
    mod._guard_enforce_warned = False
    ctx = _FakeCtx(capabilities)
    holder = {}

    class _Client(_StubClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            holder["client"] = self

    class _Subscriber:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    orig = (mod.ColonyClient, mod.ColonyEventSubscriber,
            mod._configure_colony_llm)
    mod.ColonyClient = _Client
    mod.ColonyEventSubscriber = _Subscriber
    mod._configure_colony_llm = lambda *a, **k: None

    def restore():
        (mod.ColonyClient, mod.ColonyEventSubscriber,
         mod._configure_colony_llm) = orig

    mod.register(ctx)
    return mod, ctx, holder["client"], restore


def _run_turn(ctx):
    ctx.hooks["pre_llm_call"](
        session_id="sess-m1", platform="sms", sender_id="+15550001")
    return ctx.hooks["post_llm_call"](
        session_id="sess-m1", platform="sms",
        user_message="hello", assistant_response="hi there")


def _guard_posts(client, wait=0.0):
    deadline = time.time() + wait
    while wait and time.time() < deadline:
        if any(p["path"] == _GUARD_PATH for p in client.posts):
            break
        time.sleep(0.01)
    return [p for p in client.posts if p["path"] == _GUARD_PATH]


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def test_mode_defaults_and_legacy_inheritance(monkeypatch):
    mod = _load_plugin()
    monkeypatch.delenv("COLONY_GUARD_CHAT_MODE", raising=False)
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    assert mod._guard_chat_mode() == "off"
    monkeypatch.setenv("COLONY_GUARD_CHAT_SHADOW", "1")
    assert mod._guard_chat_mode() == "shadow"
    # explicit mode always beats the legacy flag, in both directions
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "off")
    assert mod._guard_chat_mode() == "off"
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    assert mod._guard_chat_mode() == "enforce"
    # invalid explicit value falls back to the legacy inheritance
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "banana")
    assert mod._guard_chat_mode() == "off"


def test_enforce_downgrades_to_shadow_with_one_warning(monkeypatch, caplog):
    mod = _load_plugin()
    mod._guard_enforce_warned = False
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    ctx = SimpleNamespace()  # no mutation capability advertised
    with caplog.at_level(logging.WARNING):
        assert mod._effective_guard_chat_mode(ctx) == "shadow"
        assert mod._effective_guard_chat_mode(ctx) == "shadow"
    warnings = [r for r in caplog.records
                if "cannot apply post-hook reply mutations" in r.message]
    assert len(warnings) == 1               # exactly ONE honest warning


def test_enforce_stays_enforce_on_capable_host(monkeypatch):
    mod = _load_plugin()
    mod._guard_enforce_warned = False
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    ctx = SimpleNamespace(capabilities=("post_llm_call_mutation",))
    assert mod._effective_guard_chat_mode(ctx) == "enforce"


# ---------------------------------------------------------------------------
# Hook behavior end-to-end
# ---------------------------------------------------------------------------

def test_mode_off_posts_nothing(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_CHAT_MODE", raising=False)
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    mod, ctx, client, restore = _make_plugin()
    try:
        assert _run_turn(ctx) is None
        time.sleep(0.15)
        assert _guard_posts(client) == []
    finally:
        restore()


def test_mode_shadow_posts_shadow_check(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "shadow")
    mod, ctx, client, restore = _make_plugin()
    try:
        assert _run_turn(ctx) is None
        assert client.guard_seen.wait(timeout=5)
        posts = _guard_posts(client, wait=5)
        assert posts and posts[0]["json"]["mode"] == "shadow"
    finally:
        restore()


def test_enforce_on_incapable_host_behaves_as_shadow(monkeypatch):
    """The honest downgrade: enforce requested, host can't mutate ->
    the check still fires, but as shadow, and the reply is untouched."""
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    _StubClient.guard_verdict = {"decision": "block", "mode": "enforce",
                                 "findings": []}
    mod, ctx, client, restore = _make_plugin()   # _FakeCtx: no capability
    try:
        ret = _run_turn(ctx)
        assert ret is None                       # reply NOT withheld
        assert client.guard_seen.wait(timeout=5)
        posts = _guard_posts(client, wait=5)
        assert posts and posts[0]["json"]["mode"] == "shadow"
    finally:
        _StubClient.guard_verdict = {"decision": "allow", "mode": "enforce",
                                     "findings": []}
        restore()


def test_enforce_on_capable_host_withholds_blocked_reply(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    _StubClient.guard_verdict = {"decision": "block", "mode": "enforce",
                                 "findings": []}
    mod, ctx, client, restore = _make_plugin(
        capabilities=("post_llm_call_mutation",))
    try:
        ret = _run_turn(ctx)
        assert isinstance(ret, dict)
        assert "withheld" in ret["assistant_response"]
        posts = _guard_posts(client)
        assert posts and posts[0]["json"]["mode"] == "enforce"
    finally:
        _StubClient.guard_verdict = {"decision": "allow", "mode": "enforce",
                                     "findings": []}
        restore()


def test_enforce_on_capable_host_allows_clean_reply(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    mod, ctx, client, restore = _make_plugin(
        capabilities=("post_llm_call_mutation",))
    try:
        assert _run_turn(ctx) is None
        posts = _guard_posts(client)
        assert posts and posts[0]["json"]["mode"] == "enforce"
    finally:
        restore()


def test_enforce_fails_open_when_guard_is_down(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    mod, ctx, client, restore = _make_plugin(
        capabilities=("post_llm_call_mutation",))
    client.raise_on_guard = True
    try:
        assert _run_turn(ctx) is None            # a down guard never mutes
    finally:
        restore()

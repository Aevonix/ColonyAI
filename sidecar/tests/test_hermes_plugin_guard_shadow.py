"""U23 — hermes-plugin chat hot-path ResponseGuard shadow.

With COLONY_GUARD_CHAT_SHADOW=1 the post_llm_call hook mirrors the outbound
reply to POST /v1/host/response-guard/check (mode=shadow) on a fire-and-forget
daemon thread. It never blocks or modifies the reply, and every error is
swallowed. Default (0) posts nothing — regression-locked here.

Loads the plugin package straight from plugins/hermes-plugin with a stubbed
ColonyClient, mirroring tests/test_hermes_plugin_signals.py.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import time

import pytest

_PLUGIN_DIR = (pathlib.Path(__file__).resolve().parents[2]
               / "plugins" / "hermes-plugin")

_GUARD_PATH = "/v1/host/response-guard/check"


def _load_plugin():
    name = "colony_hermes_plugin_guard_test"
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
    """Records posts; optionally raises on the guard path (fail-open check)."""

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

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {}
        return _R()

    def sync_turn(self, **kwargs):
        self.synced.append(kwargs)
        return True


class _FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, name, fn):
        self.hooks[name] = fn


@pytest.fixture
def plugin():
    mod = _load_plugin()
    ctx = _FakeCtx()
    stub_holder = {}

    class _Client(_StubClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            stub_holder["client"] = self

    class _Subscriber:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    orig = (mod.ColonyClient, mod.ColonyEventSubscriber, mod._configure_colony_llm)
    mod.ColonyClient = _Client
    mod.ColonyEventSubscriber = _Subscriber
    mod._configure_colony_llm = lambda *a, **k: None
    try:
        mod.register(ctx)
        yield ctx, stub_holder["client"]
    finally:
        (mod.ColonyClient, mod.ColonyEventSubscriber,
         mod._configure_colony_llm) = orig


def _run_turn(ctx):
    ctx.hooks["pre_llm_call"](
        session_id="sess-g1", platform="sms", sender_id="+15550001")
    ret = ctx.hooks["post_llm_call"](
        session_id="sess-g1", platform="sms",
        user_message="hello", assistant_response="hi there")
    return ret


def _wait_for_sync(client, timeout=5):
    deadline = time.time() + timeout
    while not client.synced and time.time() < deadline:
        time.sleep(0.01)


def _guard_posts(client):
    return [p for p in client.posts if p["path"] == _GUARD_PATH]


def test_guard_shadow_off_by_default(plugin, monkeypatch):
    """Regression lock: flag unset -> no guard-check post, turn flow unchanged."""
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    ctx, client = plugin
    ret = _run_turn(ctx)
    assert ret is None                      # hook never modifies the reply
    _wait_for_sync(client)
    time.sleep(0.1)                         # give any stray thread a beat
    assert _guard_posts(client) == []
    assert client.synced                    # turn sync unchanged


def test_guard_shadow_posts_check_when_enabled(plugin, monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_CHAT_SHADOW", "1")
    ctx, client = plugin
    ret = _run_turn(ctx)
    assert ret is None                      # never blocks or modifies the reply
    assert client.guard_seen.wait(timeout=5), "guard check never fired"
    deadline = time.time() + 5
    while not _guard_posts(client) and time.time() < deadline:
        time.sleep(0.01)
    posts = _guard_posts(client)
    assert posts
    payload = posts[0]["json"]
    assert payload["response_text"] == "hi there"
    assert payload["incoming_message_text"] == "hello"
    assert payload["target_contact_id"] == "cid-42"
    assert payload["target_gateway"] == "sms"
    assert payload["session_id"] == "sess-g1"
    assert payload["mode"] == "shadow"      # observation only, never enforce
    assert posts[0]["timeout"] == 3         # short bounded timeout
    _wait_for_sync(client)
    assert client.synced                    # turn sync still happened


def test_guard_shadow_swallows_endpoint_errors(plugin, monkeypatch):
    """Guard endpoint failure must be invisible: fail open by construction."""
    monkeypatch.setenv("COLONY_GUARD_CHAT_SHADOW", "1")
    ctx, client = plugin
    client.raise_on_guard = True
    ret = _run_turn(ctx)
    assert ret is None
    assert client.guard_seen.wait(timeout=5), "guard check never attempted"
    _wait_for_sync(client)
    assert client.synced                    # turn sync unaffected by the failure

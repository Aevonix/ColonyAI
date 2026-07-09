"""hermes-plugin post_llm_call: the per-turn signals/ingest payload carries the
raw sender identity (same shape as turns/sync) so the sidecar's
ParticipantResolver — not the client contact cache — owns signal attribution.

Loads the plugin package straight from plugins/hermes-plugin with a stubbed
ColonyClient, registers it against a fake Hermes ctx, and drives the
pre/post_llm_call hooks.
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


def _load_plugin():
    name = "colony_hermes_plugin_under_test"
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
    """Stands in for ColonyClient: records calls, answers contacts/resolve."""

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.synced = []
        self.post_seen = threading.Event()

    def get(self, path, **kwargs):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"contact_id": "cid-42"}
        return _R()

    def post(self, path, **kwargs):
        self.posts.append({"path": path, "json": kwargs.get("json")})
        self.post_seen.set()

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


def _run_turn(ctx, client, *, sender_id):
    ctx.hooks["pre_llm_call"](
        session_id="sess-1", platform="sms", sender_id=sender_id)
    ctx.hooks["post_llm_call"](
        session_id="sess-1", platform="sms",
        user_message="hello", assistant_response="hi")
    assert client.post_seen.wait(timeout=5), "signals/ingest never fired"
    # the daemon thread posts right after sync_turn; give it a beat to finish
    deadline = time.time() + 5
    while not client.synced and time.time() < deadline:
        time.sleep(0.01)


def test_signals_ingest_includes_sender_block(plugin):
    ctx, client = plugin
    _run_turn(ctx, client, sender_id="+15550001")
    ingests = [p for p in client.posts if p["path"] == "/v1/host/signals/ingest"]
    assert ingests, f"no signals/ingest post: {client.posts}"
    payload = ingests[0]["json"]
    assert payload["sender"] == {"platform": "sms", "user_id": "+15550001"}
    assert payload["incoming_message"]["content"] == "hello"
    assert client.synced   # turn sync still happened


def test_signals_ingest_omits_sender_when_unknown(plugin):
    ctx, client = plugin
    _run_turn(ctx, client, sender_id="")
    ingests = [p for p in client.posts if p["path"] == "/v1/host/signals/ingest"]
    assert ingests
    assert "sender" not in ingests[-1]["json"]   # senderless turn: no block

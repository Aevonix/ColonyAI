"""Canonical Hermes turn writer uses one exact transport participant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import time

import pytest


_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"


def _load_plugin():
    name = "colony_hermes_plugin_under_test"
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
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return dict(self.value)


class _Client:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.synced = []
        self.sync_results = []
        self.posts = []
        self.__class__.instances.append(self)

    def get(self, path, **kwargs):
        assert path == "/v1/host/contacts/resolve"
        sender = (kwargs.get("params") or {}).get("address")
        if sender == "+15550001":
            return _Response({"contact_id": "cid-owner"})
        if sender == "+15550002":
            return _Response({"contact_id": "cid-guest"})
        return _Response({}, 404)

    def post(self, path, **kwargs):
        self.posts.append({"path": path, **kwargs})
        return _Response({})

    def sync_turn(self, **kwargs):
        self.synced.append(kwargs)
        return self.sync_results.pop(0) if self.sync_results else True


class _Context:
    def __init__(self, outbox_path):
        self.config = {"plugins": {"colony": {
            "url": "http://colony.test",
            "owner_contact_id": "cid-owner",
            "attested_system_platforms": ["cli"],
            "turn_outbox_path": str(outbox_path),
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
def plugin(monkeypatch, tmp_path):
    module = _load_plugin()
    _Client.instances.clear()
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    context = _Context(tmp_path / "turn-outbox.sqlite3")
    module.register(context)
    return context, _Client.instances[-1]


def _wait_count(client, count, timeout=3):
    deadline = time.time() + timeout
    while len(client.synced) < count and time.time() < deadline:
        time.sleep(0.01)
    assert len(client.synced) >= count


def _pre(context, *, session, task, turn, platform="sms", sender="+15550001"):
    context.hooks["pre_llm_call"](
        session_id=session, task_id=task, turn_id=turn,
        platform=platform, sender_id=sender, user_message="hello",
    )


def _post(context, *, session, task, turn=""):
    context.hooks["post_llm_call"](
        session_id=session, task_id=task, turn_id=turn,
        platform="sms", user_message="hello", assistant_response="hi",
        conversation_history=[], model="model-a",
    )


def test_turn_sync_includes_exact_sender_and_no_competing_signal_write(plugin):
    context, client = plugin
    _pre(context, session="sess-1", task="task-1", turn="turn-1")
    _post(context, session="sess-1", task="task-1", turn="turn-1")
    _wait_count(client, 1)
    assert client.synced[0]["contact_id"] == "cid-owner"
    assert client.synced[0]["sender"] == {
        "platform": "sms", "user_id": "+15550001",
    }
    assert client.synced[0]["turn_id"] == "turn-1"
    assert client.posts == []


def test_unresolved_sender_skips_turn_instead_of_owner_fallback(plugin):
    context, client = plugin
    _pre(
        context, session="sess-unknown", task="task-unknown", turn="turn-unknown",
        sender="+19999999",
    )
    _post(
        context, session="sess-unknown", task="task-unknown", turn="turn-unknown",
    )
    time.sleep(0.05)
    assert client.synced == []


def test_missing_host_turn_id_uses_task_id_and_duplicate_hook_writes_once(plugin):
    context, client = plugin
    _pre(context, session="sess-no-turn", task="task-stable", turn="")
    _post(context, session="sess-no-turn", task="task-stable")
    _post(context, session="sess-no-turn", task="task-stable")
    _wait_count(client, 1)
    time.sleep(0.05)
    assert len(client.synced) == 1
    assert client.synced[0]["turn_id"].startswith("hermes:")


def test_failed_canonical_write_releases_local_claim_for_retry(plugin):
    context, client = plugin
    client.sync_results = [False, True]
    _pre(
        context, session="sess-retry", task="task-retry", turn="turn-retry",
        platform="cli", sender="",
    )
    context.hooks["post_llm_call"](
        session_id="sess-retry", task_id="task-retry", turn_id="turn-retry",
        platform="cli", user_message="persist this", assistant_response="okay",
        conversation_history=[],
    )
    _wait_count(client, 1)
    time.sleep(0.02)
    context.hooks["post_llm_call"](
        session_id="sess-retry", task_id="task-retry", turn_id="turn-retry",
        platform="cli", user_message="persist this", assistant_response="okay",
        conversation_history=[],
    )
    _wait_count(client, 2)
    assert client.synced[0]["turn_id"] == client.synced[1]["turn_id"]

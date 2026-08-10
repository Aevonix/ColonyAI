"""The canonical Hermes plugin forwards host turn IDs to Colony V2."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_CLIENT_PATH = (
    Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin" / "client.py"
)


def _load_client_module():
    name = "colony_hermes_client_turn_id_test"
    spec = importlib.util.spec_from_file_location(name, _CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"accepted": True}


def test_stable_turn_id_uses_v2_put_and_is_url_escaped(monkeypatch):
    module = _load_client_module()
    client = module.ColonyClient(url="http://sidecar.test")
    calls = []
    monkeypatch.setattr(
        client,
        "put",
        lambda path, **kwargs: calls.append(("PUT", path, kwargs["json"])) or _Response(),
    )
    monkeypatch.setattr(
        client,
        "post",
        lambda path, **kwargs: calls.append(("POST", path, kwargs["json"])) or _Response(),
    )

    assert client.sync_turn(
        session_id="session-1",
        contact_id="contact-1",
        turn_id="host/turn 7",
        user_message="hello",
        assistant_message="hi",
    )

    assert calls[0][0:2] == ("PUT", "/v2/host/turns/host%2Fturn%207")
    assert calls[0][2]["context"]["turn_id"] == "host/turn 7"


def test_missing_turn_id_keeps_v1_compatibility_path(monkeypatch):
    module = _load_client_module()
    client = module.ColonyClient(url="http://sidecar.test")
    calls = []
    monkeypatch.setattr(
        client,
        "post",
        lambda path, **kwargs: calls.append(path) or _Response(),
    )

    assert client.sync_turn(
        session_id="session-1",
        contact_id="contact-1",
        user_message="hello",
        assistant_message="hi",
    )
    assert calls == ["/v1/host/turns/sync"]


def test_hermes_task_metadata_derives_a_stable_private_turn_id():
    """Current Hermes always gives hooks session_id + task_id.

    The task anchor identifies the turn while the sidecar's canonical content
    digest remains responsible for detecting a changed retry.
    """
    module = _load_client_module()

    first = module.derive_hermes_turn_id(
        session_id="session-1",
        task_id="task-77",
        user_message="hello",
        assistant_response="first answer",
        platform="sms",
    )
    duplicate_hook = module.derive_hermes_turn_id(
        session_id="session-1",
        task_id="task-77",
        user_message="hello",
        assistant_response="changed answer",
        platform="sms",
    )
    next_turn = module.derive_hermes_turn_id(
        session_id="session-1",
        task_id="task-78",
        user_message="hello",
        assistant_response="first answer",
        platform="sms",
    )

    assert first == duplicate_hook
    assert first != next_turn
    assert first.startswith("hermes:")
    assert "session-1" not in first
    assert "task-77" not in first


def test_missing_task_id_uses_available_hook_history_stably():
    module = _load_client_module()
    metadata = {
        "session_id": "legacy-session",
        "user_message": "same question",
        "assistant_response": "same answer",
        "conversation_history": [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "context"},
        ],
        "model": "model-a",
        "platform": "cli",
    }

    assert module.derive_hermes_turn_id(**metadata) == module.derive_hermes_turn_id(
        **metadata
    )
    assert module.derive_hermes_turn_id(**metadata) != module.derive_hermes_turn_id(
        **{**metadata, "conversation_history": metadata["conversation_history"] + [
            {"role": "user", "content": "a later turn"}
        ]}
    )

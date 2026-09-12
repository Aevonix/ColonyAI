"""An erased original turn must not strand its safe durable replacement."""

import asyncio
import importlib

import pytest

from pacomind.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _Client, _Context, _load_plugin


QUESTION = "Can you recover the workshop details I asked you to forget?"
OLD_ANSWER = "The old workshop label was QX-14."
SAFE_ANSWER = "Those details are unavailable. Please provide them again."


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    module = _load_plugin("pacomind_hermes_survivor_delivery_test")
    _Client.instances.clear()
    monkeypatch.setattr(module, "PacoMindClient", _Client)
    monkeypatch.setenv("PACOMIND_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("PACOMIND_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("PACOMIND_MEMORY_TURN_WRITER", "disabled")
    database = tmp_path / "outbox.sqlite3"
    context = _Context(database, drain_timeout_ms=250)
    module.register(context)
    outbox = module.TurnOutbox(database)
    ledger = TurnIdempotencyLedger(tmp_path / "canonical.sqlite3")
    ledger.record_source("old-turn", contact_id="cid-owner", session_id="session-1",
        messages=[{"role": "user", "content": QUESTION},
                  {"role": "assistant", "content": OLD_ANSWER}])
    ledger.erase_sources(contact_id="cid-owner", turn_ids=["old-turn"])
    outbox.apply_erasure_page("cid-owner", ledger.erasure_feed("cid-owner"))
    context.hooks["pre_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="new-turn",
        platform="sms", sender_id="+15550001", user_message=QUESTION)
    return module, context, outbox, _Client.instances[-1]


def complete(context, answer=SAFE_ANSWER):
    return context.hooks["post_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="new-turn",
        user_message=QUESTION, assistant_response=answer,
        conversation_history=[], model="processor-a", platform="sms")


def test_post_turn_delivers_safe_survivor_once_without_another_user_turn(runtime):
    module, context, outbox, client = runtime
    assert complete(context) is None
    row, = outbox.snapshot()
    assert row["state"] == "delivered" and row["attempts"] == 1
    assert row["turn_id"].startswith("checkpoint:")
    assert row["payload"]["assistant_message"] == SAFE_ANSWER
    assert row["payload"]["source_only"] is True
    assert row["payload"]["require_source_receipt"] is True
    assert not {"user_message", "summary", "model", "tools_used"} & row["payload"].keys()
    assert len(client.synced) == 1
    assert complete(context) is None  # Duplicate native lifecycle callback.
    assert len(client.synced) == 1 and outbox.snapshot() == [row]
    assert outbox.lookup("new-turn") is None


def test_fully_erased_turn_is_a_noop(runtime, monkeypatch):
    module, context, outbox, client = runtime
    monkeypatch.setattr(module.TurnOutbox, "drain",
        lambda *a, **k: pytest.fail("No survivor should not trigger a drain"))
    assert complete(context, OLD_ANSWER) is None
    assert outbox.snapshot() == [] and client.synced == []


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_failed_or_cancelled_survivor_delivery_remains_recoverable(runtime, monkeypatch, error):
    module, context, outbox, client = runtime
    attempts = []
    def fail(**payload):
        attempts.append(payload)
        raise error("delivery interrupted")
    monkeypatch.setattr(client, "sync_turn", fail)
    assert complete(context) is None
    row, = outbox.snapshot()
    assert row["state"] == "pending" and row["attempts"] == 1
    assert row["last_error"] == "delivery_exception" and row["lease_id"] == ""
    assert row["payload"]["assistant_message"] == SAFE_ANSWER
    assert "user_message" not in row["payload"] and len(attempts) == 1
    delivered = []
    deliver = lambda payload, **kw: delivered.append(payload) or True
    assert module.recover_turn_outbox(
        {"turn_outbox_path": str(outbox.path)}, deliver, timeout_seconds=1) == 1
    assert module.recover_turn_outbox(
        {"turn_outbox_path": str(outbox.path)}, deliver, timeout_seconds=1) == 0
    assert len(delivered) == 1 and delivered[0] == row["payload"]


def test_checkpoint_reports_original_erased_and_delivers_its_survivor(runtime, monkeypatch, tmp_path):
    module, _context, outbox, client = runtime
    evidence = importlib.import_module(module.__name__ + ".evidence")
    monkeypatch.setattr(evidence, "PacoMindClient", lambda **kw: client)
    messages = [{"role": "user", "content": QUESTION},
                {"role": "assistant", "content": SAFE_ANSWER}]
    result = evidence.checkpoint(messages, session_id="session-1", contact_id="cid-owner",
        home=tmp_path, url="http://127.0.0.1:7777", api_key="", outbox_path=str(outbox.path))
    assert result["state"] == "erased"
    row, = outbox.snapshot()
    assert row["state"] == "delivered" and row["attempts"] == 1
    assert row["payload"]["checkpoint_messages"] == messages[1:]
    assert result["survivor_turn_id"] == row["turn_id"]
    assert result["survivor_state"] == "delivered"
    assert outbox.lookup(result["turn_id"]) is None

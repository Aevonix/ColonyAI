"""Reference-store specifics beyond the conformance suite: triggers,
reserved receipt kinds, pinned attempt budget, digest self-verification."""

import sqlite3

import pytest

from colony_hostworker.conformance import (
    SqliteStoreHarness,
    build_envelope,
    build_intent,
    delivery_gate_evidence,
)
from colony_hostworker.conformance.suite import (
    OWNER_A,
    SOURCE,
    _dispatch_ok,
    _gated,
)
from colony_hostworker.store import (
    ActionStoreError,
)
from colony_hostworker.worker import DEFAULT_ACTION_TYPE


@pytest.fixture()
def harness():
    instance = SqliteStoreHarness()
    yield instance
    instance.close()


def test_actions_are_pinned_to_one_attempt(harness):
    scenario = _gated(harness)
    assert scenario.action["max_attempts"] == 1
    # Even a direct SQL write cannot widen the budget: schema CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        harness.store._conn.execute(
            "UPDATE actions SET max_attempts=3 WHERE action_id=?",
            (scenario.action["action_id"],),
        )


def test_receipts_and_events_are_trigger_immutable(harness):
    scenario = _gated(harness)
    connection = harness.store._conn
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE receipts SET evidence_json='{}' WHERE action_id=?",
            (scenario.action["action_id"],),
        )
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "DELETE FROM receipts WHERE action_id=?",
            (scenario.action["action_id"],),
        )
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE action_events SET actor='attacker'"
        )
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE actions SET payload_json='{}' WHERE action_id=?",
            (scenario.action["action_id"],),
        )


def test_get_action_verifies_payload_digest(harness):
    scenario = _gated(harness)
    # Bypass triggers entirely by dropping them first (simulating a corrupt
    # or hostile database file) — the read path must still refuse.
    connection = harness.store._conn
    connection.execute("DROP TRIGGER actions_identity_no_update")
    connection.execute(
        "UPDATE actions SET payload_json='{\"forged\": true}' WHERE action_id=?",
        (scenario.action["action_id"],),
    )
    with pytest.raises(ActionStoreError):
        harness.store.get_action(scenario.action["action_id"])


@pytest.mark.parametrize(
    "kind", ["gate", "dispatch_recovery", "dispatch_observation"]
)
def test_reserved_receipt_kinds_cannot_be_forged(harness, kind):
    scenario = _gated(harness)
    with pytest.raises(ActionStoreError):
        harness.store.add_receipt(
            scenario.action["action_id"],
            "forged-receipt",
            kind,
            "passed",
            {"forged": True},
        )
    dispatched, _request = _dispatch_ok(harness, scenario)
    with pytest.raises(ActionStoreError):
        harness.store.accept(
            dispatched["action_id"],
            OWNER_A,
            "forged-acceptance",
            kind,
            {"forged": True},
        )


def test_lease_next_never_accepts_dispatched_state(harness):
    with pytest.raises(ActionStoreError):
        harness.store.lease_next(
            OWNER_A, lease_seconds=30.0, states=("dispatched",)
        )
    with pytest.raises(ActionStoreError):
        harness.store.lease_next(
            OWNER_A, lease_seconds=30.0, states=("proposed",)
        )


def test_defer_leased_refuses_dispatched(harness):
    scenario = _gated(harness)
    dispatched, _request = _dispatch_ok(harness, scenario)
    with pytest.raises(ActionStoreError):
        harness.store.defer_leased(
            dispatched["action_id"], OWNER_A, "nope", 1.0
        )


def test_gate_receipt_written_verbatim_and_returned(harness):
    intent = build_intent()
    envelope = build_envelope(intent)
    action = harness.propose(
        idempotency_key=intent.idempotency_key,
        source=SOURCE,
        source_ref=intent.intent_id,
        action_type=DEFAULT_ACTION_TYPE,
        payload=envelope,
    )
    evidence = delivery_gate_evidence(
        action, decided_at=harness.now(), expires_at=harness.now() + 60.0
    )
    gated, receipt = harness.add_gate(
        action["action_id"], evidence, external_id=evidence["approval_id"]
    )
    assert gated["state"] == "gated"
    stored = harness.store.list_receipts(action["action_id"])
    assert [item["receipt_key"] for item in stored] == [receipt["receipt_key"]]
    assert stored[0]["evidence"] == evidence
    assert stored[0]["kind"] == "gate"
    events = harness.store.list_events(action["action_id"])
    assert [event["event_type"] for event in events] == ["proposed", "gate_passed"]


def test_ambiguous_terminal_actions_are_dead_lettered(harness):
    scenario = _gated(harness)
    dispatched, _request = _dispatch_ok(harness, scenario)
    harness.store.fail_attempt(dispatched["action_id"], OWNER_A, "boom", False)
    letters = harness.store.list_dead_letters()
    assert len(letters) == 1
    assert letters[0]["action_id"] == dispatched["action_id"]


def test_closed_store_refuses(harness):
    scenario = _gated(harness)
    harness.store.close()
    with pytest.raises(ActionStoreError):
        harness.store.get_action(scenario.action["action_id"])

"""Durability and load properties of the turn-ingestion reservation ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from colony_sidecar.turns import (
    ReservationOutcome,
    TurnIdempotencyLedger,
    canonical_turn_digest,
)


def test_ten_thousand_identical_submissions_have_one_creation(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "turns.db")
    digest = canonical_turn_digest({"turn_id": "turn-load", "content": "same"})

    first = ledger.reserve("turn-load", digest)
    assert first.outcome == ReservationOutcome.CREATED
    ledger.complete(
        "turn-load",
        digest,
        {"accepted": True, "continuity_updated": True, "skipped_reason": None},
    )

    outcomes = [ledger.reserve("turn-load", digest).outcome for _ in range(9_999)]
    assert set(outcomes) == {ReservationOutcome.REPLAYED}


def test_concurrent_reservations_choose_exactly_one_creator(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "turns.db")
    digest = canonical_turn_digest({"turn_id": "turn-race", "content": "same"})

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(
            lambda _: ledger.reserve("turn-race", digest).outcome,
            range(128),
        ))

    assert outcomes.count(ReservationOutcome.CREATED) == 1
    assert set(outcomes) <= {
        ReservationOutcome.CREATED,
        ReservationOutcome.IN_PROGRESS,
    }


def test_reservation_survives_new_ledger_instance_and_detects_conflict(tmp_path):
    path = tmp_path / "turns.db"
    first = TurnIdempotencyLedger(path)
    digest = canonical_turn_digest({"content": "first"})
    first.reserve("turn-durable", digest)
    first.complete(
        "turn-durable",
        digest,
        {"accepted": True, "continuity_updated": False},
    )

    reopened = TurnIdempotencyLedger(path)
    replay = reopened.reserve("turn-durable", digest)
    conflict = reopened.reserve(
        "turn-durable", canonical_turn_digest({"content": "changed"})
    )

    assert replay.outcome == ReservationOutcome.REPLAYED
    assert replay.response["accepted"] is True
    assert conflict.outcome == ReservationOutcome.CONFLICT

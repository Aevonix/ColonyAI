"""Phase C durable autonomy scheduler contracts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import inspect
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import types

import pytest

import colony_sidecar.autonomy.scheduler as scheduler_module
from colony_sidecar.autonomy.scheduler import (
    AutonomyScheduler,
    ScheduleStore,
    TaskSchedule,
    _receipt_json,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _claim_from_process(arguments):
    path, now_text = arguments
    now = datetime.fromisoformat(now_text)
    store = ScheduleStore(path, clock=lambda: now)
    claim = store.claim_one_due(now, lease_seconds=30)
    return claim.lease_token if claim is not None else None


def _project_endless_list(send_connection) -> None:
    class EndlessList(list):
        def __iter__(self):
            while True:
                yield self

        def __str__(self):
            raise AssertionError("projector called __str__")

        def __repr__(self):
            raise AssertionError("projector called __repr__")

    try:
        send_connection.send(_receipt_json(EndlessList()))
    finally:
        send_connection.close()


def _scheduler(path, clock, **kwargs) -> AutonomyScheduler:
    return AutonomyScheduler(
        str(path),
        clock=clock,
        lease_seconds=kwargs.pop("lease_seconds", 30),
        base_backoff_seconds=kwargs.pop("base_backoff_seconds", 5),
        max_backoff_seconds=kwargs.pop("max_backoff_seconds", 20),
        **kwargs,
    )


def test_register_api_is_compatible_name_unique_stable_and_metadata_canonical(
    tmp_path,
):
    signature = inspect.signature(AutonomyScheduler.register)
    assert list(signature.parameters) == [
        "self", "name", "callback", "interval_seconds", "metadata",
    ]
    clock = FakeClock()
    path = tmp_path / "scheduler.db"
    scheduler = _scheduler(path, clock)

    first_id = scheduler.register(
        "stable_task", lambda: "first", 60,
        metadata={"z": "last", "a": "first"},
    )
    first_next = scheduler.list_schedules()[0].next_run
    second_id = scheduler.register(
        "stable_task", lambda: "second", 120,
        metadata={"a": "first", "z": "last"},
    )
    schedules = scheduler.list_schedules()
    assert first_id == second_id
    assert len(schedules) == 1
    assert schedules[0].name == "stable_task"
    assert schedules[0].interval_seconds == 120
    assert schedules[0].next_run == first_next
    assert scheduler.disable(first_id) is True
    assert scheduler.register(
        "stable_task", lambda: None, 120,
        metadata={"a": "first", "z": "last"},
    ) == first_id
    assert scheduler.list_schedules()[0].enabled is False
    assert scheduler.enable(first_id) is True

    with sqlite3.connect(path) as conn:
        encoded = conn.execute(
            "SELECT metadata FROM schedules WHERE name='stable_task'",
        ).fetchone()[0]
    assert encoded == '{"a":"first","z":"last"}'

    for name in ("", " has-space", "x" * 129):
        with pytest.raises(ValueError):
            scheduler.register(name, lambda: None, 10)
    for interval in (True, 0, -1, 31_536_001):
        with pytest.raises(ValueError):
            scheduler.register("bad_interval", lambda: None, interval)
    for metadata in (
        {"nested": {"not": "flat"}},
        {"x": "y" * 501},
        {f"key_{index}": index for index in range(33)},
        {"not_finite": float("nan")},
    ):
        with pytest.raises(ValueError):
            scheduler.register("bad_metadata", lambda: None, 10, metadata)


def test_legacy_database_migrates_additively_and_duplicate_names_are_preserved(
    tmp_path,
):
    path = tmp_path / "legacy.db"
    clock = FakeClock()
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                callback_name TEXT NOT NULL,
                last_run TEXT,
                next_run TEXT,
                enabled INTEGER DEFAULT 1,
                metadata TEXT DEFAULT '{}'
            )
        """)
        for suffix in ("a", "b"):
            conn.execute(
                """INSERT INTO schedules
                   (id,name,interval_seconds,callback_name,next_run,enabled,metadata)
                   VALUES (?,?,?,?,?,1,'{}')""",
                (f"legacy-{suffix}", "legacy_task", 60, "legacy_task",
                 clock().isoformat()),
            )

    scheduler = _scheduler(path, clock)
    schedules = scheduler.list_schedules()
    assert len(schedules) == 2
    canonical = next(item for item in schedules if item.name == "legacy_task")
    duplicate = next(item for item in schedules if item.name != "legacy_task")
    assert canonical.id == "legacy-a"
    assert duplicate.name.startswith("legacy_task#legacy-duplicate-")
    assert duplicate.enabled is False
    assert duplicate.degraded_reason == "migration_duplicate_name:legacy-a"

    same_id = scheduler.register("legacy_task", lambda: "ok", 90)
    assert same_id == "legacy-a"
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(schedules)")
        }
        indexes = conn.execute("PRAGMA index_list(schedules)").fetchall()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
    assert {
        "failure_count", "lease_token", "lease_expires_at",
        "degraded_reason", "created_at", "updated_at",
    } <= columns
    assert any(row[1] == "schedules_name_unique" and row[2] for row in indexes)
    assert {"schedule_run_attempts", "schedule_run_receipts"} <= tables


@pytest.mark.asyncio
async def test_legacy_null_next_run_is_repaired_and_invalid_metadata_fails_private(
    tmp_path,
):
    path = tmp_path / "invalid-legacy.db"
    clock = FakeClock()
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                callback_name TEXT NOT NULL,
                last_run TEXT,
                next_run TEXT,
                enabled INTEGER DEFAULT 1,
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute(
            """INSERT INTO schedules
               (id,name,interval_seconds,callback_name,next_run,enabled,metadata)
               VALUES ('legacy-invalid','legacy_invalid',60,'legacy_invalid',
                       NULL,1,?)""",
            (json.dumps({"nested": {"unsafe": True}}),),
        )

    scheduler = _scheduler(path, clock)
    schedule = scheduler.list_schedules()[0]
    assert schedule.next_run == clock()
    assert schedule.enabled is False
    assert schedule.degraded_reason == "migration_invalid_metadata"
    assert scheduler.health["degraded_schedules"] == 1
    assert await scheduler.tick() == []
    assert scheduler.register(
        "legacy_invalid", lambda: "recovered", 60,
        metadata={"description": "validated replacement"},
    ) == "legacy-invalid"
    assert scheduler.list_schedules()[0].enabled is True
    assert scheduler.list_schedules()[0].degraded_reason is None


@pytest.mark.asyncio
async def test_sync_and_async_success_write_attempt_receipt_then_advance(tmp_path):
    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)
    calls: list[str] = []

    def sync_callback():
        calls.append("sync")
        return {"kind": "sync"}

    async def async_callback():
        await asyncio.sleep(0)
        calls.append("async")
        return {"kind": "async"}

    scheduler.register("sync_task", sync_callback, 60)
    scheduler.register("async_task", async_callback, 120)
    results = await scheduler.tick()
    assert sorted(calls) == ["async", "sync"]
    assert {item["task"]: item["status"] for item in results} == {
        "async_task": "ok", "sync_task": "ok",
    }
    for schedule in scheduler.list_schedules():
        assert schedule.last_run == clock()
        assert schedule.next_run == clock() + timedelta(
            seconds=schedule.interval_seconds,
        )
        assert schedule.failure_count == 0
        assert schedule.lease_token is None
    attempts = scheduler.list_run_attempts()
    receipts = scheduler.list_run_receipts()
    assert len(attempts) == len(receipts) == 2
    assert {item["status"] for item in receipts} == {"success"}
    assert {item["attempt_id"] for item in attempts} == {
        item["attempt_id"] for item in receipts
    }


@pytest.mark.asyncio
async def test_custom_generator_and_future_awaitables_remain_callback_compatible(
    tmp_path,
):
    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)
    calls: list[str] = []

    class CustomAwaitable:
        def __await__(self):
            async def resolve():
                await asyncio.sleep(0)
                calls.append("custom")
                return {"kind": "custom"}

            return resolve().__await__()

    @types.coroutine
    def generator_coroutine():
        yield from asyncio.sleep(0).__await__()
        calls.append("generator")
        return {"kind": "generator"}

    def future_awaitable():
        future = asyncio.get_running_loop().create_future()
        future.set_result({"kind": "future"})
        calls.append("future")
        return future

    scheduler.register("custom_awaitable", CustomAwaitable, 60)
    scheduler.register("generator_coroutine", generator_coroutine, 60)
    scheduler.register("future_awaitable", future_awaitable, 60)

    results = await scheduler.tick()
    assert sorted(calls) == ["custom", "future", "generator"]
    returned = {item["task"]: item["result"] for item in results}
    assert returned == {
        "custom_awaitable": {"kind": "custom"},
        "future_awaitable": {"kind": "future"},
        "generator_coroutine": {"kind": "generator"},
    }
    receipts = {
        item["task_name"]: item["result"]
        for item in scheduler.list_run_receipts()
    }
    assert receipts == returned


@pytest.mark.asyncio
async def test_failure_preserves_last_run_and_retries_with_bounded_backoff_restart(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    calls = 0

    def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    scheduler = _scheduler(path, clock)
    task_id = scheduler.register("retry_task", failing, 100)
    first = await scheduler.tick()
    schedule = scheduler.list_schedules()[0]
    assert first[0]["status"] == "error"
    assert schedule.last_run is None
    assert schedule.failure_count == 1
    assert schedule.next_run == clock() + timedelta(seconds=5)
    assert schedule.lease_token is None

    restarted = _scheduler(path, clock)
    assert restarted.register("retry_task", failing, 100) == task_id
    assert await restarted.tick() == []
    clock.advance(5)
    await restarted.tick()
    schedule = restarted.list_schedules()[0]
    assert schedule.last_run is None
    assert schedule.failure_count == 2
    assert schedule.next_run == clock() + timedelta(seconds=10)

    restarted.register("retry_task", lambda: "recovered", 100)
    clock.advance(10)
    recovered = await restarted.tick()
    schedule = restarted.list_schedules()[0]
    assert recovered[0]["status"] == "ok"
    assert schedule.last_run == clock()
    assert schedule.failure_count == 0
    assert schedule.next_run == clock() + timedelta(seconds=100)
    assert [item["status"] for item in restarted.list_run_receipts()] == [
        "error", "error", "success",
    ]


@pytest.mark.asyncio
async def test_concurrent_scheduler_ticks_claim_due_task_once(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def callback():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "done"

    first = _scheduler(path, clock)
    second = _scheduler(path, clock)
    first.register("one_claim", callback, 60)
    second.register("one_claim", callback, 60)

    first_tick = asyncio.create_task(first.tick())
    await asyncio.wait_for(started.wait(), timeout=1)
    second_result = await second.tick()
    assert second_result == []
    assert calls == 1
    release.set()
    first_result = await first_tick
    assert first_result[0]["status"] == "ok"
    assert len(first.list_run_attempts()) == 1
    assert len(first.list_run_receipts()) == 1


def test_cross_process_due_claim_is_atomic(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    scheduler = _scheduler(path, clock)
    scheduler.register("process_claim", lambda: None, 60)

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        tokens = list(pool.map(
            _claim_from_process,
            [(str(path), clock().isoformat())] * 2,
        ))
    assert sum(token is not None for token in tokens) == 1
    assert len({token for token in tokens if token is not None}) == 1
    assert scheduler.health["active_leases"] == 1
    assert scheduler.health["open_attempts"] == 1


@pytest.mark.asyncio
async def test_expired_crash_lease_has_one_atomic_retry_and_late_finish_is_ignored(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    crashed = _scheduler(path, clock, lease_seconds=10)
    recovered = _scheduler(path, clock, lease_seconds=10)
    calls = 0

    def callback():
        nonlocal calls
        calls += 1
        return "recovered"

    crashed.register("crash_task", callback, 60)
    recovered.register("crash_task", callback, 60)
    abandoned = crashed._store.claim_one_due(clock(), lease_seconds=10)
    assert abandoned is not None
    assert abandoned.lease_token
    assert recovered.health["active_leases"] == 1
    assert await recovered.tick() == []

    clock.advance(11)
    assert recovered.health["expired_leases"] == 1
    assert recovered.health["healthy"] is False
    result = await recovered.tick()
    assert result[0]["status"] == "ok"
    assert calls == 1
    attempts = recovered.list_run_attempts()
    receipts = recovered.list_run_receipts()
    assert len(attempts) == len(receipts) == 2
    assert [item["status"] for item in receipts] == [
        "lease_expired", "success",
    ]
    assert attempts[0]["lease_token"] != attempts[1]["lease_token"]
    assert crashed._store.complete_success(
        abandoned, {"late": True}, now=clock(),
    ) is False
    assert len(recovered.list_run_receipts()) == 2
    assert recovered.health["active_leases"] == 0
    assert recovered.health["expired_leases"] == 0


def test_completion_rejects_cross_schedule_attempt_without_poisoning_claims(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    scheduler = _scheduler(path, clock)
    scheduler.register("claim_a", lambda: None, 60)
    scheduler.register("claim_b", lambda: None, 60)
    claim_a = scheduler._store.claim_one_due(clock(), lease_seconds=30)
    assert claim_a is not None
    claim_b = scheduler._store.claim_one_due(
        clock(), lease_seconds=30,
        exclude_schedule_ids={claim_a.schedule.id},
    )
    assert claim_b is not None

    forged = type(claim_a)(
        schedule=claim_a.schedule,
        attempt_id=claim_b.attempt_id,
        lease_token=claim_a.lease_token,
        claimed_at=claim_a.claimed_at,
        lease_expires_at=claim_a.lease_expires_at,
    )
    assert scheduler._store.complete_success(
        forged, {"forged": True}, now=clock(),
    ) is False
    assert scheduler.list_run_receipts() == []

    assert scheduler._store.complete_success(
        claim_a, {"claim": "a"}, now=clock(),
    ) is True
    assert scheduler._store.complete_success(
        claim_b, {"claim": "b"}, now=clock(),
    ) is True
    receipts = scheduler.list_run_receipts()
    assert len(receipts) == 2
    assert {item["attempt_id"] for item in receipts} == {
        claim_a.attempt_id, claim_b.attempt_id,
    }
    assert {item["status"] for item in receipts} == {"success"}


def test_idempotent_enable_does_not_revoke_a_live_lease(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    first = _scheduler(path, clock)
    second = _scheduler(path, clock)
    task_id = first.register("live_claim", lambda: None, 60)
    second.register("live_claim", lambda: None, 60)
    claim = first._store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None

    assert second.register(
        "live_claim", lambda: None, 120, metadata={"revision": 2},
    ) == task_id
    registered = first.list_schedules()[0]
    assert registered.interval_seconds == 120
    assert registered.lease_token == claim.lease_token
    assert second.enable(task_id) is True
    schedule = first.list_schedules()[0]
    assert schedule.lease_token == claim.lease_token
    assert first.health["active_leases"] == 1
    assert second._store.claim_one_due(clock(), lease_seconds=30) is None
    assert first._store.complete_success(claim, "done", now=clock()) is True
    assert [item["status"] for item in first.list_run_receipts()] == [
        "success",
    ]


def test_compatibility_upsert_preserves_scheduler_owned_state_during_claim(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    original_last_run = clock() - timedelta(minutes=5)
    original_next_run = clock()
    stale = TaskSchedule(
        id="compat-upsert",
        name="compat_upsert",
        interval_seconds=60,
        callback_name="compat_upsert",
        last_run=original_last_run,
        next_run=original_next_run,
        enabled=True,
        metadata={"version": 1},
        failure_count=3,
        degraded_reason="operator_observed_degradation",
    )
    store.upsert(stale)
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None

    # This object predates the claim. Configuration changes remain valid, but
    # none of its stale scheduler-owned fields may revoke or rewrite the lease.
    stale.interval_seconds = 120
    stale.metadata = {"version": 2}
    stale.last_run = None
    stale.next_run = clock() - timedelta(hours=1)
    stale.failure_count = 0
    stale.lease_token = "caller-supplied-token"
    stale.lease_expires_at = clock() + timedelta(hours=1)
    stale.degraded_reason = None
    store.upsert(stale)

    persisted = store.list_all()[0]
    assert persisted.interval_seconds == 120
    assert persisted.metadata == {"version": 2}
    assert persisted.last_run == original_last_run
    assert persisted.next_run == original_next_run
    assert persisted.failure_count == 3
    assert persisted.degraded_reason == "operator_observed_degradation"
    assert persisted.lease_token == claim.lease_token
    assert persisted.lease_expires_at == claim.lease_expires_at
    assert store.claim_one_due(clock(), lease_seconds=30) is None
    assert store.complete_success(claim, "done", now=clock()) is True
    assert [item["status"] for item in store.list_receipts()] == ["success"]


def test_enable_after_compatibility_disable_preserves_original_live_claim(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    stale = TaskSchedule(
        id="compat-disable-enable",
        name="compat_disable_enable",
        interval_seconds=60,
        callback_name="compat_disable_enable",
        next_run=clock(),
    )
    store.upsert(stale)
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None

    stale.enabled = False
    stale.interval_seconds = 120
    store.upsert(stale)
    disabled = store.list_all()[0]
    assert disabled.enabled is False
    assert disabled.lease_token == claim.lease_token

    assert store.set_enabled(stale.id, True) is True
    enabled = store.list_all()[0]
    assert enabled.enabled is True
    assert enabled.lease_token == claim.lease_token
    assert enabled.lease_expires_at == claim.lease_expires_at
    assert store.claim_one_due(clock(), lease_seconds=30) is None
    assert [item["attempt_id"] for item in store.list_attempts()] == [
        claim.attempt_id,
    ]
    assert store.list_receipts() == []

    assert store.complete_success(claim, "done", now=clock()) is True
    receipts = store.list_receipts()
    assert len(receipts) == 1
    assert receipts[0]["attempt_id"] == claim.attempt_id
    assert receipts[0]["lease_token"] == claim.lease_token
    assert receipts[0]["status"] == "success"


def test_delete_refuses_live_or_unreceipted_claim_then_allows_terminal_row(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    store.upsert(TaskSchedule(
        id="delete-claimed",
        name="delete_claimed",
        interval_seconds=60,
        callback_name="delete_claimed",
        next_run=clock(),
    ))
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None

    assert store.delete(claim.schedule.id) is False
    assert store.list_all()[0].lease_token == claim.lease_token
    assert store.list_receipts() == []
    assert store.complete_success(claim, "done", now=clock()) is True

    assert store.delete(claim.schedule.id) is True
    assert store.list_all() == []
    assert [item["status"] for item in store.list_receipts()] == ["success"]
    assert store.delete(claim.schedule.id) is False


def test_delete_refuses_open_attempt_even_if_legacy_writer_cleared_lease(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    store.upsert(TaskSchedule(
        id="delete-open-attempt",
        name="delete_open_attempt",
        interval_seconds=60,
        callback_name="delete_open_attempt",
        next_run=clock(),
    ))
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE schedules SET lease_token=NULL,lease_expires_at=NULL "
            "WHERE id=?",
            (claim.schedule.id,),
        )

    assert store.delete(claim.schedule.id) is False
    assert store.complete_success(claim, "late", now=clock()) is False
    assert [item["status"] for item in store.list_receipts()] == ["lease_lost"]
    assert store.delete(claim.schedule.id) is True


def test_disable_terminalizes_live_claim_before_explicit_delete(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    store.upsert(TaskSchedule(
        id="cancel-then-delete",
        name="cancel_then_delete",
        interval_seconds=60,
        callback_name="cancel_then_delete",
        next_run=clock(),
    ))
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None

    assert store.set_enabled(claim.schedule.id, False) is True
    assert [item["status"] for item in store.list_receipts()] == ["disabled"]
    assert store.delete(claim.schedule.id) is True
    assert store.complete_success(claim, "late", now=clock()) is False
    assert [item["status"] for item in store.list_receipts()] == ["disabled"]


@pytest.mark.asyncio
async def test_unknown_callback_degrades_without_deleting_and_register_recovers(
    tmp_path,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    original = _scheduler(path, clock)
    task_id = original.register("durable_task", lambda: "ok", 60)

    restarted = _scheduler(path, clock)
    result = await restarted.tick()
    schedules = restarted.list_schedules()
    assert result == [{
        "task": "durable_task",
        "status": "degraded",
        "error": "callback_unregistered:durable_task",
    }]
    assert len(schedules) == 1
    assert schedules[0].id == task_id
    assert schedules[0].enabled is False
    assert schedules[0].degraded_reason == "callback_unregistered:durable_task"
    assert restarted.health["healthy"] is False
    assert restarted.health["degraded_schedules"] == 1
    assert restarted.list_run_receipts()[0]["status"] == "degraded"

    assert restarted.register("durable_task", lambda: "restored", 60) == task_id
    restored = await restarted.tick()
    assert restored[0]["status"] == "ok"
    assert restarted.list_schedules()[0].enabled is True
    assert restarted.list_schedules()[0].degraded_reason is None


def test_attempts_and_receipts_are_database_append_only(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    scheduler = _scheduler(path, clock)
    scheduler.register("append_only", lambda: None, 60)
    claim = scheduler._store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None
    assert scheduler._store.complete_success(claim, None, now=clock()) is True

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(
                "UPDATE schedule_run_attempts SET task_name='changed'",
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM schedule_run_receipts")


@pytest.mark.asyncio
async def test_non_json_oversized_and_nan_results_get_bounded_success_receipts(
    tmp_path,
):
    class ResultObject:
        def __repr__(self) -> str:
            return "result-object"

    class HostileResult:
        def __str__(self) -> str:
            raise RuntimeError("no string")

        def __repr__(self) -> str:
            raise RuntimeError("no representation")

    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)
    scheduler.register("object_result", lambda: ResultObject(), 60)
    scheduler.register("huge_result", lambda: "x" * 100_000, 60)
    scheduler.register("nan_result", lambda: float("nan"), 60)
    scheduler.register("hostile_result", lambda: HostileResult(), 60)
    scheduler.register("bmp_result", lambda: "漢" * 100_000, 60)
    scheduler.register("emoji_result", lambda: "😀" * 100_000, 60)
    scheduler.register(
        "nested_result",
        lambda: {
            "nested": [
                {"payload": "😀" * 20_000},
                {"payload": "漢" * 20_000},
            ],
        },
        60,
    )

    results = await scheduler.tick()
    assert {item["status"] for item in results} == {"ok"}
    receipts = scheduler.list_run_receipts()
    assert len(receipts) == 7
    assert {item["status"] for item in receipts} == {"success"}
    assert all(
        len(item["result_json"].encode("utf-8")) <= 8192
        for item in receipts
    )
    projections = {
        item["task_name"]: item["result"] for item in receipts
    }
    returned = {
        item["task"]: item["result"] for item in results
    }
    assert returned == projections
    assert all(
        len(json.dumps(
            item["result"], ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")) <= 8192
        for item in results
    )
    assert projections["object_result"]["projection"] == "bounded"
    assert projections["object_result"]["reason"] == "not_json"
    assert projections["huge_result"]["reason"] == "oversized"
    assert projections["nan_result"]["reason"] == "not_json"
    assert projections["hostile_result"]["reason"] == "not_json"
    assert projections["hostile_result"]["type"] == "HostileResult"
    assert projections["bmp_result"]["reason"] == "oversized"
    assert "漢" in projections["bmp_result"]["summary"]
    assert projections["emoji_result"]["reason"] == "oversized"
    assert "😀" in projections["emoji_result"]["summary"]
    assert projections["nested_result"]["reason"] == "oversized"
    assert projections["nested_result"]["summary"].startswith(
        '{"nested":[{"payload":"',
    )


def test_endless_container_subclass_projection_is_bounded_by_process_timeout():
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_project_endless_list,
        args=(send_connection,),
    )
    process.start()
    send_connection.close()
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
        pytest.fail("receipt projection invoked the endless user iterator")
    assert process.exitcode == 0
    projected = json.loads(receive_connection.recv())
    receive_connection.close()
    assert projected["reason"] == "not_json"
    assert projected["type"] == "EndlessList"
    assert projected["summary"] == "<EndlessList opaque>"
    assert len(json.dumps(projected, separators=(",", ":")).encode()) <= 8192


def test_projection_is_cross_process_deterministic_and_ignores_object_addresses():
    script = """
from colony_sidecar.autonomy.scheduler import _receipt_json
class PlainResult:
    pass
mixed = dict({
    ("oversized", "x" * 70_000),
    ("opaque", PlainResult()),
})
print(_receipt_json({"gamma", "alpha", "beta"}))
print(_receipt_json({PlainResult(), PlainResult()}))
print(_receipt_json(mixed))
"""
    projections = []
    for seed in ("1", "2", "3"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        projections.append(subprocess.check_output(
            [sys.executable, "-c", script],
            env=environment,
            text=True,
            timeout=5,
        ).strip())
    assert len(set(projections)) == 1

    first = json.loads(_receipt_json(object()))
    second = json.loads(_receipt_json(object()))
    assert first == second
    assert "0x" not in first["summary"]
    assert first["summary"] == "<object opaque>"


def test_oversized_projection_preserves_diagnostic_whitespace_exactly():
    projected = json.loads(_receipt_json(
        "MARK     FIVE-SPACES" + (" payload" * 5000),
    ))
    assert projected["reason"] == "oversized"
    assert "MARK     FIVE-SPACES" in projected["summary"]
    assert "MARK FIVE-SPACES" not in projected["summary"]
    assert len(json.dumps(projected, separators=(",", ":")).encode()) <= 8192


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            "result",
            ["result", "HostileResult", "not_json", "<HostileResult opaque>"],
        ),
        (
            "exception_class",
            ["exception_class", "HostileError", "stable"],
        ),
        (
            "exception_argument",
            [
                "exception_argument",
                "RuntimeError",
                "<HostileArgument opaque>",
            ],
        ),
    ],
)
def test_hostile_metaclass_name_data_descriptor_never_runs(
    case, expected,
):
    script = r'''
import json
import os
from colony_sidecar.autonomy.scheduler import (
    _receipt_json,
    _safe_exception_projection,
)

class HostileMeta(type):
    @property
    def __name__(cls):
        while True:
            pass

case = os.environ["SCHEDULER_HOSTILE_TYPE_CASE"]
if case == "result":
    class HostileResult(metaclass=HostileMeta):
        pass
    projection = json.loads(_receipt_json(HostileResult()))
    rendered = [case, projection["type"], projection["reason"], projection["summary"]]
elif case == "exception_class":
    class HostileError(RuntimeError, metaclass=HostileMeta):
        pass
    rendered = [case, *_safe_exception_projection(HostileError("stable"))]
else:
    class HostileArgument(metaclass=HostileMeta):
        pass
    rendered = [case, *_safe_exception_projection(RuntimeError(HostileArgument()))]
print(json.dumps(rendered, separators=(",", ":")))
'''
    environment = dict(os.environ)
    environment["SCHEDULER_HOSTILE_TYPE_CASE"] = case
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        env=environment,
        text=True,
        timeout=3,
    )
    assert json.loads(output) == expected


@pytest.mark.parametrize("attribute", ["__mro__", "__dict__"])
def test_full_tick_awaitability_ignores_hostile_metaclass_descriptors(
    attribute,
):
    script = r'''
import asyncio
from datetime import datetime, timezone
import json
import os
import tempfile
from colony_sidecar.autonomy.scheduler import AutonomyScheduler

hostile_attribute = os.environ["SCHEDULER_HOSTILE_AWAIT_ATTRIBUTE"]

class HostileMeta(type):
    @property
    def __mro__(cls):
        if hostile_attribute == "__mro__":
            while True:
                pass
        return type.__dict__["__mro__"].__get__(cls, type)

    @property
    def __dict__(cls):
        if hostile_attribute == "__dict__":
            while True:
                pass
        return type.__dict__["__dict__"].__get__(cls, type)

class HostileResult(metaclass=HostileMeta):
    pass

async def run():
    with tempfile.TemporaryDirectory() as directory:
        now = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)
        scheduler = AutonomyScheduler(
            f"{directory}/scheduler.db",
            clock=lambda: now,
            lease_seconds=30,
        )
        scheduler.register("hostile_result", lambda: HostileResult(), 60)
        results = await scheduler.tick()
        receipt = scheduler.list_run_receipts()[0]
        return {
            "results": results,
            "receipt": receipt,
            "health": scheduler.health,
        }

print(json.dumps(asyncio.run(run()), separators=(",", ":"), sort_keys=True))
'''
    environment = dict(os.environ)
    environment["SCHEDULER_HOSTILE_AWAIT_ATTRIBUTE"] = attribute
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        env=environment,
        text=True,
        timeout=3,
    )
    payload = json.loads(output)
    returned = payload["results"][0]
    assert returned["status"] == "ok"
    assert returned["result"] == payload["receipt"]["result"]
    assert returned["result"]["type"] == "HostileResult"
    assert payload["health"]["open_attempts"] == 0
    assert payload["health"]["active_leases"] == 0


def test_full_tick_treats_hostile_subclass_await_key_as_opaque_despite_base():
    script = r'''
import asyncio
from datetime import datetime, timezone
import json
import tempfile
from colony_sidecar.autonomy.scheduler import AutonomyScheduler

armed = False

class HostileKey(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        if armed:
            while True:
                pass
        return str.__eq__(self, other)

class HostileAwaitableMeta(type):
    def __new__(metaclass, name, bases, namespace):
        await_method = namespace.pop("__await__")
        namespace[HostileKey("__await__")] = await_method
        return type.__new__(metaclass, name, bases, namespace)

class ExactAwaitableBase:
    def __await__(self):
        async def resolve():
            await asyncio.sleep(0)
            return {"kind": "base-awaitable"}

        return resolve().__await__()

class HostileSubclass(ExactAwaitableBase, metaclass=HostileAwaitableMeta):
    def __await__(self):
        async def resolve():
            await asyncio.sleep(0)
            return {"kind": "hostile-subclass-awaitable"}

        return resolve().__await__()

armed = True

async def run():
    with tempfile.TemporaryDirectory() as directory:
        now = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)
        scheduler = AutonomyScheduler(
            f"{directory}/scheduler.db",
            clock=lambda: now,
            lease_seconds=30,
        )
        scheduler.register("hostile_subclass", HostileSubclass, 60)
        results = await scheduler.tick()
        receipt = scheduler.list_run_receipts()[0]
        return {
            "results": results,
            "receipt": receipt,
            "health": scheduler.health,
        }

print(json.dumps(asyncio.run(run()), separators=(",", ":"), sort_keys=True))
'''
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        text=True,
        timeout=3,
    )
    payload = json.loads(output)
    returned = payload["results"][0]
    assert returned["task"] == "hostile_subclass"
    assert returned["status"] == "ok"
    assert returned["result"]["projection"] == "bounded"
    assert returned["result"]["reason"] == "not_json"
    assert returned["result"]["summary"] == "<HostileSubclass opaque>"
    assert returned["result"]["type"] == "HostileSubclass"
    assert returned["result"] == payload["receipt"]["result"]
    assert len(payload["receipt"]["result_json"].encode("utf-8")) <= 8192
    assert payload["health"]["open_attempts"] == 0
    assert payload["health"]["active_leases"] == 0


@pytest.mark.asyncio
async def test_full_tick_treats_exact_none_await_method_as_opaque(tmp_path):
    class ExactAwaitableBase:
        def __await__(self):
            async def resolve():
                await asyncio.sleep(0)
                return {"kind": "base-awaitable"}

            return resolve().__await__()

    class ExplicitNonAwaitable(ExactAwaitableBase):
        __await__ = None

    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)
    scheduler.register("explicit_nonawaitable", ExplicitNonAwaitable, 60)

    results = await scheduler.tick()
    receipt = scheduler.list_run_receipts()[0]
    returned = results[0]
    assert returned["task"] == "explicit_nonawaitable"
    assert returned["status"] == "ok"
    assert returned["result"]["projection"] == "bounded"
    assert returned["result"]["reason"] == "not_json"
    assert returned["result"]["summary"] == "<ExplicitNonAwaitable opaque>"
    assert returned["result"]["type"] == "ExplicitNonAwaitable"
    assert returned["result"] == receipt["result"]
    assert receipt["status"] == "success"
    assert scheduler.health["open_attempts"] == 0
    assert scheduler.health["active_leases"] == 0


def test_projection_pins_cycle_depth_node_integer_and_surrogate_bounds():
    cycle = []
    cycle.append(cycle)
    too_deep = 0
    for _ in range(10):
        too_deep = [too_deep]
    too_many_nodes = [[index, index, index, index] for index in range(64)]
    huge_integer = 1 << 5000

    for value, reason in (
        (cycle, "not_json"),
        (too_deep, "oversized"),
        (too_many_nodes, "oversized"),
        (huge_integer, "oversized"),
    ):
        encoded = _receipt_json(value)
        projected = json.loads(encoded)
        assert projected["projection"] == "bounded"
        assert projected["reason"] == reason
        assert len(encoded.encode("utf-8")) <= 8192

    small_surrogate = _receipt_json({"value": "\ud800"})
    assert small_surrogate.isascii()
    assert json.loads(small_surrogate) == {"value": "\ud800"}
    large_surrogate = _receipt_json("\ud800" * 70_000)
    assert large_surrogate.isascii()
    assert json.loads(large_surrogate)["reason"] == "oversized"
    assert len(large_surrogate.encode("utf-8")) <= 8192


def test_concurrent_builtin_mutation_projection_is_process_bounded():
    script = r'''
import json
import sys
import threading
from colony_sidecar.autonomy.scheduler import _receipt_json

sys.setswitchinterval(0.000001)
shared_list = list(range(64))
shared_dict = {str(index): index for index in range(64)}
stop = threading.Event()

def mutate():
    index = 0
    while not stop.is_set():
        shared_list.append(index)
        if shared_list:
            shared_list.pop(0)
        key = str(index % 64)
        shared_dict.pop(key, None)
        shared_dict[key] = index
        index += 1

thread = threading.Thread(target=mutate, daemon=True)
thread.start()
try:
    for _ in range(2_000):
        for value in (shared_list, shared_dict):
            encoded = _receipt_json(value)
            json.loads(encoded)
            if len(encoded.encode("utf-8")) > 8192:
                raise AssertionError("projection exceeded its byte bound")
finally:
    stop.set()
    thread.join(timeout=1)
    if thread.is_alive():
        raise AssertionError("mutation thread did not stop")
print("bounded")
'''
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        text=True,
        timeout=10,
    )
    assert output.strip() == "bounded"


@pytest.mark.asyncio
async def test_hostile_metaclass_result_terminalizes_without_user_conversion(
    tmp_path,
):
    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name in {"__name__", "__module__", "__qualname__"}:
                raise AssertionError(f"unsafe metaclass access: {name}")
            return type.__getattribute__(cls, name)

    class HostileResult(metaclass=HostileMeta):
        def __str__(self):
            raise AssertionError("projector called __str__")

        def __repr__(self):
            raise AssertionError("projector called __repr__")

    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)
    scheduler.register("hostile_metaclass", lambda: HostileResult(), 60)

    result = await scheduler.tick()
    assert result[0]["status"] == "ok"
    receipt = scheduler.list_run_receipts()[0]
    assert receipt["status"] == "success"
    assert receipt["result"]["type"] == "HostileResult"
    assert scheduler.health["run_attempts"] == 1
    assert scheduler.health["terminal_receipts"] == 1
    assert scheduler.health["open_attempts"] == 0
    assert scheduler.health["active_leases"] == 0


def test_success_projection_finishes_before_sqlite_write_transaction(
    tmp_path, monkeypatch,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    store.upsert(TaskSchedule(
        id="projection-before-write",
        name="projection_before_write",
        interval_seconds=60,
        callback_name="projection_before_write",
        next_run=clock(),
    ))
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None
    projection_started = threading.Event()
    release_projection = threading.Event()

    def blocking_projection(_value):
        projection_started.set()
        if not release_projection.wait(timeout=2):
            raise AssertionError("test did not release projection")
        return '{"bounded":true}'

    monkeypatch.setattr(scheduler_module, "_receipt_json", blocking_projection)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            store.complete_success,
            claim,
            {"result": "value"},
            now=clock(),
        )
        assert projection_started.wait(timeout=1)
        with sqlite3.connect(path, timeout=0.2, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        release_projection.set()
        assert future.result(timeout=2) is True

    assert store.health_snapshot(clock())["open_attempts"] == 0
    assert store.health_snapshot(clock())["active_leases"] == 0
    assert store.list_receipts()[0]["result"] == {"bounded": True}


def test_failure_projection_finishes_before_sqlite_write_transaction(
    tmp_path, monkeypatch,
):
    path = tmp_path / "scheduler.db"
    clock = FakeClock()
    store = ScheduleStore(str(path), clock=clock)
    store.upsert(TaskSchedule(
        id="failure-before-write",
        name="failure_before_write",
        interval_seconds=60,
        callback_name="failure_before_write",
        next_run=clock(),
    ))
    claim = store.claim_one_due(clock(), lease_seconds=30)
    assert claim is not None
    projection_started = threading.Event()
    release_projection = threading.Event()

    def blocking_projection(_error):
        projection_started.set()
        if not release_projection.wait(timeout=2):
            raise AssertionError("test did not release failure projection")
        return "RuntimeError", "bounded failure"

    monkeypatch.setattr(
        scheduler_module,
        "_safe_exception_projection",
        blocking_projection,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            store.complete_failure,
            claim,
            RuntimeError("untrusted"),
            now=clock(),
            base_backoff_seconds=5,
            max_backoff_seconds=20,
        )
        assert projection_started.wait(timeout=1)
        with sqlite3.connect(path, timeout=0.2, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        release_projection.set()
        assert future.result(timeout=2) is True

    receipt = store.list_receipts()[0]
    assert receipt["error_type"] == "RuntimeError"
    assert receipt["error_message"] == "bounded failure"
    assert store.health_snapshot(clock())["open_attempts"] == 0
    assert store.health_snapshot(clock())["active_leases"] == 0


@pytest.mark.asyncio
async def test_exception_text_is_safely_bounded_before_terminal_receipt(tmp_path):
    class HostileError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("string conversion failed")

    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)

    def fail():
        raise HostileError()

    scheduler.register("hostile_error", fail, 60)
    result = await scheduler.tick()
    assert result[0]["status"] == "error"
    assert len(result[0]["error"]) <= 1000
    receipt = scheduler.list_run_receipts()[0]
    assert receipt["status"] == "error"
    assert receipt["error_type"] == "HostileError"
    assert len(receipt["error_message"]) <= 1000
    assert scheduler.list_schedules()[0].lease_token is None


@pytest.mark.asyncio
async def test_hostile_exception_diagnostics_are_projected_before_transaction(
    tmp_path,
):
    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name in {"__name__", "__module__", "__qualname__"}:
                raise AssertionError(f"unsafe metaclass access: {name}")
            return type.__getattribute__(cls, name)

    class HostileError(RuntimeError, metaclass=HostileMeta):
        def __str__(self):
            raise AssertionError("diagnostics called __str__")

        def __repr__(self):
            raise AssertionError("diagnostics called __repr__")

    clock = FakeClock()
    scheduler = _scheduler(tmp_path / "scheduler.db", clock)

    def fail():
        raise HostileError("stable     spacing")

    scheduler.register("hostile_exception", fail, 60)
    result = await scheduler.tick()
    assert result == [{
        "task": "hostile_exception",
        "status": "error",
        "error": "stable     spacing",
    }]
    receipt = scheduler.list_run_receipts()[0]
    assert receipt["error_type"] == "HostileError"
    assert receipt["error_message"] == "stable     spacing"
    assert scheduler.health["open_attempts"] == 0
    assert scheduler.health["active_leases"] == 0

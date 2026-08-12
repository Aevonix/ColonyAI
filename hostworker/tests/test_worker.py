"""Worker state machine: one mutation ever, GET-only recovery, fail-closed."""

import os
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from colony_hostworker.admission import FileDispatchAdmission
from colony_hostworker.contract import (
    EFFECT_SCHEMA,
    EXECUTION_RESULT_SCHEMA,
    canonical_json_utf8,
    sha256_json_utf8,
)
from colony_hostworker.conformance import (
    ManualClock,
    build_envelope,
    build_intent,
    delivery_gate_evidence,
    grant_gate_evidence,
)
from colony_hostworker.sqlite_store import SqliteActionStore
from colony_hostworker.worker import (
    DEFAULT_ACTION_TYPE,
    DEFAULT_SOURCE_PREFIX,
    GovernedActionWorker,
    GovernedActionWorkerError,
    OBSERVATION_MAX_ATTEMPTS,
    validate_execution_result,
)

PRINCIPAL = "test-host"
SOURCE = DEFAULT_SOURCE_PREFIX + PRINCIPAL
ORIGIN = "http://127.0.0.1:8123"
DEFAULT_TOOLS = (
    "colony_create_commitment",
    "colony_task_complete",
    "colony_autonomy_enable",
)


def completed_result(request, observed_at):
    effect = {
        "schema": EFFECT_SCHEMA,
        "version": 1,
        "effect_id": "effect-" + request["execution_digest"][:12],
        "outcome": "recorded",
        "verification": {"ok": True},
    }
    return {
        "schema": EXECUTION_RESULT_SCHEMA,
        "version": 1,
        "execution_digest": request["execution_digest"],
        "action_id": request["action_id"],
        "action_digest": request["action_digest"],
        "intent_id": request["intent_id"],
        "intent_digest": request["intent_digest"],
        "tool_name": request["tool_name"],
        "status": "completed",
        "effect_state": "performed",
        "effect": effect,
        "effect_digest": sha256_json_utf8(effect),
        "observed_at": float(observed_at),
    }


class FakeDispatcher:
    """Deterministic endpoint double: the durable projection is stable per
    execution digest, exactly like the real ledger-backed endpoint."""

    def __init__(self, clock):
        self.clock = clock
        self.put_count = 0
        self.get_count = 0
        self.execute_error = None
        self.observe_error = None
        self.observe_errors_remaining = 0
        self._projections = {}

    def _result(self, request):
        digest = request["execution_digest"]
        if digest not in self._projections:
            self._projections[digest] = completed_result(request, self.clock())
        return dict(self._projections[digest])

    def execute(self, request):
        self.put_count += 1
        if self.execute_error is not None:
            raise self.execute_error
        return self._result(request)

    def observe(self, request):
        self.get_count += 1
        if self.observe_errors_remaining > 0:
            self.observe_errors_remaining -= 1
            raise RuntimeError("endpoint unavailable")
        if self.observe_error is not None:
            raise self.observe_error
        return self._result(request)


@dataclass
class Env:
    clock: ManualClock
    store: SqliteActionStore
    dispatcher: FakeDispatcher
    worker: GovernedActionWorker
    admission_path: str
    enabled: tuple = field(default_factory=tuple)


def write_admission_file(path, enabled, clock):
    document = {
        "schema": "ColonyHostWorkerAdmissionV1",
        "version": 1,
        "authorized": True,
        "authorization_id": uuid.uuid4().hex,
        "colony_origin": ORIGIN,
        "enabled_tools": sorted(enabled),
        "binding_identity": None,
        "created_at": clock() - 10.0,
        "expires_at": clock() + 24 * 3600.0,
    }
    raw = canonical_json_utf8(document) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(raw)
    os.chmod(path, 0o600)


def make_env(tmp_path, *, enabled=DEFAULT_TOOLS, admitted=True):
    clock = ManualClock()
    store = SqliteActionStore(str(tmp_path / "store.sqlite3"), clock=clock)
    dispatcher = FakeDispatcher(clock)
    admission_path = str(tmp_path / "admission.json")
    admission = FileDispatchAdmission(
        admission_path,
        colony_origin=ORIGIN,
        enabled_tools=enabled,
        clock=clock,
    )
    if admitted:
        write_admission_file(admission_path, enabled, clock)
    worker = GovernedActionWorker(
        store,
        dispatcher,
        admission,
        enabled_tools=enabled,
        admission_principals=(PRINCIPAL,),
        clock=clock,
        owner="worker-under-test",
    )
    return Env(
        clock=clock,
        store=store,
        dispatcher=dispatcher,
        worker=worker,
        admission_path=admission_path,
        enabled=tuple(enabled),
    )


def gated_action(
    env,
    *,
    tool_name="colony_create_commitment",
    args=None,
    grant=False,
    expires_in=3600.0,
    grant_expires_in=3600.0,
):
    intent = build_intent(tool_name=tool_name, args=args)
    envelope = build_envelope(intent)
    action = env.store.propose(
        intent.idempotency_key,
        SOURCE,
        DEFAULT_ACTION_TYPE,
        envelope,
        source_ref=intent.intent_id,
    )
    now = env.clock.now()
    if grant:
        evidence = grant_gate_evidence(
            action,
            decided_at=now,
            expires_at=now + expires_in,
            grant_expires_at=now + grant_expires_in,
        )
    else:
        evidence = delivery_gate_evidence(
            action, decided_at=now, expires_at=now + expires_in
        )
    env.store.gate(
        action["action_id"], evidence, external_id=evidence["approval_id"]
    )
    return action["action_id"], intent


# ------------------------------------------------------------------ happy


def test_happy_path_completes_with_exactly_one_put(tmp_path):
    env = make_env(tmp_path)
    action_id, intent = gated_action(env)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "completed"
    assert env.dispatcher.put_count == 1
    assert env.dispatcher.get_count == 1  # read-only verification
    assert result["result"]["status"] == "completed"
    assert result["result"]["intent_id"] == intent.intent_id
    kinds = [receipt["kind"] for receipt in env.store.list_receipts(action_id)]
    assert sorted(kinds) == sorted(
        [
            "gate",
            "dispatch_recovery",
            "governed_action_acceptance",
            "verification",
        ]
    )
    assert env.worker.process_one() is None  # nothing left


def test_completed_action_result_round_trips(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    env.worker.process_one()
    action = env.store.get_action(action_id)
    assert action["attempt_count"] == 1
    assert action["lease_owner"] is None
    assert action["terminal_at"] is not None


# --------------------------------------------------------------- recovery


def test_put_failure_recovers_via_get_only(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    env.dispatcher.execute_error = OSError("connection reset mid-PUT")
    result = env.worker.process_one()
    assert result is not None and result["state"] == "completed"
    assert env.dispatcher.put_count == 1  # the failed PUT was never retried
    assert env.dispatcher.get_count == 2  # observation + verification
    assert env.store.get_action(action_id)["attempt_count"] == 1


def test_unresolved_outcome_ends_ambiguous_without_second_put(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    env.dispatcher.execute_error = OSError("connection reset mid-PUT")
    env.dispatcher.observe_error = RuntimeError("endpoint gone")
    for _ in range(OBSERVATION_MAX_ATTEMPTS + 2):
        result = env.worker.process_one()
        if result is not None and result["state"] == "failed":
            break
        env.clock.advance(2.0)
    action = env.store.get_action(action_id)
    assert action["state"] == "failed"
    assert action["result"]["status"] == "ambiguous"
    assert action["result"]["observation_attempts"] == OBSERVATION_MAX_ATTEMPTS
    assert env.dispatcher.put_count == 1
    observation_receipts = [
        receipt
        for receipt in env.store.list_receipts(action_id)
        if receipt["kind"] == "dispatch_observation"
    ]
    assert len(observation_receipts) == OBSERVATION_MAX_ATTEMPTS


def test_crash_between_dispatch_and_put_reconciles_read_only(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    # First worker consumes the gate, then dies before its PUT.
    original_execute = env.dispatcher.execute

    def crash(request):
        raise KeyboardInterrupt("worker killed mid-flight")

    env.dispatcher.execute = crash
    with pytest.raises(KeyboardInterrupt):
        env.worker.process_one()
    assert env.store.get_action(action_id)["state"] == "dispatched"
    env.dispatcher.execute = original_execute
    # Lease expires; a fresh pass reconciles by GET only.
    env.clock.advance(120.0)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "completed"
    assert env.dispatcher.put_count == 0  # the effect was never re-attempted


def test_post_effect_retry_is_read_only(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    env.dispatcher.observe_errors_remaining = 1  # verification GET fails once
    first = env.worker.process_one()
    assert first is not None and first["state"] == "accepted"
    assert env.dispatcher.put_count == 1
    env.clock.advance(2.0)
    second = env.worker.process_one()
    assert second is not None and second["state"] == "completed"
    assert env.dispatcher.put_count == 1  # still exactly one mutation
    assert env.store.get_action(action_id)["state"] == "completed"


# ------------------------------------------------------------ fail closed


def test_expired_gate_never_dispatches(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env, expires_in=50.0)
    env.clock.advance(60.0)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "failed"
    assert "owner_approval_expired" in (result["last_error"] or "")
    assert env.dispatcher.put_count == 0


def test_expired_grant_never_dispatches(tmp_path):
    env = make_env(tmp_path)
    _action_id, _intent = gated_action(
        env,
        tool_name="colony_task_complete",
        args={"task_id": "task-1"},
        grant=True,
        expires_in=3600.0,
        grant_expires_in=50.0,
    )
    env.clock.advance(60.0)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "failed"
    assert env.dispatcher.put_count == 0


def test_grant_never_covers_non_grantable_tool(tmp_path):
    env = make_env(tmp_path)
    _action_id, _intent = gated_action(
        env, tool_name="colony_autonomy_enable", args={}, grant=True
    )
    result = env.worker.process_one()
    assert result is not None and result["state"] == "failed"
    assert env.dispatcher.put_count == 0


def test_duplicate_gate_receipts_fail_closed(tmp_path):
    env = make_env(tmp_path)
    action_id, _intent = gated_action(env)
    action = env.store.get_action(action_id)
    second = delivery_gate_evidence(
        action, decided_at=env.clock.now(), expires_at=env.clock.now() + 3600.0
    )
    env.store.gate(
        action_id,
        second,
        receipt_key="owner-gate-2",
        external_id=second["approval_id"],
    )
    result = env.worker.process_one()
    assert result is not None and result["state"] == "failed"
    assert env.dispatcher.put_count == 0


def test_disabled_tool_fails_closed(tmp_path):
    env = make_env(tmp_path, enabled=("colony_create_commitment",))
    _action_id, _intent = gated_action(
        env, tool_name="colony_task_complete", args={"task_id": "task-2"}
    )
    result = env.worker.process_one()
    assert result is not None and result["state"] == "failed"
    assert env.dispatcher.put_count == 0


def test_foreign_source_is_never_leased(tmp_path):
    env = make_env(tmp_path)
    intent = build_intent()
    envelope = build_envelope(intent)
    action = env.store.propose(
        intent.idempotency_key,
        DEFAULT_SOURCE_PREFIX + "someone-else",
        DEFAULT_ACTION_TYPE,
        envelope,
        source_ref=intent.intent_id,
    )
    evidence = delivery_gate_evidence(
        action, decided_at=env.clock.now(), expires_at=env.clock.now() + 3600.0
    )
    env.store.gate(action["action_id"], evidence, external_id=evidence["approval_id"])
    assert env.worker.process_one() is None
    assert env.store.get_action(action["action_id"])["state"] == "gated"


# -------------------------------------------------------------- admission


def test_admission_closed_defers_without_consuming(tmp_path):
    env = make_env(tmp_path, admitted=False)
    action_id, _intent = gated_action(env)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "gated"
    assert result["attempt_count"] == 0
    assert env.dispatcher.put_count == 0
    events = [event["event_type"] for event in env.store.list_events(action_id)]
    assert "dispatch_admission_deferred" in events
    # Admission opens; the same action then completes normally.
    write_admission_file(env.admission_path, env.enabled, env.clock)
    env.clock.advance(2.0)
    result = env.worker.process_one()
    assert result is not None and result["state"] == "completed"
    assert env.dispatcher.put_count == 1


# ---------------------------------------------------------- construction


def test_worker_requires_a_real_admission(tmp_path):
    env = make_env(tmp_path)
    for bad in (None, lambda: None, SimpleNamespace(assert_live=lambda: None)):
        with pytest.raises(GovernedActionWorkerError):
            GovernedActionWorker(
                env.store,
                env.dispatcher,
                bad,
                enabled_tools=("colony_create_commitment",),
                admission_principals=(PRINCIPAL,),
                clock=env.clock,
            )


def test_worker_validates_configuration(tmp_path):
    env = make_env(tmp_path)

    def build(**overrides):
        arguments: dict[str, Any] = dict(
            enabled_tools=("colony_create_commitment",),
            admission_principals=(PRINCIPAL,),
            clock=env.clock,
        )
        arguments.update(overrides)
        return GovernedActionWorker(
            env.store, env.dispatcher, env.worker.admission, **arguments
        )

    with pytest.raises(GovernedActionWorkerError):
        build(enabled_tools=("unknown_tool",))
    with pytest.raises(GovernedActionWorkerError):
        build(enabled_tools=())
    with pytest.raises(GovernedActionWorkerError):
        build(admission_principals=("bad principal!",))
    with pytest.raises(GovernedActionWorkerError):
        build(lease_seconds=1.0)
    with pytest.raises(GovernedActionWorkerError):
        build(lease_seconds=1e9)
    with pytest.raises(GovernedActionWorkerError):
        build(source_prefix="")
    with pytest.raises(GovernedActionWorkerError):
        build(action_type=" ")


# ------------------------------------------------------ result validation


def _request_and_result(clock):
    request = {
        "execution_digest": "a" * 64,
        "action_id": str(uuid.uuid4()),
        "action_digest": "b" * 64,
        "intent_id": "hti_" + "c" * 32,
        "intent_digest": "d" * 64,
        "tool_name": "colony_create_commitment",
    }
    return request, completed_result(request, clock())


def test_validate_execution_result_accepts_bound_completed():
    clock = ManualClock()
    request, result = _request_and_result(clock)
    normalized = validate_execution_result(result, request, clock=clock)
    assert normalized["effect"]["outcome"] == "recorded"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result.update(execution_digest="f" * 64),
        lambda result: result.update(status="executing"),
        lambda result: result.update(status="failed"),
        lambda result: result.update(effect_state="unknown"),
        lambda result: result.update(tool_name="colony_task_complete"),
        lambda result: result.update(effect_digest="0" * 64),
        lambda result: result.update(observed_at=-5.0),
        lambda result: result.pop("effect"),
        lambda result: result.update(extra=True),
    ],
)
def test_validate_execution_result_refuses_unbound_or_transient(mutate):
    clock = ManualClock()
    request, result = _request_and_result(clock)
    mutate(result)
    with pytest.raises(Exception):
        validate_execution_result(result, request, clock=clock)


def test_validate_execution_result_refuses_future_observation():
    clock = ManualClock()
    request, result = _request_and_result(clock)
    result["observed_at"] = clock() + 3600.0
    with pytest.raises(GovernedActionWorkerError):
        validate_execution_result(result, request, clock=clock)

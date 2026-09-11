"""Executable store-adapter conformance suite.

Every case exercises one or more of the numbered invariants in
:mod:`colony_hostworker.store` through the public store API only, using a
fresh harness per case.  The adversarial cases are constructed so that a
store which "merely implements the method signatures" — one that trusts the
caller's pre-check, caches receipts read at lease time, skips the
in-transaction gate validator, re-leases dispatched work, or honors a
retryable flag after a mutation — FAILS loudly here instead of dispatching
a second mutation or honoring dead authority in production.

A host must pass this suite with its own harness before running live:

    from colony_hostworker.conformance import assert_store_conformance
    assert_store_conformance(my_harness_factory)

or, for the bundled reference store::

    python -m colony_hostworker.conformance
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..gate import (
    GRANT_UNLIMITED_SENTINEL,
    GateAuthorization,
    assert_dispatchable,
    validate_owner_gate,
)
from ..store import (
    ActionStoreError,
    GATE_RECEIPT_KIND,
    RECOVERY_RECEIPT_KIND,
)
from ..worker import DEFAULT_ACTION_TYPE, DEFAULT_SOURCE_PREFIX, build_execution_request
from .harness import (
    HarnessFactory,
    StoreHarness,
    build_envelope,
    build_intent,
    delivery_gate_evidence,
    grant_gate_evidence,
)

SOURCE = DEFAULT_SOURCE_PREFIX + "conformance-host"
OWNER_A = "conformance-worker-a"
OWNER_B = "conformance-worker-b"
LEASE_SECONDS = 60.0


class ConformanceFailure(AssertionError):
    """The store under test violated a documented invariant."""


@dataclass
class ConformanceResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Scenario:
    action: Mapping[str, Any]
    intent: Any
    evidence: Mapping[str, Any]
    gate_receipt: Mapping[str, Any]


def _require(condition: bool, case: str, message: str) -> None:
    if not condition:
        raise ConformanceFailure("%s: %s" % (case, message))


def _expect_refusal(case: str, message: str, callable_: Callable[[], Any]) -> None:
    try:
        callable_()
    except ActionStoreError:
        return
    except Exception as error:
        raise ConformanceFailure(
            "%s: %s — raised %r instead of an ActionStoreError" % (case, message, error)
        )
    raise ConformanceFailure("%s: %s — the store did not refuse" % (case, message))


def _gated(
    harness: StoreHarness,
    *,
    tool_name: str = "colony_create_commitment",
    args: Mapping[str, Any] | None = None,
    grant: bool = False,
    standing_grant: bool = False,
    expires_in: float = 3600.0,
    grant_expires_in: float = 3600.0,
) -> Scenario:
    intent = build_intent(tool_name=tool_name, args=args)
    envelope = build_envelope(intent)
    action = harness.propose(
        idempotency_key=intent.idempotency_key,
        source=SOURCE,
        source_ref=intent.intent_id,
        action_type=DEFAULT_ACTION_TYPE,
        payload=envelope,
    )
    now = harness.now()
    if grant:
        evidence = grant_gate_evidence(
            action,
            decided_at=now,
            expires_at=now + expires_in,
            grant_expires_at=(
                GRANT_UNLIMITED_SENTINEL
                if standing_grant else now + grant_expires_in
            ),
        )
    else:
        evidence = delivery_gate_evidence(
            action, decided_at=now, expires_at=now + expires_in
        )
    action, receipt = harness.add_gate(
        action["action_id"], evidence, external_id=evidence["approval_id"]
    )
    return Scenario(action=action, intent=intent, evidence=evidence, gate_receipt=receipt)


def _lease_gated(harness: StoreHarness, owner: str = OWNER_A) -> Mapping[str, Any]:
    leased = harness.store.lease_next(
        owner, lease_seconds=LEASE_SECONDS, states=("gated",)
    )
    if leased is None:
        raise ConformanceFailure("harness: gated action could not be leased")
    return leased


def _real_validator(tool_name: str):
    def validate(action, receipts, now):
        return assert_dispatchable(
            validate_owner_gate(action, receipts, tool_name=tool_name, now=now)
        )

    return validate


def _permissive_authorization(
    scenario: Scenario, *, granted: bool = False, expired: bool = False
) -> GateAuthorization:
    """A fabricated always-yes authorization used to prove the STORE's own
    checks refuse even when a (broken or malicious) validator would not."""

    return GateAuthorization(
        shape="bounded_grant" if granted else "message_delivery",
        granted=granted,
        receipt_key=scenario.gate_receipt["receipt_key"],
        evidence_sha256=scenario.gate_receipt["evidence_sha256"],
        approval_id=scenario.evidence["approval_id"],
        decision_id=scenario.evidence["decision_id"],
        revision=1,
        decided_at=float(scenario.evidence["decided_at_epoch"]),
        expires_at=float(scenario.evidence["expires_at_epoch"]),
        expired=expired,
    )


def _placeholder_digest(scenario: Scenario) -> str:
    return hashlib.sha256(
        ("conformance:" + scenario.action["action_id"]).encode("utf-8")
    ).hexdigest()


def _dispatch(
    harness: StoreHarness,
    scenario: Scenario,
    *,
    owner: str = OWNER_A,
    validator,
    execution_digest: str | None = None,
    window: float = 600.0,
    max_observations: int = 5,
):
    return harness.store.begin_owner_authorized_dispatch(
        scenario.action["action_id"],
        owner,
        gate_receipt_key=scenario.gate_receipt["receipt_key"],
        expected_source=scenario.action["source"],
        expected_source_ref=scenario.action["source_ref"],
        expected_action_type=scenario.action["action_type"],
        expected_payload=scenario.action["payload"],
        expected_approval_id=scenario.evidence["approval_id"],
        expected_decision_id=scenario.evidence["decision_id"],
        expected_execution_digest=execution_digest or _placeholder_digest(scenario),
        observation_window_seconds=window,
        max_observations=max_observations,
        gate_validator=validator,
    )


def _require_unconsumed(harness: StoreHarness, scenario: Scenario, case: str) -> None:
    action = harness.store.get_action(scenario.action["action_id"])
    _require(action["state"] == "gated", case, "action left the gated state")
    _require(
        int(action["attempt_count"]) == 0, case, "an attempt was consumed"
    )
    recovery = [
        receipt
        for receipt in harness.store.list_receipts(scenario.action["action_id"])
        if receipt["kind"] == RECOVERY_RECEIPT_KIND
    ]
    _require(not recovery, case, "a recovery contract was written despite refusal")


def _dispatch_ok(
    harness: StoreHarness,
    scenario: Scenario,
    *,
    owner: str = OWNER_A,
    window: float = 600.0,
    max_observations: int = 5,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Lease and dispatch a valid scenario the way the worker does."""

    action = _lease_gated(harness, owner)
    authorization = validate_owner_gate(
        action,
        harness.store.list_receipts(action["action_id"]),
        tool_name=scenario.intent.tool_name,
        now=harness.now(),
    )
    request = build_execution_request(action, scenario.intent, authorization)
    dispatched = _dispatch(
        harness,
        scenario,
        owner=owner,
        validator=_real_validator(scenario.intent.tool_name),
        execution_digest=request["execution_digest"],
        window=window,
        max_observations=max_observations,
    )
    return dispatched, request


# ----------------------------------------------------------------- cases


def check_happy_path_lifecycle(factory: HarnessFactory) -> None:
    """I1/I2/I5/I8/I9: the full governed lifecycle, exactly once each."""

    case = "happy_path_lifecycle"
    harness = factory()
    try:
        scenario = _gated(harness)
        # I1: idempotent re-propose returns the identical immutable action.
        again = harness.propose(
            idempotency_key=scenario.intent.idempotency_key,
            source=SOURCE,
            source_ref=scenario.intent.intent_id,
            action_type=DEFAULT_ACTION_TYPE,
            payload=build_envelope(scenario.intent),
        )
        _require(
            again["action_id"] == scenario.action["action_id"],
            case,
            "idempotent propose minted a second action",
        )
        dispatched, request = _dispatch_ok(harness, scenario)
        _require(dispatched["state"] == "dispatched", case, "dispatch did not commit")
        _require(
            int(dispatched["attempt_count"]) == 1,
            case,
            "attempt_count did not increment exactly once",
        )
        recovery = [
            receipt
            for receipt in harness.store.list_receipts(dispatched["action_id"])
            if receipt["kind"] == RECOVERY_RECEIPT_KIND
        ]
        _require(
            len(recovery) == 1
            and recovery[0]["external_id"] == request["execution_digest"],
            case,
            "the GET-only recovery contract was not written atomically "
            "with the dispatch transition",
        )
        acceptance_key = "governed-action-acceptance:" + request["execution_digest"]
        endpoint_projection = {
            "status": "completed",
            "execution_digest": request["execution_digest"],
            "observed": "conformance",
        }
        accepted, _receipt = harness.store.accept(
            dispatched["action_id"],
            OWNER_A,
            acceptance_key,
            "governed_action_acceptance",
            endpoint_projection,
            external_id=dispatched["action_id"],
        )
        _require(accepted["state"] == "accepted", case, "accept did not commit")
        verified, _receipt = harness.store.verify(
            accepted["action_id"],
            OWNER_A,
            "governed-action-verification:" + request["execution_digest"],
            endpoint_projection,
            qualifying_receipt_keys=(acceptance_key,),
        )
        _require(verified["state"] == "verified", case, "verify did not commit")
        completed = harness.store.complete(
            verified["action_id"], OWNER_A, {"status": "completed"}
        )
        _require(
            completed["state"] == "completed"
            and completed["lease_owner"] is None
            and completed["terminal_at"] is not None,
            case,
            "complete did not terminalize cleanly",
        )
        # I8: terminal states are frozen.
        _expect_refusal(
            case,
            "a completed action accepted another transition",
            lambda: harness.store.fail_attempt(
                completed["action_id"], OWNER_A, "late failure", False
            ),
        )
    finally:
        harness.close()


def check_gate_validator_runs_inside_dispatch(factory: HarnessFactory) -> None:
    """I4: the store MUST invoke the supplied validator, against durable
    receipts re-read in the transaction, with the store's own clock — and a
    validator verdict of anything but a live authorization must abort.

    Catches: a store that never calls the validator, calls it with the
    caller's stale receipt view, or ignores its verdict."""

    case = "gate_validator_runs_inside_dispatch"
    harness = factory()
    try:
        scenario = _gated(harness)
        _lease_gated(harness)
        harness.advance(40.0)  # the store's clock, not the pre-check's
        seen: dict[str, Any] = {}

        def sentinel(action, receipts, now):
            seen["action_id"] = action.get("action_id")
            seen["receipts"] = list(receipts)
            seen["now"] = now
            raise RuntimeError("sentinel refuses")

        _expect_refusal(
            case,
            "a raising validator did not abort the dispatch",
            lambda: _dispatch(harness, scenario, validator=sentinel),
        )
        _require(bool(seen), case, "the store never invoked the gate validator")
        _require(
            seen.get("action_id") == scenario.action["action_id"],
            case,
            "the validator saw a different action",
        )
        durable_gates = [
            receipt
            for receipt in seen.get("receipts", ())
            if receipt.get("kind") == GATE_RECEIPT_KIND
        ]
        _require(
            len(durable_gates) == 1
            and durable_gates[0].get("evidence") == dict(scenario.evidence)
            and durable_gates[0].get("evidence_sha256")
            == scenario.gate_receipt["evidence_sha256"],
            case,
            "the validator was not given the durable gate receipt",
        )
        _require(
            float(seen.get("now", -1.0)) == harness.now(),
            case,
            "the validator was not given the store's point-of-use clock",
        )
        _require_unconsumed(harness, scenario, case)

        # A validator returning garbage must abort too.
        _expect_refusal(
            case,
            "a non-authorization validator result was accepted",
            lambda: _dispatch(
                harness, scenario, validator=lambda *_: {"approved": True}
            ),
        )
        # And so must an authorization the validator marked expired.
        _expect_refusal(
            case,
            "an expired authorization was accepted",
            lambda: _dispatch(
                harness,
                scenario,
                validator=lambda *_: _permissive_authorization(
                    scenario, expired=True
                ),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_evidence_mutated_between_precheck_and_dispatch(
    factory: HarnessFactory,
) -> None:
    """ADVERSARIAL/TOCTOU: the gate evidence set changes after the caller's
    pre-check (a second gate receipt lands post-lease).  A conforming store
    re-reads and re-validates inside the dispatch transaction and refuses.

    Catches: a store that validates at lease time, caches the receipt list,
    or otherwise trusts the caller's earlier look at the evidence."""

    case = "evidence_mutated_between_precheck_and_dispatch"
    harness = factory()
    try:
        scenario = _gated(harness)
        leased = _lease_gated(harness)
        # The caller's pre-check: passes against the receipts as they are NOW.
        precheck = validate_owner_gate(
            leased,
            harness.store.list_receipts(leased["action_id"]),
            tool_name=scenario.intent.tool_name,
            now=harness.now(),
        )
        _require(not precheck.expired, case, "fixture pre-check unexpectedly failed")
        # Between check and use, a second owner-gate receipt lands (an
        # approval-router race or a forged duplicate approval).
        second = delivery_gate_evidence(
            scenario.action,
            decided_at=harness.now(),
            expires_at=harness.now() + 3600.0,
        )
        harness.add_gate(
            scenario.action["action_id"],
            second,
            receipt_key="owner-gate-2",
            external_id=second["approval_id"],
        )
        _expect_refusal(
            case,
            "the store dispatched on pre-check evidence after the durable "
            "gate set changed",
            lambda: _dispatch(
                harness,
                scenario,
                validator=_real_validator(scenario.intent.tool_name),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_duplicate_gates_refused(factory: HarnessFactory) -> None:
    """ADVERSARIAL: two gate receipts must never dispatch — even when the
    validator (broken or malicious) says yes, the store's own exactly-one
    check must hold.

    Catches: a store that picks 'the first' or 'the matching' of several
    gates instead of requiring exactly one."""

    case = "duplicate_gates_refused"
    harness = factory()
    try:
        scenario = _gated(harness)
        second = delivery_gate_evidence(
            scenario.action,
            decided_at=harness.now(),
            expires_at=harness.now() + 3600.0,
        )
        harness.add_gate(
            scenario.action["action_id"],
            second,
            receipt_key="owner-gate-2",
            external_id=second["approval_id"],
        )
        _lease_gated(harness)
        _expect_refusal(
            case,
            "duplicate gates dispatched under the real validator",
            lambda: _dispatch(
                harness,
                scenario,
                validator=_real_validator(scenario.intent.tool_name),
            ),
        )
        _expect_refusal(
            case,
            "duplicate gates dispatched under a permissive validator",
            lambda: _dispatch(
                harness,
                scenario,
                validator=lambda *_: _permissive_authorization(scenario),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_tampered_gate_binding_refused(factory: HarnessFactory) -> None:
    """ADVERSARIAL: gate evidence whose action_digest binds a DIFFERENT
    payload must refuse under both the validator and the store's own
    structural binding check.

    Catches: a store that binds the gate by action_id alone and lets an
    approval for one payload authorize another."""

    case = "tampered_gate_binding_refused"
    harness = factory()
    try:
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
            action,
            decided_at=harness.now(),
            expires_at=harness.now() + 3600.0,
            action_digest="0" * 64,  # someone else's payload
        )
        action, receipt = harness.add_gate(
            action["action_id"], evidence, external_id=evidence["approval_id"]
        )
        scenario = Scenario(
            action=action, intent=intent, evidence=evidence, gate_receipt=receipt
        )
        _lease_gated(harness)
        _expect_refusal(
            case,
            "a mis-bound gate dispatched under the real validator",
            lambda: _dispatch(
                harness, scenario, validator=_real_validator(intent.tool_name)
            ),
        )
        _expect_refusal(
            case,
            "a mis-bound gate dispatched under a permissive validator",
            lambda: _dispatch(
                harness,
                scenario,
                validator=lambda *_: _permissive_authorization(scenario),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_crash_between_dispatch_and_put(factory: HarnessFactory) -> None:
    """I5/I6: after the dispatch transaction commits and the worker dies
    before (or during) its one PUT, the action must be recoverable ONLY as
    GET-only observation — never as a second mutation.

    Catches: a store that returns dispatched work to gated, lets
    ``lease_next`` hand it out again, or allows a second dispatch."""

    case = "crash_between_dispatch_and_put"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, request = _dispatch_ok(harness, scenario)
        _require(dispatched["state"] == "dispatched", case, "fixture dispatch failed")
        # The worker crashes here; its lease expires.
        harness.advance(LEASE_SECONDS * 2)
        stolen = harness.store.lease_next(
            OWNER_B,
            lease_seconds=LEASE_SECONDS,
            states=("gated", "accepted", "verified"),
        )
        _require(
            stolen is None or stolen["action_id"] != dispatched["action_id"],
            case,
            "lease_next re-leased a dispatched action for mutation",
        )
        observed = harness.store.lease_dispatched_observation(
            OWNER_B, lease_seconds=LEASE_SECONDS
        )
        _require(
            observed is not None
            and observed["action_id"] == dispatched["action_id"]
            and observed["state"] == "dispatched",
            case,
            "the dispatched action was not recoverable as GET-only work",
        )
        _expect_refusal(
            case,
            "a second owner-authorized dispatch was permitted",
            lambda: _dispatch(
                harness,
                scenario,
                owner=OWNER_B,
                validator=lambda *_: _permissive_authorization(scenario),
                execution_digest=request["execution_digest"],
            ),
        )
        recovery = [
            receipt
            for receipt in harness.store.list_receipts(dispatched["action_id"])
            if receipt["kind"] == RECOVERY_RECEIPT_KIND
        ]
        _require(
            len(recovery) == 1
            and recovery[0]["external_id"] == request["execution_digest"],
            case,
            "the recovery contract is missing, duplicated, or unbound",
        )
        current = harness.store.get_action(dispatched["action_id"])
        _require(
            int(current["attempt_count"]) == 1,
            case,
            "the crash recovery consumed another attempt",
        )
    finally:
        harness.close()


def check_lease_steal_during_observation(factory: HarnessFactory) -> None:
    """I3: once worker B holds the observation lease, worker A's stale
    handle must not be able to defer, accept, or otherwise mutate.

    Catches: a store that checks lease ownership outside the transaction,
    or not at all, letting two workers race the same observation."""

    case = "lease_steal_during_observation"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, request = _dispatch_ok(harness, scenario)
        action, attempt_one = harness.store.begin_dispatched_observation(
            dispatched["action_id"], OWNER_A, request["execution_digest"]
        )
        _require(
            attempt_one is not None and action["state"] == "dispatched",
            case,
            "fixture observation attempt failed",
        )
        harness.advance(LEASE_SECONDS * 2)  # A's lease dies
        stolen = harness.store.lease_dispatched_observation(
            OWNER_B, lease_seconds=LEASE_SECONDS
        )
        _require(
            stolen is not None and stolen["action_id"] == dispatched["action_id"],
            case,
            "worker B could not take over the expired observation",
        )
        _expect_refusal(
            case,
            "worker A deferred an observation it no longer leases",
            lambda: harness.store.defer_dispatched_observation(
                dispatched["action_id"],
                OWNER_A,
                request["execution_digest"],
                attempt_one["receipt_key"],
                "stale worker",
                1.0,
            ),
        )
        _expect_refusal(
            case,
            "worker A accepted an action it no longer leases",
            lambda: harness.store.accept(
                dispatched["action_id"],
                OWNER_A,
                "governed-action-acceptance:" + request["execution_digest"],
                "governed_action_acceptance",
                {"status": "completed"},
            ),
        )
        current = harness.store.get_action(dispatched["action_id"])
        _require(
            current["state"] == "dispatched"
            and current["lease_owner"] == OWNER_B,
            case,
            "the stale worker mutated state despite losing the lease",
        )
    finally:
        harness.close()


def check_expired_gate_at_point_of_use(factory: HarnessFactory) -> None:
    """ADVERSARIAL: an approval that was live when written but is expired at
    dispatch time must never dispatch — under the real validator AND under
    a permissive one (the store re-checks expiry itself).

    Catches: a store that trusts 'valid when the receipt was written'."""

    case = "expired_gate_at_point_of_use"
    harness = factory()
    try:
        scenario = _gated(harness, expires_in=50.0)
        _lease_gated(harness)
        harness.advance(55.0)  # inside the lease, past the gate expiry
        _expect_refusal(
            case,
            "an expired gate dispatched under the real validator",
            lambda: _dispatch(
                harness,
                scenario,
                validator=_real_validator(scenario.intent.tool_name),
            ),
        )
        _expect_refusal(
            case,
            "an expired gate dispatched under a permissive validator",
            lambda: _dispatch(
                harness,
                scenario,
                validator=lambda *_: _permissive_authorization(scenario),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_expired_grant_at_point_of_use(factory: HarnessFactory) -> None:
    """ADVERSARIAL: a bounded grant whose own expiry passed — while the gate
    receipt's outer expiry is still live — must never dispatch.  Only the
    shape-aware validator knows the grant expiry field, so this case FAILS
    on any store that does not actually run the supplied validator inside
    the transaction (I4).

    Catches: a store whose structural checks pass and which skips or
    short-circuits the semantic validator."""

    case = "expired_grant_at_point_of_use"
    harness = factory()
    try:
        scenario = _gated(
            harness,
            tool_name="colony_task_complete",
            args={"task_id": "task-conformance-1"},
            grant=True,
            expires_in=3600.0,
            grant_expires_in=50.0,
        )
        _lease_gated(harness)
        harness.advance(55.0)  # grant dead, outer gate expiry still live
        _expect_refusal(
            case,
            "an expired bounded grant dispatched",
            lambda: _dispatch(
                harness,
                scenario,
                validator=_real_validator(scenario.intent.tool_name),
            ),
        )
        _require_unconsumed(harness, scenario, case)
    finally:
        harness.close()


def check_non_grantable_tool_with_grant_proof(factory: HarnessFactory) -> None:
    """ADVERSARIAL: a syntactically perfect standing-grant receipt presented
    for either ``non_grantable`` autonomy tool must never
    dispatch — under the real validator AND under a permissive one (the
    store's grant backstop must hold on its own).

    Catches: a store or configuration that lets standing-grant authority
    reach tools the catalog reserves for per-message owner approval."""

    case = "non_grantable_tool_with_grant_proof"
    for tool_name in ("colony_autonomy_enable", "colony_autonomy_disable"):
        harness = factory()
        try:
            scenario = _gated(
                harness,
                tool_name=tool_name,
                args={},
                grant=True,
                standing_grant=True,
            )
            _lease_gated(harness)
            _expect_refusal(
                case,
                "%s dispatched under a standing grant and the real validator"
                % tool_name,
                lambda: _dispatch(
                    harness,
                    scenario,
                    validator=_real_validator(scenario.intent.tool_name),
                ),
            )
            _expect_refusal(
                case,
                "%s dispatched under a standing grant and a permissive validator"
                % tool_name,
                lambda: _dispatch(
                    harness,
                    scenario,
                    validator=lambda *_: _permissive_authorization(
                        scenario, granted=True
                    ),
                ),
            )
            _require_unconsumed(harness, scenario, case)
        finally:
            harness.close()


def check_observation_budget_exhaustion(factory: HarnessFactory) -> None:
    """I7: the observation journal is consumed durably BEFORE each GET and
    the action terminalizes as explicitly ambiguous when the budget runs
    out — it never becomes retryable.

    Catches: a store with unbounded reconciliation or one that converts an
    exhausted observation into a fresh mutation attempt."""

    case = "observation_budget_exhaustion"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, request = _dispatch_ok(
            harness, scenario, max_observations=2
        )
        _action, first = harness.store.begin_dispatched_observation(
            dispatched["action_id"], OWNER_A, request["execution_digest"]
        )
        _require(first is not None, case, "attempt 1 was not journaled")
        harness.store.defer_dispatched_observation(
            dispatched["action_id"],
            OWNER_A,
            request["execution_digest"],
            first["receipt_key"],
            "unresolved",
            1.0,
        )
        harness.advance(2.0)
        released = harness.store.lease_dispatched_observation(
            OWNER_A, lease_seconds=LEASE_SECONDS
        )
        _require(released is not None, case, "deferred observation never re-leased")
        _action, second = harness.store.begin_dispatched_observation(
            dispatched["action_id"], OWNER_A, request["execution_digest"]
        )
        _require(second is not None, case, "attempt 2 was not journaled")
        final = harness.store.defer_dispatched_observation(
            dispatched["action_id"],
            OWNER_A,
            request["execution_digest"],
            second["receipt_key"],
            "unresolved",
            1.0,
        )
        _require(
            final["state"] == "failed"
            and isinstance(final.get("result"), Mapping)
            and final["result"].get("status") == "ambiguous",
            case,
            "an exhausted observation budget did not terminalize as "
            "explicitly ambiguous",
        )
        _require(
            int(final["attempt_count"]) == 1,
            case,
            "exhaustion consumed another mutation attempt",
        )
    finally:
        harness.close()


def check_observation_deadline_exhaustion(factory: HarnessFactory) -> None:
    """I7: once the observation deadline passes, the action terminalizes as
    ambiguous instead of observing (or mutating) further."""

    case = "observation_deadline_exhaustion"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, request = _dispatch_ok(
            harness, scenario, window=100.0, max_observations=5
        )
        _action, first = harness.store.begin_dispatched_observation(
            dispatched["action_id"], OWNER_A, request["execution_digest"]
        )
        _require(first is not None, case, "attempt 1 was not journaled")
        harness.store.defer_dispatched_observation(
            dispatched["action_id"],
            OWNER_A,
            request["execution_digest"],
            first["receipt_key"],
            "unresolved",
            1.0,
        )
        harness.advance(150.0)  # past the observation deadline
        leased = harness.store.lease_dispatched_observation(
            OWNER_A, lease_seconds=LEASE_SECONDS
        )
        _require(
            leased is None or leased["action_id"] != dispatched["action_id"],
            case,
            "an over-deadline observation contract was leased again",
        )
        final = harness.store.get_action(dispatched["action_id"])
        _require(
            final["state"] == "failed"
            and isinstance(final.get("result"), Mapping)
            and final["result"].get("status") == "ambiguous",
            case,
            "an over-deadline dispatch did not terminalize as ambiguous",
        )
    finally:
        harness.close()


def check_dispatched_never_regates(factory: HarnessFactory) -> None:
    """I6/I10: after the one mutation attempt, a failure — even one claimed
    to be retryable — must terminalize, never return the action to gated.

    Catches: a store carrying the general-purpose retry edge into the
    governed subset, which would re-dispatch a possibly-performed effect."""

    case = "dispatched_never_regates"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, _request = _dispatch_ok(harness, scenario)
        failed = harness.store.fail_attempt(
            dispatched["action_id"], OWNER_A, "provider timeout", True
        )
        _require(
            failed["state"] == "failed",
            case,
            "a retryable failure re-opened a dispatched action "
            "(state %r)" % failed["state"],
        )
        _require(
            int(failed["attempt_count"]) == 1 and failed["lease_owner"] is None,
            case,
            "the terminal failure left attempt count or lease inconsistent",
        )
    finally:
        harness.close()


def check_receipt_and_idempotency_immutability(factory: HarnessFactory) -> None:
    """I1/I2: reusing an idempotency key or a receipt key with different
    content must conflict; identical replays must be no-ops."""

    case = "receipt_and_idempotency_immutability"
    harness = factory()
    try:
        scenario = _gated(harness)
        other_intent = build_intent()
        _expect_refusal(
            case,
            "an idempotency key was reused with a different payload",
            lambda: harness.propose(
                idempotency_key=scenario.intent.idempotency_key,
                source=SOURCE,
                source_ref=other_intent.intent_id,
                action_type=DEFAULT_ACTION_TYPE,
                payload=build_envelope(other_intent),
            ),
        )
        mutated = dict(scenario.evidence)
        mutated["decision_id"] = "decision-tampered"
        _expect_refusal(
            case,
            "a gate receipt key was reused with different evidence",
            lambda: harness.add_gate(
                scenario.action["action_id"],
                mutated,
                receipt_key=scenario.gate_receipt["receipt_key"],
                external_id=mutated["approval_id"],
            ),
        )
        # Identical replay is an idempotent no-op.
        _action, replay = harness.add_gate(
            scenario.action["action_id"],
            scenario.evidence,
            receipt_key=scenario.gate_receipt["receipt_key"],
            external_id=scenario.evidence["approval_id"],
        )
        _require(
            replay["evidence_sha256"] == scenario.gate_receipt["evidence_sha256"],
            case,
            "an identical gate replay was not idempotent",
        )
        gates = [
            receipt
            for receipt in harness.store.list_receipts(scenario.action["action_id"])
            if receipt["kind"] == GATE_RECEIPT_KIND
        ]
        _require(len(gates) == 1, case, "the idempotent replay duplicated the gate")
    finally:
        harness.close()


def check_verify_requires_durable_acceptance(factory: HarnessFactory) -> None:
    """I9: an action cannot reach verified without its durable acceptance
    receipt existing in the same store."""

    case = "verify_requires_durable_acceptance"
    harness = factory()
    try:
        scenario = _gated(harness)
        dispatched, request = _dispatch_ok(harness, scenario)
        acceptance_key = "governed-action-acceptance:" + request["execution_digest"]
        accepted, _receipt = harness.store.accept(
            dispatched["action_id"],
            OWNER_A,
            acceptance_key,
            "governed_action_acceptance",
            {"status": "completed"},
        )
        _expect_refusal(
            case,
            "verify passed without its qualifying acceptance receipt",
            lambda: harness.store.verify(
                accepted["action_id"],
                OWNER_A,
                "governed-action-verification:" + request["execution_digest"],
                {"status": "completed"},
                qualifying_receipt_keys=("no-such-receipt-key",),
            ),
        )
        current = harness.store.get_action(accepted["action_id"])
        _require(
            current["state"] == "accepted",
            case,
            "the refused verification changed lifecycle state",
        )
    finally:
        harness.close()


CASES: tuple[Callable[[HarnessFactory], None], ...] = (
    check_happy_path_lifecycle,
    check_gate_validator_runs_inside_dispatch,
    check_evidence_mutated_between_precheck_and_dispatch,
    check_duplicate_gates_refused,
    check_tampered_gate_binding_refused,
    check_crash_between_dispatch_and_put,
    check_lease_steal_during_observation,
    check_expired_gate_at_point_of_use,
    check_expired_grant_at_point_of_use,
    check_non_grantable_tool_with_grant_proof,
    check_observation_budget_exhaustion,
    check_observation_deadline_exhaustion,
    check_dispatched_never_regates,
    check_receipt_and_idempotency_immutability,
    check_verify_requires_durable_acceptance,
)


def run_store_conformance(factory: HarnessFactory) -> list[ConformanceResult]:
    """Run every case against fresh harnesses; a crash is a failure too."""

    results = []
    for case in CASES:
        name = case.__name__
        try:
            case(factory)
        except ConformanceFailure as failure:
            results.append(ConformanceResult(name=name, passed=False, detail=str(failure)))
        except Exception as error:  # a store crashing mid-case is a failure
            results.append(
                ConformanceResult(
                    name=name,
                    passed=False,
                    detail="store raised %r" % (error,),
                )
            )
        else:
            results.append(ConformanceResult(name=name, passed=True))
    return results


def assert_store_conformance(factory: HarnessFactory) -> list[ConformanceResult]:
    """Raise :class:`ConformanceFailure` unless EVERY case passes."""

    results = run_store_conformance(factory)
    failures = [result for result in results if not result.passed]
    if failures:
        raise ConformanceFailure(
            "store adapter failed %d/%d conformance cases:\n%s"
            % (
                len(failures),
                len(results),
                "\n".join(
                    "  - %s: %s" % (item.name, item.detail) for item in failures
                ),
            )
        )
    return results


__all__ = (
    "CASES",
    "ConformanceFailure",
    "ConformanceResult",
    "assert_store_conformance",
    "run_store_conformance",
)

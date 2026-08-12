"""One-mutation-at-most governed action worker.

The worker is deliberately transport neutral: it drives any
:class:`~colony_hostworker.store.ActionStore` and any dispatcher exposing
``execute`` (one PUT) and ``observe`` (side-effect-free GET), such as
:class:`~colony_hostworker.client.GovernedActionClient`.

The state machine, unchanged from the deployed reference implementation:

    lease -> parse immutable intent -> validate owner gate -> check
    admission -> ATOMICALLY consume the gate (store re-validates it inside
    the dispatch transaction) -> exactly one PUT -> bounded GET-only
    reconciliation -> read-only verification -> complete.

Invariants the worker upholds (its half of the one-mutation guarantee; the
store's half is specified in :mod:`colony_hostworker.store`):

* a mutation is NEVER retried after dispatch — any doubt after the PUT is
  resolved exclusively through the bounded GET-only observation contract;
* a post-effect retry is read-only verification only;
* exactly one owner gate receipt is required, validated both before and —
  via the ``gate_validator`` parameter — inside the dispatch transaction;
* an authorization expired at point of use never dispatches;
* the dispatch admission is consulted immediately before consuming the
  gate, and refusal defers without consuming anything.

What a host still writes: action ingress (proposing envelopes and writing
gate receipts from its own approval capture — deliberately out of scope
here), credential/admission provisioning, and the scheduling loop that
calls :meth:`GovernedActionWorker.process_one`.
"""

from __future__ import annotations

import hmac
import math
import os
import re
import uuid
from typing import Any, Iterable, Mapping

from .catalog import ACTION_TOOL_NAMES, TOOL_CATALOG
from .contract import (
    APPROVAL_BINDING_SCHEMA,
    EFFECT_FIELDS,
    EFFECT_SCHEMA,
    EXECUTION_REQUEST_SCHEMA,
    EXECUTION_RESULT_FIELDS,
    EXECUTION_RESULT_MAX_BYTES,
    EXECUTION_RESULT_SCHEMA,
    GATE_CLOCK_SKEW_SECONDS,
    INTENT_ENVELOPE_SCHEMA,
    SAFE_ID_RE,
    SHA256_RE,
    canonical_json_utf8,
    sha256_json_utf8,
)
from .gate import (
    DEFAULT_REGISTRY,
    GateAuthorization,
    ProvenanceShapeRegistry,
    assert_dispatchable,
    validate_owner_gate,
)
from .intent import HermesActionIntentError, HermesToolActionIntentV1
from .store import ActionStoreError

# Durable-store conventions shared with the host's ingress (the component
# that proposes actions).  Configurable per host, but the defaults are the
# published convention and should not be changed casually.
DEFAULT_SOURCE_PREFIX = "hermes-action-intent:"
DEFAULT_ACTION_TYPE = "hermes_tool_action"

OBSERVATION_MAX_ATTEMPTS = 8
OBSERVATION_WINDOW_SECONDS = 5 * 60
OBSERVATION_RETRY_SECONDS = 1.0
ADMISSION_DEFER_SECONDS = 1.0


class GovernedActionWorkerError(RuntimeError):
    """The worker refused a configuration or an exchange."""


def _safe_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise GovernedActionWorkerError("%s is not a safe identifier" % name)
    return value


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise GovernedActionWorkerError("%s is not a SHA-256 digest" % name)
    return value


def _bounded_verification(value: Any, *, depth=0, counter=None) -> Any:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 256 or depth > 6:
        raise GovernedActionWorkerError("effect verification is too complex")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > (1 << 63) - 1:
            raise GovernedActionWorkerError("effect verification integer is unsafe")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernedActionWorkerError("effect verification number is unsafe")
        return value
    if isinstance(value, str):
        if len(value) > 2048 or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        ):
            raise GovernedActionWorkerError("effect verification text is unsafe")
        return value
    if isinstance(value, list):
        return [
            _bounded_verification(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = _safe_id(key, "verification key")
            result[key] = _bounded_verification(
                item, depth=depth + 1, counter=counter
            )
        return result
    raise GovernedActionWorkerError("effect verification is not JSON")


def build_execution_request(
    action: Mapping[str, Any],
    intent: HermesToolActionIntentV1,
    authorization: GateAuthorization,
) -> dict[str, Any]:
    """Build the immutable ``ColonyGovernedActionExecutionV1`` document.

    The ``execution_digest`` (UTF-8 canonical-JSON convention) pins the
    whole request; the store's recovery contract and the endpoint's durable
    ledger are both keyed by it.
    """

    approval = {
        "schema": APPROVAL_BINDING_SCHEMA,
        "version": 1,
        "approval_id": authorization.approval_id,
        "decision_id": authorization.decision_id,
        "revision": authorization.revision,
        "authorization_receipt_sha256": authorization.evidence_sha256,
        "decided_at": authorization.decided_at,
        "expires_at": authorization.expires_at,
    }
    unsigned = {
        "schema": EXECUTION_REQUEST_SCHEMA,
        "version": 1,
        "action_id": action["action_id"],
        "action_digest": action["payload_sha256"],
        "intent_id": intent.intent_id,
        "intent_digest": intent.intent_digest,
        "tool_name": intent.tool_name,
        "args": intent.args,
        "args_sha256": intent.args_sha256,
        "approval": approval,
    }
    return {**unsigned, "execution_digest": sha256_json_utf8(unsigned)}


def validate_execution_result(
    value: Any,
    request: Mapping[str, Any],
    *,
    clock,
) -> dict[str, Any]:
    """Validate one endpoint projection as a COMPLETED, digest-bound result.

    Anything that is not a completed, performed, fully digest-bound
    projection of exactly this request raises — including transient
    ``prepared`` / ``executing`` projections, which the caller treats as
    unresolved observation, never as failure and NEVER as license to retry
    the mutation.
    """

    if not isinstance(value, Mapping) or set(value) != EXECUTION_RESULT_FIELDS:
        raise GovernedActionWorkerError("execution response fields are invalid")
    if (
        value.get("schema") != EXECUTION_RESULT_SCHEMA
        or isinstance(value.get("version"), bool)
        or value.get("version") != 1
        or value.get("status") != "completed"
        or value.get("effect_state") != "performed"
    ):
        raise GovernedActionWorkerError("execution response state is invalid")
    for field_name in ("execution_digest", "action_digest", "intent_digest"):
        _sha(value.get(field_name), field_name)
        if not hmac.compare_digest(
            value[field_name], str(request.get(field_name) or "")
        ):
            raise GovernedActionWorkerError("execution response is not bound")
    for field_name in ("action_id", "intent_id", "tool_name"):
        if value.get(field_name) != request.get(field_name):
            raise GovernedActionWorkerError("execution response is not bound")
    effect = value.get("effect")
    if not isinstance(effect, Mapping) or set(effect) != EFFECT_FIELDS:
        raise GovernedActionWorkerError("effect projection is invalid")
    if (
        effect.get("schema") != EFFECT_SCHEMA
        or isinstance(effect.get("version"), bool)
        or effect.get("version") != 1
    ):
        raise GovernedActionWorkerError("effect schema is invalid")
    _safe_id(effect.get("effect_id"), "effect_id")
    _safe_id(effect.get("outcome"), "outcome")
    verification = _bounded_verification(effect.get("verification"))
    normalized_effect = {
        "schema": EFFECT_SCHEMA,
        "version": 1,
        "effect_id": effect["effect_id"],
        "outcome": effect["outcome"],
        "verification": verification,
    }
    effect_digest = _sha(value.get("effect_digest"), "effect_digest")
    if not hmac.compare_digest(effect_digest, sha256_json_utf8(normalized_effect)):
        raise GovernedActionWorkerError("effect digest does not match")
    observed_at = value.get("observed_at")
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
        raise GovernedActionWorkerError("observation time is invalid")
    observed_at = float(observed_at)
    now = float(clock())
    if (
        not math.isfinite(observed_at)
        or observed_at <= 0
        or observed_at > now + GATE_CLOCK_SKEW_SECONDS
    ):
        raise GovernedActionWorkerError("observation time is invalid")
    normalized = dict(value)
    normalized["effect"] = normalized_effect
    normalized["observed_at"] = observed_at
    if len(canonical_json_utf8(normalized).encode("utf-8")) > EXECUTION_RESULT_MAX_BYTES:
        raise GovernedActionWorkerError("execution response is too large")
    return normalized


class GovernedActionWorker:
    """One-mutation-at-most worker with read-only verification recovery."""

    def __init__(
        self,
        store,
        dispatcher,
        admission,
        *,
        enabled_tools: Iterable[str],
        admission_principals: Iterable[str],
        clock,
        lease_seconds: float = 45.0,
        owner: str = "",
        registry: ProvenanceShapeRegistry = DEFAULT_REGISTRY,
        catalog: Mapping[str, Any] = TOOL_CATALOG,
        source_prefix: str = DEFAULT_SOURCE_PREFIX,
        action_type: str = DEFAULT_ACTION_TYPE,
    ) -> None:
        enabled = frozenset(str(item or "").strip() for item in enabled_tools)
        principals = frozenset(
            str(item or "").strip() for item in admission_principals
        )
        if not enabled or enabled - ACTION_TOOL_NAMES:
            raise GovernedActionWorkerError("worker tool allowlist is invalid")
        if not principals or any(
            not SAFE_ID_RE.fullmatch(item) for item in principals
        ):
            raise GovernedActionWorkerError("worker principal allowlist is invalid")
        try:
            lease = float(lease_seconds)
        except (TypeError, ValueError) as error:
            raise GovernedActionWorkerError("worker lease is invalid") from error
        if not math.isfinite(lease) or not 5 <= lease <= 300:
            raise GovernedActionWorkerError("worker lease is invalid")
        if not hasattr(dispatcher, "execute") or not hasattr(dispatcher, "observe"):
            raise GovernedActionWorkerError("worker dispatcher is invalid")
        if not callable(clock):
            raise GovernedActionWorkerError("worker clock is invalid")
        if not isinstance(registry, ProvenanceShapeRegistry):
            raise GovernedActionWorkerError("worker shape registry is invalid")
        if not isinstance(source_prefix, str) or not source_prefix.strip():
            raise GovernedActionWorkerError("worker source prefix is invalid")
        if not isinstance(action_type, str) or not action_type.strip():
            raise GovernedActionWorkerError("worker action type is invalid")
        # The admission object stands between every leased gate and the one
        # mutation; a missing or method-less admission must fail loudly at
        # construction, never quietly at dispatch time.  Hosts MUST pass a
        # real DispatchAdmission (e.g. FileDispatchAdmission) — a stub that
        # always returns removes the operator kill-switch.
        assert_live = getattr(admission, "assert_live", None)
        if admission is None or not callable(assert_live) or not hasattr(
            assert_live, "__self__"
        ):
            raise GovernedActionWorkerError("worker dispatch admission is invalid")
        self.store = store
        self.dispatcher = dispatcher
        self.admission = admission
        self.enabled_tools = enabled
        self.admission_principals = principals
        self.clock = clock
        self.lease_seconds = lease
        self.registry = registry
        self.catalog = catalog
        self.source_prefix = source_prefix
        self.action_type = action_type
        self.owner = owner or "governed-action-worker:%d:%s" % (
            os.getpid(),
            uuid.uuid4().hex[:12],
        )

    # ------------------------------------------------------------- leasing

    def _sources(self) -> tuple[str, ...]:
        """Exact source values this worker owns: one per admitted principal.

        Ownership is exact-match (``prefix + principal``), never bare
        prefix-match, so a look-alike principal cannot smuggle work into an
        admitted worker.
        """

        return tuple(
            sorted(self.source_prefix + principal
                   for principal in self.admission_principals)
        )

    def _lease(self):
        for source in self._sources():
            self.store.recover_expired_leases(
                source=source, action_type=self.action_type
            )
        for state in ("verified", "accepted"):
            for source in self._sources():
                leased = self.store.lease_next(
                    self.owner,
                    lease_seconds=self.lease_seconds,
                    states=(state,),
                    source=source,
                    action_type=self.action_type,
                )
                if leased is not None:
                    return leased
        for source in self._sources():
            leased = self.store.lease_dispatched_observation(
                self.owner,
                lease_seconds=self.lease_seconds,
                source=source,
                action_type=self.action_type,
            )
            if leased is not None:
                return leased
        for source in self._sources():
            leased = self.store.lease_next(
                self.owner,
                lease_seconds=self.lease_seconds,
                states=("gated",),
                source=source,
                action_type=self.action_type,
            )
            if leased is not None:
                return leased
        return None

    # ------------------------------------------------------------- parsing

    def _parse(self, action: Mapping[str, Any]) -> HermesToolActionIntentV1:
        payload = action.get("payload")
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"schema", "version", "intent"}
            or payload.get("schema") != INTENT_ENVELOPE_SCHEMA
            or isinstance(payload.get("version"), bool)
            or payload.get("version") != 1
            or action.get("max_attempts") != 1
        ):
            raise GovernedActionWorkerError("action envelope is invalid")
        try:
            intent = HermesToolActionIntentV1.from_mapping(payload["intent"])
        except HermesActionIntentError as error:
            raise GovernedActionWorkerError("action intent is invalid") from error
        if (
            intent.tool_name not in self.enabled_tools
            or action.get("source") not in self._sources()
            or action.get("action_type") != self.action_type
            or action.get("source_ref") != intent.intent_id
            or action.get("payload_sha256") != sha256_json_utf8(dict(payload))
        ):
            raise GovernedActionWorkerError("action binding is invalid")
        return intent

    def _validate_gate(
        self,
        action: Mapping[str, Any],
        receipts,
        *,
        tool_name: str,
        now: float,
    ) -> GateAuthorization:
        return validate_owner_gate(
            action,
            receipts,
            tool_name=tool_name,
            now=now,
            registry=self.registry,
            catalog=self.catalog,
        )

    def _gate_validator(self, tool_name: str):
        """The in-transaction re-validation the store runs (invariant I4).

        Same validator as the pre-check, but executed against the durable
        projections re-read inside the dispatch transaction and the store's
        clock, with point-of-use expiry enforced by
        :func:`~colony_hostworker.gate.assert_dispatchable`.
        """

        def revalidate(action, receipts, now):
            return assert_dispatchable(
                self._validate_gate(
                    action, receipts, tool_name=tool_name, now=now
                )
            )

        return revalidate

    # ------------------------------------------------------------ recovery

    def _fail(self, action_id: str, code: str):
        safe = re.sub(r"[^a-z0-9_]+", "_", str(code).lower()).strip("_")
        try:
            return self.store.fail_attempt(
                action_id,
                self.owner,
                safe[:160] or "governed_action_failed",
                retryable=False,
            )
        except ActionStoreError:
            return self._current(action_id)

    def _current(self, action_id: str):
        try:
            return self.store.get_action(action_id)
        except Exception:
            # A lifecycle race must never cause a second mutation attempt.
            return None

    def _accept_execution(self, action, request, response):
        acceptance_key = "governed-action-acceptance:" + request["execution_digest"]
        try:
            accepted, _ = self.store.accept(
                action["action_id"],
                self.owner,
                acceptance_key,
                "governed_action_acceptance",
                response,
                external_id=response["action_id"],
                result={
                    "status": "accepted",
                    "execution_digest": request["execution_digest"],
                    "effect_digest": response["effect_digest"],
                },
            )
            return accepted
        except ActionStoreError:
            return self._current(action["action_id"])

    def _observe_dispatched(self, action, request):
        """Perform one durable, bounded GET attempt and never issue a PUT."""

        try:
            action, observation_receipt = self.store.begin_dispatched_observation(
                action["action_id"],
                self.owner,
                request["execution_digest"],
            )
        except ActionStoreError:
            return self._current(action["action_id"])
        if action["state"] != "dispatched" or observation_receipt is None:
            return action
        try:
            observed = validate_execution_result(
                self.dispatcher.observe(request), request, clock=self.clock
            )
        except Exception:
            try:
                return self.store.defer_dispatched_observation(
                    action["action_id"],
                    self.owner,
                    request["execution_digest"],
                    observation_receipt["receipt_key"],
                    "endpoint_observation_unavailable_or_noncompleted",
                    OBSERVATION_RETRY_SECONDS,
                )
            except ActionStoreError:
                return self._current(action["action_id"])
        return self._accept_execution(action, request, observed)

    # --------------------------------------------------------- the machine

    def process_one(self):
        """Lease and advance at most one governed action; return its row.

        Safe to call from a simple host loop; every step is individually
        durable and crash-recoverable per the store contract.
        """

        action = self._lease()
        if action is None:
            return None
        try:
            intent = self._parse(action)
            gate = self._validate_gate(
                action,
                self.store.list_receipts(action["action_id"]),
                tool_name=intent.tool_name,
                now=float(self.clock()),
            )
            request = build_execution_request(action, intent, gate)
        except Exception:
            return self._fail(action["action_id"], "invalid_governed_action")

        if action["state"] == "gated":
            if gate.expired:
                return self._fail(action["action_id"], "owner_approval_expired")
            try:
                self.admission.assert_live()
            except Exception:
                try:
                    return self.store.defer_leased(
                        action["action_id"],
                        self.owner,
                        "worker_dispatch_admission_closed",
                        ADMISSION_DEFER_SECONDS,
                        event_type="dispatch_admission_deferred",
                    )
                except ActionStoreError:
                    return self._current(action["action_id"])
            try:
                action = self.store.begin_owner_authorized_dispatch(
                    action["action_id"],
                    self.owner,
                    gate_receipt_key=gate.receipt_key,
                    expected_source=action["source"],
                    expected_source_ref=intent.intent_id,
                    expected_action_type=self.action_type,
                    expected_payload=action["payload"],
                    expected_approval_id=gate.approval_id,
                    expected_decision_id=gate.decision_id,
                    expected_execution_digest=request["execution_digest"],
                    observation_window_seconds=OBSERVATION_WINDOW_SECONDS,
                    max_observations=OBSERVATION_MAX_ATTEMPTS,
                    gate_validator=self._gate_validator(intent.tool_name),
                )
            except Exception:
                return self._fail(action["action_id"], "owner_gate_rejected")
            try:
                response = validate_execution_result(
                    self.dispatcher.execute(request), request, clock=self.clock
                )
            except Exception:
                # The one PUT's outcome is unknown; from here on the only
                # legal traffic for this action is GET.
                action = self._observe_dispatched(action, request)
            else:
                action = self._accept_execution(action, request, response)
        elif action["state"] == "dispatched":
            action = self._observe_dispatched(action, request)

        if action is None:
            return None

        acceptance_key = "governed-action-acceptance:" + request["execution_digest"]
        if action["state"] == "accepted":
            try:
                observed = validate_execution_result(
                    self.dispatcher.observe(request), request, clock=self.clock
                )
                acceptance = next(
                    receipt
                    for receipt in self.store.list_receipts(action["action_id"])
                    if receipt["receipt_key"] == acceptance_key
                )
                if acceptance.get("evidence") != observed:
                    raise GovernedActionWorkerError(
                        "durable endpoint observation changed"
                    )
            except Exception:
                try:
                    return self.store.defer_leased(
                        action["action_id"],
                        self.owner,
                        "endpoint_verification_unavailable",
                        OBSERVATION_RETRY_SECONDS,
                        event_type="verification_deferred",
                    )
                except ActionStoreError:
                    return self._current(action["action_id"])
            try:
                action, _ = self.store.verify(
                    action["action_id"],
                    self.owner,
                    "governed-action-verification:" + request["execution_digest"],
                    observed,
                    qualifying_receipt_keys=(acceptance_key,),
                )
            except ActionStoreError:
                return self._current(action["action_id"])

        if action["state"] == "verified":
            try:
                return self.store.complete(
                    action["action_id"],
                    self.owner,
                    {
                        "status": "completed",
                        "execution_digest": request["execution_digest"],
                        "intent_id": intent.intent_id,
                        "tool_name": intent.tool_name,
                    },
                )
            except ActionStoreError:
                return self._current(action["action_id"])
        return action


__all__ = (
    "ADMISSION_DEFER_SECONDS",
    "DEFAULT_ACTION_TYPE",
    "DEFAULT_SOURCE_PREFIX",
    "GovernedActionWorker",
    "GovernedActionWorkerError",
    "OBSERVATION_MAX_ATTEMPTS",
    "OBSERVATION_RETRY_SECONDS",
    "OBSERVATION_WINDOW_SECONDS",
    "build_execution_request",
    "validate_execution_result",
)

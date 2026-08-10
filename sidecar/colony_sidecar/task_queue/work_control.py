"""Generic, durable control contract for Colony queue work.

``WorkControlV1`` deliberately controls only real jobs owned by the durable
task queue.  A queue job is the stable target, its server-minted claim is the
attempt, and a durable run ID binds both without copying job payloads into the
control ledger.

The module contains validation/projection helpers plus a small service facade.
Persistence and state transitions remain in :class:`QueueManager` so normal
job lifecycle writes and control compare-and-swap operations share one lock
and one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


WORK_CONTROL_SCHEMA = "WorkControlV1"
WORK_CONTROL_VERSION = 1
WORK_CONTROL_OPERATIONS = frozenset({"steer", "interrupt", "cancel", "retry"})
WORK_CONTROL_INTERRUPT_CAPABILITY_PREFIX = "work_control:interrupt:v1:"
WORK_CONTROL_STEER_CAPABILITY_PREFIX = "work_control:steer:v1:"

_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkControlError(RuntimeError):
    """Typed public failure from the WorkControl service."""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


def work_control_mode() -> str:
    """Return additive control-plane rollout posture.

    ``off`` removes the WorkControl mutation/delivery surface and advertised
    capabilities. Mode-invariant lifecycle corrections still prevent false
    active cancellation and effectful automatic retry; disabling the feature
    deliberately does not restore those unsafe P6 behaviors.
    """

    value = os.environ.get("COLONY_WORK_CONTROL_MODE", "off").strip().lower()
    if value not in {"off", "shadow", "live"}:
        raise WorkControlError(
            "work_control_configuration_invalid",
            "COLONY_WORK_CONTROL_MODE must be off, shadow, or live",
            status_code=503,
        )
    return value


def work_control_ack_timeout_secs() -> float:
    """Bound a worker command lease so controls cannot strand work forever."""

    raw = os.environ.get("COLONY_WORK_CONTROL_ACK_TIMEOUT_SECS", "30")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise WorkControlError(
            "work_control_configuration_invalid",
            "COLONY_WORK_CONTROL_ACK_TIMEOUT_SECS must be numeric",
            status_code=503,
        ) from exc
    if not 1.0 <= value <= 300.0:
        raise WorkControlError(
            "work_control_configuration_invalid",
            "COLONY_WORK_CONTROL_ACK_TIMEOUT_SECS must be within 1..300",
            status_code=503,
        )
    return value


def canonical_json(value: Any) -> str:
    """Encode a value exactly once for all WorkControl digests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def steer_capability(job_type: object) -> str:
    value = getattr(job_type, "value", job_type)
    return f"{WORK_CONTROL_STEER_CAPABILITY_PREFIX}{str(value)}"


def interrupt_capability(job_type: object) -> str:
    value = getattr(job_type, "value", job_type)
    return f"{WORK_CONTROL_INTERRUPT_CAPABILITY_PREFIX}{str(value)}"


def validate_operation_id(value: object) -> str:
    operation_id = str(value or "").strip()
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise WorkControlError(
            "invalid_operation_id",
            "operation_id must be a canonical 1..128 character identifier",
            status_code=422,
        )
    return operation_id


def validate_digest(value: object, *, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise WorkControlError(
            f"invalid_{field}",
            f"{field} must be a lowercase SHA-256 digest",
            status_code=422,
        )
    return digest


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise WorkControlError(
            f"invalid_{field}",
            f"{field} must be 1..{maximum} characters",
            status_code=422,
        )
    return text


def normalize_operation_request(
    *,
    operation_id: object,
    operation: object,
    target_id: object,
    run_id: object,
    attempt_id: Optional[object],
    expected_revision: object,
    expected_state_digest: object,
    parameters: Optional[Mapping[str, Any]],
    reason: Optional[object],
) -> Dict[str, Any]:
    """Validate and canonicalize one caller-authored control operation."""

    op_id = validate_operation_id(operation_id)
    op = str(operation or "").strip().lower()
    if op not in WORK_CONTROL_OPERATIONS:
        raise WorkControlError(
            "unsupported_operation",
            "operation must be steer, interrupt, cancel, or retry",
            status_code=422,
        )
    target = _bounded_text(target_id, field="target_id", maximum=192)
    run = _bounded_text(run_id, field="run_id", maximum=128)
    attempt = str(attempt_id or "").strip() or None
    if attempt is not None and len(attempt) > 128:
        raise WorkControlError(
            "invalid_attempt_id",
            "attempt_id must be at most 128 characters",
            status_code=422,
        )
    if type(expected_revision) is not int or expected_revision < 1:
        raise WorkControlError(
            "invalid_expected_revision",
            "expected_revision must be a positive integer",
            status_code=422,
        )
    state_digest = validate_digest(
        expected_state_digest, field="expected_state_digest",
    )
    params = dict(parameters or {})
    if op == "steer":
        unknown = set(params) - {"directive", "context_refs"}
        if unknown:
            raise WorkControlError(
                "invalid_steer_parameters",
                "steer accepts only directive and context_refs",
                status_code=422,
            )
        directive = _bounded_text(
            params.get("directive"), field="directive", maximum=4000,
        )
        refs = params.get("context_refs", [])
        if not isinstance(refs, list) or len(refs) > 32:
            raise WorkControlError(
                "invalid_context_refs",
                "context_refs must be a list of at most 32 references",
                status_code=422,
            )
        normalized_refs = []
        for item in refs:
            normalized_refs.append(
                _bounded_text(item, field="context_ref", maximum=256)
            )
        params = {
            "directive": directive,
            "context_refs": list(dict.fromkeys(normalized_refs)),
        }
    elif params:
        raise WorkControlError(
            "unexpected_operation_parameters",
            f"{op} does not accept parameters",
            status_code=422,
        )
    try:
        encoded_parameters = canonical_json(params)
    except (TypeError, ValueError) as exc:
        raise WorkControlError(
            "invalid_operation_parameters",
            "operation parameters must be finite JSON",
            status_code=422,
        ) from exc
    if len(encoded_parameters.encode("utf-8")) > 16 * 1024:
        raise WorkControlError(
            "operation_parameters_too_large",
            "operation parameters exceed 16 KiB",
            status_code=422,
        )
    reason_text = str(reason or "").strip()
    if len(reason_text) > 500:
        raise WorkControlError(
            "invalid_reason", "reason must be at most 500 characters",
            status_code=422,
        )
    return {
        "schema": "WorkControlOperationV1",
        "version": 1,
        "operation_id": op_id,
        "operation": op,
        "target_id": target,
        "run_id": run,
        "attempt_id": attempt,
        "expected_revision": expected_revision,
        "expected_state_digest": state_digest,
        "parameters": params,
        "reason": reason_text,
    }


def operation_request_digest(request: Mapping[str, Any]) -> str:
    return digest_json(dict(request))


def build_receipt(row: Mapping[str, Any], *, replayed: bool = False) -> Dict[str, Any]:
    """Project a durable operation row without exposing job payloads."""

    ack = json.loads(row.get("ack_details") or "{}")
    receipt = {
        "schema": "WorkControlReceiptV1",
        "version": 1,
        "operation_id": str(row["operation_id"]),
        "operation": str(row["operation_type"]),
        "request_digest": str(row["request_digest"]),
        "requested_by": str(row["requested_by"]),
        "request_authority": json.loads(row.get("request_authority") or "{}"),
        "target_id": str(row["target_id"]),
        "run_id": str(row["run_id"]),
        "authority_digest": str(row["authority_digest"]),
        "attempt_id": row.get("attempt_id"),
        "worker_id": row.get("worker_id"),
        "status": str(row["status"]),
        "from_job_status": row.get("from_job_status"),
        "to_job_status": row.get("to_job_status"),
        "effect_disposition": str(row.get("effect_disposition") or "unknown"),
        "accepted_revision": int(row["accepted_revision"]),
        "result_revision": (
            int(row["result_revision"])
            if row.get("result_revision") is not None else None
        ),
        "result_state_digest": row.get("result_state_digest"),
        "created_at": str(row["created_at"]),
        "ack_deadline": row.get("ack_deadline"),
        "acknowledged_at": row.get("acknowledged_at"),
        "acknowledgement": ack,
        "ack_authority": json.loads(row.get("ack_authority") or "{}"),
        "replayed": bool(replayed),
    }
    # The digest deliberately excludes itself and the transport-only replay bit.
    bound = dict(receipt)
    bound.pop("replayed", None)
    receipt["receipt_digest"] = digest_json(bound)
    return receipt


@dataclass(frozen=True)
class WorkControlService:
    """Public in-process facade over QueueManager's transactional methods."""

    queue: Any

    async def inspect(self, target_id: str) -> Dict[str, Any]:
        return await self.queue.get_work_control_target(target_id)

    async def operate(self, **request: Any) -> Dict[str, Any]:
        return await self.queue.apply_work_control_operation(**request)

    async def receipt(
        self, target_id: str, operation_id: str,
    ) -> Dict[str, Any]:
        return await self.queue.get_work_control_receipt(
            target_id, operation_id,
        )

    async def reconcile_effect(self, **request: Any) -> Dict[str, Any]:
        return await self.queue.reconcile_work_effect(**request)

    async def pending_for_worker(self, worker_id: str) -> list[Dict[str, Any]]:
        return await self.queue.pending_work_control_operations(worker_id)

    async def acknowledge(
        self,
        *,
        worker_id: str,
        operation_id: str,
        attempt_id: str,
        outcome: str,
        details: Optional[Mapping[str, Any]] = None,
        ack_authority: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.queue.acknowledge_work_control_operation(
            worker_id=worker_id,
            operation_id=operation_id,
            attempt_id=attempt_id,
            outcome=outcome,
            details=dict(details or {}),
            ack_authority=dict(ack_authority or {}),
        )

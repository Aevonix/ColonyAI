"""Versioned execution results for canonical Colony WorkOrders.

The task queue is only a transport.  A terminal queue state is converted into
an :class:`ExecutionResultV1`, bound to the exact WorkOrder authority digest,
before a project step can use it.  Mutating or disclosing effects remain
unverified unless a separately wired receipt verifier attests concrete receipt
references.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


EXECUTION_RESULT_VERSION = 1
TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "skipped", "cancelled"})
EFFECT_CLASSES = frozenset({"none", "read_only", "mutation", "disclosure"})
VERIFICATION_RESULTS = frozenset({"verified", "unverified", "not_applicable"})
EXTERNAL_EFFECT_CLASSES = frozenset({"mutation", "disclosure"})

_REF_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,31}:[^\s]{1,480}$")


class ExecutionResultError(ValueError):
    """Raised when an execution claim is stale, malformed, or unbound."""


def _bounded(value: object, name: str, maximum: int, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if (required and not text) or len(text) > maximum:
        qualifier = f"1..{maximum}" if required else f"0..{maximum}"
        raise ExecutionResultError(f"{name} must be {qualifier} characters")
    return text


def _parse_time(value: object, name: str) -> datetime:
    text = _bounded(value, name, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionResultError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ExecutionResultError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def bounded_refs(values: object, *, name: str = "receipt_refs") -> Tuple[str, ...]:
    """Normalize bounded reference-only evidence without copying artifacts."""

    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ExecutionResultError(f"{name} must be a list")
    if len(values) > 32:
        raise ExecutionResultError(f"{name} may contain at most 32 references")
    refs = []
    for item in values:
        if isinstance(item, Mapping):
            ref = _bounded(item.get("ref"), f"{name}.ref", 512)
            digest = str(item.get("sha256") or "").strip().lower()
            if digest:
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ExecutionResultError(f"{name}.sha256 must be 64 hex characters")
                ref = f"{ref}#sha256={digest}"
        else:
            ref = _bounded(item, name, 512)
        if not _REF_SCHEME.fullmatch(ref):
            raise ExecutionResultError(
                f"{name} entries must be bounded scheme-qualified references"
            )
        refs.append(ref)
    return tuple(sorted(set(refs)))


@dataclass(frozen=True)
class ReceiptVerificationV1:
    """Decision returned by an independently wired effect-receipt verifier."""

    verified: bool
    receipt_refs: Tuple[str, ...] = ()
    verifier_identity: str = ""
    detail: str = ""

    @classmethod
    def from_value(cls, value: object) -> "ReceiptVerificationV1":
        if isinstance(value, cls):
            result = value
        elif isinstance(value, Mapping):
            verified = value.get("verified")
            if not isinstance(verified, bool):
                raise ExecutionResultError("receipt verifier decision must use a boolean")
            result = cls(
                verified=verified,
                receipt_refs=bounded_refs(value.get("receipt_refs")),
                verifier_identity=_bounded(
                    value.get("verifier_identity"),
                    "verifier_identity",
                    256,
                    required=False,
                ),
                detail=_bounded(value.get("detail"), "detail", 1000, required=False),
            )
        else:
            raise ExecutionResultError("receipt verifier returned an invalid decision")
        if not isinstance(result.verified, bool):
            raise ExecutionResultError("receipt verifier decision must use a boolean")
        if result.verified:
            refs = bounded_refs(result.receipt_refs)
            identity = _bounded(result.verifier_identity, "verifier_identity", 256)
            if not refs:
                raise ExecutionResultError(
                    "a verified external effect requires at least one receipt reference"
                )
            return replace(result, receipt_refs=refs, verifier_identity=identity)
        return replace(
            result,
            receipt_refs=bounded_refs(result.receipt_refs),
            verifier_identity=_bounded(
                result.verifier_identity,
                "verifier_identity",
                256,
                required=False,
            ),
            detail=_bounded(result.detail, "detail", 1000, required=False),
        )


@dataclass(frozen=True)
class ExecutionResultV1:
    """One logical, receipt-aware terminal result for a WorkOrder.

    A WorkOrder may have several run attempts.  ``result_ref`` remains stable
    across them; ``run_id`` and ``attempt_number`` identify the current
    terminal attempt stored in the logical result row.
    """

    result_ref: str
    work_order_id: str
    work_order_digest: str
    work_order_version: int
    run_id: str
    attempt_number: int
    terminal_outcome: str
    started_at: str
    ended_at: str
    executor_identity: str
    effect_class: str
    receipt_refs: Tuple[str, ...]
    verification_result: str
    verifier_identity: str = ""
    summary: str = ""
    error: str = ""
    schema: str = "ExecutionResultV1"
    version: int = EXECUTION_RESULT_VERSION

    @staticmethod
    def ref_for(work_order_id: str) -> str:
        return f"execution-result:{work_order_id}"

    def payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["receipt_refs"] = list(self.receipt_refs)
        return payload

    def with_verification(
        self,
        verification_result: str,
        *,
        receipt_refs: Optional[Iterable[str]] = None,
        verifier_identity: str = "",
        error: Optional[str] = None,
    ) -> "ExecutionResultV1":
        if verification_result not in VERIFICATION_RESULTS:
            raise ExecutionResultError("invalid verification_result")
        refs = self.receipt_refs if receipt_refs is None else bounded_refs(tuple(receipt_refs))
        return replace(
            self,
            verification_result=verification_result,
            receipt_refs=refs,
            verifier_identity=_bounded(
                verifier_identity, "verifier_identity", 256, required=False
            ),
            error=self.error if error is None else _bounded(
                error, "error", 2000, required=False
            ),
        )

    @classmethod
    def from_job(cls, order: Any, job: Any) -> "ExecutionResultV1":
        """Bind a terminal queue job to its exact WorkOrder contract.

        Completed jobs should carry a nested ``execution_result`` object.  A
        legacy completed payload is retained as an *unverified* logical result
        so operators can see the claim without treating it as evidence.
        Transport failures/cancellations are converted deterministically.
        """

        result = getattr(job, "result", None)
        output = dict(getattr(result, "output", None) or {})
        raw = output.get("execution_result")
        typed = isinstance(raw, Mapping)
        raw = dict(raw) if typed else {}

        transport_status = str(getattr(getattr(job, "status", None), "value", "") or "")
        action_plane = output.get("action_plane")
        action_plane = dict(action_plane) if isinstance(action_plane, Mapping) else {}

        if typed:
            if raw.get("schema") != "ExecutionResultV1":
                raise ExecutionResultError("execution result schema is stale or unsupported")
            if type(raw.get("version")) is not int or raw["version"] != EXECUTION_RESULT_VERSION:
                raise ExecutionResultError("execution result version is stale or unsupported")
            if str(raw.get("work_order_id") or "") != order.work_order_id:
                raise ExecutionResultError("execution result WorkOrder ID mismatch")
            if str(raw.get("work_order_digest") or "") != order.work_order_digest:
                raise ExecutionResultError("execution result WorkOrder digest mismatch")
            if (
                type(raw.get("work_order_version")) is not int
                or raw["work_order_version"] != order.version
            ):
                raise ExecutionResultError("execution result WorkOrder version is stale")

        semantic = str(raw.get("terminal_outcome") or output.get("status") or "").lower()
        action_state = str(action_plane.get("state") or "").lower()
        if transport_status == "failed":
            outcome = "failed"
        elif transport_status == "cancelled":
            outcome = "cancelled"
        elif semantic in ("skipped", "cancelled") or action_state == "skipped":
            outcome = "skipped" if semantic != "cancelled" else "cancelled"
        elif semantic in ("failed", "error") or action_state == "failed":
            outcome = "failed"
        elif semantic in ("succeeded", "completed", "verified") or action_state == "completed":
            outcome = "succeeded"
        else:
            outcome = "failed"

        claimed_outcome = str(raw.get("terminal_outcome") or "").lower()
        if typed and claimed_outcome not in TERMINAL_OUTCOMES:
            raise ExecutionResultError("invalid terminal_outcome")
        if typed and claimed_outcome != outcome:
            raise ExecutionResultError("execution result conflicts with transport outcome")

        expected_effect = str(order.effect_class)
        claimed_effect = str(raw.get("effect_class") or expected_effect)
        if expected_effect not in EFFECT_CLASSES or claimed_effect != expected_effect:
            raise ExecutionResultError("execution result effect class mismatch")

        worker_identity = str(
            getattr(result, "worker_node_id", None)
            or getattr(job, "claimed_by", None)
            or "unknown-queue-worker"
        )
        claimed_identity = str(raw.get("executor_identity") or worker_identity)
        if typed and worker_identity != "unknown-queue-worker" and claimed_identity != worker_identity:
            raise ExecutionResultError("execution result executor identity mismatch")

        if typed and type(raw.get("attempt_number")) is not int:
            raise ExecutionResultError("attempt_number must be an integer")
        attempt_number = int(
            raw.get("attempt_number") or (getattr(job, "retry_count", 0) + 1)
        )
        if attempt_number < 1 or attempt_number > int(order.max_attempts):
            raise ExecutionResultError("execution result attempt exceeds WorkOrder budget")

        run_id = str(
            raw.get("run_id")
            or output.get("run_id")
            or action_plane.get("run_id")
            or action_plane.get("action_id")
            or f"queue:{job.job_id}:attempt:{attempt_number}"
        )
        run_id = _bounded(run_id, "run_id", 256)

        server_started = (
            getattr(result, "started_at", None)
            or getattr(job, "claimed_at", None)
            or getattr(job, "posted_at", None)
        )
        server_ended = (
            getattr(result, "completed_at", None)
            or getattr(job, "last_heartbeat", None)
            or getattr(job, "claimed_at", None)
            or getattr(job, "posted_at", None)
            or datetime.now(timezone.utc)
        )
        if typed:
            # Worker timestamps remain schema-validated diagnostic claims,
            # but they cannot define learning/expectation time.  The queue
            # manager's persisted JobResult timestamps are server-owned.
            worker_started = _parse_time(raw.get("started_at"), "started_at")
            worker_ended = _parse_time(raw.get("ended_at"), "ended_at")
            if worker_ended < worker_started:
                raise ExecutionResultError("worker ended_at precedes started_at")
        if server_started is None:
            server_started = server_ended
        if not isinstance(server_started, datetime) \
                or not isinstance(server_ended, datetime):
            raise ExecutionResultError(
                "queue result requires server-owned terminal timestamps"
            )
        started = server_started
        ended = server_ended
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        started = started.astimezone(timezone.utc)
        ended = ended.astimezone(timezone.utc)
        if ended < started:
            raise ExecutionResultError("ended_at precedes started_at")

        refs = bounded_refs(raw.get("receipt_refs") if typed else output.get("evidence_refs"))
        summary = _bounded(
            raw.get("summary") if typed else (output.get("summary") or output.get("result")),
            "summary",
            4000,
            required=False,
        )
        raw_error = (
            raw.get("error") if typed else (
                getattr(result, "error", None)
                or output.get("reason")
                or action_plane.get("last_error")
            )
        )
        error = _bounded(raw_error, "error", 2000, required=False)
        if not typed and outcome == "succeeded":
            error = error or "legacy completion lacks ExecutionResultV1 attestation"

        verification = "not_applicable" if outcome != "succeeded" else "unverified"
        return cls(
            result_ref=cls.ref_for(order.work_order_id),
            work_order_id=order.work_order_id,
            work_order_digest=order.work_order_digest,
            work_order_version=order.version,
            run_id=run_id,
            attempt_number=attempt_number,
            terminal_outcome=outcome,
            started_at=_iso(started),
            ended_at=_iso(ended),
            executor_identity=_bounded(claimed_identity, "executor_identity", 256),
            effect_class=expected_effect,
            receipt_refs=refs,
            verification_result=verification,
            summary=summary,
            error=error,
        )

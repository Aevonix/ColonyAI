"""Canonical WorkOrderV1 bridge from durable projects to external executors.

Colony owns the goal/work ledger; it does not execute external effects.  A
project step becomes one deterministic ``agent_action`` queue job containing
bounded references and success criteria.  The host-side Action Plane decides
authorization and records real effect receipts.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from colony_sidecar.execution_results import (
    EXTERNAL_EFFECT_CLASSES,
    ExecutionResultError,
    ExecutionResultV1,
    ReceiptVerificationV1,
)
from colony_sidecar.projects.store import ProjectStore
from colony_sidecar.scope_bounds import VIEWER_SCOPE_MAX_CHARS
from colony_sidecar.task_queue.models import Job, JobPriority, JobStatus, JobType
from colony_sidecar.task_queue.models import JobCapabilityRequirement
from colony_sidecar.task_queue.routing import (
    ACTION_PLANE_ROUTE,
    WORK_ORDER_ROUTE,
)


WORK_ORDER_VERSION = 1
WORK_ORDER_EXECUTOR_CAPABILITY = WORK_ORDER_ROUTE
logger = logging.getLogger(__name__)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|credential|password|secret|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_IMPORT_PATH = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$"
)
_ACTION_KIND = {
    "analyze": ("agent_project_analyze", ("memory:read", "reasoning"), "internal"),
    "research": ("agent_project_research", ("memory:read", "web:read", "reasoning"), "read_only"),
    "internal": ("agent_project_internal", ("memory:read", "reasoning"), "internal"),
    "directed": (
        "agent_project_directed",
        (
            "code:execute",
            "filesystem:read",
            "filesystem:write",
            "git:write",
            "memory:read",
            "reasoning",
            "terminal:execute",
            "web:read",
        ),
        "mutation",
    ),
    "deliver": ("agent_project_deliver", ("messaging:send",), "disclosure"),
}


def action_authority(action_kind: str) -> Tuple[str, Tuple[str, ...], str]:
    """Return the canonical WorkOrder action/capability/risk contract."""

    action = _ACTION_KIND.get(str(action_kind or ""))
    if action is None:
        raise WorkOrderError("unsupported project action kind")
    return action


class WorkOrderError(ValueError):
    pass


class ReceiptVerifierConfigurationError(RuntimeError):
    """A configured external-effect verifier could not be built safely."""


def load_receipt_verifier_from_env(environ: Optional[Mapping[str, str]] = None) -> Any:
    """Construct the configured receipt verifier without host coupling.

    The verifier remains an operator-selected plugin.  Colony knows only a
    ``module:attribute`` factory and a bounded JSON object of keyword
    arguments; it does not import any particular host deployment by default.  An
    explicitly configured verifier fails closed on every import, parse,
    construction, or interface error.
    """

    env = os.environ if environ is None else environ
    spec = str(env.get("COLONY_WORK_ORDER_RECEIPT_VERIFIER", "") or "").strip()
    if not spec:
        return None
    if len(spec) > 512 or not _IMPORT_PATH.fullmatch(spec):
        raise ReceiptVerifierConfigurationError(
            "COLONY_WORK_ORDER_RECEIPT_VERIFIER must be module:attribute"
        )

    raw_config = str(
        env.get("COLONY_WORK_ORDER_RECEIPT_VERIFIER_CONFIG", "{}") or "{}"
    )
    if len(raw_config.encode("utf-8")) > 16 * 1024:
        raise ReceiptVerifierConfigurationError("receipt verifier config is too large")
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError) as exc:
        raise ReceiptVerifierConfigurationError(
            "receipt verifier config must be valid JSON"
        ) from exc
    if not isinstance(config, dict) or len(config) > 64:
        raise ReceiptVerifierConfigurationError(
            "receipt verifier config must be a JSON object with at most 64 fields"
        )
    if not all(isinstance(key, str) and key for key in config):
        raise ReceiptVerifierConfigurationError(
            "receipt verifier config keys must be non-empty strings"
        )

    module_name, attribute_path = spec.split(":", 1)
    try:
        target = importlib.import_module(module_name)
        for name in attribute_path.split("."):
            target = getattr(target, name)
    except (ImportError, AttributeError) as exc:
        raise ReceiptVerifierConfigurationError(
            "configured receipt verifier could not be imported"
        ) from exc
    if not callable(target):
        raise ReceiptVerifierConfigurationError(
            "configured receipt verifier factory is not callable"
        )
    try:
        verifier = target(**config)
    except Exception as exc:
        raise ReceiptVerifierConfigurationError(
            "configured receipt verifier factory failed"
        ) from exc
    if not callable(getattr(verifier, "verify", None)) and not callable(verifier):
        raise ReceiptVerifierConfigurationError(
            "configured receipt verifier has no callable verify interface"
        )
    return verifier


def _bounded_text(value: object, name: str, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise WorkOrderError(f"{name} must be 1..{maximum} characters")
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class WorkOrderV1:
    work_order_id: str
    idempotency_key: str
    project_id: str
    step_id: str
    step_ordinal: int
    objective: str
    success_criteria: Tuple[str, ...]
    context_refs: Tuple[str, ...]
    capability_allowlist: Tuple[str, ...]
    risk_class: str
    recipient_scope: str
    max_runtime_seconds: int
    max_attempts: int
    issued_at: str
    deadline: str
    action_hint: str
    work_order_digest: str
    source: str = "project_engine"
    schema: str = "WorkOrderV1"
    version: int = WORK_ORDER_VERSION

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorkOrderV1":
        """Reconstruct and verify every authority-bearing queue field.

        A WorkOrder digest is useful only when the queue validates the object
        that produced it.  Treating ``schema=WorkOrderV1`` as a routing hint
        would let a malformed row acquire the privileged Action Plane route.
        """

        if not isinstance(payload, Mapping):
            raise WorkOrderError("WorkOrder payload must be an object")
        if payload.get("schema") != "WorkOrderV1":
            raise WorkOrderError("WorkOrder schema is invalid")
        if payload.get("version") != WORK_ORDER_VERSION:
            raise WorkOrderError("WorkOrder version is stale")
        try:
            order = cls(
                work_order_id=str(payload["work_order_id"]),
                idempotency_key=str(payload["idempotency_key"]),
                project_id=str(payload["project_id"]),
                step_id=str(payload["step_id"]),
                step_ordinal=int(payload["step_ordinal"]),
                objective=str(payload["objective"]),
                success_criteria=tuple(payload["success_criteria"]),
                context_refs=tuple(payload["context_refs"]),
                capability_allowlist=tuple(payload["capability_allowlist"]),
                risk_class=str(payload["risk_class"]),
                recipient_scope=str(payload["recipient_scope"]),
                max_runtime_seconds=int(payload["max_runtime_seconds"]),
                max_attempts=int(payload["max_attempts"]),
                issued_at=str(payload["issued_at"]),
                deadline=str(payload["deadline"]),
                action_hint=str(payload["action_hint"]),
                work_order_digest=str(payload["work_order_digest"]),
                source=str(payload["source"]),
                schema=str(payload["schema"]),
                version=int(payload["version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            missing = exc.args[0] if isinstance(exc, KeyError) else str(exc)
            raise WorkOrderError(
                f"invalid WorkOrder authority field: {missing}"
            ) from exc
        order.validate()
        order.validate_payload(payload)
        return order

    @classmethod
    def for_project_step(
        cls,
        project,
        step,
        *,
        context_refs: Iterable[str] = (),
        now: Optional[datetime] = None,
    ) -> "WorkOrderV1":
        action = action_authority(str(step.action_kind or ""))
        action_hint, capabilities, risk_class = action
        project_id = _bounded_text(project.id, "project_id", 128)
        step_id = _bounded_text(step.id, "step_id", 128)
        project_source = _bounded_text(project.source, "project_source", 64)
        boundary_subjects = " ".join(
            item for item in (
                str(getattr(step, "boundary_subject", "") or "").strip(),
                str(getattr(project, "subject_person_id", "") or "").strip(),
                " ".join(
                    str(item) for item in (
                        getattr(project, "entity_ids", None) or ()
                    ) if str(item).strip()
                ),
            ) if item
        )
        objective = _bounded_text(
            f"Project: {project.title}\nObjective: {project.objective}\n"
            f"Current step: {step.description}\n"
            f"Boundary subject: {boundary_subjects or 'project subject'}",
            "objective",
            4000,
        )
        criteria = (
            "Complete only the named project step",
            "Return structured status and evidence receipts",
            "Do not claim an external effect without independent evidence",
        )
        refs = tuple(sorted({
            _bounded_text(item, "context_ref", 256) for item in context_refs if str(item).strip()
        }))
        # The deadline is authority-relevant, so its source must be stable
        # across polling/restarts.  A caller may override it for deterministic
        # construction; otherwise the durable step creation time is used.
        observed = now or datetime.fromtimestamp(
            float(
                getattr(step, "work_order_issued_at", 0.0)
                or getattr(step, "created_at", 0.0)
                or getattr(project, "created_at", 0.0)
            ),
            tz=timezone.utc,
        )
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        runtime = 900 if step.action_kind == "research" else 600
        if project_source in {"cognition_spine", "governed_action"}:
            recipient_scope = _bounded_text(
                getattr(project, "viewer_scope", "") or "owner",
                "recipient_scope", VIEWER_SCOPE_MAX_CHARS,
            )
        else:
            recipient_scope = (
                "owner" if project.source == "owner" else "project:%s" % project_id
            )
        authority = {
            "schema": "WorkOrderV1",
            "version": WORK_ORDER_VERSION,
            "source": project_source,
            "project_id": project_id,
            "step_id": step_id,
            "step_ordinal": int(step.ordinal),
            "objective": objective,
            "success_criteria": list(criteria),
            "context_refs": list(refs),
            "capability_allowlist": list(capabilities),
            "risk_class": risk_class,
            "recipient_scope": recipient_scope,
            "max_runtime_seconds": runtime,
            "max_attempts": 2,
            "issued_at": observed.isoformat(),
            "deadline": (observed + timedelta(hours=24)).isoformat(),
            "action_hint": action_hint,
        }
        digest = cls.authority_digest_from_payload(authority)
        return cls(
            work_order_id="work-%s" % digest[:24],
            idempotency_key="project-step:%s" % digest,
            project_id=project_id,
            step_id=step_id,
            step_ordinal=int(step.ordinal),
            objective=objective,
            success_criteria=criteria,
            context_refs=refs,
            capability_allowlist=tuple(capabilities),
            risk_class=risk_class,
            recipient_scope=str(authority["recipient_scope"]),
            max_runtime_seconds=runtime,
            max_attempts=2,
            issued_at=str(authority["issued_at"]),
            deadline=str(authority["deadline"]),
            action_hint=action_hint,
            work_order_digest=digest,
            source=project_source,
        )

    @property
    def effect_class(self) -> str:
        """Normalize authority risk into the effect class used by receipts."""

        return self.risk_class if self.risk_class in (
            "read_only", "mutation", "disclosure"
        ) else "none"

    def authority_payload(self) -> Dict[str, Any]:
        """Return every field that may change execution authority or budget."""

        return {
            "schema": self.schema,
            "version": self.version,
            "source": self.source,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "step_ordinal": self.step_ordinal,
            "objective": self.objective,
            "success_criteria": list(self.success_criteria),
            "context_refs": list(self.context_refs),
            "capability_allowlist": list(self.capability_allowlist),
            "risk_class": self.risk_class,
            "recipient_scope": self.recipient_scope,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_attempts": self.max_attempts,
            "issued_at": self.issued_at,
            "deadline": self.deadline,
            "action_hint": self.action_hint,
        }

    @classmethod
    def authority_digest_from_payload(cls, payload: Mapping[str, Any]) -> str:
        fields = (
            "schema", "version", "source", "project_id", "step_id",
            "step_ordinal", "objective", "success_criteria", "context_refs",
            "capability_allowlist", "risk_class", "recipient_scope",
            "max_runtime_seconds", "max_attempts", "issued_at", "deadline",
            "action_hint",
        )
        try:
            authority = {name: payload[name] for name in fields}
        except KeyError as exc:
            raise WorkOrderError(f"missing authority field: {exc.args[0]}") from exc
        return hashlib.sha256(_canonical(authority).encode("utf-8")).hexdigest()

    def validate(self) -> None:
        if self.schema != "WorkOrderV1" or self.version != WORK_ORDER_VERSION:
            raise WorkOrderError("WorkOrder schema/version is invalid")
        _bounded_text(self.project_id, "project_id", 128)
        _bounded_text(self.step_id, "step_id", 128)
        _bounded_text(self.source, "source", 64)
        _bounded_text(self.objective, "objective", 4000)
        _bounded_text(
            self.recipient_scope, "recipient_scope", VIEWER_SCOPE_MAX_CHARS,
        )
        if self.step_ordinal < 0:
            raise WorkOrderError("step_ordinal must be non-negative")
        if not self.success_criteria or len(self.success_criteria) > 32:
            raise WorkOrderError("success_criteria must contain 1..32 entries")
        if len(self.context_refs) > 128:
            raise WorkOrderError("context_refs exceeds 128 entries")
        if self.max_runtime_seconds <= 0 or self.max_runtime_seconds > 86400:
            raise WorkOrderError("max_runtime_seconds is outside 1..86400")
        if self.max_attempts <= 0 or self.max_attempts > 10:
            raise WorkOrderError("max_attempts is outside 1..10")
        if not re.fullmatch(r"[0-9a-f]{64}", self.work_order_digest):
            raise WorkOrderError("WorkOrder digest is invalid")
        matching = [
            authority for authority in _ACTION_KIND.values()
            if authority[0] == self.action_hint
        ]
        if len(matching) != 1:
            raise WorkOrderError("WorkOrder action hint is unsupported")
        _hint, expected_capabilities, expected_risk = matching[0]
        if tuple(self.capability_allowlist) != tuple(expected_capabilities):
            raise WorkOrderError(
                "WorkOrder capability allowlist does not match action authority"
            )
        if self.risk_class != expected_risk:
            raise WorkOrderError(
                "WorkOrder risk class does not match action authority"
            )
        for name, values, maximum in (
            ("success_criteria", self.success_criteria, 1000),
            ("context_refs", self.context_refs, 256),
            ("capability_allowlist", self.capability_allowlist, 128),
        ):
            for value in values:
                _bounded_text(value, name, maximum)
        try:
            issued = datetime.fromisoformat(self.issued_at.replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(self.deadline.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkOrderError("WorkOrder timestamps must be ISO-8601") from exc
        if issued.tzinfo is None or deadline.tzinfo is None or deadline <= issued:
            raise WorkOrderError("WorkOrder deadline must follow issued_at")
        digest = self.authority_digest_from_payload(self.authority_payload())
        if digest != self.work_order_digest:
            raise WorkOrderError("WorkOrder authority digest mismatch")
        if self.work_order_id != "work-%s" % digest[:24]:
            raise WorkOrderError("WorkOrder ID does not match authority digest")
        if self.idempotency_key != "project-step:%s" % digest:
            raise WorkOrderError("WorkOrder idempotency key does not match authority digest")

    def legacy_work_order_id(self) -> str:
        """Return the pre-P1 transport ID for migration collision detection.

        It is never authorized for execution by the P1 adapter.  Looking it up
        before posting prevents an in-flight pre-P1 job from being duplicated
        under the stronger digest.
        """

        legacy_identity = {
            "version": self.version,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "step_ordinal": self.step_ordinal,
            "objective": self.objective,
            "context_refs": self.context_refs,
            "action_hint": self.action_hint,
        }
        digest = hashlib.sha256(_canonical(legacy_identity).encode("utf-8")).hexdigest()
        return "work-%s" % digest[:24]

    def validate_payload(self, payload: Mapping[str, Any]) -> None:
        digest = self.authority_digest_from_payload(payload)
        if digest != self.work_order_digest:
            raise WorkOrderError("queued WorkOrder authority digest mismatch")
        if str(payload.get("work_order_digest") or "") != digest:
            raise WorkOrderError("queued WorkOrder digest field mismatch")
        if str(payload.get("work_order_id") or "") != self.work_order_id:
            raise WorkOrderError("queued WorkOrder ID mismatch")
        if int(payload.get("version") or 0) != self.version:
            raise WorkOrderError("queued WorkOrder version is stale")

    def payload(self) -> Dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["success_criteria"] = list(self.success_criteria)
        result["context_refs"] = list(self.context_refs)
        result["capability_allowlist"] = list(self.capability_allowlist)
        # Compatibility fields used by the current host action adapter. They
        # repeat bounded public metadata, never credentials or copied context.
        result["description"] = self.objective
        result["context"] = {
            "WORK_ORDER_ID": self.work_order_id,
            "PROJECT_ID": self.project_id,
            "STEP_ID": self.step_id,
        }
        return result


class QueueWorkOrderAdapter:
    """Idempotently post/poll WorkOrderV1 jobs on Colony's durable queue."""

    def __init__(
        self,
        task_queue_manager,
        *,
        project_store: Optional[ProjectStore] = None,
        receipt_verifier: Any = None,
        approval_authority: Any = None,
        posted_by: str = "project-engine",
    ) -> None:
        self.manager = task_queue_manager
        # Production passes the durable ProjectEngine store.  The in-memory
        # default keeps direct/library construction backward compatible while
        # preserving the same ledger invariants in tests.
        self.project_store = project_store or ProjectStore()
        self.receipt_verifier = receipt_verifier
        self.approval_authority = approval_authority
        self.posted_by = posted_by

    def _approval_store(self):
        if self.approval_authority is not None:
            return self.approval_authority
        from colony_sidecar.initiatives.approval_authority import (
            ApprovalAuthorityStore,
        )

        self.approval_authority = ApprovalAuthorityStore()
        return self.approval_authority

    async def _prepare_effect_authority(
        self,
        queue,
        job: Job,
        order: WorkOrderV1,
    ) -> Job:
        """Materialize or consume canonical authority while the job is blocked.

        The queue row is born blocked, so a bounded grant can be consumed
        without a claim race. Every retry reuses the exact grant-use or request
        record and repairs an interrupted cross-database status transition.
        """

        from colony_sidecar.initiatives.approval_authority import (
            prepare_action_approval,
        )

        authority = prepare_action_approval(
            self._approval_store(),
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
            deadline=job.deadline or order.deadline,
            approval_started_at=job.posted_at,
        )
        state = authority["state"]
        tags = authority["tags"]
        target = None
        reason = "approval_request_materialized"
        if state in {"authorized_grant", "authorized_direct"}:
            target = JobStatus.QUEUED
            reason = (
                "bounded_grant"
                if state == "authorized_grant"
                else "approval_decision_reconciled"
            )
        elif state == "rejected":
            target = JobStatus.CANCELLED
            reason = "approval_rejection_reconciled"
        elif state in {"expired", "superseded"}:
            target = JobStatus.FAILED
            reason = "canonical_approval_%s" % state

        if target is None:
            await queue.merge_job_tags(job.job_id, tags)
        else:
            changed = await queue.update_job_status(
                job.job_id,
                target,
                reason=reason,
                tags=tags,
                remove_tags=(
                    "hold_kind", "blocked_reason", "awaiting_owner_approval",
                ),
            )
            if not changed:
                raise WorkOrderError(
                    "canonical approval resolved but queue transition must reconcile"
                )
        refreshed = await queue.get_job(job.job_id)
        return refreshed or job

    async def execute(self, project, step, *, context_refs: Iterable[str] = ()) -> Tuple[Optional[bool], str]:
        if not step.work_order_issued_at:
            step.work_order_issued_at = datetime.now(timezone.utc).timestamp()
        issued_at = datetime.fromtimestamp(step.work_order_issued_at, tz=timezone.utc)
        order = WorkOrderV1.for_project_step(
            project, step, context_refs=context_refs, now=issued_at
        )
        try:
            if step.work_order_digest and step.work_order_digest != order.work_order_digest:
                raise WorkOrderError(
                    "step authority changed after WorkOrder issue; explicit reissue required"
                )
            # Atomically insert missing parents or validate existing ones.
            # Never let a stale polling object upsert a terminal lifecycle
            # back to active before posting new work.
            work_order_ref = self.project_store.prepare_work_order(
                project, step, order,
            )
            step.work_order_ref = work_order_ref
            step.work_order_digest = order.work_order_digest
        except (ValueError, WorkOrderError) as exc:
            return False, f"work order rejected: {exc}"

        queue = self.manager.queue
        job = await queue.get_job(order.work_order_id)
        if job is None:
            legacy_id = order.legacy_work_order_id()
            legacy_job = await queue.get_job(legacy_id)
            if legacy_job is not None:
                return None, (
                    f"work_order:{legacy_id}:legacy_migration_hold; "
                    "cancel or reconcile it before canonical reissue"
                )
            job = Job(
                job_id=order.work_order_id,
                job_type=JobType.AGENT_ACTION,
                payload=order.payload(),
                priority=(JobPriority.HIGH if project.source == "owner" else JobPriority.NORMAL),
                deadline=datetime.fromisoformat(order.deadline),
                max_retries=max(0, order.max_attempts - 1),
                timeout_secs=float(order.max_runtime_seconds),
                posted_by=self.posted_by,
                capabilities=[
                    JobCapabilityRequirement(name=capability)
                    for capability in (
                        WORK_ORDER_EXECUTOR_CAPABILITY,
                        ACTION_PLANE_ROUTE,
                        *order.capability_allowlist,
                    )
                ],
                tags={
                    "schema": order.schema,
                    "project_id": order.project_id,
                    "step_id": order.step_id,
                    "risk_class": order.risk_class,
                    "idempotency_key": order.idempotency_key,
                    "work_order_digest": order.work_order_digest,
                    "work_order_version": str(order.version),
                    "executor_protocol": WORK_ORDER_EXECUTOR_CAPABILITY,
                },
            )
            if order.effect_class in EXTERNAL_EFFECT_CLASSES:
                job.status = JobStatus.BLOCKED
                job.tags.update({
                    "hold_kind": "approval",
                    "blocked_reason": "awaiting_owner_approval",
                    "awaiting_owner_approval": "true",
                    "approval_requested_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                })
            await queue.post(job)
            if (
                order.effect_class in EXTERNAL_EFFECT_CLASSES
                and job.status is JobStatus.BLOCKED
                and job.tags.get("blocked_reason") == "awaiting_owner_approval"
                and not job.tags.get("approval_request_id")
            ):
                job = await self._prepare_effect_authority(
                    queue, job, order,
                )
            return None, (
                f"work_order:{order.work_order_id}:"
                f"{job.status.value}"
            )

        if (
            order.effect_class in EXTERNAL_EFFECT_CLASSES
            and job.status is JobStatus.BLOCKED
            and job.tags.get("blocked_reason") == "awaiting_owner_approval"
        ):
            job = await self._prepare_effect_authority(queue, job, order)

        try:
            order.validate_payload(job.payload)
        except WorkOrderError as exc:
            return False, f"work order rejected: {exc}"

        reconciliation = str(
            job.tags.get("effect_reconciliation_finding") or ""
        ).lower()
        if reconciliation == "applied":
            return None, (
                f"work_order:{order.work_order_id}:"
                "independently_verified_applied_no_retry"
            )
        if reconciliation == "not_applied":
            return None, (
                f"work_order:{order.work_order_id}:"
                "independently_verified_not_applied_retry_available"
            )

        if job.status in (
            JobStatus.QUEUED,
            JobStatus.CLAIMED,
            JobStatus.RUNNING,
            JobStatus.BLOCKED,
            JobStatus.ABANDONED,
        ):
            return None, f"work_order:{order.work_order_id}:{job.status.value}"
        if job.status not in {JobStatus.COMPLETED, JobStatus.NEUTRAL}:
            if job.status not in (JobStatus.CANCELLED, JobStatus.FAILED):
                return False, f"work order {order.work_order_id} has unknown state {job.status.value}"

        return await self._project_terminal_result(order, job, step=step)

    async def reconcile_terminal_results(
        self, *, limit: int = 25,
    ) -> Dict[str, Any]:
        """Project already-terminal queue truth without pursuing Projects.

        This path never posts, claims, retries, or authorizes work.  It only
        polls immutable WorkOrders that already exist in the Project ledger
        and materializes a terminal queue result through the same validation,
        verification, and idempotent storage path used by ``execute``.
        """

        report: Dict[str, Any] = {
            "checked": 0,
            "terminal": 0,
            "projected": 0,
            "errors": 0,
        }
        queue = self.manager.queue
        rows = self.project_store.work_orders_awaiting_reconciliation(
            limit=limit,
        )
        terminal_states = {
            JobStatus.COMPLETED,
            JobStatus.NEUTRAL,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
        for row in rows:
            report["checked"] += 1
            work_order_id = str(row.get("work_order_id") or "")
            try:
                order = WorkOrderV1.from_payload(row["payload"])
                if (
                    order.work_order_id != work_order_id
                    or order.work_order_digest != row["work_order_digest"]
                    or order.project_id != row["project_id"]
                    or order.step_id != row["step_id"]
                ):
                    raise WorkOrderError(
                        "persisted WorkOrder ledger identity mismatch"
                    )
                job = await queue.get_job(order.work_order_id)
                if job is None or job.status not in terminal_states:
                    continue
                report["terminal"] += 1
                order.validate_payload(job.payload)
                attempts_before = len(
                    self.project_store.execution_attempts_for(
                        order.work_order_id,
                    )
                )
                await self._project_terminal_result(order, job)
                attempts_after = len(
                    self.project_store.execution_attempts_for(
                        order.work_order_id,
                    )
                )
                if attempts_after > attempts_before:
                    report["projected"] += attempts_after - attempts_before
            except Exception as exc:
                report["errors"] += 1
                logger.warning(
                    "terminal WorkOrder reconciliation failed for %s: %s",
                    work_order_id[:64], exc,
                )
        return report

    async def _project_terminal_result(
        self, order: WorkOrderV1, job: Job, *, step: Any = None,
    ) -> Tuple[Optional[bool], str]:
        """Validate and persist one terminal queue outcome idempotently."""

        queue = self.manager.queue
        reconciliation = str(
            job.tags.get("effect_reconciliation_finding") or ""
        ).lower()
        if reconciliation == "applied":
            return None, (
                f"work_order:{order.work_order_id}:"
                "independently_verified_applied_no_retry"
            )
        if reconciliation == "not_applied":
            return None, (
                f"work_order:{order.work_order_id}:"
                "independently_verified_not_applied_retry_available"
            )

        # A started mutation/disclosure that stopped without independent
        # not-applied evidence is neither success nor failure.  Keep the
        # durable queue attempt/audit as reconciliation evidence, but never
        # turn ambiguity into a false failed ExecutionResult or a replan that
        # could duplicate the external effect.
        if (
            job.status is JobStatus.NEUTRAL
            and str(job.tags.get("ambiguous_prior_effects") or "").lower()
            == "true"
            and str(job.tags.get("verification_pending") or "").lower()
            == "true"
        ):
            return None, (
                f"work_order:{order.work_order_id}:"
                "awaiting_independent_effect_reconciliation"
            )

        try:
            result = ExecutionResultV1.from_job(order, job)
            result = await self._verify(order, result, job)
            if (
                result.terminal_outcome == "succeeded"
                and result.verification_result == "verified"
            ):
                attested = await queue.attest_job_success(
                    order.work_order_id,
                    report={
                        "status": "verified",
                        "execution_result": result.payload(),
                    },
                    verifier_identity=result.verifier_identity,
                    verifier_type=str(
                        getattr(
                            self.receipt_verifier,
                            "verifier_type",
                            "receipt_verifier",
                        )
                    ),
                )
                if not attested:
                    return False, (
                        "independent execution result could not attest queue job"
                    )
            transport_status = job.status.value
            payload = result.payload()
            prior_attempts = self.project_store.execution_attempts_for(
                order.work_order_id,
            )
            exact_replay = any(
                attempt["run_id"] == result.run_id
                and int(attempt["attempt_number"]) == result.attempt_number
                and attempt["transport_status"] == transport_status
                and attempt["result"] == payload
                for attempt in prior_attempts
            )
            if exact_replay:
                # Avoid rewriting Step timestamps/outbox state every autonomy
                # tick while retaining strict collision checks below for a
                # reused run/attempt with different content.
                result_ref = result.result_ref
            else:
                result_ref = self.project_store.save_execution_result(
                    order,
                    result,
                    transport_status=transport_status,
                )
            if step is not None:
                # Preserve the existing execute() contract for its caller's
                # in-memory Step; the store already bound the durable row.
                step.result_ref = result_ref
        except (ExecutionResultError, ValueError) as exc:
            return False, f"execution result rejected: {exc}"

        if result.terminal_outcome in ("skipped", "cancelled"):
            reason = result.error or result.summary or result.terminal_outcome
            return True, f"SKIPPED: {reason[:1200]}"
        if result.terminal_outcome == "failed":
            error = result.error or result.summary or "worker failed"
            return False, f"work order {order.work_order_id} failed: {error[:1200]}"
        if result.verification_result != "verified":
            return None, (
                f"work order {order.work_order_id} completed without verified evidence "
                f"(result_ref={result.result_ref})"
            )
        summary = result.summary or "receipt-backed completion"
        return True, f"work order {order.work_order_id} completed: {summary[:1200]}"

    async def _verify(self, order: WorkOrderV1, result: ExecutionResultV1, job: Job) -> ExecutionResultV1:
        if result.terminal_outcome != "succeeded":
            return result.with_verification("not_applicable")

        if self.receipt_verifier is None:
            return result.with_verification(
                "unverified",
                error=result.error or (
                    "completion has no independent evidence verifier"
                ),
            )
        verify = getattr(self.receipt_verifier, "verify", self.receipt_verifier)
        decision = verify(
            order=order,
            result=result,
            job=job,
            output=dict(job.result.output or {}) if job.result else {},
        )
        if inspect.isawaitable(decision):
            decision = await decision
        checked = ReceiptVerificationV1.from_value(decision)
        if (
            checked.verified
            and checked.verifier_identity == result.executor_identity
        ):
            return result.with_verification(
                "unverified",
                receipt_refs=checked.receipt_refs or result.receipt_refs,
                verifier_identity=checked.verifier_identity,
                error="executor cannot independently verify its own result",
            )
        if not checked.verified:
            return result.with_verification(
                "unverified",
                receipt_refs=checked.receipt_refs or result.receipt_refs,
                verifier_identity=checked.verifier_identity,
                error=checked.detail or result.error or "external receipt verification failed",
            )
        return result.with_verification(
            "verified",
            receipt_refs=checked.receipt_refs,
            verifier_identity=checked.verifier_identity,
        )

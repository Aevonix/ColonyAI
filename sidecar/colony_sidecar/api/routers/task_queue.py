"""Task Queue API — ``/v1/host/queue`` endpoints for distributed job scheduling.

Exposes the TaskQueueManager / QueueManager surface to external workers
(including the host agent's cron-driven worker).
"""

from __future__ import annotations

import logging
import math
import os
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from colony_sidecar.api.authority import (
    WorkerGrant,
    request_authority,
    worker_authority_mode,
)
from colony_sidecar.initiatives.approval_authority import (
    AUTHORIZATION_PROJECTION_SCHEMA,
    ApprovalAuthorityError,
    ApprovalAuthorityStore,
    DEFAULT_GRANT_MAX_USES,
    DEFAULT_GRANT_TTL_SECONDS,
    authority_mode,
    approval_binding_digest,
    approval_presentation_digest,
    build_action_binding,
    build_approval_presentation,
)
from colony_sidecar.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobStatus,
    JobType,
    WorkerCapabilities,
    is_canonical_job_id,
)
from colony_sidecar.task_queue.queue_manager import (
    QueueExecutionUnavailable,
    TaskQueueManager,
)
from colony_sidecar.util.session_safety import (
    load_last_user_message_at,
    save_last_user_message_at,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/host/queue", tags=["task_queue"])

_RESERVED_JOB_TAGS = frozenset({
    "approved_by", "approved_at", "auto_approved_by_policy",
    "action_digest", "bounded_grant_id", "rejected_by", "rejected_at",
    "rejected_reason", "hold_kind", "blocked_reason",
    "agent_action_route", "agent_action_route_node",
    "thought_route", "thought_route_node",
    "outbound_target",
    "action_result_contract", "operational_completion_only",
    "verification_pending", "worker_completion_terminalized",
})
_RESERVED_JOB_TAG_PREFIXES = (
    "approval_", "governor_", "worker_authority_", "success_",
)


def _reserved_job_tags(tags: Optional[Dict[str, str]]) -> List[str]:
    return sorted(
        key for key in (tags or {})
        if key in _RESERVED_JOB_TAGS
        or any(key.startswith(prefix) for prefix in _RESERVED_JOB_TAG_PREFIXES)
    )

class WorkerRegisterRequest(BaseModel):
    node_id: str
    capabilities: Optional[List[str]] = None
    capacity: Optional[Dict[str, float]] = None
    max_concurrent: Optional[int] = Field(None, ge=1, le=1024)
    job_types: Optional[List[str]] = None
    available: bool = True
    load: float = Field(0.0, ge=0.0, le=1.0)


class WorkerHeartbeatRequest(BaseModel):
    job_ids: List[str] = []
    progress: Optional[Dict[str, float]] = None
    claim_attempt_ids: Dict[str, str] = {}
    load: Optional[float] = None

    @model_validator(mode="after")
    def exact_attempts_for_jobs(self):
        job_ids = set(self.job_ids)
        attempt_ids = set(self.claim_attempt_ids)
        if job_ids != attempt_ids or any(
            not str(value).strip()
            for value in self.claim_attempt_ids.values()
        ):
            raise ValueError(
                "claim_attempt_ids must contain one non-empty exact attempt "
                "for every job_id and no other entries"
            )
        return self


class JobPostRequest(BaseModel):
    job_type: str = "agent_action"
    payload: Dict[str, Any] = {}
    priority: str = "normal"
    capabilities: Optional[List[Dict[str, Any]]] = None
    deadline: Optional[str] = None
    max_retries: int = 3
    timeout_secs: float = 3600.0
    depends_on: List[str] = []
    tags: Optional[Dict[str, str]] = None


class JobClaimRequest(BaseModel):
    node_id: str
    capabilities: Optional[List[str]] = None
    capacity: Optional[Dict[str, float]] = None
    max_concurrent: Optional[int] = Field(None, ge=1, le=1024)
    job_types: Optional[List[str]] = None


class JobCompleteRequest(BaseModel):
    output: Dict[str, Any] = {}
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    # Deprecated compatibility telemetry. Queue timing is always derived from
    # the durable server claim/start ledger and this value is ignored.
    started_at: Optional[str] = None


class ActionSuccessAttestationRequest(BaseModel):
    """Independent evidence for one exact generic Action Plane attempt."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema")
    version: int
    job_id: str = Field(min_length=1, max_length=128)
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_class: str
    terminal_outcome: str
    receipt_refs: List[Any] = Field(min_length=1, max_length=32)
    observed_at: str
    summary: str = Field("", max_length=500)

    @model_validator(mode="after")
    def exact_action_receipt(self):
        from colony_sidecar.task_queue.action_receipts import (
            ActionReceiptAttestationV1,
        )

        ActionReceiptAttestationV1.from_payload(
            self.model_dump(by_alias=True)
        )
        return self


class JobFailRequest(BaseModel):
    error: str
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    # Deprecated and non-authoritative; retained so older workers still parse.
    started_at: Optional[str] = None


class JobHeartbeatRequest(BaseModel):
    progress: Optional[float] = None
    log_lines: Optional[List[str]] = None
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class JobStartRequest(BaseModel):
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class JobReleaseRequest(BaseModel):
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class WorkControlOperationRequest(BaseModel):
    """Strict caller-authored CAS command for one durable work target."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema", pattern=r"^WorkControlOperationV1$")
    version: int = Field(ge=1, le=1)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$",
    )
    operation: str
    target_id: str = Field(min_length=1, max_length=192)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: Optional[str] = Field(None, max_length=128)
    expected_revision: int = Field(ge=1)
    expected_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: Dict[str, Any] = {}
    reason: str = Field("", max_length=500)


class WorkControlAckRequest(BaseModel):
    """Exact claimant acknowledgement; identity never comes from this body."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema", pattern=r"^WorkControlAckV1$")
    version: int = Field(ge=1, le=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: str
    details: Dict[str, Any] = {}


class WorkControlAckEnvelopeRequest(WorkControlAckRequest):
    """Slash-safe worker acknowledgement representation."""

    node_id: str = Field(min_length=1, max_length=192)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )


class WorkControlWorkerOutcomeRequest(BaseModel):
    """Durable slash-safe steer outcome recorded before acknowledgement."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(
        alias="schema", pattern=r"^WorkControlWorkerOutcomeV1$",
    )
    version: int = Field(ge=1, le=1)
    node_id: str = Field(min_length=1, max_length=192)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: str
    details: Dict[str, Any] = {}


class WorkEffectReconciliationRequest(BaseModel):
    """Independent exact-attempt applied/not-applied evidence."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(
        alias="schema", pattern=r"^WorkEffectReconciliationV1$",
    )
    version: int = Field(ge=1, le=1)
    reconciliation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )
    target_id: str = Field(min_length=1, max_length=192)
    attempt_id: str = Field(min_length=1, max_length=128)
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding: str
    evidence_refs: List[str] = Field(min_length=1, max_length=32)
    observed_at: str
    summary: str = Field("", max_length=1000)


class BoundedGrantRequest(BaseModel):
    """An exact-scope, time/use-bounded replacement for ``always``."""

    expires_in_seconds: int = Field(
        DEFAULT_GRANT_TTL_SECONDS, ge=60, le=30 * 24 * 60 * 60,
    )
    max_uses: int = Field(DEFAULT_GRANT_MAX_USES, ge=1, le=100)
    # Optional only so clients can echo the scope they displayed. The server
    # rejects any difference; omitting it means "the exact displayed scope".
    exact_scope: Optional[Dict[str, Any]] = None


class JobApproveRequest(BaseModel):
    # Deprecated compatibility input. It is intentionally ignored: authority
    # comes from the authenticated request principal, never caller prose.
    approved_by: Optional[str] = None
    # Deprecated spelling. In shadow migration mode it maps to a bounded grant
    # with safe defaults; it never creates a permanent standing approval.
    always: bool = False
    approval_request_id: Optional[str] = None
    expected_action_digest: Optional[str] = None
    decision_id: Optional[str] = None
    grant: Optional[BoundedGrantRequest] = None


class JobRejectRequest(BaseModel):
    # Deprecated and ignored for authority; retained so old clients parse.
    rejected_by: Optional[str] = None
    reason: str = "rejected_by_owner"
    approval_request_id: Optional[str] = None
    expected_action_digest: Optional[str] = None
    decision_id: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    decision: str
    decision_id: str
    expected_action_digest: str
    grant: Optional[BoundedGrantRequest] = None


class ApprovalRelayCanaryRequest(BaseModel):
    """The only caller-controlled canary value is an opaque retry token."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema", pattern=r"^ApprovalRelayCanaryV1$")
    version: int = Field(ge=1, le=1)
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:@/+-]+$",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_queue() -> TaskQueueManager:
    try:
        return TaskQueueManager.get_instance()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Task queue not initialized")


def _work_control_http_error(exc: Exception) -> HTTPException:
    from colony_sidecar.task_queue.work_control import WorkControlError

    if isinstance(exc, WorkControlError):
        return HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        )
    raise exc


@router.get("/contract")
async def queue_contract(request: Request = None) -> Dict[str, Any]:
    """Return the authenticated, deploy-pinned worker protocol identity."""

    from colony_sidecar.task_queue.contract import (
        QueueContractIdentityError,
        queue_contract_identity,
    )

    try:
        contract = queue_contract_identity()
    except QueueContractIdentityError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "queue_contract_identity_unavailable",
            "message": str(exc),
        }) from exc
    try:
        runtime = _get_queue().queue.execution_readiness()
    except HTTPException:
        contract["runtime_readiness"] = {
            "queue_initialized": False,
            "thought": {
                "ready": False,
                "node_id": None,
                "reason": "queue_not_initialized",
            },
        }
    else:
        contract["runtime_readiness"] = {
            "queue_initialized": True,
            "queue_execution_ready": runtime["ready"],
            "queue_execution_reason": runtime["reason"],
            "thought": runtime["typed_routes"]["thought"],
        }
    return contract


def _governor() -> Any:
    """The server-side WorkerGovernor (item 5), or None if not wired."""
    try:
        from colony_sidecar.api.routers.host import _worker_governor
        return _worker_governor
    except Exception:
        return None


def _worker_authority_error(code: str, message: str, *, status: int = 403):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _worker_request_context(
    request: Optional[Request],
    *,
    node_id: str,
    required_scope: str,
    claimant: bool = False,
) -> Dict[str, Any]:
    """Resolve worker authority from transport authentication, never a body claim."""

    mode = worker_authority_mode()
    if mode == "invalid":
        raise _worker_authority_error(
            "worker_authority_configuration_invalid",
            "COLONY_WORKER_AUTHORITY_MODE must be shadow or enforce",
            status=503,
        )
    if request is None:
        # Embedded/in-process queue integrations are a separate trusted lane.
        return {
            "mode": "internal",
            "principal": "trusted-internal",
            "credential": "in_process",
            "grant": None,
            "would_deny": False,
        }

    authority = request_authority(request)
    grant = next(
        (item for item in authority.worker_grants if item.node_id == node_id),
        None,
    )
    scoped = (
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope(required_scope)
        and grant is not None
    )
    if mode == "enforce" and not scoped:
        if not authority.authenticated or authority.legacy or authority.anonymous:
            raise _worker_authority_error(
                "scoped_worker_authority_required",
                "a scoped authenticated worker principal is required",
            )
        if not authority.has_scope(required_scope):
            raise _worker_authority_error(
                "worker_scope_required",
                f"worker principal requires exact scope {required_scope}",
            )
        raise _worker_authority_error(
            "worker_claimant_mismatch" if claimant else "worker_node_not_granted",
            (
                "authenticated worker principal does not own this job claim"
                if claimant
                else "authenticated worker principal is not granted this node_id"
            ),
        )
    return {
        "mode": mode,
        "principal": authority.principal_id,
        "credential": authority.credential_id or "none",
        "grant": grant,
        "would_deny": not scoped,
    }


def _worker_tags(context: Dict[str, Any]) -> Dict[str, str]:
    return {
        "worker_authority_mode": str(context["mode"]),
        "worker_authority_principal": str(context["principal"]),
        "worker_authority_credential": str(context["credential"]),
        "worker_authority_would_deny": (
            "true" if context.get("would_deny") else "false"
        ),
    }


def _job_type_set(values: List[str], *, field: str) -> set[JobType]:
    result: set[JobType] = set()
    for value in values:
        try:
            result.add(JobType(value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_job_type", "field": field, "value": value},
            ) from exc
    return result


def _body_exceeds_worker_grant(
    body: WorkerRegisterRequest | JobClaimRequest,
    grant: WorkerGrant,
) -> bool:
    grant_capacity = grant.capacity_map()
    if (
        body.capabilities is not None
        and not set(body.capabilities).issubset(grant.capabilities)
    ):
        return True
    if body.job_types is not None:
        requested_types = set(body.job_types)
        if not requested_types or not requested_types.issubset(grant.job_types):
            return True
    if body.max_concurrent is not None and body.max_concurrent > grant.max_concurrent:
        return True
    if body.capacity is not None:
        for key, value in body.capacity.items():
            amount = float(value)
            if (
                not math.isfinite(amount)
                or amount < 0
                or key not in grant_capacity
                or amount > grant_capacity.get(key, -1.0)
            ):
                return True
    return False


def _bounded_worker_capabilities(
    body: WorkerRegisterRequest | JobClaimRequest,
    context: Dict[str, Any],
) -> WorkerCapabilities:
    """Build effective caps; enforce bodies may omit or narrow keyring ceilings."""

    grant: WorkerGrant | None = context.get("grant")
    if context["mode"] != "enforce":
        exceeds = grant is not None and _body_exceeds_worker_grant(body, grant)
        if exceeds:
            context["would_deny"] = True
        # A correctly scoped shadow consumer should exercise the exact
        # enforcement shape: omissions derive from its server grant. Legacy,
        # unprovisioned, and intentionally over-broad shadow traffic retains
        # historical body behavior so migration cannot silently break it.
        use_grant = grant is not None and not exceeds and not context.get("would_deny")
        return WorkerCapabilities(
            node_id=body.node_id,
            capabilities=(
                set(grant.capabilities)
                if use_grant and body.capabilities is None
                else set(body.capabilities or [])
            ),
            capacity=(
                grant.capacity_map()
                if use_grant and body.capacity is None
                else body.capacity or {}
            ),
            max_concurrent=(
                grant.max_concurrent
                if use_grant and body.max_concurrent is None
                else body.max_concurrent or 4
            ),
            job_types=_job_type_set(
                sorted(grant.job_types)
                if use_grant and body.job_types is None
                else body.job_types or [],
                field="job_types",
            ),
            available=bool(getattr(body, "available", True)),
            load=float(getattr(body, "load", 0.0)),
        )
    assert grant is not None

    capabilities = (
        set(grant.capabilities)
        if body.capabilities is None
        else set(body.capabilities)
    )
    job_type_names = (
        set(grant.job_types)
        if body.job_types is None
        else set(body.job_types)
    )
    capacity = grant.capacity_map() if body.capacity is None else dict(body.capacity)
    max_concurrent = body.max_concurrent or grant.max_concurrent
    if _body_exceeds_worker_grant(body, grant):
        raise _worker_authority_error(
            "worker_grant_exceeded",
            "worker request may only narrow its server-owned keyring grant",
        )
    return WorkerCapabilities(
        node_id=body.node_id,
        capabilities=capabilities,
        capacity={key: float(value) for key, value in capacity.items()},
        max_concurrent=max_concurrent,
        job_types=_job_type_set(sorted(job_type_names), field="job_types"),
        available=bool(getattr(body, "available", True)),
        load=float(getattr(body, "load", 0.0)),
    )


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _approval_store() -> ApprovalAuthorityStore:
    return ApprovalAuthorityStore()


def _approval_error(exc: ApprovalAuthorityError) -> HTTPException:
    status = 404 if exc.code == "request_not_found" else 409
    if exc.code in {"authority_required", "approval_scope_required"}:
        status = 403
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


def _decision_authority(request: Optional[Request]) -> tuple[str, str, str]:
    """Return server-derived actor/evidence/mode for an approval decision."""

    mode = authority_mode()
    if mode == "invalid":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "approval_authority_mode_invalid",
                "message": (
                    "COLONY_APPROVAL_AUTHORITY_MODE must be shadow or enforce"
                ),
            },
        )
    if request is None:
        # Direct in-process calls are an explicit trusted integration surface,
        # not an HTTP body claim. They remain for embedded Colony deployments.
        return "trusted-internal", "in_process", mode

    authority = request_authority(request)
    allowed = (
        authority.authenticated
        and not authority.legacy
        and authority.has_scope("api:access")
        and authority.has_scope("approvals:decide")
    )
    if mode == "enforce" and not allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "approval_scope_required",
                "message": "a scoped authenticated approvals:decide principal is required",
            },
        )

    actor = authority.principal_id
    credential = authority.credential_id or "none"
    if allowed:
        evidence = f"scoped_principal:{actor}:{credential}"
    else:
        # Shadow mode preserves legacy bearer/dev traffic during consumer
        # migration, but records that it would fail enforcement. The body's
        # approved_by/rejected_by value still has no effect.
        evidence = f"shadow_compat:{actor}:{credential}"
    return actor, evidence, mode


def _approval_relay_canary_authority(request: Request) -> str:
    """Require the dedicated scoped bridge even while migration is shadow."""

    authority = request_authority(request)
    allowed = bool(
        authority.authenticated
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("api:access")
        and authority.has_scope("approvals:decide")
    )
    if not allowed:
        raise HTTPException(status_code=403, detail={
            "code": "approval_relay_canary_scope_required",
            "message": (
                "a scoped authenticated api:access + approvals:decide "
                "principal is required"
            ),
        })
    return authority.principal_id


def _job_binding(job: Job):
    return build_action_binding(
        job_id=job.job_id,
        job_type=job.job_type.value,
        payload=job.payload,
    )


async def _ensure_job_approval_request(
    queue: TaskQueueManager,
    job: Job,
) -> tuple[ApprovalAuthorityStore, Dict[str, Any], Any]:
    binding = _job_binding(job)
    store = _approval_store()
    presentation = build_approval_presentation(
        job_id=job.job_id,
        job_type=job.job_type.value,
        payload=job.payload,
        deadline=job.deadline,
    )
    approval_request = store.ensure_request(
        job_id=job.job_id,
        binding=binding,
        presentation=presentation,
    )
    await queue.queue.merge_job_tags(job.job_id, {
        "approval_request_id": approval_request["request_id"],
        "action_digest": binding.action_digest,
        "approval_scope_digest": binding.scope_digest,
        "approval_binding_digest": approval_request["binding_digest"],
        "approval_request_digest": approval_request["request_digest"],
        "approval_presentation_digest": approval_request[
            "presentation_digest"
        ],
        "approval_expires_at": approval_request["expires_at"],
    })
    return store, approval_request, binding


async def _decide_job(
    *,
    job: Job,
    decision: str,
    decision_id: Optional[str],
    approval_request_id: Optional[str],
    expected_action_digest: Optional[str],
    grant: Optional[BoundedGrantRequest],
    request: Optional[Request],
    rejection_reason: str = "rejected_by_owner",
) -> Dict[str, Any]:
    queue = _get_queue()
    actor, evidence, mode = _decision_authority(request)
    from colony_sidecar.task_queue.approval_relay_canary import (
        is_exact_job as is_exact_approval_relay_canary,
    )

    relay_canary = is_exact_approval_relay_canary(job)
    if relay_canary and grant is not None:
        raise HTTPException(status_code=409, detail={
            "code": "approval_relay_canary_grant_forbidden",
            "message": "the inert approval relay canary cannot create a grant",
        })
    # An exact client retry is a read of the durable winner after the queue
    # transition. This includes approvals that remain BLOCKED solely on a
    # separate dependency; they must not be mistaken for a new approval gate.
    if approval_request_id and expected_action_digest and decision_id:
        store = _approval_store()
        stored = store.get_request(approval_request_id)
        if (
            stored is not None
            and stored.get("job_id") == job.job_id
            and stored.get("status") in {"approved", "rejected"}
        ):
            try:
                replay = store.decide(
                    approval_request_id,
                    decision=decision,
                    decision_id=decision_id,
                    expected_action_digest=expected_action_digest,
                    decided_by=actor,
                    authority_evidence=evidence,
                )
            except ApprovalAuthorityError as exc:
                raise _approval_error(exc) from exc
            if relay_canary and replay["replayed"]:
                # A pre-fix scheduler could have projected an APPROVE winner
                # as QUEUED in the cross-database crash window.  An exact
                # replay succeeds only after the server repairs the canonical
                # canary to its sole valid terminal state.
                repaired = await (
                    queue.queue.repair_approval_relay_canary_terminal(
                        job.job_id
                    )
                )
                terminal = await queue.queue.get_job(job.job_id)
                if (
                    not repaired
                    or terminal is None
                    or terminal.status is not JobStatus.CANCELLED
                ):
                    raise HTTPException(status_code=409, detail={
                        "code": "approval_relay_canary_not_cancelled",
                        "message": (
                            "the durable canary winner has not converged "
                            "to CANCELLED"
                        ),
                    })
                winner = replay["request"]
                return {
                    "success": True,
                    "job_id": terminal.job_id,
                    "status": JobStatus.CANCELLED.value,
                    "decision": decision,
                    "decided_by": winner["decided_by"],
                    "decided_at": winner["decided_at"],
                    "approval_request": winner,
                    "bounded_grant": None,
                    "replayed": True,
                    "authority_mode": mode,
                }
            queue_transition_applied = bool(
                replay["replayed"]
                and (
                    (
                        decision == "approve"
                        and (
                            job.status is not JobStatus.BLOCKED
                            or (
                                job.tags.get("blocked_reason")
                                == "dependencies_pending"
                                and job.tags.get("approval_request_id")
                                == approval_request_id
                                and job.tags.get("approval_decision_id")
                                == decision_id
                            )
                        )
                    )
                    or (
                        decision == "reject"
                        and job.status is JobStatus.CANCELLED
                    )
                )
            )
            if queue_transition_applied:
                winner = replay["request"]
                return {
                    "success": True,
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "decision": decision,
                    "decided_by": winner["decided_by"],
                    "decided_at": winner["decided_at"],
                    "approval_request": winner,
                    "bounded_grant": replay["grant"],
                    "replayed": True,
                    "authority_mode": mode,
                }
    if job.status != JobStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value}, not blocked",
        )
    if job.tags.get("blocked_reason") != "awaiting_owner_approval":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_not_approval_blocked",
                "message": "only owner-approval-blocked jobs can be decided",
            },
        )

    try:
        store, approval_request, binding = await _ensure_job_approval_request(queue, job)
        if approval_request_id and approval_request_id != approval_request["request_id"]:
            raise ApprovalAuthorityError(
                "request_superseded", "approval request is not current for this job"
            )
        if mode == "enforce" and not approval_request_id:
            raise ApprovalAuthorityError(
                "request_id_required", "approval_request_id is required in enforce mode"
            )
        if mode == "enforce" and not expected_action_digest:
            raise ApprovalAuthorityError(
                "action_digest_required", "expected_action_digest is required in enforce mode"
            )
        if mode == "enforce" and not decision_id:
            raise ApprovalAuthorityError(
                "decision_id_required", "decision_id is required in enforce mode"
            )

        normalized_decision_id = decision_id or (
            "compat_" + os.urandom(16).hex()
        )
        effective_grant = grant
        exact_scope = None
        if effective_grant is not None:
            exact_scope = (
                binding.scope
                if effective_grant.exact_scope is None
                else effective_grant.exact_scope
            )

        result = store.decide(
            approval_request["request_id"],
            decision=decision,
            decision_id=normalized_decision_id,
            expected_action_digest=expected_action_digest or binding.action_digest,
            decided_by=actor,
            authority_evidence=evidence,
            grant_scope=exact_scope,
            grant_ttl_seconds=(
                effective_grant.expires_in_seconds
                if effective_grant else DEFAULT_GRANT_TTL_SECONDS
            ),
            grant_max_uses=(
                effective_grant.max_uses if effective_grant else DEFAULT_GRANT_MAX_USES
            ),
        )
    except ApprovalAuthorityError as exc:
        raise _approval_error(exc) from exc

    decided_at = result["request"]["decided_at"]
    tags = {
        "approval_request_id": result["request"]["request_id"],
        "action_digest": result["request"]["action_digest"],
        "approval_decision_id": result["request"]["decision_id"],
        "approval_authority": actor,
        "approval_authority_mode": mode,
    }
    if relay_canary:
        tags.update({
            "approval_relay_canary_decision": decision,
            "approval_relay_canary_terminalized": "true",
            "external_effect": "false",
        })
        if decision == "approve":
            tags.update({"approved_by": actor, "approved_at": decided_at})
        else:
            tags.update({
                "rejected_by": actor,
                "rejected_at": decided_at,
                "rejected_reason": rejection_reason,
            })
        new_status = JobStatus.CANCELLED
        reason = "approval_relay_canary_%s_no_effect" % decision
    elif decision == "approve":
        tags.update({"approved_by": actor, "approved_at": decided_at})
        if result["grant"]:
            tags["bounded_grant_id"] = result["grant"]["grant_id"]
        new_status = JobStatus.QUEUED
        reason = f"approved_by_principal={actor}"
        if job.depends_on:
            dependencies_ready = True
            for dependency_id in job.depends_on:
                dependency = await queue.queue.get_job(dependency_id)
                if (
                    dependency is None
                    or dependency.status is not JobStatus.COMPLETED
                ):
                    dependencies_ready = False
                    break
            if not dependencies_ready:
                # Approval provenance is durable, but it cannot erase a
                # separate dependency gate. unblock_ready_jobs() will queue
                # the job only after every prerequisite independently closes.
                new_status = JobStatus.BLOCKED
                reason = "approved_waiting_for_dependencies"
                tags.update({
                    "hold_kind": "dependency",
                    "blocked_reason": "dependencies_pending",
                })
    else:
        tags.update({
            "rejected_by": actor,
            "rejected_at": decided_at,
            "rejected_reason": rejection_reason,
        })
        new_status = JobStatus.CANCELLED
        reason = rejection_reason
    if relay_canary:
        # Derive and apply the canary terminal state from the durable winner,
        # including an idempotent scheduler race or historical QUEUED row.
        changed = await queue.queue.repair_approval_relay_canary_terminal(
            job.job_id
        )
    else:
        changed = await queue.queue.update_job_status(
            job.job_id,
            new_status,
            reason=reason,
            tags=tags,
            remove_tags=[
                "hold_kind", "blocked_reason", "awaiting_owner_approval",
                "governor_error", "governor_last_recheck_at",
            ],
        )
    if not changed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_transition_failed",
                "message": "decision is durable but the job transition must be reconciled",
            },
        )
    return {
        "success": True,
        "job_id": job.job_id,
        "status": new_status.value,
        "decision": decision,
        "decided_by": actor,
        "decided_at": decided_at,
        "approval_request": result["request"],
        "bounded_grant": result["grant"],
        "replayed": result["replayed"],
        "authority_mode": mode,
    }


def _job_to_dict(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "payload": job.payload,
        "priority": job.priority.value,
        "status": job.status.value,
        "capabilities": [
            {
                "name": capability.name,
                "minimum": capability.minimum,
                "preferred": capability.preferred,
            }
            for capability in job.capabilities
        ],
        "claimed_by": job.claimed_by,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "claim_attempt_id": job.claim_attempt_id,
        "claim_expires_at": (
            job.claim_expires_at.isoformat() if job.claim_expires_at else None
        ),
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "max_retries": job.max_retries,
        "retry_count": job.retry_count,
        "timeout_secs": job.timeout_secs,
        "depends_on": job.depends_on,
        "tags": job.tags,
        "result": {
            "worker_node_id": job.result.worker_node_id,
            "status": job.result.status.value,
            "claim_attempt_id": job.result.claim_attempt_id,
            "output": job.result.output,
            "error": job.result.error,
            "started_at": job.result.started_at.isoformat() if job.result and job.result.started_at else None,
            "completed_at": job.result.completed_at.isoformat() if job.result and job.result.completed_at else None,
            "duration_seconds": job.result.duration_seconds if job.result else None,
        } if job.result else None,
    }


def _job_inspection_dict(job: Job) -> Dict[str, Any]:
    """Return authority canary fields without payload/result disclosure."""

    import hashlib
    import json

    route_tag_names = {
        "agent_action_route", "agent_action_route_node",
        "thought_route", "thought_route_node",
        "schema", "risk_class", "idempotency_key",
        "work_order_digest", "work_order_version", "executor_protocol",
        "thought_job_digest", "concern_id", "viewer_scope", "shareability",
        "action_digest", "action_result_contract", "verification_pending",
        "success_attestation_schema", "success_evidence_digest",
        "success_receipt_refs_digest", "success_verifier_identity",
        "success_verifier_type",
    }

    def digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    result_projection = None
    if job.result is not None:
        result_projection = {
            "status": job.result.status.value,
            "claim_attempt_id": job.result.claim_attempt_id,
            "worker_node_id": job.result.worker_node_id,
            "output": job.result.output,
            "error": job.result.error,
        }
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "status": job.status.value,
        "capabilities": [
            {
                "name": capability.name,
                "minimum": capability.minimum,
                "preferred": capability.preferred,
            }
            for capability in job.capabilities
        ],
        "claimed_by": job.claimed_by,
        "claim_attempt_id": job.claim_attempt_id,
        "claim_expires_at": (
            job.claim_expires_at.isoformat() if job.claim_expires_at else None
        ),
        "tags": {
            key: value for key, value in (job.tags or {}).items()
            if key in route_tag_names
        },
        "payload_sha256": digest(job.payload),
        "result_sha256": (
            digest(result_projection) if result_projection is not None else None
        ),
        "result_status": (
            job.result.status.value if job.result is not None else None
        ),
        "result_claim_attempt_id": (
            job.result.claim_attempt_id if job.result is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# WorkControlV1 — generic operator control over durable queue work
# ---------------------------------------------------------------------------

@router.get("/work")
async def get_work_control_target_query(
    target_id: str = Query(min_length=1, max_length=192),
) -> Dict[str, Any]:
    """Slash-safe target inspection; path form remains compatibility-only."""

    return await get_work_control_target(target_id)


@router.post("/work/operations")
async def apply_work_control_operation_body(
    body: WorkControlOperationRequest,
    request: Request,
) -> Dict[str, Any]:
    """Slash-safe mutation using the exact target ID already in the body."""

    return await apply_work_control_operation(body.target_id, body, request)


@router.get("/work/operations/receipt")
async def get_work_control_receipt_query(
    target_id: str = Query(min_length=1, max_length=192),
    operation_id: str = Query(min_length=1, max_length=128),
) -> Dict[str, Any]:
    """Slash-safe receipt lookup for canonical operation identifiers."""

    return await get_work_control_receipt(target_id, operation_id)


@router.post("/work/reconciliations")
async def reconcile_work_effect(
    body: WorkEffectReconciliationRequest,
    request: Request,
) -> Dict[str, Any]:
    """Close one ambiguous effect using an independent scoped verifier."""

    authority = request_authority(request)
    if (
        not authority.authenticated
        or authority.anonymous
        or authority.legacy
        or not authority.has_scope("workers:attest")
    ):
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": "a scoped workers:attest verifier is required",
        })
    queue = _get_queue()
    job = await queue.queue.get_job(body.target_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    executor_node = str(
        job.result.worker_node_id if job.result is not None else ""
    ).strip()
    executor_principal = str(
        (job.tags or {}).get("worker_authority_principal") or ""
    ).strip()
    if (
        authority.principal_id in {executor_node, executor_principal}
        or any(
            grant.node_id == executor_node
            for grant in authority.worker_grants
        )
    ):
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": "the executor cannot reconcile its own effect",
        })
    payload = body.model_dump(by_alias=True)
    try:
        return await queue.queue.reconcile_work_effect(
            reconciliation_id=payload["reconciliation_id"],
            target_id=payload["target_id"],
            attempt_id=payload["attempt_id"],
            authority_digest=payload["authority_digest"],
            finding=payload["finding"],
            evidence_refs=payload["evidence_refs"],
            observed_at=payload["observed_at"],
            summary=payload.get("summary") or "",
            verifier_identity=authority.principal_id,
            verifier_type="scoped_effect_reconciler",
            verifier_authority={
                "authority_kind": "scoped_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "required_scope": "workers:attest",
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/work/{target_id}")
async def get_work_control_target(target_id: str) -> Dict[str, Any]:
    """Read the exact revision/digest and currently allowed operations."""

    try:
        return await _get_queue().queue.work_control.inspect(target_id)
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.post("/work/{target_id}/operations")
async def apply_work_control_operation(
    target_id: str,
    body: WorkControlOperationRequest,
    request: Request,
) -> Dict[str, Any]:
    """CAS-apply an idempotent command from an exact scoped principal."""

    authority = request_authority(request)
    if (
        not authority.authenticated
        or authority.anonymous
        or authority.legacy
        or not authority.has_scope("work:control")
    ):
        raise HTTPException(status_code=403, detail={
            "code": "exact_work_control_principal_required",
            "message": (
                "WorkControl mutations require a scoped work:control principal"
            ),
        })
    payload = body.model_dump(by_alias=True)
    if payload["target_id"] != target_id:
        raise HTTPException(status_code=409, detail={
            "code": "work_target_path_mismatch",
            "message": "body target_id must match the path target",
        })
    try:
        return await _get_queue().queue.work_control.operate(
            operation_id=payload["operation_id"],
            operation=payload["operation"],
            target_id=payload["target_id"],
            run_id=payload["run_id"],
            attempt_id=payload.get("attempt_id"),
            expected_revision=payload["expected_revision"],
            expected_state_digest=payload["expected_state_digest"],
            parameters=payload.get("parameters") or {},
            reason=payload.get("reason") or "",
            requested_by=authority.principal_id,
            request_authority={
                "authority_kind": "scoped_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "required_scope": "work:control",
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/work/{target_id}/operations/{operation_id}")
async def get_work_control_receipt(
    target_id: str,
    operation_id: str,
) -> Dict[str, Any]:
    """Read accepted and immutable outcome receipts for one operation."""

    try:
        return await _get_queue().queue.work_control.receipt(
            target_id, operation_id,
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/workers/{node_id}/controls")
async def pending_worker_controls(
    node_id: str,
    request: Request,
) -> List[Dict[str, Any]]:
    """Deliver pending commands only to their transport-attested worker."""

    context = _worker_request_context(
        request,
        node_id=node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "WorkControl delivery requires an exact scoped worker grant",
        )
    return await _get_queue().queue.work_control.pending_for_worker(node_id)


@router.post("/workers/controls/ack")
async def acknowledge_worker_control_body(
    body: WorkControlAckEnvelopeRequest,
    request: Request,
) -> Dict[str, Any]:
    """Slash-safe worker acknowledgement for canonical operation IDs."""

    return await acknowledge_worker_control(
        body.node_id, body.operation_id, body, request,
    )


@router.post("/workers/controls/outcome")
async def record_worker_control_outcome(
    body: WorkControlWorkerOutcomeRequest,
    request: Request,
) -> Dict[str, Any]:
    """Persist an idempotent steer result before the worker sends its ack."""

    context = _worker_request_context(
        request,
        node_id=body.node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "durable WorkControl outcome requires an exact worker grant",
        )
    try:
        return await _get_queue().queue.record_work_control_worker_outcome(
            worker_id=body.node_id,
            operation_id=body.operation_id,
            attempt_id=body.attempt_id,
            outcome=body.outcome,
            details=body.details,
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.post("/workers/{node_id}/controls/{operation_id}/ack")
async def acknowledge_worker_control(
    node_id: str,
    operation_id: str,
    body: WorkControlAckRequest,
    request: Request,
) -> Dict[str, Any]:
    """Persist the exact claimant's cooperative acknowledgement."""

    context = _worker_request_context(
        request,
        node_id=node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "WorkControl acknowledgement requires an exact scoped worker grant",
        )
    authority = request_authority(request)
    try:
        return await _get_queue().queue.work_control.acknowledge(
            worker_id=node_id,
            operation_id=operation_id,
            attempt_id=body.attempt_id,
            outcome=body.outcome,
            details=body.details,
            ack_authority={
                "authority_kind": "scoped_worker_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "worker_id": node_id,
                "required_scope": "workers:lifecycle",
                "worker_authority_mode": context.get("mode"),
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------

@router.post("/workers/register")
async def register_worker(
    body: WorkerRegisterRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Register a worker node with the scheduler."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=body.node_id, required_scope="workers:register",
    )
    caps = _bounded_worker_capabilities(body, context)
    await queue.queue.register_worker(caps)
    logger.info("Worker registered: %s (types=%s)", body.node_id, body.job_types)
    return {
        "success": True,
        "node_id": body.node_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/workers/{node_id}/heartbeat")
async def worker_heartbeat(
    node_id: str,
    body: WorkerHeartbeatRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Receive a worker heartbeat."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=node_id, required_scope="workers:lifecycle",
    )
    progress = body.progress or {}
    updated = await queue.queue.send_heartbeat(
        worker_id=node_id,
        job_ids=body.job_ids,
        progress=progress,
        claim_attempt_ids=body.claim_attempt_ids,
    )
    if updated != len(set(body.job_ids)):
        raise HTTPException(status_code=409, detail={
            "code": "worker_heartbeat_claim_mismatch",
            "message": "one or more heartbeat jobs are not claimed by this worker",
        })
    if body.load is not None:
        await queue.queue.update_worker_load(node_id, body.load)
    return {
        "success": True,
        "node_id": node_id,
        "jobs_updated": updated,
        "worker_authority": _worker_tags(context),
    }


@router.post("/workers/{node_id}/deregister")
async def deregister_worker(
    node_id: str,
    request: Request = None,
) -> Dict[str, Any]:
    """Remove a worker from the scheduler."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=node_id, required_scope="workers:register",
    )
    await queue.queue.deregister_worker(node_id)
    logger.info("Worker deregistered: %s", node_id)
    return {
        "success": True,
        "node_id": node_id,
        "worker_authority": _worker_tags(context),
    }


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@router.post("/approvals/canary")
async def create_approval_relay_canary(
    body: ApprovalRelayCanaryRequest,
    request: Request,
) -> Dict[str, Any]:
    """Ensure one canonical approval request with no executable outcome."""

    principal = _approval_relay_canary_authority(request)
    from colony_sidecar.task_queue.approval_relay_canary import (
        SCHEMA,
        TERMINAL_POLICY,
        idempotency_digest,
    )

    digest = idempotency_digest(body.idempotency_key)
    queue = _get_queue()
    try:
        job, created = await queue.queue.ensure_approval_relay_canary(digest)
        projection = await get_job_approval_projection(job.job_id)
    except (ApprovalAuthorityError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail={
            "code": "approval_relay_canary_conflict",
            "message": str(exc),
        }) from exc
    canonical_request = projection.get("request") or {}
    if not canonical_request.get("request_id"):
        raise HTTPException(status_code=503, detail={
            "code": "approval_relay_canary_authority_unavailable",
            "message": "canonical approval request was not materialized",
        })
    return {
        "schema": "ApprovalRelayCanaryReceiptV1",
        "version": 1,
        "canary_schema": SCHEMA,
        "created": created,
        "created_by": principal,
        "job_id": job.job_id,
        "job_status": projection["job_status"],
        "external_effect": False,
        "terminal_policy": TERMINAL_POLICY,
        "idempotency_digest": digest,
        "request_id": canonical_request["request_id"],
        "action_digest": projection["action_digest"],
        "scope_digest": projection["scope_digest"],
        "binding_digest": projection["binding_digest"],
        "request_digest": projection["request_digest"],
        "presentation_digest": projection["presentation_digest"],
        "request_status": canonical_request["status"],
        "decision": canonical_request.get("decision"),
        "decision_id": canonical_request.get("decision_id"),
    }


@router.post("/jobs")
async def create_job(body: JobPostRequest) -> Dict[str, Any]:
    """Post a new job to the queue."""
    queue = _get_queue()
    reserved = _reserved_job_tags(body.tags)
    if reserved:
        raise HTTPException(status_code=400, detail={
            "code": "reserved_job_tags",
            "message": "authority tags may only be set by Colony control planes",
            "tags": reserved,
        })
    reserved_canary_hint = str(
        body.payload.get("action_hint") or ""
    ).strip()
    if (
        body.payload.get("schema") == "ApprovalRelayCanaryV1"
        or reserved_canary_hint == "approval_relay_canary"
    ):
        raise HTTPException(status_code=400, detail={
            "code": "approval_relay_canary_authority_reserved",
            "message": (
                "ApprovalRelayCanaryV1 may only be issued by Colony's "
                "server-owned approval canary endpoint"
            ),
        })
    if body.payload.get("schema") == "WorkOrderV1":
        raise HTTPException(status_code=400, detail={
            "code": "work_order_authority_reserved",
            "message": (
                "WorkOrderV1 authority may only be issued by the durable "
                "ProjectEngine adapter"
            ),
        })
    if body.job_type == JobType.THOUGHT.value or (
        body.payload.get("schema") == "ThoughtJobV1"
    ):
        raise HTTPException(status_code=400, detail={
            "code": "thought_job_authority_reserved",
            "message": (
                "ThoughtJobV1 may only be issued by Colony's cognition spine"
            ),
        })
    from colony_sidecar.task_queue.routing import (
        AGENT_ACTION_ROUTE_CAPABILITIES,
    )
    caller_route_capabilities = sorted({
        str(capability.get("name") or "").strip()
        for capability in (body.capabilities or [])
    } & AGENT_ACTION_ROUTE_CAPABILITIES)
    if caller_route_capabilities:
        raise HTTPException(status_code=400, detail={
            "code": "agent_action_route_authority_reserved",
            "message": (
                "agent_action executor routes are derived by Colony and "
                "cannot be selected by API callers"
            ),
            "capabilities": caller_route_capabilities,
        })
    job_type = JobType(body.job_type) if body.job_type else JobType.AGENT_ACTION
    payload = dict(body.payload)
    public_effect = False
    if job_type is JobType.AGENT_ACTION:
        from colony_sidecar.initiatives.action_registry import get_action

        action_hint = str(payload.get("action_hint") or "").strip()
        action_spec = get_action(action_hint)
        if action_spec is None:
            raise HTTPException(status_code=400, detail={
                "code": "unregistered_agent_action",
                "message": (
                    "public agent_action jobs require a named action_hint "
                    "from Colony's server-owned action registry"
                ),
            })
        declared_risk = str(payload.get("risk") or "").strip().lower()
        canonical_risk = action_spec.risk.value
        if declared_risk and declared_risk != canonical_risk:
            raise HTTPException(status_code=400, detail={
                "code": "agent_action_risk_mismatch",
                "message": "agent_action risk must match the server registry",
            })
        payload["risk"] = canonical_risk
        public_effect = canonical_risk != "read_only"
    # JobPriority is an int Enum (NORMAL=50, HIGH=80, ...); look up by NAME, not value —
    # JobPriority("HIGH") tries to match a member whose value is the string "HIGH" and always
    # raises (it even 500'd the default "normal"). Accept the name or the numeric value.
    if body.priority:
        _p = str(body.priority).upper()
        priority = JobPriority[_p] if _p in JobPriority.__members__ else JobPriority(int(body.priority))
    else:
        priority = JobPriority.NORMAL

    caps: List[JobCapabilityRequirement] = []
    if body.capabilities:
        for c in body.capabilities:
            caps.append(JobCapabilityRequirement(
                name=c["name"],
                minimum=c.get("minimum"),
                preferred=c.get("preferred", False),
            ))

    job = Job(
        job_type=job_type,
        payload=payload,
        priority=priority,
        capabilities=caps,
        deadline=_parse_dt(body.deadline),
        max_retries=body.max_retries,
        timeout_secs=body.timeout_secs,
        depends_on=body.depends_on,
        tags=body.tags or {},
        posted_by="api",
    )
    if public_effect:
        job.status = JobStatus.BLOCKED
        job.tags.update({
            "hold_kind": "approval",
            "blocked_reason": "awaiting_owner_approval",
            "awaiting_owner_approval": "true",
            "approval_requested_at": datetime.now(timezone.utc).isoformat(),
        })
    if job_type is JobType.AGENT_ACTION:
        from colony_sidecar.task_queue.routing import (
            expected_agent_action_routes,
        )
        try:
            expected_agent_action_routes(job)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={
                "code": "agent_action_routing_invalid",
                "message": str(exc),
            }) from exc
    job_id = await queue.queue.post(job)
    return {"success": True, "job_id": job_id}


@router.post("/jobs/claim")
async def claim_job(
    body: JobClaimRequest,
    request: Request = None,
) -> Optional[Dict[str, Any]]:
    """Claim through QueueManager's mandatory atomic authority boundary."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=body.node_id, required_scope="workers:claim",
    )
    caps = _bounded_worker_capabilities(body, context)
    try:
        job = await queue.queue.claim_job(
            body.node_id,
            caps,
            authority_tags=_worker_tags(context),
        )
    except QueueExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "queue_execution_unavailable",
            "message": str(exc),
        }) from exc
    if job is None:
        return None
    out = _job_to_dict(job)
    mode = str(job.tags.get("governor_mode") or "")
    if mode:
        governor = {
            "mode": mode,
            "enforced": str(job.tags.get("governor_enforced") or "false")
            == "true",
            "would_refuse": str(
                job.tags.get("governor_would_refuse") or "false"
            ) == "true",
        }
        error = str(job.tags.get("governor_error") or "")
        if error:
            governor["error"] = error
        out["governor"] = governor
    return out


@router.post("/jobs/{job_id}/start")
async def start_job(
    job_id: str,
    body: JobStartRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Transition a claimed job to RUNNING."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    claim_attempt_id = body.claim_attempt_id
    replayed = (
        job.status is JobStatus.RUNNING
        and job.claim_attempt_id == claim_attempt_id
    )
    if not await queue.queue.start_job(
        job_id, worker_id, claim_attempt_id=claim_attempt_id,
    ):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in the expected state",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": claim_attempt_id,
        "replayed": replayed,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    body: JobCompleteRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Mark a job as completed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or (
        job.result.worker_node_id
        if (
            job.status in {
                JobStatus.COMPLETED, JobStatus.NEUTRAL, JobStatus.FAILED,
            }
            and job.result is not None
            and str(job.tags.get("worker_completion_terminalized") or "")
            == "true"
        )
        else None
    )
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )

    # Completion auditing and trust evidence live in QueueManager so embedded
    # and direct consumers cannot bypass the HTTP-only path.
    audit = await queue.queue.complete_job(
        job_id=job_id,
        worker_id=worker_id,
        output=body.output,
        started_at=None,
        claim_attempt_id=body.claim_attempt_id,
    )

    if not audit.get("transitioned"):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not running under this worker claim",
        })

    return {
        "success": bool(audit.get("transitioned")),
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
        **audit,
    }


@router.post("/attestations/jobs/{job_id}")
async def attest_action_success(
    job_id: str,
    body: ActionSuccessAttestationRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Promote a generic effect only from an independent scoped verifier."""

    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    authority = request_authority(request)
    executor_node = str(
        job.result.worker_node_id if job.result is not None else ""
    ).strip()
    executor_principal = str(
        (job.tags or {}).get("worker_authority_principal") or ""
    ).strip()
    same_executor_grant = any(
        grant.node_id == executor_node
        for grant in authority.worker_grants
    )
    if (
        not authority.authenticated
        or authority.legacy
        or same_executor_grant
        or (
            executor_principal
            and authority.principal_id == executor_principal
        )
        or authority.principal_id == executor_node
    ):
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": (
                "the executor principal cannot attest its own action result"
            ),
        })
    from colony_sidecar.task_queue.action_receipts import (
        ActionReceiptAttestationV1,
    )

    receipt = ActionReceiptAttestationV1.from_payload(
        body.model_dump(by_alias=True)
    )
    result = await queue.queue.attest_action_success(
        job_id,
        attestation=receipt,
        verifier_identity=authority.principal_id,
        verifier_type="scoped_receipt_verifier",
    )
    if result is None:
        raise HTTPException(status_code=409, detail={
            "code": "action_attestation_rejected",
            "message": (
                "receipt does not match the pending action and claim attempt"
            ),
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "action_digest": body.action_digest,
        "verifier_identity": authority.principal_id,
        "schema": "ActionReceiptAttestationResultV1",
        "version": 1,
        **result,
    }


@router.post("/jobs/{job_id}/fail")
async def fail_job(
    job_id: str,
    body: JobFailRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Mark a job as failed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or (
        job.result.worker_node_id
        if (
            job.status in {JobStatus.QUEUED, JobStatus.FAILED}
            and job.result is not None
            and job.result.status is JobStatus.FAILED
            and job.result.claim_attempt_id == body.claim_attempt_id
        )
        else None
    )
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    transitioned = await queue.queue.fail_job(
        job_id=job_id,
        worker_id=worker_id,
        error=body.error,
        started_at=None,
        claim_attempt_id=body.claim_attempt_id,
    )

    if not transitioned:
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in a fail-able state",
        })

    stored = await queue.queue.get_job(job_id)

    return {
        "success": True,
        "job_id": job_id,
        "job_status": stored.status.value if stored is not None else "unknown",
        "replayed": job.claimed_by is None,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/heartbeat")
async def job_heartbeat(
    job_id: str,
    body: JobHeartbeatRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Update job progress heartbeat."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    progress = {job_id: body.progress} if body.progress is not None else None
    updated = await queue.queue.send_heartbeat(
        worker_id,
        [job_id],
        progress=progress,
        claim_attempt_ids=(
            {job_id: body.claim_attempt_id} if body.claim_attempt_id else {}
        ),
    )
    if updated != 1:
        raise HTTPException(status_code=409, detail={
            "code": "worker_heartbeat_claim_mismatch",
            "message": "job is no longer claimed by this worker",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/release")
async def release_job(
    job_id: str,
    body: JobReleaseRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Release a claimed job back to the queue."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    claim_attempt_id = body.claim_attempt_id
    worker_id = job.claimed_by
    replayed = False
    if not worker_id and claim_attempt_id is not None:
        worker_id = await queue.queue.worker_for_claim_attempt(
            job_id, claim_attempt_id,
        )
        replayed = worker_id is not None
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    if not await queue.queue.release_job(
        job_id, worker_id, claim_attempt_id=claim_attempt_id,
    ):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in a releasable state",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": claim_attempt_id,
        "replayed": replayed,
        "worker_authority": _worker_tags(context),
    }


@router.get("/jobs/blocked")
async def list_blocked_jobs(
    task_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    after: Optional[str] = None,
    response: Response = None,
) -> List[Dict[str, Any]]:
    """List BLOCKED jobs awaiting owner approval (v0.17.0), optionally by type.

    Dependency-blocked jobs are excluded — they resolve automatically
    when their dependencies complete.
    """
    if after is not None and not is_canonical_job_id(after):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_blocked_jobs_cursor",
            "message": "after must be a canonical queue job ID",
        })
    queue = _get_queue()
    jobs = await queue.queue.get_jobs_by_status(JobStatus.BLOCKED)
    canonical_jobs = []
    legacy_count = 0
    for job in jobs:
        if job.tags.get("blocked_reason", "") != "awaiting_owner_approval":
            continue
        if task_type and job.job_type.value != task_type:
            continue
        if is_canonical_job_id(job.job_id):
            canonical_jobs.append(job)
        else:
            legacy_count += 1
    if response is not None:
        response.headers["X-Colony-Blocked-Legacy-Count"] = str(legacy_count)

    items = []
    for job in sorted(canonical_jobs, key=lambda item: item.job_id):
        blocked_reason = job.tags.get("blocked_reason", "")
        if after is not None and job.job_id <= after:
            continue
        # Approval-at-birth is owned by QueueManager.post. This GET is a
        # projection only; polling it can never create or change authority.
        approval_request = _approval_store().get_request_for_job(job.job_id)
        presentation = (
            approval_request.get("presentation")
            if approval_request is not None else None
        )
        items.append({
            "id": job.job_id,
            "action_hint": (
                presentation.get("action_name") if presentation else None
            ),
            "risk": (
                presentation.get("risk") if presentation
                else "projection_unavailable"
            ),
            "description": (
                presentation.get("summary") if presentation
                else "Approval presentation unavailable"
            ),
            "created_at": job.posted_at.isoformat() if job.posted_at else None,
            "blocked_reason": blocked_reason,
            "approval_request_id": (
                approval_request.get("request_id") if approval_request else None
            ),
            "action_digest": (
                approval_request.get("action_digest") if approval_request else None
            ),
            "approval_expires_at": (
                approval_request.get("expires_at") if approval_request else None
            ),
            "approval_scope": (
                approval_request.get("scope") if approval_request else None
            ),
            "presentation": presentation,
            "presentation_digest": (
                approval_request.get("presentation_digest")
                if approval_request else None
            ),
            "binding_digest": (
                approval_request.get("binding_digest")
                if approval_request else None
            ),
            "request_digest": (
                approval_request.get("request_digest")
                if approval_request else None
            ),
            "projection_status": (
                "available" if presentation else "projection_unavailable"
            ),
        })
        if len(items) >= limit:
            break
    return items


@router.post("/jobs/{job_id}/approve")
async def approve_job(
    job_id: str,
    body: JobApproveRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Compatibility approval endpoint backed by immutable authority state.

    ``approved_by`` is accepted only so existing clients keep parsing; it is
    ignored. The decision actor is derived from the authenticated principal.
    ``always`` now means a seven-day/five-use exact-scope grant, never a
    permanent action-name bypass.
    """
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    grant = body.grant
    if body.always and grant is None:
        grant = BoundedGrantRequest()
    result = await _decide_job(
        job=job,
        decision="approve",
        decision_id=body.decision_id,
        approval_request_id=body.approval_request_id,
        expected_action_digest=body.expected_action_digest,
        grant=grant,
        request=request,
    )
    # Response aliases keep old integrations operational while exposing the
    # new durable model. Both aliases contain the same bounded grant.
    result["approved_by"] = result["decided_by"]
    result["approved_at"] = result["decided_at"]
    result["standing_approval"] = result["bounded_grant"]
    logger.info("Job %s approved by principal %s", job_id, result["decided_by"])
    return result


@router.post("/jobs/{job_id}/reject")
async def reject_job(
    job_id: str,
    body: JobRejectRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Reject a BLOCKED job using server-derived decision authority."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await _decide_job(
        job=job,
        decision="reject",
        decision_id=body.decision_id,
        approval_request_id=body.approval_request_id,
        expected_action_digest=body.expected_action_digest,
        grant=None,
        request=request,
        rejection_reason=body.reason,
    )
    result["rejected_by"] = result["decided_by"]
    result["reason"] = body.reason
    logger.info("Job %s rejected by principal %s", job_id, result["decided_by"])
    return result


# ---------------------------------------------------------------------------
# Durable approval requests and bounded grants
# ---------------------------------------------------------------------------

@router.get("/approvals/requests")
async def list_approval_requests(
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
) -> List[Dict[str, Any]]:
    # The authority database may also contain non-queue experiment/charter
    # records, and a cross-database crash may leave a historical orphan. This
    # queue API exposes only requests that still have a canonical queue job.
    # The host bridge uses `/jobs/blocked`, not this administrative ledger view.
    candidates = _approval_store().list_requests(status=status, limit=500)
    queue = _get_queue()
    result: List[Dict[str, Any]] = []
    for candidate in candidates:
        if await queue.queue.get_job(candidate["job_id"]) is None:
            continue
        result.append(candidate)
        if len(result) >= limit:
            break
    return result


@router.get("/approvals/requests/{request_id}")
async def get_approval_request(request_id: str) -> Dict[str, Any]:
    result = _approval_store().get_request(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if await _get_queue().queue.get_job(result["job_id"]) is None:
        raise HTTPException(status_code=404, detail={
            "code": "queue_approval_request_not_found",
            "message": "Approval request is not owned by a canonical queue job",
        })
    return result


def _request_projection(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return {
        "request_id": value["request_id"],
        "request_digest": value["request_digest"],
        "job_id": value["job_id"],
        "action_digest": value["action_digest"],
        "scope_digest": value["scope_digest"],
        "binding_digest": value["binding_digest"],
        "presentation_digest": value["presentation_digest"],
        "status": value["status"],
        "created_at": value["created_at"],
        "expires_at": value["expires_at"],
        "superseded_by": value.get("superseded_by"),
        "decision": value.get("decision"),
        "decision_id": value.get("decision_id"),
        "decided_at": value.get("decided_at"),
        "decided_by": value.get("decided_by"),
        "authority_evidence": value.get("authority_evidence"),
        "grant_id": value.get("grant_id"),
    }


async def _queue_approval_state_projection(
    queue: TaskQueueManager,
    job: Job,
    authorization: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    """Project bounded queue state without trusting mutable job tags."""

    hold_kind: Optional[str] = None
    blocked_reason: Optional[str] = None
    if job.status is JobStatus.BLOCKED:
        auth_kind = str(authorization.get("kind") or "")
        auth_status = str(authorization.get("status") or "")
        if auth_kind == "request" and auth_status == "pending":
            hold_kind = "approval"
            blocked_reason = "awaiting_owner_approval"
        elif auth_status == "authorized" and job.depends_on:
            dependency_failed = False
            dependency_pending = False
            for dependency_id in job.depends_on:
                dependency = await queue.queue.get_job(dependency_id)
                if dependency is None:
                    dependency_pending = True
                elif dependency.status in {
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.NEUTRAL,
                }:
                    dependency_failed = True
                elif dependency.status is not JobStatus.COMPLETED:
                    dependency_pending = True
            if dependency_failed or dependency_pending:
                hold_kind = "dependency"
                blocked_reason = (
                    "dependency_terminal_transition_pending"
                    if dependency_failed else "dependencies_pending"
                )
        if hold_kind is None:
            # A durable decision and its queue transition live in different
            # SQLite commits. Any other BLOCKED combination is conservatively
            # incomplete, rather than copied from caller/worker-controlled tags.
            hold_kind = "authority_transition"
            blocked_reason = (
                "canonical_authority_release_pending"
                if auth_status == "authorized"
                else "canonical_authority_terminal_pending"
                if auth_status in {"rejected", "expired", "superseded"}
                else "canonical_authority_unavailable"
            )
    return {
        "job_status": job.status.value,
        "hold_kind": hold_kind,
        "blocked_reason": blocked_reason,
    }


@router.get("/approvals/jobs/{job_id}")
async def get_job_approval_projection(job_id: str) -> Dict[str, Any]:
    """Return the exact transport-neutral authority the host may import.

    This endpoint recomputes the immutable job binding and presentation. It
    reads decision/grant provenance from the canonical authority database,
    never from worker-controlled tags, and does not expose the raw job payload.
    """

    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        binding = _job_binding(job)
        presentation = build_approval_presentation(
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
            deadline=job.deadline,
        )
    except ApprovalAuthorityError as exc:
        raise _approval_error(exc) from exc
    presentation_digest = approval_presentation_digest(presentation)
    binding_digest = approval_binding_digest(
        job_id=job.job_id,
        job_type=job.job_type.value,
        action_digest=binding.action_digest,
        scope_digest=binding.scope_digest,
    )
    store = _approval_store()
    direct_request = store.get_request_for_job(job.job_id)
    grant_use = store.get_grant_use(binding.action_digest)
    source_request = None
    authorization: Dict[str, Any]
    expires_at = None
    if direct_request is not None:
        source_request = direct_request
        binding_matches = bool(
            direct_request["action_digest"] == binding.action_digest
            and direct_request["scope_digest"] == binding.scope_digest
            and direct_request["presentation_digest"] == presentation_digest
        )
        if (
            binding_matches
            and direct_request["status"] == "approved"
            and direct_request.get("decision") == "approve"
        ):
            authorization = {
                "kind": "direct_decision",
                "status": "authorized",
                "request_id": direct_request["request_id"],
                "decision_id": direct_request["decision_id"],
                "decision": direct_request["decision"],
                "decided_at": direct_request["decided_at"],
                "decided_by": direct_request["decided_by"],
                "authority_evidence": direct_request["authority_evidence"],
            }
        else:
            authorization = {
                "kind": "request",
                "status": (
                    direct_request["status"]
                    if binding_matches else "invalid_binding"
                ),
                "request_id": direct_request["request_id"],
                "decision_id": direct_request.get("decision_id"),
                "decision": direct_request.get("decision"),
                "binding_matches": binding_matches,
            }
        expires_at = direct_request["expires_at"]
    elif (
        grant_use is not None
        and grant_use.get("operation_id") == job.job_id
        and grant_use.get("scope_digest") == binding.scope_digest
    ):
        source_request = store.get_request(grant_use["source_request_id"])
        source_request_matches = bool(
            source_request is not None
            and source_request["request_id"] == grant_use["source_request_id"]
            and source_request["status"] == "approved"
            and source_request.get("decision") == "approve"
            and source_request.get("decision_id") == grant_use["decision_id"]
            and source_request.get("grant_id") == grant_use["grant_id"]
            and source_request.get("decided_by") == grant_use["granted_by"]
            and source_request["scope_digest"] == grant_use["scope_digest"]
        )
        authorization = {
            "kind": "bounded_grant",
            "status": (
                "authorized" if source_request_matches else
                (
                    "missing_source_request" if source_request is None
                    else "invalid_provenance"
                )
            ),
            "grant_id": grant_use["grant_id"],
            "source_request_id": grant_use["source_request_id"],
            "decision_id": grant_use["decision_id"],
            "granted_by": grant_use["granted_by"],
            "scope_digest": grant_use["scope_digest"],
            "operation_id": grant_use["operation_id"],
            "consumed_at": grant_use["consumed_at"],
            "grant_created_at": grant_use["grant_created_at"],
            "expires_at": grant_use["grant_expires_at"],
            "grant_status": grant_use["grant_status"],
            "uses": grant_use["uses"],
            "max_uses": grant_use["max_uses"],
            "source_request_matches": source_request_matches,
        }
        expires_at = grant_use["grant_expires_at"]
    else:
        authorization = {"kind": "none", "status": "missing"}

    request_value = source_request or direct_request
    queue_authority_state = await _queue_approval_state_projection(
        queue, job, authorization,
    )
    projection = {
        "schema": AUTHORIZATION_PROJECTION_SCHEMA,
        "version": 1,
        "authority_mode": authority_mode(),
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "job_status": job.status.value,
        "queue_authority_state": queue_authority_state,
        "action_digest": binding.action_digest,
        "scope_digest": binding.scope_digest,
        "binding_digest": binding_digest,
        "presentation": presentation,
        "presentation_digest": presentation_digest,
        "request": _request_projection(request_value),
        "request_digest": (
            request_value.get("request_digest") if request_value else None
        ),
        "expires_at": expires_at,
        "authorization": authorization,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    projection["projection_digest"] = hashlib.sha256(json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return projection


@router.post("/approvals/requests/{request_id}/decision")
async def decide_approval_request(
    request_id: str,
    body: ApprovalDecisionRequest,
    request: Request = None,
) -> Dict[str, Any]:
    stored = _approval_store().get_request(request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    queue = _get_queue()
    job = await queue.queue.get_job(stored["job_id"])
    if job is None:
        raise HTTPException(status_code=409, detail={
            "code": "approval_job_missing",
            "message": "the approval request's job no longer exists",
        })
    return await _decide_job(
        job=job,
        decision=body.decision,
        decision_id=body.decision_id,
        approval_request_id=request_id,
        expected_action_digest=body.expected_action_digest,
        grant=body.grant,
        request=request,
    )


@router.get("/approvals/grants")
async def list_bounded_grants(
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return _approval_store().list_grants(status=status)


@router.delete("/approvals/grants/{grant_id}")
async def revoke_bounded_grant(grant_id: str) -> Dict[str, Any]:
    if not _approval_store().revoke_grant(grant_id):
        raise HTTPException(status_code=404, detail="Bounded grant not found or inactive")
    logger.info("Bounded approval grant revoked: %s", grant_id)
    return {"success": True, "grant_id": grant_id}


# Legacy read/revoke paths remain during migration. They return bounded grants
# and never mint permanent action-name authority.
@router.get("/approvals/standing")
async def list_standing_approvals() -> List[Dict[str, Any]]:
    return _approval_store().list_grants()


@router.delete("/approvals/standing/{action_name}")
async def revoke_standing_approval(action_name: str) -> Dict[str, Any]:
    """Revoke all active bounded grants for an exact legacy action name."""
    store = _approval_store()
    matching = [
        item for item in store.list_grants(status="active")
        if item.get("scope", {}).get("action_name") == action_name
    ]
    changed = False
    for item in matching:
        changed = store.revoke_grant(item["grant_id"]) or changed
    if not changed:
        raise HTTPException(
            status_code=404,
            detail=f"No active bounded approval for {action_name}",
        )
    logger.info("Bounded approvals revoked for %s", action_name)
    return {"success": True, "action_name": action_name}


@router.get("/jobs/pending")
async def list_pending_jobs(
    limit: int = Query(50, ge=1, le=200),
    task_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List pending (queued + claimed + running + blocked) jobs."""
    queue = _get_queue()
    jobs: List[Job] = []
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.QUEUED))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.CLAIMED))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.RUNNING))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.BLOCKED))

    items = []
    for job in jobs:
        if task_type and job.job_type.value != task_type:
            continue
        items.append(_job_to_dict(job))
    return items[:limit]


@router.get("/jobs/completed")
async def list_completed_jobs(
    since: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List completed jobs since a timestamp, optionally filtered by type."""
    queue = _get_queue()
    since_dt = _parse_dt(since) or datetime.min.replace(tzinfo=timezone.utc)
    completed = await queue.queue.get_completed_jobs_since(
        since_dt, limit=limit, job_type=task_type)
    return completed


@router.get("/jobs/neutral")
async def list_neutral_jobs(
    task_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List terminal jobs that still need independent verification."""

    queue = _get_queue()
    jobs = await queue.queue.get_jobs_by_status(JobStatus.NEUTRAL)
    return [
        _job_to_dict(job) for job in jobs
        if not task_type or job.job_type.value == task_type
    ][:limit]


@router.get("/inspection/jobs/{job_id}")
async def inspect_job(job_id: str) -> Dict[str, Any]:
    """Read one exact job for authenticated executor canary attestation."""

    job = await _get_queue().queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_inspection_dict(job)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/governor")
async def governor_status() -> Dict[str, Any]:
    """Worker-governor status (item 5): enforcement mode + per-job-type
    earned-trust stages."""
    gov = _governor()
    queue = _get_queue()
    central = await queue.queue.governance_status()
    if gov is None:
        return {"available": False, "central": central}
    try:
        return {"available": True, **gov.status(), "central": central}
    except Exception as exc:
        return {"available": True, "error": str(exc), "central": central}


@router.get("/stats")
async def queue_stats(request: Request = None) -> Dict[str, Any]:
    """Return queue statistics."""
    queue = _get_queue()
    stats = await queue.queue.get_queue_stats()
    scheduler = (
        getattr(request.app.state, "queue_scheduler", None)
        if request is not None else None
    )
    worker_truth = {
        "registered_workers": stats.registered_workers,
        "active_workers": stats.active_workers,
        "stale_workers": stats.stale_workers,
        "available_workers": stats.available_workers,
        "worker_heartbeat_ttl_secs": stats.worker_heartbeat_ttl_secs,
    }
    return {
        "by_status": stats.by_status,
        "by_type": stats.by_type,
        "total_workers": stats.total_workers,
        "available_workers": stats.available_workers,
        "registered_workers": stats.registered_workers,
        "active_workers": stats.active_workers,
        "stale_workers": stats.stale_workers,
        "worker_heartbeat_ttl_secs": stats.worker_heartbeat_ttl_secs,
        "last_user_message_at": load_last_user_message_at(),
        # Reuse the same worker snapshot so a heartbeat on the TTL boundary
        # cannot make top-level and nested readiness disagree in one response.
        "governance": await queue.queue.governance_status(worker_truth),
        "scheduler": (
            scheduler.health
            if scheduler is not None else {
                "running": False,
                "healthy": False,
                "last_tick_at": None,
                "last_error": "scheduler_unavailable",
                "tick_count": 0,
            }
        ),
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(6, ge=1, le=48),
) -> Dict[str, Any]:
    """Return a digest of completed and failed jobs in the last N hours."""
    queue = _get_queue()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    digest = await queue.queue.get_digest_jobs(since)

    completed_lines = []
    for job in digest.get("completed", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        completed_lines.append(f"✓ {desc}")

    failed_lines = []
    for job in digest.get("failed", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        err = job.get("error", "unknown error")
        failed_lines.append(f"⚠ {desc} — {err}")

    neutral_lines = []
    for job in digest.get("neutral", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        reason = (job.get("tags") or {}).get(
            "governor_outcome_reason", "needs independent verification"
        )
        neutral_lines.append(f"? {desc} — {reason}")

    return {
        "period_hours": hours,
        "since": since.isoformat(),
        "completed_count": len(digest.get("completed", [])),
        "failed_count": len(digest.get("failed", [])),
        "needs_verification_count": len(digest.get("neutral", [])),
        "completed": completed_lines,
        "failed": failed_lines,
        "needs_verification": neutral_lines,
    }

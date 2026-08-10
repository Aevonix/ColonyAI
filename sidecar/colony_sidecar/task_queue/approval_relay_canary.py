"""Server-owned, permanently inert approval-relay calibration job.

The canary exists only to prove Colony -> host owner transport -> Colony
decision relay before generic effects are enabled.  It deliberately uses the
normal canonical approval ledger while having no executable terminal path.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Mapping

from colony_sidecar.task_queue.models import Job, JobPriority, JobStatus, JobType


SCHEMA = "ApprovalRelayCanaryV1"
VERSION = 1
ACTION_HINT = "approval_relay_canary"
JOB_ID_PREFIX = "approval-relay-canary:"
POSTED_BY = "colony:approval-relay-canary"
TERMINAL_POLICY = "record_decision_then_cancel_without_execution"
APPROVAL_TTL_SECONDS = 60 * 60
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def idempotency_digest(value: str) -> str:
    """Hash one bounded opaque token so caller material is never persisted."""

    if not isinstance(value, str) or value != value.strip():
        raise ValueError("approval relay canary idempotency key is invalid")
    if not 16 <= len(value) <= 128 or any(character.isspace() for character in value):
        raise ValueError("approval relay canary idempotency key is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def job_id_for_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("approval relay canary idempotency digest is invalid")
    return JOB_ID_PREFIX + digest


def payload_for_digest(digest: str) -> dict[str, Any]:
    job_id_for_digest(digest)
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "action_hint": ACTION_HINT,
        "risk": "mutating",
        "description": (
            "Approval relay calibration only. No tool, worker, external "
            "write, or outbound action will run for either decision."
        ),
        "external_effect": False,
        "reversible": True,
        "terminal_policy": TERMINAL_POLICY,
        "idempotency_digest": digest,
    }


def build_job(digest: str) -> Job:
    """Build the only queue object accepted as this reserved canary."""

    return Job(
        job_id=job_id_for_digest(digest),
        job_type=JobType.AGENT_ACTION,
        payload=payload_for_digest(digest),
        priority=JobPriority.NORMAL,
        max_retries=0,
        timeout_secs=60.0,
        posted_by=POSTED_BY,
        status=JobStatus.BLOCKED,
        tags={
            "hold_kind": "approval",
            "blocked_reason": "awaiting_owner_approval",
            "awaiting_owner_approval": "true",
            "approval_relay_canary": "true",
            "external_effect": "false",
            "terminal_policy": TERMINAL_POLICY,
        },
    )


def is_exact_job(job: Job) -> bool:
    """Recognize only the complete server-derived object, never loose tags."""

    payload: Mapping[str, Any] = job.payload if isinstance(job.payload, Mapping) else {}
    digest = payload.get("idempotency_digest")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return False
    expected_payload = payload_for_digest(digest)
    return bool(
        job.job_type is JobType.AGENT_ACTION
        and hmac.compare_digest(job.job_id, job_id_for_digest(digest))
        and job.posted_by == POSTED_BY
        and job.max_retries == 0
        and float(job.timeout_secs) == 60.0
        and dict(payload) == expected_payload
        and job.tags.get("approval_relay_canary") == "true"
        and job.tags.get("external_effect") == "false"
        and job.tags.get("terminal_policy") == TERMINAL_POLICY
    )


def assert_exact_job(job: Job) -> None:
    if not is_exact_job(job):
        raise ValueError("approval relay canary identity or inert contract drifted")

"""Task Queue API — ``/v1/host/queue`` endpoints for distributed job scheduling.

Exposes the TaskQueueManager / QueueManager surface to external workers
(including the host agent's cron-driven worker).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from colony_sidecar.initiatives import standing_approvals
from colony_sidecar.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.util.session_safety import (
    load_last_user_message_at,
    save_last_user_message_at,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/host/queue", tags=["task_queue"])

class WorkerRegisterRequest(BaseModel):
    node_id: str
    capabilities: List[str] = []
    capacity: Optional[Dict[str, float]] = None
    max_concurrent: int = 4
    job_types: List[str] = []
    available: bool = True
    load: float = 0.0


class WorkerHeartbeatRequest(BaseModel):
    job_ids: List[str] = []
    progress: Optional[Dict[str, float]] = None
    load: Optional[float] = None


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
    max_concurrent: int = 4
    job_types: Optional[List[str]] = None


class JobCompleteRequest(BaseModel):
    output: Dict[str, Any] = {}
    started_at: Optional[str] = None


class JobFailRequest(BaseModel):
    error: str
    started_at: Optional[str] = None


class JobHeartbeatRequest(BaseModel):
    progress: Optional[float] = None
    log_lines: Optional[List[str]] = None


class JobApproveRequest(BaseModel):
    approved_by: str = "owner"
    # v0.18.0: also grant a standing approval for this job's action —
    # future jobs with the same action_hint skip the gate entirely.
    always: bool = False


class JobRejectRequest(BaseModel):
    rejected_by: str = "owner"
    reason: str = "rejected_by_owner"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_queue() -> TaskQueueManager:
    try:
        return TaskQueueManager.get_instance()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Task queue not initialized")


def _governor() -> Any:
    """The server-side WorkerGovernor (item 5), or None if not wired."""
    try:
        from colony_sidecar.api.routers.host import _worker_governor
        return _worker_governor
    except Exception:
        return None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _job_to_dict(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "payload": job.payload,
        "priority": job.priority.value,
        "status": job.status.value,
        "claimed_by": job.claimed_by,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "max_retries": job.max_retries,
        "retry_count": job.retry_count,
        "timeout_secs": job.timeout_secs,
        "depends_on": job.depends_on,
        "tags": job.tags,
        "result": {
            "output": job.result.output,
            "error": job.result.error,
            "started_at": job.result.started_at.isoformat() if job.result and job.result.started_at else None,
            "completed_at": job.result.completed_at.isoformat() if job.result and job.result.completed_at else None,
            "duration_seconds": job.result.duration_seconds if job.result else None,
        } if job.result else None,
    }


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------

@router.post("/workers/register")
async def register_worker(body: WorkerRegisterRequest) -> Dict[str, Any]:
    """Register a worker node with the scheduler."""
    queue = _get_queue()
    caps = WorkerCapabilities(
        node_id=body.node_id,
        capabilities=set(body.capabilities),
        capacity=body.capacity or {},
        max_concurrent=body.max_concurrent,
        job_types={JobType(jt) for jt in body.job_types},
        available=body.available,
        load=body.load,
    )
    await queue.queue.register_worker(caps)
    logger.info("Worker registered: %s (types=%s)", body.node_id, body.job_types)
    return {"success": True, "node_id": body.node_id}


@router.post("/workers/{node_id}/heartbeat")
async def worker_heartbeat(node_id: str, body: WorkerHeartbeatRequest) -> Dict[str, Any]:
    """Receive a worker heartbeat."""
    queue = _get_queue()
    progress = body.progress or {}
    await queue.queue.send_heartbeat(
        worker_id=node_id,
        job_ids=body.job_ids,
        progress=progress,
    )
    if body.load is not None:
        await queue.queue.update_worker_load(node_id, body.load)
    return {"success": True, "node_id": node_id}


@router.post("/workers/{node_id}/deregister")
async def deregister_worker(node_id: str) -> Dict[str, Any]:
    """Remove a worker from the scheduler."""
    queue = _get_queue()
    await queue.queue.deregister_worker(node_id)
    logger.info("Worker deregistered: %s", node_id)
    return {"success": True, "node_id": node_id}


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs")
async def create_job(body: JobPostRequest) -> Dict[str, Any]:
    """Post a new job to the queue."""
    queue = _get_queue()
    job_type = JobType(body.job_type) if body.job_type else JobType.AGENT_ACTION
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
        payload=body.payload,
        priority=priority,
        capabilities=caps,
        deadline=_parse_dt(body.deadline),
        max_retries=body.max_retries,
        timeout_secs=body.timeout_secs,
        depends_on=body.depends_on,
        tags=body.tags or {},
        posted_by="api",
    )
    job_id = await queue.queue.post(job)
    return {"success": True, "job_id": job_id}


@router.post("/jobs/claim")
async def claim_job(body: JobClaimRequest) -> Optional[Dict[str, Any]]:
    """Atomically claim the highest-priority eligible job."""
    queue = _get_queue()
    caps = WorkerCapabilities(
        node_id=body.node_id,
        capabilities=set(body.capabilities or []),
        capacity=body.capacity or {},
        max_concurrent=body.max_concurrent,
        job_types={JobType(jt) for jt in (body.job_types or [])},
        available=True,
        load=0.0,
    )
    job = await queue.queue.claim_job(body.node_id, caps)
    if job is None:
        return None

    # Server-side enforcement (item 5): re-decide the claim against the
    # worker's real capabilities and the owner's boundaries. Shadow only
    # observes; live blocks a boundaried job and releases a capability
    # mismatch back to the queue for a worker that can cover it.
    gov = _governor()
    if gov is not None:
        try:
            verdict = gov.evaluate_claim(
                job, caps.capabilities, worker_node_id=body.node_id)
        except Exception:
            logger.debug("worker governor claim eval failed", exc_info=True)
            verdict = None
        if verdict is not None and not verdict.get("allowed", True):
            if not verdict.get("boundary_ok", True):
                await queue.queue.update_job_status(
                    job.job_id, JobStatus.BLOCKED,
                    reason=f"governor_boundary: {verdict.get('reason', '')}",
                    tags={"blocked_reason": "boundary_refused",
                          "governor_reason": str(verdict.get("reason", ""))[:200]})
            else:
                await queue.queue.release_job(job.job_id)
            logger.info("Governor refused claim of %s by %s: %s",
                        job.job_id, body.node_id, verdict.get("reason"))
            return None
        if verdict is not None:
            out = _job_to_dict(job)
            out["governor"] = {
                "mode": "shadow" if verdict.get("shadow") else "live",
                "enforced": verdict.get("enforced", False),
                "would_refuse": verdict.get("would_refuse", False),
            }
            return out
    return _job_to_dict(job)


@router.post("/jobs/{job_id}/start")
async def start_job(job_id: str) -> Dict[str, Any]:
    """Transition a claimed job to RUNNING."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or "unknown"
    await queue.queue.start_job(job_id, worker_id)
    return {"success": True, "job_id": job_id}


@router.post("/jobs/{job_id}/complete")
async def complete_job(job_id: str, body: JobCompleteRequest) -> Dict[str, Any]:
    """Mark a job as completed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or "unknown"

    # Server-side completion audit (item 5): the worker's report is verified
    # against what the job was authorized to do BEFORE it is trusted. A
    # mutation reported on a read-only job is a violation, flagged + recorded
    # (which trips the job type's circuit breaker in the trust engine).
    gov = _governor()
    verdict = "unverified"
    findings: List[str] = []
    if gov is not None:
        try:
            audit = gov.audit_report(job, body.output)
            verdict = audit.get("verdict", "unverified")
            findings = audit.get("findings", [])
        except Exception:
            logger.debug("worker governor audit failed", exc_info=True)

    await queue.queue.complete_job(
        job_id=job_id,
        worker_id=worker_id,
        output=body.output,
        started_at=_parse_dt(body.started_at),
    )

    if gov is not None:
        latency = None
        started = _parse_dt(body.started_at)
        if started is not None:
            latency = (datetime.now(timezone.utc) - started).total_seconds()
        try:
            await gov.record_outcome(job, body.output, verdict, latency=latency,
                                     attempts=job.retry_count)
        except Exception:
            logger.debug("worker governor record failed", exc_info=True)

    return {"success": True, "job_id": job_id, "verdict": verdict,
            "findings": findings}


@router.post("/jobs/{job_id}/fail")
async def fail_job(job_id: str, body: JobFailRequest) -> Dict[str, Any]:
    """Mark a job as failed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or "unknown"
    await queue.queue.fail_job(
        job_id=job_id,
        worker_id=worker_id,
        error=body.error,
        started_at=_parse_dt(body.started_at),
    )

    # Feed the failure to the trust engine so clustered failures trip the
    # job type's circuit breaker (item 5 -> item 4).
    gov = _governor()
    if gov is not None:
        try:
            await gov.record_outcome(job, {"summary": body.error}, "clean",
                                     outcome="failure", attempts=job.retry_count)
        except Exception:
            logger.debug("worker governor fail-record failed", exc_info=True)

    return {"success": True, "job_id": job_id}


@router.post("/jobs/{job_id}/heartbeat")
async def job_heartbeat(job_id: str, body: JobHeartbeatRequest) -> Dict[str, Any]:
    """Update job progress heartbeat."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or "unknown"
    progress = {job_id: body.progress} if body.progress is not None else None
    await queue.queue.send_heartbeat(worker_id, [job_id], progress=progress)
    return {"success": True, "job_id": job_id}


@router.post("/jobs/{job_id}/release")
async def release_job(job_id: str) -> Dict[str, Any]:
    """Release a claimed job back to the queue."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    await queue.queue.release_job(job_id)
    return {"success": True, "job_id": job_id}


@router.get("/jobs/blocked")
async def list_blocked_jobs(
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List BLOCKED jobs awaiting owner approval (v0.17.0).

    Dependency-blocked jobs are excluded — they resolve automatically
    when their dependencies complete.
    """
    queue = _get_queue()
    jobs = await queue.queue.get_jobs_by_status(JobStatus.BLOCKED)
    items = []
    for job in jobs:
        blocked_reason = job.tags.get("blocked_reason", "")
        if blocked_reason != "awaiting_owner_approval":
            continue
        items.append({
            "id": job.job_id,
            "action_hint": job.payload.get("action_hint"),
            "risk": job.payload.get("risk"),
            "description": job.payload.get("description", ""),
            "created_at": job.posted_at.isoformat() if job.posted_at else None,
            "blocked_reason": blocked_reason,
        })
    return items[:limit]


@router.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str, body: JobApproveRequest) -> Dict[str, Any]:
    """Approve a BLOCKED job — transitions it to QUEUED for claiming (v0.17.0).

    With ``{"always": true}`` (v0.18.0) the approval is also recorded as
    a standing approval for the job's ``action_hint``, so future jobs
    with the same action skip the gate.
    """
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value}, not blocked",
        )
    approved_at = datetime.now(timezone.utc).isoformat()
    await queue.queue.update_job_status(
        job_id,
        JobStatus.QUEUED,
        reason=f"approved_by={body.approved_by}",
        tags={"approved_by": body.approved_by, "approved_at": approved_at},
    )
    logger.info("Job %s approved by %s", job_id, body.approved_by)

    standing_entry = None
    if body.always:
        action_hint = job.payload.get("action_hint")
        if action_hint:
            standing_entry = standing_approvals.grant(
                action_hint, approved_by=body.approved_by,
            )
        else:
            logger.warning(
                "Job %s has no action_hint — standing approval not granted",
                job_id,
            )

    return {
        "success": True,
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "approved_by": body.approved_by,
        "approved_at": approved_at,
        "standing_approval": standing_entry,
    }


@router.post("/jobs/{job_id}/reject")
async def reject_job(job_id: str, body: JobRejectRequest) -> Dict[str, Any]:
    """Reject a BLOCKED job — transitions it to CANCELLED (v0.17.0)."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value}, not blocked",
        )
    rejected_at = datetime.now(timezone.utc).isoformat()
    await queue.queue.update_job_status(
        job_id,
        JobStatus.CANCELLED,
        reason=body.reason,
        tags={
            "rejected_by": body.rejected_by,
            "rejected_at": rejected_at,
            "rejected_reason": body.reason,
        },
    )
    logger.info("Job %s rejected by %s: %s", job_id, body.rejected_by, body.reason)
    return {
        "success": True,
        "job_id": job_id,
        "status": JobStatus.CANCELLED.value,
        "rejected_by": body.rejected_by,
        "reason": body.reason,
    }


# ---------------------------------------------------------------------------
# Standing approvals (v0.18.0)
# ---------------------------------------------------------------------------

@router.get("/approvals/standing")
async def list_standing_approvals() -> List[Dict[str, Any]]:
    """List all standing approvals (action name, approved_by, granted_at)."""
    return standing_approvals.list()


@router.delete("/approvals/standing/{action_name}")
async def revoke_standing_approval(action_name: str) -> Dict[str, Any]:
    """Revoke a standing approval — future jobs for the action block again."""
    if not standing_approvals.revoke(action_name):
        raise HTTPException(
            status_code=404,
            detail=f"No standing approval for {action_name}",
        )
    logger.info("Standing approval revoked for %s", action_name)
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
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List completed jobs since a timestamp."""
    queue = _get_queue()
    since_dt = _parse_dt(since) or datetime.min.replace(tzinfo=timezone.utc)
    completed = await queue.queue.get_completed_jobs_since(since_dt, limit=limit)
    return completed


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/governor")
async def governor_status() -> Dict[str, Any]:
    """Worker-governor status (item 5): enforcement mode + per-job-type
    earned-trust stages."""
    gov = _governor()
    if gov is None:
        return {"available": False}
    try:
        return {"available": True, **gov.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/stats")
async def queue_stats() -> Dict[str, Any]:
    """Return queue statistics."""
    queue = _get_queue()
    stats = await queue.queue.get_queue_stats()
    return {
        "by_status": stats.by_status,
        "by_type": stats.by_type,
        "total_workers": stats.total_workers,
        "available_workers": stats.available_workers,
        "last_user_message_at": load_last_user_message_at(),
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

    return {
        "period_hours": hours,
        "since": since.isoformat(),
        "completed_count": len(digest.get("completed", [])),
        "failed_count": len(digest.get("failed", [])),
        "completed": completed_lines,
        "failed": failed_lines,
    }

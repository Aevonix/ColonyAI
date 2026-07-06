"""Persistent job queue backed by SQLite with WAL mode.

Thread-safe for concurrent asyncio tasks via aiosqlite. All
state-change methods are transactional. Emits typed events to the
Colony event bus on each state transition.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from colony_sidecar.task_queue.models import (
    AuditEntry,
    CircularDependencyError,
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobResult,
    JobStatus,
    JobType,
    QueueStats,
    WorkerCapabilities,
    deadline_urgency,
)

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _job_from_row(row: aiosqlite.Row) -> Job:
    """Deserialize a SQLite row into a Job dataclass."""
    caps_raw = json.loads(row["capabilities"] or "[]")
    capabilities = [
        JobCapabilityRequirement(
            name=c["name"],
            minimum=c.get("minimum"),
            preferred=c.get("preferred", False),
        )
        for c in caps_raw
    ]
    result = None
    if row["result"]:
        r = json.loads(row["result"])
        result = JobResult(
            job_id=r["job_id"],
            worker_node_id=r.get("worker_node_id", ""),
            status=JobStatus(r["status"]),
            output=r.get("output", {}),
            error=r.get("error"),
            started_at=_parse_dt(r.get("started_at")),
            completed_at=_parse_dt(r.get("completed_at")),
            duration_seconds=r.get("duration_seconds"),
        )
    return Job(
        job_id=row["job_id"],
        job_type=JobType(row["job_type"]),
        payload=json.loads(row["payload"] or "{}"),
        priority=JobPriority(row["priority"]),
        capabilities=capabilities,
        deadline=_parse_dt(row["deadline"]),
        max_retries=row["max_retries"],
        retry_count=row["retry_count"],
        timeout_secs=row["timeout_secs"],
        depends_on=json.loads(row["depends_on"] or "[]"),
        posted_by=row["posted_by"] or "",
        posted_at=_parse_dt(row["posted_at"]) or datetime.now(timezone.utc),
        status=JobStatus(row["status"]),
        claimed_by=row["claimed_by"],
        claimed_at=_parse_dt(row["claimed_at"]),
        last_heartbeat=_parse_dt(row["last_heartbeat"]),
        result=result,
        tags=json.loads(row["tags"] or "{}"),
    )


def _worker_from_row(row: aiosqlite.Row) -> WorkerCapabilities:
    """Deserialize a SQLite row into a WorkerCapabilities dataclass."""
    job_types_raw = json.loads(row["job_types"] or "[]")
    return WorkerCapabilities(
        node_id=row["node_id"],
        capabilities=set(json.loads(row["capabilities"] or "[]")),
        capacity=json.loads(row["capacity"] or "{}"),
        max_concurrent=row["max_concurrent"],
        job_types={JobType(jt) for jt in job_types_raw},
        available=bool(row["available"]),
        load=row["load"],
        registered_at=_parse_dt(row["registered_at"]) or datetime.now(timezone.utc),
        last_seen=_parse_dt(row["last_seen"]) or datetime.now(timezone.utc),
    )


def _serialize_result(result: JobResult) -> str:
    return json.dumps({
        "job_id": result.job_id,
        "worker_node_id": result.worker_node_id,
        "status": result.status.value,
        "output": result.output,
        "error": result.error,
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "duration_seconds": result.duration_seconds,
    })


def _serialize_caps(caps: List[JobCapabilityRequirement]) -> str:
    return json.dumps([
        {"name": c.name, "minimum": c.minimum, "preferred": c.preferred}
        for c in caps
    ])


class QueueManager:
    """Persistent job queue backed by SQLite with WAL mode.

    Usage::

        mgr = QueueManager(db_path=Path("~/.colony/task_queue.db"))
        await mgr.start()
        job_id = await mgr.post(job)
        ...
        await mgr.stop()
    """

    def __init__(
        self,
        db_path: Path,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the database connection and apply schema."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._apply_schema()

    async def stop(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def _apply_schema(self) -> None:
        schema = _SCHEMA_PATH.read_text()
        # Execute each statement separately
        for stmt in schema.split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._db.execute(stmt)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Job submission
    # ------------------------------------------------------------------

    async def post(self, job: Job) -> str:
        """Add a new job to the queue. Returns job_id.

        If the job has dependencies, it enters BLOCKED state.
        Validates dependency DAG for cycles.
        """
        assert self._db is not None

        # Determine initial status
        if job.depends_on:
            await self._validate_dependencies(job)
            # Check if any deps are already in terminal-failed state
            for dep_id in job.depends_on:
                dep = await self.get_job(dep_id)
                if dep and dep.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                    job.status = JobStatus.FAILED
                    if not job.result:
                        job.result = JobResult(
                            job_id=job.job_id,
                            worker_node_id="",
                            status=JobStatus.FAILED,
                            error=f"dependency {dep_id} failed",
                        )
                    break
            else:
                # Check if all deps are already completed
                all_done = True
                for dep_id in job.depends_on:
                    dep = await self.get_job(dep_id)
                    if dep is None or dep.status != JobStatus.COMPLETED:
                        all_done = False
                        break
                if not all_done:
                    job.status = JobStatus.BLOCKED

        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, payload, priority, capabilities,
                deadline, max_retries, retry_count, timeout_secs, depends_on,
                posted_by, posted_at, status, claimed_by, claimed_at,
                last_heartbeat, result, tags
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                job.job_id,
                job.job_type.value,
                json.dumps(job.payload),
                job.priority.value,
                _serialize_caps(job.capabilities),
                job.deadline.isoformat() if job.deadline else None,
                job.max_retries,
                job.retry_count,
                job.timeout_secs,
                json.dumps(job.depends_on),
                job.posted_by,
                job.posted_at.isoformat(),
                job.status.value,
                job.claimed_by,
                job.claimed_at.isoformat() if job.claimed_at else None,
                job.last_heartbeat.isoformat() if job.last_heartbeat else None,
                _serialize_result(job.result) if job.result else None,
                json.dumps(job.tags),
            ),
        )
        await self._audit(job.job_id, None, job.status.value, reason="posted")
        await self._db.commit()
        logger.debug("Posted job %s (type=%s, status=%s)", job.job_id, job.job_type, job.status)
        return job.job_id

    async def _validate_dependencies(self, job: Job) -> None:
        """DFS cycle detection over dependency DAG."""
        visited: set = set()
        stack: set = set()

        async def dfs(jid: str) -> None:
            if jid in stack:
                raise CircularDependencyError(
                    f"Circular dependency detected involving job {jid}"
                )
            if jid in visited:
                return
            stack.add(jid)
            dep_job = await self.get_job(jid)
            if dep_job:
                for child in dep_job.depends_on:
                    await dfs(child)
            # Also check the new job itself
            if jid == job.job_id:
                for child in job.depends_on:
                    await dfs(child)
            stack.discard(jid)
            visited.add(jid)

        # Check that none of our deps transitively depend on us
        for dep_id in job.depends_on:
            if dep_id == job.job_id:
                raise CircularDependencyError(
                    f"Job {job.job_id} depends on itself"
                )
            dep_job = await self.get_job(dep_id)
            if dep_job:
                await self._check_no_cycle(dep_job, job.job_id, set())

    async def _check_no_cycle(self, current: Job, target_id: str, visited: set) -> None:
        if current.job_id in visited:
            return
        visited.add(current.job_id)
        for dep_id in current.depends_on:
            if dep_id == target_id:
                raise CircularDependencyError(
                    f"Circular dependency: {target_id} ← ... ← {current.job_id}"
                )
            dep = await self.get_job(dep_id)
            if dep:
                await self._check_no_cycle(dep, target_id, visited)

    # ------------------------------------------------------------------
    # Job claiming (atomic, optimistic-lock via SQLite transaction)
    # ------------------------------------------------------------------

    async def claim_job(
        self,
        worker_id: str,
        worker_caps: WorkerCapabilities,
    ) -> Optional[Job]:
        """Atomically claim the highest-priority eligible QUEUED job.

        Returns None if no eligible jobs are available.
        """
        assert self._db is not None
        now = datetime.now(timezone.utc)
        queued = await self.get_queued_jobs_sorted(now)

        for job in queued:
            if not worker_caps.can_accept(job):
                continue
            # Attempt atomic claim
            cur = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = ?, claimed_at = ?, last_heartbeat = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (
                    JobStatus.CLAIMED.value,
                    worker_id,
                    now.isoformat(),
                    now.isoformat(),
                    job.job_id,
                ),
            )
            if cur.rowcount == 1:
                await self._audit(
                    job.job_id, JobStatus.QUEUED.value, JobStatus.CLAIMED.value,
                    node_id=worker_id,
                )
                await self._db.commit()
                job.status = JobStatus.CLAIMED
                job.claimed_by = worker_id
                job.claimed_at = now
                job.last_heartbeat = now
                return job
            # Another worker got it first; try next
        return None

    # ------------------------------------------------------------------
    # Job state transitions
    # ------------------------------------------------------------------

    async def start_job(self, job_id: str, worker_id: str) -> None:
        """Transition CLAIMED → RUNNING."""
        assert self._db is not None
        now = _now_iso()
        await self._db.execute(
            """
            UPDATE jobs SET status = ?, last_heartbeat = ?
            WHERE job_id = ? AND status = ? AND claimed_by = ?
            """,
            (JobStatus.RUNNING.value, now, job_id, JobStatus.CLAIMED.value, worker_id),
        )
        await self._audit(job_id, JobStatus.CLAIMED.value, JobStatus.RUNNING.value, node_id=worker_id)
        await self._db.commit()

    async def complete_job(
        self,
        job_id: str,
        worker_id: str,
        output: Dict[str, Any],
        started_at: Optional[datetime] = None,
    ) -> None:
        """Transition RUNNING → COMPLETED and unblock dependents."""
        assert self._db is not None
        now = datetime.now(timezone.utc)
        result = JobResult(
            job_id=job_id,
            worker_node_id=worker_id,
            status=JobStatus.COMPLETED,
            output=output,
            started_at=started_at,
            completed_at=now,
            duration_seconds=(now - started_at).total_seconds() if started_at else None,
        )
        await self._db.execute(
            """
            UPDATE jobs SET status = ?, result = ?, claimed_by = NULL, claimed_at = NULL
            WHERE job_id = ? AND claimed_by = ?
            """,
            (
                JobStatus.COMPLETED.value,
                _serialize_result(result),
                job_id,
                worker_id,
            ),
        )
        await self._audit(job_id, JobStatus.RUNNING.value, JobStatus.COMPLETED.value, node_id=worker_id)
        await self._db.commit()
        await self.unblock_ready_jobs()

        # Emit JobCompletedEvent to the event bus (best-effort)
        if self._event_bus is not None:
            try:
                from colony_sidecar.task_queue.events import JobCompletedEvent
                self._event_bus.emit(JobCompletedEvent(
                    job_id=job_id,
                    worker_node_id=worker_id,
                    duration_seconds=result.duration_seconds,
                ))
            except Exception:
                pass

    async def get_completed_jobs_since(
        self,
        since: datetime,
        limit: int = 20,
        job_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return jobs completed after *since* with their result payloads.

        Used by the autonomy loop to discover recently finished tasks
        and generate follow-up initiatives (Gap C). ``job_type`` filters to
        one type (the API exposed the param but it was silently dropped).
        """
        assert self._db is not None
        sql = ("SELECT job_id, job_type, payload, result, priority FROM jobs "
               "WHERE status = ? AND claimed_at IS NULL")
        params: list = [JobStatus.COMPLETED.value]
        if job_type:
            sql += " AND job_type = ?"
            params.append(job_type)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        completed: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
                result_data = json.loads(row["result"]) if row["result"] else {}
                completed_at = result_data.get("completed_at")
                if completed_at:
                    from datetime import datetime as _dt
                    try:
                        ts = _dt.fromisoformat(completed_at)
                        if ts < since:
                            continue
                    except (ValueError, TypeError):
                        pass
                completed.append({
                    "job_id": row["job_id"],
                    "job_type": row["job_type"],
                    "payload": payload,
                    "result": result_data,
                    "description": payload.get("description", ""),
                    "entity_id": payload.get("entity_id"),
                })
            except (json.JSONDecodeError, TypeError):
                continue
        return completed

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        started_at: Optional[datetime] = None,
    ) -> None:
        """Transition RUNNING → FAILED. Re-queues if retries remain."""
        assert self._db is not None
        now = datetime.now(timezone.utc)
        job = await self.get_job(job_id)
        if job is None:
            return

        result = JobResult(
            job_id=job_id,
            worker_node_id=worker_id,
            status=JobStatus.FAILED,
            error=error,
            started_at=started_at,
            completed_at=now,
        )
        new_retry = job.retry_count + 1
        if job.retry_count < job.max_retries and not job.is_expired():
            # Re-queue
            await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, claimed_by = NULL, claimed_at = NULL,
                    result = ?
                WHERE job_id = ?
                """,
                (JobStatus.QUEUED.value, new_retry, _serialize_result(result), job_id),
            )
            await self._audit(
                job_id, JobStatus.RUNNING.value, JobStatus.QUEUED.value,
                node_id=worker_id, reason=f"retry {new_retry}/{job.max_retries}: {error}",
            )
        else:
            await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, result = ?,
                    claimed_by = NULL, claimed_at = NULL
                WHERE job_id = ?
                """,
                (JobStatus.FAILED.value, new_retry, _serialize_result(result), job_id),
            )
            await self._audit(
                job_id, JobStatus.RUNNING.value, JobStatus.FAILED.value,
                node_id=worker_id, reason=error,
            )
        await self._db.commit()

    async def release_job(self, job_id: str) -> None:
        """Transition CLAIMED/RUNNING → QUEUED, clearing the worker claim."""
        assert self._db is not None
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = NULL, claimed_at = NULL
            WHERE job_id = ? AND status IN (?, ?)
            """,
            (JobStatus.QUEUED.value, job_id, JobStatus.CLAIMED.value, JobStatus.RUNNING.value),
        )
        await self._audit(
            job_id, JobStatus.CLAIMED.value, JobStatus.QUEUED.value,
            reason="released_by_worker",
        )
        await self._db.commit()

    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        reason: str = "",
        tags: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Update a job's status and optionally merge new tags.

        Returns True if the job was found and updated.
        """
        assert self._db is not None
        job = await self.get_job(job_id)
        if job is None or job.is_terminal():
            return False
        old_status = job.status.value
        if tags:
            job.tags.update(tags)
            await self._db.execute(
                "UPDATE jobs SET status = ?, tags = ? WHERE job_id = ?",
                (status.value, json.dumps(job.tags), job_id),
            )
        else:
            await self._db.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (status.value, job_id),
            )
        await self._audit(job_id, old_status, status.value, reason=reason)
        await self._db.commit()
        return True

    async def merge_job_tags(self, job_id: str, tags: Dict[str, str]) -> bool:
        """Merge ``tags`` into a job's tag map WITHOUT changing its status.

        Unlike :meth:`update_job_status`, this is allowed on terminal jobs:
        post-completion bookkeeping (e.g. the autonomy writeback's
        ``memory_synced`` idempotency marker) tags jobs that are already
        COMPLETED/FAILED. ``update_job_status`` refuses terminal jobs to
        prevent illegal status revivals, so tag-only updates need their own
        path. Returns True if the job was found and updated.
        """
        assert self._db is not None
        if not tags:
            return False
        job = await self.get_job(job_id)
        if job is None:
            return False
        job.tags.update(tags)
        await self._db.execute(
            "UPDATE jobs SET tags = ? WHERE job_id = ?",
            (json.dumps(job.tags), job_id),
        )
        await self._db.commit()
        return True

    async def cancel_job(self, job_id: str, reason: str = "") -> bool:
        """Cancel a job from any non-terminal state. Returns True if cancelled."""
        assert self._db is not None
        job = await self.get_job(job_id)
        if job is None or job.is_terminal():
            return False
        old_status = job.status.value
        await self._db.execute(
            "UPDATE jobs SET status = ?, claimed_by = NULL WHERE job_id = ?",
            (JobStatus.CANCELLED.value, job_id),
        )
        await self._audit(job_id, old_status, JobStatus.CANCELLED.value, reason=reason)
        await self._db.commit()
        return True

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def send_heartbeat(
        self,
        worker_id: str,
        job_ids: List[str],
        progress: Optional[Dict[str, float]] = None,
    ) -> None:
        """Update last_heartbeat for listed jobs and worker last_seen."""
        assert self._db is not None
        now = _now_iso()
        for job_id in job_ids:
            pct = (progress or {}).get(job_id)
            await self._db.execute(
                """
                UPDATE jobs SET last_heartbeat = ? WHERE job_id = ? AND claimed_by = ?
                """,
                (now, job_id, worker_id),
            )
            await self._db.execute(
                """
                INSERT OR REPLACE INTO heartbeats (node_id, job_id, timestamp, progress)
                VALUES (?, ?, ?, ?)
                """,
                (worker_id, job_id, now, pct),
            )
        await self._db.execute(
            "UPDATE workers SET last_seen = ? WHERE node_id = ?",
            (now, worker_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Scheduler phases
    # ------------------------------------------------------------------

    async def expire_past_deadlines(self, now: datetime) -> int:
        """Transition expired QUEUED/CLAIMED/RUNNING jobs → FAILED. Returns count."""
        assert self._db is not None
        now_iso = now.isoformat()
        cur = await self._db.execute(
            """
            SELECT job_id, status FROM jobs
            WHERE deadline IS NOT NULL
              AND deadline < ?
              AND status NOT IN ('completed', 'failed', 'cancelled', 'abandoned')
            """,
            (now_iso,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid, old_status = row["job_id"], row["status"]
            await self._db.execute(
                "UPDATE jobs SET status = ?, claimed_by = NULL, claimed_at = NULL WHERE job_id = ?",
                (JobStatus.FAILED.value, jid),
            )
            await self._audit(jid, old_status, JobStatus.FAILED.value, reason="deadline_expired")
            count += 1
        if count:
            await self._db.commit()
        return count

    async def abandon_silent_jobs(self, now: datetime, timeout_secs: float) -> int:
        """Mark RUNNING/CLAIMED jobs with stale heartbeats as ABANDONED. Returns count."""
        assert self._db is not None
        from datetime import timedelta
        cutoff = (now - timedelta(seconds=timeout_secs)).isoformat()
        cur = await self._db.execute(
            """
            SELECT job_id, status, claimed_by FROM jobs
            WHERE status IN ('running', 'claimed')
              AND (last_heartbeat IS NULL OR last_heartbeat < ?)
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid, old_status, node = row["job_id"], row["status"], row["claimed_by"]
            await self._db.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (JobStatus.ABANDONED.value, jid),
            )
            await self._audit(
                jid, old_status, JobStatus.ABANDONED.value,
                node_id=node, reason="heartbeat_timeout",
            )
            count += 1
        if count:
            await self._db.commit()
        return count

    async def requeue_retryable_jobs(self, now: datetime) -> int:
        """Move ABANDONED jobs with remaining retries back to QUEUED. Returns count."""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT job_id, retry_count, max_retries, deadline FROM jobs
            WHERE status = 'abandoned'
            """,
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid = row["job_id"]
            retry = row["retry_count"]
            max_r = row["max_retries"]
            deadline = _parse_dt(row["deadline"])
            expired = deadline is not None and now > deadline

            if retry < max_r and not expired:
                new_retry = retry + 1
                await self._db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, retry_count = ?, claimed_by = NULL, claimed_at = NULL
                    WHERE job_id = ?
                    """,
                    (JobStatus.QUEUED.value, new_retry, jid),
                )
                await self._audit(
                    jid, JobStatus.ABANDONED.value, JobStatus.QUEUED.value,
                    reason=f"retry {new_retry}/{max_r}",
                )
                count += 1
            else:
                await self._db.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (JobStatus.FAILED.value, jid),
                )
                await self._audit(
                    jid, JobStatus.ABANDONED.value, JobStatus.FAILED.value,
                    reason="max_retries_exceeded",
                )
        if count or rows:
            await self._db.commit()
        return count

    async def unblock_ready_jobs(self) -> int:
        """Transition BLOCKED jobs to QUEUED when all deps completed. Returns count."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT job_id, depends_on FROM jobs WHERE status = 'blocked'"
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid = row["job_id"]
            deps = json.loads(row["depends_on"] or "[]")
            if not deps:
                continue
            # Check if any dep failed/cancelled
            any_failed = False
            fail_reason = ""
            all_complete = True
            for dep_id in deps:
                dep = await self.get_job(dep_id)
                if dep is None:
                    continue
                if dep.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                    any_failed = True
                    fail_reason = f"dependency {dep_id} {dep.status.value}"
                    break
                if dep.status != JobStatus.COMPLETED:
                    all_complete = False

            if any_failed:
                await self._db.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (JobStatus.FAILED.value, jid),
                )
                await self._audit(
                    jid, JobStatus.BLOCKED.value, JobStatus.FAILED.value,
                    reason=fail_reason,
                )
                count += 1
            elif all_complete:
                await self._db.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (JobStatus.QUEUED.value, jid),
                )
                await self._audit(
                    jid, JobStatus.BLOCKED.value, JobStatus.QUEUED.value,
                    reason="dependencies_met",
                )
                count += 1
        if count:
            await self._db.commit()
        return count

    async def expire_blocked_approvals(
        self,
        now: datetime,
        timeout_hours: float = 72.0,
    ) -> int:
        """Fail BLOCKED jobs awaiting owner approval older than *timeout_hours*.

        Only jobs blocked with the ``awaiting_owner_approval`` tag are
        affected — dependency-blocked jobs are left for
        ``unblock_ready_jobs``. Returns count.
        """
        assert self._db is not None
        from datetime import timedelta
        cutoff = (now - timedelta(hours=timeout_hours)).isoformat()
        cur = await self._db.execute(
            """
            SELECT job_id, tags FROM jobs
            WHERE status = 'blocked' AND posted_at < ?
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            tags = json.loads(row["tags"] or "{}")
            if tags.get("blocked_reason") != "awaiting_owner_approval":
                continue
            jid = row["job_id"]
            await self._db.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (JobStatus.FAILED.value, jid),
            )
            await self._audit(
                jid, JobStatus.BLOCKED.value, JobStatus.FAILED.value,
                reason="owner_approval_timeout",
            )
            count += 1
        if count:
            await self._db.commit()
        return count

    async def abandon_jobs_for_node(self, node_id: str) -> List[str]:
        """Immediately abandon all CLAIMED/RUNNING jobs held by node_id."""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT job_id, status FROM jobs
            WHERE claimed_by = ? AND status IN ('claimed', 'running')
            """,
            (node_id,),
        )
        rows = await cur.fetchall()
        abandoned = []
        for row in rows:
            jid, old_status = row["job_id"], row["status"]
            await self._db.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (JobStatus.ABANDONED.value, jid),
            )
            await self._audit(
                jid, old_status, JobStatus.ABANDONED.value,
                node_id=node_id, reason="node_declared_dead",
            )
            abandoned.append(jid)
        if abandoned:
            await self._db.commit()
        return abandoned

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_job(self, job_id: str) -> Optional[Job]:
        """Retrieve a job by ID."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        )
        row = await cur.fetchone()
        return _job_from_row(row) if row else None

    # -- v0.13.0 digest helpers ------------------------------------------------

    async def get_digest_jobs(
        self,
        since: datetime,
        limit: int = 50,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return completed and failed jobs since *since* for digest generation."""
        assert self._db is not None
        since_iso = since.isoformat()

        # Completed jobs — completed_at is stored inside the result JSON blob,
        # not as a table column, so we filter by posted_at and parse result.
        cur = await self._db.execute(
            """
            SELECT job_id, job_type, payload, result, tags, posted_at
            FROM jobs
            WHERE status = 'completed'
              AND posted_at > ?
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (since_iso, limit),
        )
        completed = []
        for row in await cur.fetchall():
            result_blob = row["result"]
            result_dict = {}
            completed_at = None
            if result_blob:
                try:
                    result_dict = json.loads(result_blob)
                    completed_at = result_dict.get("completed_at")
                except Exception:
                    pass
            # Skip if result JSON says completed before *since*
            if completed_at:
                try:
                    ca_dt = datetime.fromisoformat(completed_at)
                    if ca_dt < since:
                        continue
                except (ValueError, TypeError):
                    pass
            completed.append({
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "payload": json.loads(row["payload"]) if row["payload"] else {},
                "result": result_dict,
                "completed_at": completed_at,
                "tags": json.loads(row["tags"]) if row["tags"] else {},
            })

        # Failed / abandoned jobs
        cur = await self._db.execute(
            """
            SELECT job_id, job_type, payload, result, tags, posted_at
            FROM jobs
            WHERE status IN ('failed', 'abandoned')
              AND posted_at > ?
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (since_iso, limit),
        )
        failed = []
        for row in await cur.fetchall():
            result_blob = row["result"]
            error = ""
            if result_blob:
                try:
                    error = json.loads(result_blob).get("error", "")
                except Exception:
                    pass
            failed.append({
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "payload": json.loads(row["payload"]) if row["payload"] else {},
                "error": error,
                "tags": json.loads(row["tags"]) if row["tags"] else {},
            })

        return {"completed": completed, "failed": failed}

    async def get_queued_jobs_sorted(self, now: datetime) -> List[Job]:
        """Return QUEUED jobs ordered by composite priority key."""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued'
            ORDER BY priority DESC, posted_at ASC
            """
        )
        rows = await cur.fetchall()
        jobs = [_job_from_row(r) for r in rows]
        # Re-sort with deadline urgency boost
        jobs.sort(
            key=lambda j: (-deadline_urgency(j, now), -j.priority.value, j.posted_at),
        )
        return jobs

    async def get_jobs_by_status(self, status: JobStatus) -> List[Job]:
        """Return all jobs with a given status."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?", (status.value,)
        )
        rows = await cur.fetchall()
        return [_job_from_row(r) for r in rows]

    async def get_queue_stats(self) -> QueueStats:
        """Return counts per status and per job type."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        )
        by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}

        cur2 = await self._db.execute(
            "SELECT job_type, COUNT(*) as cnt FROM jobs GROUP BY job_type"
        )
        by_type = {r["job_type"]: r["cnt"] for r in await cur2.fetchall()}

        cur3 = await self._db.execute("SELECT COUNT(*) as cnt FROM workers")
        total_workers = (await cur3.fetchone())["cnt"]

        cur4 = await self._db.execute(
            "SELECT COUNT(*) as cnt FROM workers WHERE available = 1 AND load < 1.0"
        )
        available_workers = (await cur4.fetchone())["cnt"]

        return QueueStats(
            by_status=by_status,
            by_type=by_type,
            total_workers=total_workers,
            available_workers=available_workers,
        )

    async def get_audit_log(
        self,
        job_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """Retrieve audit log entries, optionally filtered by job_id."""
        assert self._db is not None
        if job_id:
            cur = await self._db.execute(
                "SELECT * FROM job_audit WHERE job_id = ? ORDER BY timestamp DESC LIMIT ?",
                (job_id, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM job_audit ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        return [
            AuditEntry(
                id=r["id"],
                job_id=r["job_id"],
                timestamp=_parse_dt(r["timestamp"]) or datetime.now(timezone.utc),
                from_status=r["from_status"],
                to_status=r["to_status"],
                node_id=r["node_id"],
                reason=r["reason"],
                details=json.loads(r["details"] or "{}"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Worker registry
    # ------------------------------------------------------------------

    async def register_worker(self, caps: WorkerCapabilities) -> None:
        """Register or update a worker node."""
        assert self._db is not None
        now = _now_iso()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO workers
                (node_id, capabilities, capacity, max_concurrent, job_types,
                 available, load, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                caps.node_id,
                json.dumps(sorted(caps.capabilities)),
                json.dumps(caps.capacity),
                caps.max_concurrent,
                json.dumps([jt.value for jt in caps.job_types]),
                int(caps.available),
                caps.load,
                caps.registered_at.isoformat() if caps.registered_at else now,
                now,
            ),
        )
        await self._db.commit()

    async def deregister_worker(self, node_id: str) -> None:
        """Remove a worker from the registry."""
        assert self._db is not None
        await self._db.execute("DELETE FROM workers WHERE node_id = ?", (node_id,))
        await self._db.commit()

    async def update_worker_load(self, node_id: str, load: float) -> None:
        """Update the load factor for a worker."""
        assert self._db is not None
        now = _now_iso()
        await self._db.execute(
            "UPDATE workers SET load = ?, last_seen = ? WHERE node_id = ?",
            (load, now, node_id),
        )
        await self._db.commit()

    async def get_available_workers(self) -> List[WorkerCapabilities]:
        """Return all workers that are available and not at capacity."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM workers WHERE available = 1 AND load < 1.0"
        )
        rows = await cur.fetchall()
        return [_worker_from_row(r) for r in rows]

    async def get_all_workers(self) -> List[WorkerCapabilities]:
        """Return all registered workers."""
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM workers")
        rows = await cur.fetchall()
        return [_worker_from_row(r) for r in rows]

    async def notify_worker(self, worker_id: str, job_id: str) -> None:
        """Hint to a worker that a job is available. No-op in pull-based model."""
        logger.debug("notify_worker: worker=%s job=%s", worker_id, job_id)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def prune_old_jobs(
        self,
        completed_retention_days: int = 30,
        failed_retention_days: int = 7,
    ) -> int:
        """Delete terminal jobs older than the retention window. Returns count."""
        assert self._db is not None
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        c_cutoff = (now - timedelta(days=completed_retention_days)).isoformat()
        f_cutoff = (now - timedelta(days=failed_retention_days)).isoformat()

        cur = await self._db.execute(
            """
            DELETE FROM jobs
            WHERE (status = 'completed' AND posted_at < ?)
               OR (status IN ('failed', 'cancelled', 'abandoned') AND posted_at < ?)
            """,
            (c_cutoff, f_cutoff),
        )
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _audit(
        self,
        job_id: str,
        from_status: Optional[str],
        to_status: str,
        node_id: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict] = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO job_audit (job_id, timestamp, from_status, to_status, node_id, reason, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                _now_iso(),
                from_status,
                to_status,
                node_id,
                reason,
                json.dumps(details or {}),
            ),
        )


# ---------------------------------------------------------------------------
# Priority / type maps for the API layer
# ---------------------------------------------------------------------------

_PRIORITY_MAP: Dict[str, JobPriority] = {
    "low": JobPriority.LOW,
    "normal": JobPriority.NORMAL,
    "high": JobPriority.HIGH,
    "critical": JobPriority.CRITICAL,
}

_TYPE_MAP: Dict[str, JobType] = {jt.value: jt for jt in JobType}


class TaskQueueManager:
    """Singleton facade over QueueManager for the Colony API layer.

    Provides the ``list_tasks()`` / ``submit()`` interface expected by the
    API tasks router, translating between the API's string-based model and
    QueueManager's typed Job model.

    Usage::

        # At startup (called by server lifespan):
        mgr = await TaskQueueManager.initialize()

        # In request handlers:
        mgr = TaskQueueManager.get_instance()
        result = await mgr.submit(task_type="inference", priority="normal", params={...})
    """

    _instance: "Optional[TaskQueueManager]" = None

    def __init__(self, db_path: Path, event_bus: Optional[Any] = None) -> None:
        self.queue = QueueManager(db_path=db_path, event_bus=event_bus)

    @classmethod
    def get_instance(cls) -> "TaskQueueManager":
        if cls._instance is None:
            raise RuntimeError(
                "TaskQueueManager not initialized. "
                "Ensure the Colony server lifespan has started."
            )
        return cls._instance

    @classmethod
    async def initialize(
        cls,
        db_path: Optional[Path] = None,
        event_bus: Optional[Any] = None,
    ) -> "TaskQueueManager":
        """Create, start, and register the singleton TaskQueueManager."""
        import os

        if db_path is None:
            colony_home = Path(
                os.environ.get("COLONY_HOME", "~/.colony")
            ).expanduser()
            task_db_env = os.environ.get("COLONY_TASK_DB_PATH", "")
            db_path = Path(task_db_env) if task_db_env else colony_home / "task_queue.db"

        instance = cls(db_path=db_path, event_bus=event_bus)
        await instance.queue.start()
        cls._instance = instance
        logger.info("TaskQueueManager initialized at %s", db_path)
        return instance

    async def stop(self) -> None:
        """Stop the underlying QueueManager and clear the singleton."""
        await self.queue.stop()
        TaskQueueManager._instance = None

    async def submit(
        self,
        task_type: str,
        priority: str = "normal",
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        initial_status: Optional[JobStatus] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Submit a job to the SQLite queue.

        ``initial_status`` lets callers create a job directly in a
        non-QUEUED state (e.g. BLOCKED awaiting owner approval) so no
        worker can claim it before the gate is resolved (v0.17.0).

        Returns a task dict compatible with the API response schema.
        """
        job_type = _TYPE_MAP.get(task_type, JobType.CUSTOM)
        job_priority = _PRIORITY_MAP.get(priority, JobPriority.NORMAL)
        job_tags: Dict[str, str] = {"task_type": task_type}
        if idempotency_key:
            job_tags["idempotency_key"] = idempotency_key
        if tags:
            job_tags.update(tags)

        job = Job(
            job_id=str(_uuid_module.uuid4()),
            job_type=job_type,
            payload=params or {},
            priority=job_priority,
            posted_by="api",
            tags=job_tags,
        )
        if initial_status is not None:
            job.status = initial_status

        await self.queue.post(job)
        logger.info("TaskQueueManager submitted job %s type=%s", job.job_id, task_type)

        return {
            "id": job.job_id,
            "type": task_type,
            "status": "pending",
            "priority": priority,
            "params": params or {},
            "idempotency_key": idempotency_key,
            "result": None,
            "error": None,
            "created_at": job.posted_at.isoformat(),
            "started_at": None,
            "completed_at": None,
        }

    async def list_tasks(
        self,
        statuses: Optional[List[str]] = None,
        task_type: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Any:
        """List jobs from the SQLite queue.

        Returns a ListResponse-compatible object.
        """
        from colony_sidecar.api.schemas.common import ListResponse

        all_jobs: List[Job] = []
        if statuses:
            # Map API status strings to JobStatus enum values
            _status_map = {
                "pending": JobStatus.QUEUED,
                "running": JobStatus.RUNNING,
                "completed": JobStatus.COMPLETED,
                "failed": JobStatus.FAILED,
                "cancelled": JobStatus.CANCELLED,
            }
            for s in statuses:
                js = _status_map.get(s)
                if js is not None:
                    all_jobs.extend(await self.queue.get_jobs_by_status(js))
        else:
            for js in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COMPLETED,
                       JobStatus.FAILED, JobStatus.CANCELLED):
                all_jobs.extend(await self.queue.get_jobs_by_status(js))

        items = []
        for job in all_jobs:
            t = job.tags.get("task_type", job.job_type.value)
            if task_type and t != task_type:
                continue
            items.append({
                "id": job.job_id,
                "type": t,
                "status": {
                    JobStatus.QUEUED: "pending",
                    JobStatus.CLAIMED: "pending",
                    JobStatus.BLOCKED: "pending",
                    JobStatus.RUNNING: "running",
                    JobStatus.COMPLETED: "completed",
                    JobStatus.FAILED: "failed",
                    JobStatus.ABANDONED: "failed",
                    JobStatus.CANCELLED: "cancelled",
                }.get(job.status, job.status.value),
                "priority": next(
                    (k for k, v in _PRIORITY_MAP.items() if v == job.priority),
                    "normal",
                ),
                "params": job.payload,
                "result": job.result.output if job.result else None,
                "error": job.result.error if job.result else None,
                "created_at": job.posted_at.isoformat(),
                "started_at": job.result.started_at.isoformat() if (job.result and job.result.started_at) else None,
                "completed_at": job.result.completed_at.isoformat() if (job.result and job.result.completed_at) else None,
            })

        return ListResponse.paginate(items, limit, cursor)

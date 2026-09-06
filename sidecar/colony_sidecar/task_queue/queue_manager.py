"""Persistent job queue backed by SQLite with WAL mode.

Thread-safe for concurrent asyncio tasks via aiosqlite. All
state-change methods are transactional. Emits typed events to the
Colony event bus on each state transition.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import logging
import math
import os
import uuid as _uuid_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import aiosqlite

from colony_sidecar.task_queue.config import (
    DEFAULT_WORKER_HEARTBEAT_TTL_SECS,
    validate_worker_heartbeat_ttl,
)
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
    is_canonical_job_id,
)

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_SOURCE_RUNTIME_HOLD_KIND = "source_runtime"
_SOURCE_RUNTIME_BLOCKED_REASON = "source_runtime_hold"
_SOURCE_RUNTIME_TAGS = frozenset({
    "hold_kind",
    "blocked_reason",
    "source_runtime_hold_reason",
    "source_runtime_held_at",
})


class QueueExecutionUnavailable(RuntimeError):
    """Raised when maintenance authority is not ready to support claims."""


class _ReentrantAsyncLock:
    """Task-reentrant lock for one shared aiosqlite transaction connection."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("queue database operation requires an asyncio task")
        if self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        task = asyncio.current_task()
        if task is not self._owner or self._depth <= 0:
            raise RuntimeError("queue database operation lock ownership was lost")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
        return False


def _serialized_mutation(method):
    """Keep every multi-statement write/rollback isolated on the connection."""

    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._mutation_lock:
            try:
                return await method(self, *args, **kwargs)
            except BaseException as original:
                # No later operation may accidentally commit a partial write
                # left by an exception/cancellation on the shared connection.
                if self._db is not None:
                    rollback = asyncio.create_task(self._db.rollback())
                    cancelled_during_cleanup = False
                    while not rollback.done():
                        try:
                            await asyncio.shield(rollback)
                        except asyncio.CancelledError:
                            # Repeated shutdown cancellation must not release
                            # the connection lock before ROLLBACK is durable.
                            cancelled_during_cleanup = True
                    rollback.result()
                    if (
                        cancelled_during_cleanup
                        and not isinstance(original, asyncio.CancelledError)
                    ):
                        raise asyncio.CancelledError() from original
                raise

    return wrapped


def _serialized_read(method):
    """Keep reads outside every other task's uncommitted transaction view."""

    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._mutation_lock:
            return await method(self, *args, **kwargs)

    return wrapped


def _positive_seconds(value: Any, *, default: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return parsed


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
            claim_attempt_id=r.get("claim_attempt_id"),
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
        claim_attempt_id=(
            row["claim_attempt_id"] if "claim_attempt_id" in row.keys() else None
        ),
        claim_expires_at=_parse_dt(
            row["claim_expires_at"]
            if "claim_expires_at" in row.keys() else None
        ),
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
        "claim_attempt_id": result.claim_attempt_id,
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
        claim_timeout_secs: float = 30.0,
        worker_heartbeat_ttl_secs: float = (
            DEFAULT_WORKER_HEARTBEAT_TTL_SECS
        ),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None
        self._mutation_lock = _ReentrantAsyncLock()
        self._outcome_lock = asyncio.Lock()
        self._outcome_tasks: set[asyncio.Task] = set()
        self._claim_timeout_secs = _positive_seconds(
            claim_timeout_secs,
            default=30.0,
            field="claim_timeout_secs",
        )
        self._worker_heartbeat_ttl_secs = validate_worker_heartbeat_ttl(
            worker_heartbeat_ttl_secs
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_bus = event_bus
        # One mandatory central authority for every claim consumer (HTTP,
        # embedded WorkerNode, mesh, and direct QueueManager callers).
        self._worker_governor: Any = None
        # Optional, synchronous and read-only.  Deployments can install a
        # source-specific runtime rollback fence without changing the legacy
        # queue contract for any job while this callback is absent.
        self._runtime_claim_hold: Optional[Callable[[Job], str]] = None
        self._execution_ready = True
        self._execution_readiness_reason = "standalone_default"
        self._routing_ready = True
        self._routing_readiness_reason = "agent_action_routes_ready"
        self._thought_runtime_ready = False
        self._thought_runtime_node = ""
        self._thought_runtime_reason = "thought_handler_not_registered"
        # Public in-process control facade.  The implementation remains on
        # this manager so WorkControl CAS and job lifecycle transitions share
        # the queue's re-entrant mutation lock and SQLite connection.
        from colony_sidecar.task_queue.work_control import WorkControlService

        self.work_control = WorkControlService(self)

    @property
    def worker_heartbeat_ttl_secs(self) -> float:
        """The single worker-liveness TTL shared by claims and scheduling."""

        return self._worker_heartbeat_ttl_secs

    def configure_worker_heartbeat_ttl(self, value: object) -> None:
        """Apply a validated compatibility override before scheduling starts."""

        self._worker_heartbeat_ttl_secs = validate_worker_heartbeat_ttl(value)

    def configure_runtime_claim_hold(
        self, callback: Optional[Callable[[Job], str]],
    ) -> None:
        """Install the deployment's read-only pre-claim/start hold fence."""

        if callback is not None and not callable(callback):
            raise TypeError("runtime claim hold callback must be callable")
        self._runtime_claim_hold = callback

    def _worker_now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("worker liveness clock must return a datetime")
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _worker_now_iso(self) -> str:
        return self._worker_now().isoformat()

    def _worker_last_seen_is_fresh(
        self,
        last_seen: object,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        parsed = _parse_dt(last_seen if isinstance(last_seen, str) else None)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        current = now or self._worker_now()
        cutoff = current - timedelta(
            seconds=self._worker_heartbeat_ttl_secs
        )
        # Match the queue's existing job-heartbeat timeout boundary: a row
        # becomes stale only after it is strictly older than the cutoff.
        return parsed >= cutoff

    async def _worker_truth_snapshot(self) -> Dict[str, Any]:
        """Classify durable registry rows with the canonical heartbeat TTL."""

        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT available, load, last_seen FROM workers"
        )
        rows = await cursor.fetchall()
        now = self._worker_now()
        active_workers = 0
        available_workers = 0
        for row in rows:
            fresh = self._worker_last_seen_is_fresh(
                row["last_seen"], now=now
            )
            if not fresh:
                continue
            active_workers += 1
            if bool(row["available"]) and float(row["load"]) < 1.0:
                available_workers += 1
        registered_workers = len(rows)
        return {
            "registered_workers": registered_workers,
            "active_workers": active_workers,
            "stale_workers": registered_workers - active_workers,
            "available_workers": available_workers,
            "worker_heartbeat_ttl_secs": self._worker_heartbeat_ttl_secs,
        }

    def configure_governance(self, governor: Any) -> None:
        """Atomically replace the process-local worker authority handle."""

        self._worker_governor = governor

    def set_execution_ready(self, ready: bool, reason: str = "") -> None:
        """Gate every claim on canonical scheduler/maintenance readiness."""

        if ready and not self._routing_ready:
            self._execution_ready = False
            self._execution_readiness_reason = self._routing_readiness_reason
            return
        self._execution_ready = bool(ready)
        self._execution_readiness_reason = str(
            reason or ("ready" if ready else "maintenance_unavailable")
        )[:500]

    def set_thought_runtime_ready(
        self,
        ready: bool,
        *,
        node_id: str = "",
        reason: str = "",
    ) -> bool:
        """Compare-and-set Thought execution readiness for the exact owner."""

        expected = os.environ.get(
            "COLONY_THOUGHT_WORKER_NODE_ID", ""
        ).strip()
        node = str(node_id or "").strip()
        if ready:
            if not expected or node != expected:
                # A spoofed/misconfigured worker must not evict an already
                # healthy cognition owner or overwrite its readiness reason.
                return False
            self._thought_runtime_ready = True
            self._thought_runtime_node = node
            self._thought_runtime_reason = "thought_handler_ready"
            return True

        if self._thought_runtime_node and node != self._thought_runtime_node:
            return False
        self._thought_runtime_ready = False
        self._thought_runtime_node = ""
        if not expected:
            self._thought_runtime_reason = "thought_owner_not_configured"
        else:
            self._thought_runtime_reason = str(
                reason or "thought_handler_not_registered"
            )[:500]
        return True

    def execution_readiness(self) -> Dict[str, Any]:
        return {
            "ready": self._execution_ready,
            "reason": self._execution_readiness_reason,
            "routing_ready": self._routing_ready,
            "routing_reason": self._routing_readiness_reason,
            "typed_routes": {
                "thought": {
                    "ready": self._thought_runtime_ready,
                    "node_id": self._thought_runtime_node or None,
                    "reason": self._thought_runtime_reason,
                },
            },
        }

    def governance_configuration(
        self,
        worker_truth: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        from colony_sidecar.task_queue.governor import workers_mode

        mode = workers_mode()
        governor = self._worker_governor
        ready = False
        if governor is not None:
            try:
                ready = governor.ready_for_live_claims() is True
            except Exception:
                ready = False
        status = {
            "mode": mode,
            "governor_attached": governor is not None,
            "ready_for_live_claims": ready,
            "claim_path": "queue_manager_atomic",
            "execution_ready": self._execution_ready,
            "execution_readiness_reason": self._execution_readiness_reason,
            # The scheduler/maintenance gate is kept explicit because it must
            # become ready before an embedded worker can register at startup.
            "maintenance_ready": self._execution_ready,
            "maintenance_readiness_reason": self._execution_readiness_reason,
        }
        if worker_truth is not None:
            status.update(worker_truth)
            has_active_worker = (
                int(worker_truth.get("active_workers") or 0) > 0
            )
            status["execution_ready"] = (
                self._execution_ready and has_active_worker
            )
            if not self._execution_ready:
                status["execution_readiness_reason"] = (
                    self._execution_readiness_reason
                )
            elif not has_active_worker:
                status["execution_readiness_reason"] = "no_fresh_workers"
        return status

    @_serialized_read
    async def governance_status(
        self,
        worker_truth: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Operational view of central authority and distinct held jobs."""

        status = self.governance_configuration(
            (
                worker_truth
                if worker_truth is not None
                else await self._worker_truth_snapshot()
            )
        )
        holds: Dict[str, int] = {}
        held_jobs: List[Dict[str, str]] = []
        if self._db is not None:
            cursor = await self._db.execute(
                """
                SELECT job_id, job_type, posted_at, tags
                FROM jobs WHERE status = ? ORDER BY rowid DESC
                """,
                (JobStatus.BLOCKED.value,),
            )
            for row in await cursor.fetchall():
                try:
                    tags = json.loads(row["tags"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    tags = {}
                kind = str(tags.get("hold_kind") or "legacy_blocked")
                holds[kind] = holds.get(kind, 0) + 1
                if (
                    kind in {"governor_unavailable", "boundary", "trust"}
                    and len(held_jobs) < 100
                ):
                    held_jobs.append({
                        "job_id": str(row["job_id"]),
                        "job_type": str(row["job_type"]),
                        "hold_kind": kind,
                        "reason": str(tags.get("governor_reason") or "")[:500],
                        "posted_at": str(row["posted_at"] or ""),
                    })
        status["holds"] = holds
        status["held_total"] = sum(holds.values())
        status["governance_held_jobs"] = held_jobs
        status["governance_held_jobs_truncated"] = (
            sum(holds.get(kind, 0) for kind in (
                "governor_unavailable", "boundary", "trust")) > len(held_jobs)
        )
        if self._db is not None:
            cursor = await self._db.execute(
                """SELECT COUNT(*) AS pending,
                          MAX(delivery_attempts) AS max_attempts,
                          MAX(last_error) AS last_error
                   FROM worker_outcome_outbox WHERE state = 'pending'"""
            )
            outcome = await cursor.fetchone()
            status["outcome_reconciliation"] = {
                "pending": int(outcome["pending"] or 0),
                "max_attempts": int(outcome["max_attempts"] or 0),
                "last_error": outcome["last_error"],
            }
        return status

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
        # These migrations are execution fences, not background cleanup.
        # They must converge persisted retry/control state before start()
        # returns and any worker is able to claim or poll work.
        await self.reconcile_legacy_effect_retries()
        await self.reconcile_work_control_delivery_state()
        await self._reconcile_persisted_agent_action_routes()
        await self._reconcile_persisted_thought_routes()

    @_serialized_mutation
    async def stop(self) -> None:
        """Close the database connection."""
        tasks = list(self._outcome_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._outcome_tasks.clear()
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
        # Additive migration for pre-attempt queue databases. SQLite's CREATE
        # TABLE IF NOT EXISTS does not add columns to an existing table.
        migrations = {
            "jobs": {
                "claim_attempt_id": "TEXT",
                "claim_expires_at": "TEXT",
            },
            "job_audit": {"claim_attempt_id": "TEXT"},
            "heartbeats": {"claim_attempt_id": "TEXT"},
            "worker_outcome_outbox": {
                "worker_mode": "TEXT NOT NULL DEFAULT 'off'",
                "success_attested": "INTEGER NOT NULL DEFAULT 0",
            },
            "work_control_operations": {
                "ack_deadline": "TEXT",
                "ack_authority": "TEXT NOT NULL DEFAULT '{}'",
            },
        }
        for table, additions in migrations.items():
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            for name, sql_type in additions.items():
                if name not in columns:
                    await self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                    )
        await self._db.commit()

    async def _reconcile_persisted_agent_action_routes(self) -> None:
        """Bind inactive legacy rows; quarantine ambiguity; stop on active drift.

        Active attempts are never rewritten during startup.  A deploy must
        drain them before changing the attempt/routing contract, otherwise a
        newly configured executor could inherit work claimed under different
        authority.
        """

        assert self._db is not None
        from colony_sidecar.task_queue.routing import (
            AGENT_ACTION_ROUTE_CAPABILITIES,
            bind_agent_action_routes,
            expected_agent_action_routes,
        )
        from colony_sidecar.task_queue.governor import job_declares_effect

        missing_attempt_cursor = await self._db.execute(
            """SELECT job_id FROM jobs
               WHERE status IN (?, ?)
                 AND (claim_attempt_id IS NULL OR claim_attempt_id = '')""",
            (JobStatus.CLAIMED.value, JobStatus.RUNNING.value),
        )
        active_legacy: List[str] = [
            str(row["job_id"])
            for row in await missing_attempt_cursor.fetchall()
        ]

        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE job_type = ?",
            (JobType.AGENT_ACTION.value,),
        )
        rows = await cursor.fetchall()
        dirty = False
        for row in rows:
            job = _job_from_row(row)
            names = {
                capability.name for capability in (job.capabilities or [])
            }
            supplied = names & AGENT_ACTION_ROUTE_CAPABILITIES
            try:
                expected = set(expected_agent_action_routes(job))
            except ValueError as exc:
                expected = set()
                route_error = str(exc)
            else:
                route_error = ""

            if job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}:
                expected_job = copy.deepcopy(job)
                try:
                    bind_agent_action_routes(expected_job)
                except ValueError:
                    expected_route = ""
                    expected_owner = ""
                else:
                    expected_route = str(
                        expected_job.tags.get("agent_action_route") or ""
                    )
                    expected_owner = str(
                        expected_job.tags.get("agent_action_route_node") or ""
                    )
                actual_route = str(
                    job.tags.get("agent_action_route") or ""
                )
                actual_owner = str(
                    job.tags.get("agent_action_route_node") or ""
                )
                if (
                    not job.claim_attempt_id
                    or supplied != expected
                    or not expected
                    or _serialize_caps(job.capabilities)
                    != _serialize_caps(expected_job.capabilities)
                    or json.dumps(job.payload, sort_keys=True)
                    != json.dumps(expected_job.payload, sort_keys=True)
                    or actual_route != expected_route
                    or actual_owner != expected_owner
                    or json.dumps(job.tags, sort_keys=True)
                    != json.dumps(expected_job.tags, sort_keys=True)
                    or (
                        job_declares_effect(expected_job)
                        and not self._server_approval_provenance_valid(
                            expected_job
                        )
                    )
                ):
                    if job.job_id not in active_legacy:
                        active_legacy.append(job.job_id)
                continue
            if job.status not in {
                JobStatus.QUEUED,
                JobStatus.BLOCKED,
                JobStatus.ABANDONED,
            }:
                continue

            reconciled_job = copy.deepcopy(job)
            if not route_error:
                try:
                    bind_agent_action_routes(
                        reconciled_job,
                        allow_legacy_migration=True,
                    )
                except ValueError as exc:
                    route_error = str(exc)
            if route_error:
                tags = dict(job.tags)
                tags.update({
                    "hold_kind": "route_migration",
                    "blocked_reason": "agent_action_route_unresolved",
                    "agent_action_route_error": route_error[:500],
                })
                await self._db.execute(
                    "UPDATE jobs SET status = ?, tags = ? WHERE job_id = ?",
                    (
                        JobStatus.BLOCKED.value,
                        json.dumps(tags),
                        job.job_id,
                    ),
                )
                await self._audit(
                    job.job_id,
                    job.status.value,
                    JobStatus.BLOCKED.value,
                    reason="agent_action_route_quarantined",
                )
                dirty = True
                continue

            if (
                job_declares_effect(reconciled_job)
                and not self._server_approval_provenance_valid(
                    reconciled_job
                )
            ):
                self._hold_for_missing_approval(reconciled_job)

            prior_caps = _serialize_caps(job.capabilities)
            prior_tags = json.dumps(job.tags, sort_keys=True)
            prior_payload = json.dumps(job.payload, sort_keys=True)
            prior_status = job.status
            job = reconciled_job
            if (
                _serialize_caps(job.capabilities) == prior_caps
                and json.dumps(job.tags, sort_keys=True) == prior_tags
                and json.dumps(job.payload, sort_keys=True) == prior_payload
                and job.status is prior_status
            ):
                continue
            await self._db.execute(
                """UPDATE jobs SET status = ?, payload = ?,
                          capabilities = ?, tags = ?
                   WHERE job_id = ?""",
                (
                    job.status.value,
                    json.dumps(job.payload),
                    _serialize_caps(job.capabilities),
                    json.dumps(job.tags),
                    job.job_id,
                ),
            )
            await self._audit(
                job.job_id,
                prior_status.value,
                job.status.value,
                reason="agent_action_route_reconciled",
            )
            dirty = True

        if active_legacy:
            self._routing_ready = False
            self._routing_readiness_reason = (
                "incompatible_active_attempts:"
                + ",".join(sorted(active_legacy)[:20])
            )[:500]
            self._execution_ready = False
            self._execution_readiness_reason = self._routing_readiness_reason
        if dirty:
            await self._db.commit()

    async def _reconcile_persisted_thought_routes(self) -> None:
        """Canonicalize inactive ThoughtJobV1 rows; hold active drift."""

        assert self._db is not None
        from colony_sidecar.task_queue.routing import bind_thought_route

        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE job_type = ?",
            (JobType.THOUGHT.value,),
        )
        active_drift: List[str] = []
        dirty = False
        for row in await cursor.fetchall():
            job = _job_from_row(row)
            canonical = copy.deepcopy(job)
            try:
                bind_thought_route(canonical)
            except ValueError as exc:
                route_error = str(exc)
            else:
                route_error = ""

            if job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}:
                if (
                    route_error
                    or not job.claim_attempt_id
                    or _serialize_caps(job.capabilities)
                    != _serialize_caps(canonical.capabilities)
                    or str(job.tags.get("thought_route") or "")
                    != str(canonical.tags.get("thought_route") or "")
                    or str(job.tags.get("thought_route_node") or "")
                    != str(canonical.tags.get("thought_route_node") or "")
                    or json.dumps(job.tags, sort_keys=True)
                    != json.dumps(canonical.tags, sort_keys=True)
                ):
                    active_drift.append(job.job_id)
                continue
            if job.status not in {
                JobStatus.QUEUED, JobStatus.BLOCKED, JobStatus.ABANDONED,
            }:
                continue
            if route_error:
                tags = dict(job.tags)
                tags.update({
                    "hold_kind": "route_migration",
                    "blocked_reason": "thought_route_unresolved",
                    "thought_route_error": route_error[:500],
                })
                await self._db.execute(
                    "UPDATE jobs SET status = ?, tags = ? WHERE job_id = ?",
                    (JobStatus.BLOCKED.value, json.dumps(tags), job.job_id),
                )
                await self._audit(
                    job.job_id,
                    job.status.value,
                    JobStatus.BLOCKED.value,
                    reason="thought_route_quarantined",
                )
                dirty = True
                continue
            if (
                _serialize_caps(job.capabilities)
                == _serialize_caps(canonical.capabilities)
                and json.dumps(job.tags, sort_keys=True)
                == json.dumps(canonical.tags, sort_keys=True)
            ):
                continue
            await self._db.execute(
                "UPDATE jobs SET capabilities = ?, tags = ? WHERE job_id = ?",
                (
                    _serialize_caps(canonical.capabilities),
                    json.dumps(canonical.tags),
                    job.job_id,
                ),
            )
            await self._audit(
                job.job_id,
                job.status.value,
                job.status.value,
                reason="thought_route_reconciled",
            )
            dirty = True

        if active_drift:
            self._routing_ready = False
            self._routing_readiness_reason = (
                "incompatible_active_thought_attempts:"
                + ",".join(sorted(active_drift)[:20])
            )[:500]
            self._execution_ready = False
            self._execution_readiness_reason = self._routing_readiness_reason
        if dirty:
            await self._db.commit()

    # ------------------------------------------------------------------
    # Job submission
    # ------------------------------------------------------------------

    @staticmethod
    def _server_approval_provenance_valid(job: Job) -> bool:
        """Verify durable owner/grant evidence or recomputable policy output."""

        # ApprovalRelayCanaryV1 is a calibration object, never executable
        # authority.  Even a durable APPROVE or a matching bounded grant must
        # remain invalid at the worker-claim boundary.
        from colony_sidecar.task_queue.approval_relay_canary import (
            is_exact_job as is_exact_approval_relay_canary,
        )

        if is_exact_approval_relay_canary(job):
            return False

        tags = getattr(job, "tags", {}) or {}
        approved_by = str(tags.get("approved_by") or "").strip()
        policy = str(tags.get("auto_approved_by_policy") or "").strip()

        try:
            from colony_sidecar.initiatives.approval_authority import (
                build_action_binding,
            )

            current_binding = build_action_binding(
                job_id=job.job_id,
                job_type=job.job_type.value,
                payload=job.payload,
            )
        except Exception:
            return False
        captured_digest = str(tags.get("action_digest") or "").strip()
        if captured_digest and captured_digest != current_binding.action_digest:
            return False

        if not approved_by and policy != "bounded_grant":
            # Policy classification is not execution authority. Read-only
            # jobs never enter this verifier because they declare no effect;
            # every mutation/disclosure/outbound effect must bind to the
            # canonical direct-decision or exact grant-use ledger below.
            return False

        try:
            from colony_sidecar.initiatives.approval_authority import (
                ApprovalAuthorityStore,
                build_action_binding,
            )

            binding = current_binding
            if str(tags.get("action_digest") or "") != binding.action_digest:
                return False
            store = ApprovalAuthorityStore()
            direct_request = store.get_request_for_job(job.job_id)
            if policy == "bounded_grant":
                # Historical partial transitions may contain both a target
                # request and a grant use. The target's first-valid direct
                # request always wins; grant tags cannot bypass it.
                if direct_request is not None:
                    return False
                use = store.get_grant_use(binding.action_digest)
                return bool(
                    use is not None
                    and str(use.get("grant_id") or "")
                    == str(tags.get("bounded_grant_id") or "")
                    and str(use.get("operation_id") or "") == job.job_id
                )

            request_id = str(tags.get("approval_request_id") or "")
            request = store.get_request(request_id) if request_id else None
            return bool(
                request is not None
                and direct_request is not None
                and direct_request.get("request_id") == request.get("request_id")
                and request.get("status") == "approved"
                and request.get("decision") == "approve"
                and str(request.get("job_id") or "") == job.job_id
                and str(request.get("action_digest") or "")
                == binding.action_digest
                and str(request.get("decision_id") or "")
                == str(tags.get("approval_decision_id") or "")
                and str(request.get("decided_by") or "") == approved_by
            )
        except Exception:
            return False

    @staticmethod
    def _hold_for_missing_approval(job: Job) -> None:
        for key in (
            "approved_by", "approved_at", "auto_approved_by_policy",
            "bounded_grant_id", "bounded_grant_expires_at",
            "bounded_grant_ttl_state", "bounded_grant_uses_state",
            "approval_source_request_id", "approval_decision_id",
            "approval_provenance",
            "outbound_target",
        ):
            job.tags.pop(key, None)
        job.status = JobStatus.BLOCKED
        job.tags.update({
            "hold_kind": "approval",
            "blocked_reason": "awaiting_owner_approval",
            "awaiting_owner_approval": "true",
        })
        posted_at = job.posted_at
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        else:
            posted_at = posted_at.astimezone(timezone.utc)
        job.tags.setdefault("approval_requested_at", posted_at.isoformat())

    @staticmethod
    def _materialize_effect_approval(job: Job) -> None:
        """Resolve canonical authority before an approval-held row is visible."""

        from colony_sidecar.initiatives.approval_authority import (
            ApprovalAuthorityStore,
            build_action_binding,
            build_approval_presentation,
            prepare_action_approval,
        )
        from colony_sidecar.task_queue.approval_relay_canary import (
            is_exact_job as is_exact_approval_relay_canary,
        )

        store = ApprovalAuthorityStore()
        relay_canary = is_exact_approval_relay_canary(job)
        if relay_canary:
            from colony_sidecar.task_queue.approval_relay_canary import (
                APPROVAL_TTL_SECONDS,
            )

            # The canary deliberately bypasses reusable grants.  Creating its
            # exact request first preserves queue-first crash safety while
            # ensuring resolve_action_gate() can only reuse that request. Its
            # attended calibration window is deliberately shorter than the
            # generic approval lifetime; an idempotent replay keeps the exact
            # original request and expiry.
            binding = build_action_binding(
                job_id=job.job_id,
                job_type=job.job_type.value,
                payload=job.payload,
            )
            presentation = build_approval_presentation(
                job_id=job.job_id,
                job_type=job.job_type.value,
                payload=job.payload,
                deadline=job.deadline,
            )
            store.ensure_request(
                job_id=job.job_id,
                binding=binding,
                ttl_seconds=APPROVAL_TTL_SECONDS,
                presentation=presentation,
            )
        authority = prepare_action_approval(
            store,
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
            deadline=job.deadline,
            approval_started_at=job.posted_at,
        )
        job.tags.update(authority["tags"])
        state = authority["state"]
        if relay_canary:
            for key in (
                "auto_approved_by_policy", "bounded_grant_id",
                "bounded_grant_expires_at", "approval_source_request_id",
                "bounded_grant_ttl_state", "bounded_grant_uses_state",
            ):
                job.tags.pop(key, None)
            if state == "pending":
                return
            if state in {"authorized_direct", "rejected"}:
                winner = authority["request"] or {}
                decision = str(winner.get("decision") or "")
                if decision not in {"approve", "reject"}:
                    raise RuntimeError(
                        "approval relay canary winner has an invalid decision"
                    )
                for key in (
                    "hold_kind", "blocked_reason", "awaiting_owner_approval",
                ):
                    job.tags.pop(key, None)
                job.tags.update({
                    "approval_relay_canary_decision": decision,
                    "approval_relay_canary_terminalized": "true",
                    "external_effect": "false",
                })
                job.status = JobStatus.CANCELLED
                return
            if state == "authorized_grant":
                # This should be unreachable because ensure_request() wins
                # before grant resolution.  Keep an older/corrupt ledger
                # fail-closed rather than ever projecting QUEUED.
                job.status = JobStatus.BLOCKED
                job.tags.update({
                    "hold_kind": "approval",
                    "blocked_reason": "approval_relay_canary_grant_forbidden",
                    "awaiting_owner_approval": "true",
                })
                return
            if state in {"expired", "superseded"}:
                for key in (
                    "hold_kind", "blocked_reason", "awaiting_owner_approval",
                ):
                    job.tags.pop(key, None)
                job.status = JobStatus.FAILED
                return
            raise RuntimeError(
                "approval relay canary resolver returned an invalid state"
            )
        if state == "pending":
            return
        for key in ("hold_kind", "blocked_reason", "awaiting_owner_approval"):
            job.tags.pop(key, None)
        if state in {"authorized_grant", "authorized_direct"}:
            job.status = JobStatus.QUEUED
        elif state == "rejected":
            job.status = JobStatus.CANCELLED
        elif state in {"expired", "superseded"}:
            job.status = JobStatus.FAILED
        else:
            raise RuntimeError("canonical approval resolver returned an invalid state")

    @_serialized_mutation
    async def repair_approval_relay_canary_terminal(self, job_id: str) -> bool:
        """Converge an exact decided canary to inert CANCELLED state.

        This is intentionally stronger than the generic status transition:
        it repairs historical QUEUED/CLAIMED crash-window rows, clears every
        claim lease, and derives the winner only from the canonical approval
        ledger.  Pending/expired canaries are not reported as successful
        decisions.
        """

        assert self._db is not None
        from colony_sidecar.task_queue.approval_relay_canary import (
            assert_exact_job,
        )

        job = await self.get_job(job_id)
        if job is None:
            return False
        assert_exact_job(job)
        old_status = job.status
        old_attempt_id = job.claim_attempt_id
        self._materialize_effect_approval(job)
        if job.status is not JobStatus.CANCELLED:
            return False
        decision = str(
            job.tags.get("approval_relay_canary_decision") or ""
        )
        if decision not in {"approve", "reject"}:
            return False
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, tags = ?, claimed_by = NULL, claimed_at = NULL,
                claim_attempt_id = NULL, claim_expires_at = NULL,
                last_heartbeat = NULL
            WHERE job_id = ?
            """,
            (JobStatus.CANCELLED.value, json.dumps(job.tags), job_id),
        )
        if old_status is not JobStatus.CANCELLED:
            await self._audit(
                job_id,
                old_status.value,
                JobStatus.CANCELLED.value,
                reason=f"approval_relay_canary_{decision}_no_effect_repaired",
            )
        if old_status in {JobStatus.CLAIMED, JobStatus.RUNNING}:
            await self._finalize_pending_controls_after_transition(
                job_id,
                old_attempt_id,
                reason="approval_relay_canary_terminal_repair",
            )
        await self._db.commit()
        return True

    @_serialized_mutation
    async def ensure_approval_relay_canary(
        self, idempotency_digest: str,
    ) -> tuple[Job, bool]:
        """Create or return one exact, server-owned inert relay canary.

        The reentrant queue mutation lock makes the read/create pair atomic
        inside the sole queue owner.  A deterministic primary key provides a
        second idempotency boundary at SQLite even if this method is retried
        after an ambiguous HTTP outcome.
        """

        from colony_sidecar.task_queue.approval_relay_canary import (
            assert_exact_job,
            build_job,
            job_id_for_digest,
        )

        identifier = job_id_for_digest(idempotency_digest)
        existing = await self.get_job(identifier)
        if existing is not None:
            assert_exact_job(existing)
            return existing, False

        job = build_job(idempotency_digest)
        await self.post(job)
        stored = await self.get_job(identifier)
        if stored is None:
            raise RuntimeError("approval relay canary was not durably created")
        assert_exact_job(stored)
        return stored, True

    @_serialized_mutation
    async def post(self, job: Job) -> str:
        """Add a new job to the queue. Returns job_id.

        If the job has dependencies, it enters BLOCKED state.
        Validates dependency DAG for cycles.
        """
        assert self._db is not None

        if not is_canonical_job_id(job.job_id):
            raise ValueError(
                "job_id must be a canonical 1..192 character queue identifier"
            )
        retired_cursor = await self._db.execute(
            "SELECT 1 FROM job_tombstones WHERE job_id = ?",
            (job.job_id,),
        )
        if await retired_cursor.fetchone() is not None:
            raise ValueError(
                "job_id is durably retired and cannot be reposted"
            )

        if (
            job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or job.claimed_by is not None
            or job.claimed_at is not None
            or job.claim_attempt_id is not None
            or job.claim_expires_at is not None
            or job.last_heartbeat is not None
        ):
            raise ValueError(
                "active claim state is server-owned and may only be minted "
                "atomically by claim_job"
            )

        if job.job_type is JobType.AGENT_ACTION:
            from colony_sidecar.task_queue.routing import (
                bind_agent_action_routes,
            )
            bind_agent_action_routes(job)
        elif job.job_type is JobType.THOUGHT:
            from colony_sidecar.task_queue.routing import bind_thought_route

            bind_thought_route(job)

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
                    job.tags.setdefault("hold_kind", "dependency")
                    job.tags.setdefault("blocked_reason", "dependencies_pending")

        # The approval ledger is a separate SQLite database. Validate every
        # caller-controlled value needed by the queue INSERT before creating a
        # request or consuming a bounded grant there.
        serialized_payload = json.dumps(job.payload)
        serialized_capabilities = _serialize_caps(job.capabilities)
        serialized_dependencies = json.dumps(job.depends_on)
        serialized_result = (
            _serialize_result(job.result) if job.result else None
        )
        json.dumps(job.tags)

        materialize_effect_approval = False
        dependency_pending = False
        if job.job_type is JobType.AGENT_ACTION:
            from colony_sidecar.task_queue.governor import job_declares_effect

            effectful = job_declares_effect(job)
            dependency_pending = bool(
                job.status is JobStatus.BLOCKED
                and job.tags.get("blocked_reason") == "dependencies_pending"
            )
            has_server_approval = self._server_approval_provenance_valid(job)
            if (
                effectful
                and not has_server_approval
                and job.status not in {
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.NEUTRAL,
                    JobStatus.COMPLETED,
                }
            ):
                self._hold_for_missing_approval(job)
            if (
                effectful
                and job.status is JobStatus.BLOCKED
                and job.tags.get("blocked_reason") == "awaiting_owner_approval"
            ):
                materialize_effect_approval = True

        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, payload, priority, capabilities,
                deadline, max_retries, retry_count, timeout_secs, depends_on,
                posted_by, posted_at, status, claimed_by, claimed_at,
                claim_attempt_id, claim_expires_at,
                last_heartbeat, result, tags
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                job.job_id,
                job.job_type.value,
                serialized_payload,
                job.priority.value,
                serialized_capabilities,
                job.deadline.isoformat() if job.deadline else None,
                job.max_retries,
                job.retry_count,
                job.timeout_secs,
                serialized_dependencies,
                job.posted_by,
                job.posted_at.isoformat(),
                job.status.value,
                job.claimed_by,
                job.claimed_at.isoformat() if job.claimed_at else None,
                job.claim_attempt_id,
                job.claim_expires_at.isoformat() if job.claim_expires_at else None,
                job.last_heartbeat.isoformat() if job.last_heartbeat else None,
                serialized_result,
                json.dumps(job.tags),
            ),
        )
        post_reason = (
            "approval_birth_staged"
            if materialize_effect_approval else "posted"
        )
        await self._audit(job.job_id, None, job.status.value, reason=post_reason)
        await self._db.commit()

        if materialize_effect_approval:
            # Cross-database ordering is intentionally queue-first: a crash
            # can leave a safe unclaimable row for scheduler reconciliation,
            # but can never leave a prompt/grant use without a canonical job.
            before_status = job.status
            self._materialize_effect_approval(job)
            if dependency_pending and job.status is JobStatus.QUEUED:
                job.status = JobStatus.BLOCKED
                job.tags.update({
                    "hold_kind": "dependency",
                    "blocked_reason": "dependencies_pending",
                })
            updated = await self._db.execute(
                "UPDATE jobs SET status=?, tags=? WHERE job_id=? AND status=?",
                (
                    job.status.value,
                    json.dumps(job.tags),
                    job.job_id,
                    JobStatus.BLOCKED.value,
                ),
            )
            if updated.rowcount == 1:
                reason = (
                    "canonical_approval_expired"
                    if job.tags.get("approval_provenance")
                    == "server_deadline_expired"
                    else "approval_authority_materialized"
                )
                await self._audit(
                    job.job_id,
                    before_status.value,
                    job.status.value,
                    reason=reason,
                )
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
            if dep_job is None:
                raise ValueError(
                    f"Dependency {dep_id} does not exist"
                )
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

    def _evaluate_claim_authority(
        self,
        job: Job,
        worker_caps: WorkerCapabilities,
        worker_id: str,
    ) -> tuple[Any, str | None]:
        """Return a strict ClaimVerdict or an authority error code."""

        from colony_sidecar.task_queue.governor import (
            ClaimVerdict,
            workers_mode,
        )

        mode = workers_mode()
        if mode == "off":
            return None, None
        governor = self._worker_governor
        if governor is None:
            return None, "governor_unavailable"
        if mode == "live":
            try:
                if governor.ready_for_live_claims() is not True:
                    return None, "governor_not_live_ready"
            except Exception:
                return None, "governor_not_live_ready"
        try:
            verdict = governor.evaluate_claim(
                job,
                worker_caps.capabilities,
                worker_node_id=worker_id,
            )
        except Exception:
            logger.warning("central worker claim authority failed", exc_info=True)
            return None, "governor_claim_unavailable"
        # Exact type + mode consistency prevents permissive partial dicts,
        # truthy strings, and compatibility authorities from becoming live
        # permission.
        if type(verdict) is not ClaimVerdict or verdict.mode != mode:
            logger.error("central worker claim received malformed authority verdict")
            return None, "governor_verdict_invalid"
        return verdict, None

    @staticmethod
    def _claim_evidence_tags(
        tags: Dict[str, str],
        *,
        mode: str,
        verdict: Any = None,
        error: str | None = None,
    ) -> Dict[str, str]:
        updated = dict(tags)
        for key in (
            "governor_mode", "governor_enforced", "governor_would_refuse",
            "governor_reason", "governor_boundary_ok",
            "governor_capability_ok", "governor_trust_ok",
            "governor_trust_reason", "governor_error",
            "worker_authority_mode", "worker_authority_principal",
            "worker_authority_credential", "worker_authority_would_deny",
        ):
            updated.pop(key, None)
        updated["governor_mode"] = mode
        if error:
            updated.update({
                "governor_enforced": "false",
                "governor_would_refuse": "true",
                "governor_reason": error,
                "governor_error": error,
            })
        elif verdict is not None:
            updated.update({
                "governor_enforced": str(verdict.enforced).lower(),
                "governor_would_refuse": str(verdict.would_refuse).lower(),
                "governor_reason": str(verdict.reason)[:500],
                "governor_boundary_ok": str(verdict.boundary_ok).lower(),
                "governor_capability_ok": str(verdict.capability_ok).lower(),
                "governor_trust_ok": str(verdict.trust_ok).lower(),
                "governor_trust_reason": str(verdict.trust_reason)[:500],
            })
        return updated

    async def _hold_queued_for_governance(
        self,
        job: Job,
        *,
        hold_kind: str,
        reason: str,
        mode: str = "live",
    ) -> bool:
        """Hold an unclaimed queued job with no stale lease fields."""

        assert self._db is not None
        tags = self._claim_evidence_tags(
            job.tags, mode=mode, error=reason)
        tags.update({
            "hold_kind": hold_kind,
            "blocked_reason": (
                "awaiting_owner_approval"
                if hold_kind == "trust" else hold_kind
            ),
            "governor_reason": reason[:500],
            "governor_last_recheck_at": _now_iso(),
        })
        if hold_kind == "trust":
            tags.setdefault("approval_requested_at", _now_iso())
        cursor = await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = NULL, claimed_at = NULL,
                last_heartbeat = NULL, tags = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                JobStatus.BLOCKED.value,
                json.dumps(tags),
                job.job_id,
                JobStatus.QUEUED.value,
            ),
        )
        if cursor.rowcount != 1:
            return False
        await self._audit(
            job.job_id,
            JobStatus.QUEUED.value,
            JobStatus.BLOCKED.value,
            reason=f"{hold_kind}:{reason}",
        )
        await self._db.commit()
        return True

    @_serialized_mutation
    async def reconcile_governance_holds(self, *, force: bool = False) -> int:
        """Release eligible authority holds exactly once.

        Off/shadow rollback releases immediately. Live mode re-evaluates only
        through a healthy strict governor; explicit boundary holds remain until
        the directive is lifted. Dependency and approval holds are untouched.
        """

        assert self._db is not None
        from datetime import timedelta
        from colony_sidecar.task_queue.governor import ClaimVerdict, workers_mode

        mode = workers_mode()
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?",
            (JobStatus.BLOCKED.value,),
        )
        now = datetime.now(timezone.utc)
        try:
            recheck_seconds = max(1.0, float(
                os.environ.get(
                    "COLONY_WORKER_HOLD_RECHECK_SECONDS", "30")
            ))
        except ValueError:
            recheck_seconds = 30.0
        released = 0
        dirty = False
        for row in await cursor.fetchall():
            job = _job_from_row(row)
            hold_kind = str(job.tags.get("hold_kind") or "")
            if hold_kind not in {
                "governor_unavailable", "boundary", "trust",
                "shadow_effect",
            }:
                continue

            release_reason = ""
            if hold_kind == "shadow_effect":
                if mode != "shadow":
                    release_reason = f"governor_mode_{mode}"
                else:
                    continue
            elif mode != "live":
                release_reason = f"governor_mode_{mode}"
            else:
                last = _parse_dt(job.tags.get("governor_last_recheck_at"))
                if (
                    not force and last is not None
                    and now - last < timedelta(seconds=recheck_seconds)
                ):
                    continue
                required = set(job.required_capabilities())
                extra = str(job.tags.get("required_capability") or "").strip()
                if extra:
                    required.add(extra)
                caps = WorkerCapabilities(
                    node_id="governance-reconciler",
                    capabilities=required,
                    job_types={job.job_type},
                )
                verdict, error = self._evaluate_claim_authority(
                    job, caps, "governance-reconciler")
                job.tags["governor_last_recheck_at"] = now.isoformat()
                if error is not None or type(verdict) is not ClaimVerdict:
                    job.tags["governor_reason"] = str(
                        error or "governor_verdict_invalid")[:500]
                elif (
                    verdict.boundary_ok
                    and verdict.capability_ok
                    and verdict.trust_ok
                ):
                    release_reason = "governor_recheck_allowed"
                else:
                    job.tags["governor_reason"] = str(verdict.reason)[:500]

            if release_reason:
                tags = dict(job.tags)
                for key in (
                    "hold_kind", "blocked_reason", "governor_error",
                    "governor_last_recheck_at",
                ):
                    tags.pop(key, None)
                tags["governor_recovered_at"] = now.isoformat()
                changed = await self._db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, claimed_by = NULL, claimed_at = NULL,
                        last_heartbeat = NULL, tags = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        JobStatus.QUEUED.value,
                        json.dumps(tags),
                        job.job_id,
                        JobStatus.BLOCKED.value,
                    ),
                )
                if changed.rowcount == 1:
                    await self._audit(
                        job.job_id,
                        JobStatus.BLOCKED.value,
                        JobStatus.QUEUED.value,
                        reason=release_reason,
                    )
                    released += 1
                    dirty = True
            elif mode == "live":
                await self._db.execute(
                    "UPDATE jobs SET tags = ? WHERE job_id = ? AND status = ?",
                    (json.dumps(job.tags), job.job_id, JobStatus.BLOCKED.value),
                )
                dirty = True
        if dirty:
            await self._db.commit()
        return released

    def _runtime_claim_hold_reason(self, job: Job) -> Optional[str]:
        """Evaluate the installed source fence; ``None`` means not installed."""

        callback = self._runtime_claim_hold
        if callback is None:
            return None
        try:
            value = callback(job)
            if not isinstance(value, str):
                raise TypeError("runtime claim hold callback must return a string")
            return value.strip()[:500]
        except Exception:
            logger.exception("runtime claim hold callback failed closed")
            return "source_runtime_hold_callback_unavailable"

    async def _hold_queued_for_runtime(self, job: Job, reason: str) -> bool:
        """Atomically hold one still-queued source job before any claim."""

        assert self._db is not None
        tags = dict(job.tags)
        tags.update({
            "hold_kind": _SOURCE_RUNTIME_HOLD_KIND,
            "blocked_reason": _SOURCE_RUNTIME_BLOCKED_REASON,
            "source_runtime_hold_reason": reason[:500],
            "source_runtime_held_at": _now_iso(),
        })
        changed = await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = NULL, claimed_at = NULL,
                claim_attempt_id = NULL, claim_expires_at = NULL,
                last_heartbeat = NULL, tags = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                JobStatus.BLOCKED.value,
                json.dumps(tags),
                job.job_id,
                JobStatus.QUEUED.value,
            ),
        )
        if changed.rowcount != 1:
            return False
        await self._audit(
            job.job_id,
            JobStatus.QUEUED.value,
            JobStatus.BLOCKED.value,
            reason=f"{_SOURCE_RUNTIME_HOLD_KIND}:{reason[:500]}",
        )
        await self._db.commit()
        return True

    @_serialized_mutation
    async def reconcile_runtime_claim_holds(self) -> int:
        """Release only this source fence's exact durable holds.

        A restart before the callback is configured leaves these jobs safely
        blocked.  Once configured, the same immutable job is re-evaluated and
        requeued only when the callback returns an empty reason.  Approval,
        dependency, governor, boundary and every other hold kind are ignored.
        """

        assert self._db is not None
        if self._runtime_claim_hold is None:
            return 0
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?",
            (JobStatus.BLOCKED.value,),
        )
        released = 0
        dirty = False
        for row in await cursor.fetchall():
            job = _job_from_row(row)
            if (
                str(job.tags.get("hold_kind") or "")
                != _SOURCE_RUNTIME_HOLD_KIND
                or str(job.tags.get("blocked_reason") or "")
                != _SOURCE_RUNTIME_BLOCKED_REASON
            ):
                continue
            reason = self._runtime_claim_hold_reason(job)
            if reason:
                tags = dict(job.tags)
                if tags.get("source_runtime_hold_reason") != reason:
                    tags["source_runtime_hold_reason"] = reason
                    await self._db.execute(
                        "UPDATE jobs SET tags = ? WHERE job_id = ? "
                        "AND status = ?",
                        (
                            json.dumps(tags), job.job_id,
                            JobStatus.BLOCKED.value,
                        ),
                    )
                    dirty = True
                continue
            tags = dict(job.tags)
            for key in _SOURCE_RUNTIME_TAGS:
                tags.pop(key, None)
            changed = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, claimed_at = NULL,
                    claim_attempt_id = NULL, claim_expires_at = NULL,
                    last_heartbeat = NULL, tags = ?
                WHERE job_id = ? AND status = ?
                  AND json_extract(tags, '$.hold_kind') = ?
                  AND json_extract(tags, '$.blocked_reason') = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    json.dumps(tags),
                    job.job_id,
                    JobStatus.BLOCKED.value,
                    _SOURCE_RUNTIME_HOLD_KIND,
                    _SOURCE_RUNTIME_BLOCKED_REASON,
                ),
            )
            if changed.rowcount == 1:
                await self._audit(
                    job.job_id,
                    JobStatus.BLOCKED.value,
                    JobStatus.QUEUED.value,
                    reason="source_runtime_hold_released",
                )
                released += 1
                dirty = True
        if dirty:
            await self._db.commit()
        return released

    @_serialized_mutation
    async def claim_job(
        self,
        worker_id: str,
        worker_caps: WorkerCapabilities,
        authority_tags: Optional[Dict[str, str]] = None,
    ) -> Optional[Job]:
        """Atomically claim the highest-priority eligible QUEUED job.

        Returns None if no eligible jobs are available.
        """
        if not self._execution_ready:
            raise QueueExecutionUnavailable(
                self._execution_readiness_reason
            )
        assert self._db is not None
        registered_limit = None
        registered = await self._db.execute(
            """
            SELECT max_concurrent, available, load, last_seen
            FROM workers WHERE node_id = ?
            """,
            (worker_id,),
        )
        registered_row = await registered.fetchone()
        if registered_row is not None:
            # Preserve the historical direct QueueManager API for callers
            # without a registry row, but never let an existing stale,
            # unavailable, or full registration become a claim candidate by
            # supplying fresher caller-owned capabilities.
            if (
                not bool(registered_row["available"])
                or float(registered_row["load"]) >= 1.0
                or not self._worker_last_seen_is_fresh(
                    registered_row["last_seen"]
                )
            ):
                return None
            registered_limit = max(1, int(registered_row["max_concurrent"]))
        await self.reconcile_runtime_claim_holds()
        await self.reconcile_governance_holds()
        now = datetime.now(timezone.utc)
        queued = await self.get_queued_jobs_sorted(now)
        effective_max_concurrent = max(1, int(worker_caps.max_concurrent))
        if registered_limit is not None:
            effective_max_concurrent = min(
                effective_max_concurrent, registered_limit,
            )

        for job in queued:
            # The scheduler normally expires these first, but claiming must
            # enforce the deadline independently.  A delayed scheduler must
            # never revive an expired bounded ThoughtJob (or any other job).
            if job.is_expired():
                continue
            runtime_hold = self._runtime_claim_hold_reason(job)
            if runtime_hold:
                await self._hold_queued_for_runtime(job, runtime_hold)
                continue
            # Startup performs the same migration in bulk, but claim_job is
            # an independent execution boundary. This closes direct calls,
            # concurrent claim races, and rows introduced by an older writer
            # after startup. Only exact independent ``not_applied`` evidence
            # can make a previously-started effect retryable again.
            effect_truth = await self._work_control_attempt_truth(job)
            if effect_truth["effect_disposition"] in {
                "ambiguous_prior_effects", "verified_applied",
            }:
                event_id = await self._quarantine_queued_effect_truth(
                    job,
                    effect_truth,
                    reason="claim_boundary_effect_retry_fence",
                )
                await self._db.commit()
                if event_id:
                    self._schedule_worker_outcome_drain(event_id)
                continue
            if job.job_type is JobType.AGENT_ACTION:
                from colony_sidecar.task_queue.governor import (
                    job_declares_effect,
                )
                if (
                    job_declares_effect(job)
                    and not self._server_approval_provenance_valid(job)
                ):
                    self._hold_for_missing_approval(job)
                    changed = await self._db.execute(
                        "UPDATE jobs SET status = ?, tags = ? "
                        "WHERE job_id = ? AND status = ?",
                        (
                            JobStatus.BLOCKED.value,
                            json.dumps(job.tags),
                            job.job_id,
                            JobStatus.QUEUED.value,
                        ),
                    )
                    if changed.rowcount == 1:
                        await self._audit(
                            job.job_id,
                            JobStatus.QUEUED.value,
                            JobStatus.BLOCKED.value,
                            reason="approval_provenance_revalidated",
                        )
                        await self._db.commit()
                    continue
            if job.job_type is JobType.AGENT_ACTION:
                from colony_sidecar.task_queue.routing import (
                    AGENT_SYNC_ROUTE,
                    HERMES_RUN_ROUTE,
                    generic_agent_job_claims_enabled,
                )
                route = str(
                    (job.tags or {}).get("agent_action_route") or ""
                )
                if (
                    route in {AGENT_SYNC_ROUTE, HERMES_RUN_ROUTE}
                    and not generic_agent_job_claims_enabled()
                ):
                    continue
            if job.job_type is JobType.THOUGHT and (
                not self._thought_runtime_ready
                or self._thought_runtime_node != worker_id
            ):
                continue
            route_node = str(
                (job.tags or {}).get("agent_action_route_node")
                or (job.tags or {}).get("thought_route_node")
                or ""
            ).strip()
            if route_node and route_node != worker_id:
                continue
            if not worker_caps.can_accept(job):
                continue
            from colony_sidecar.task_queue.governor import (
                job_declares_effect,
                workers_mode,
            )
            mode = workers_mode()
            verdict, authority_error = self._evaluate_claim_authority(
                job, worker_caps, worker_id)
            # Shadow is observational only for real effects.  Never execute a
            # mutation/disclosure/maintenance handler merely to calibrate the
            # governor, including when its dependencies are unavailable.
            if mode == "shadow" and job_declares_effect(job):
                await self._hold_queued_for_governance(
                    job,
                    hold_kind="shadow_effect",
                    reason="shadow_effect_execution_disabled",
                    mode="shadow",
                )
                continue
            if mode == "live" and authority_error is not None:
                await self._hold_queued_for_governance(
                    job,
                    hold_kind="governor_unavailable",
                    reason=authority_error,
                )
                continue
            if verdict is not None and not verdict.allowed:
                if not verdict.boundary_ok:
                    if mode == "live":
                        unavailable = verdict.boundary_reason in {
                            "boundary_checker_unavailable",
                            "boundary_check_failed_closed",
                        }
                        await self._hold_queued_for_governance(
                            job,
                            hold_kind=(
                                "governor_unavailable" if unavailable else "boundary"
                            ),
                            reason=verdict.reason,
                        )
                elif not verdict.trust_ok:
                    if mode == "live":
                        await self._hold_queued_for_governance(
                            job,
                            hold_kind="trust",
                            reason=verdict.reason,
                        )
                # A capability-only refusal remains QUEUED for another worker.
                continue
            claim_tags = self._claim_evidence_tags(
                job.tags,
                mode=mode,
                verdict=verdict,
                error=(authority_error if mode == "shadow" else None),
            )
            if authority_tags:
                for key in (
                    "worker_authority_mode",
                    "worker_authority_principal",
                    "worker_authority_credential",
                    "worker_authority_would_deny",
                ):
                    value = authority_tags.get(key)
                    if isinstance(value, str):
                        claim_tags[key] = value[:256]
            claim_attempt_id = _uuid_module.uuid4().hex
            claim_expires_at = now + timedelta(
                seconds=self._claim_timeout_secs
            )
            # Attempt atomic claim
            cur = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = ?, claimed_at = ?,
                    claim_attempt_id = ?, claim_expires_at = ?,
                    last_heartbeat = ?, tags = ?
                WHERE job_id = ? AND status = 'queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM work_control_operations AS control
                    WHERE control.target_id = jobs.job_id
                      AND control.status = 'pending_ack'
                  )
                  AND (
                    SELECT COUNT(*) FROM jobs
                    WHERE claimed_by = ? AND status IN ('claimed', 'running')
                  ) < ?
                """,
                (
                    JobStatus.CLAIMED.value,
                    worker_id,
                    now.isoformat(),
                    claim_attempt_id,
                    claim_expires_at.isoformat(),
                    now.isoformat(),
                    json.dumps(claim_tags),
                    job.job_id,
                    worker_id,
                    effective_max_concurrent,
                ),
            )
            if cur.rowcount == 1:
                await self._audit(
                    job.job_id, JobStatus.QUEUED.value, JobStatus.CLAIMED.value,
                    node_id=worker_id,
                    claim_attempt_id=claim_attempt_id,
                )
                await self._db.commit()
                job.status = JobStatus.CLAIMED
                job.claimed_by = worker_id
                job.claimed_at = now
                job.claim_attempt_id = claim_attempt_id
                job.claim_expires_at = claim_expires_at
                job.last_heartbeat = now
                job.tags = claim_tags
                return job
            # Another worker got it first; try next
        await self._db.rollback()
        return None

    # ------------------------------------------------------------------
    # Job state transitions
    # ------------------------------------------------------------------

    @staticmethod
    def _attempt_matches(job: Job, claim_attempt_id: Optional[str]) -> bool:
        """Match a lifecycle call to the exact durable claim attempt.

        The new lifecycle never accepts a missing attempt, including for an
        in-flight row created by a pre-migration binary.  Deployments must
        drain those rows before cutover; startup readiness reports and holds
        the incompatible state rather than silently downgrading.
        """
        return bool(
            job.claim_attempt_id
            and claim_attempt_id
            and job.claim_attempt_id == claim_attempt_id
        )

    @staticmethod
    def _ambiguous_attempt_replay(
        job: Optional[Job],
        worker_id: str,
        claim_attempt_id: Optional[str],
    ) -> bool:
        """Recognize an exact replay after an effectful attempt terminalized.

        Ambiguity clears the live claim from the job row, so ordinary active
        attempt matching is intentionally impossible afterward. The durable
        neutral result retains the exact worker/attempt pair needed to make a
        response-loss retry idempotent without authorizing a different worker
        or attempt.
        """

        return bool(
            job is not None
            and job.status is JobStatus.NEUTRAL
            and str(job.tags.get("ambiguous_prior_effects") or "").lower()
            == "true"
            and job.result is not None
            and job.result.status is JobStatus.NEUTRAL
            and job.result.worker_node_id == worker_id
            and claim_attempt_id
            and job.result.claim_attempt_id == claim_attempt_id
        )

    async def _attempt_has_pending_work_control(
        self,
        job_id: str,
        claim_attempt_id: Optional[str],
        *,
        stop_only: bool = False,
    ) -> bool:
        """Return whether an accepted command must resolve before lifecycle."""

        assert self._db is not None
        if not claim_attempt_id:
            return False
        operation_filter = (
            " AND operation_type IN ('interrupt', 'cancel')"
            if stop_only else ""
        )
        cursor = await self._db.execute(
            "SELECT 1 FROM work_control_operations "
            "WHERE target_id = ? AND attempt_id = ? "
            "AND status = 'pending_ack'" + operation_filter + " LIMIT 1",
            (job_id, claim_attempt_id),
        )
        return await cursor.fetchone() is not None

    async def _mark_pending_work_controls_terminal(
        self,
        operation_ids: List[str],
        *,
        status: str,
        acknowledgement_outcome: str,
        reason: str,
        ack_authority: Mapping[str, Any],
        to_job_status: Optional[str],
    ) -> List[str]:
        """Mark exact pending rows terminal without projecting receipts yet."""

        assert self._db is not None
        operation_ids = list(dict.fromkeys(
            str(value) for value in operation_ids if str(value)
        ))
        if not operation_ids:
            return []
        from colony_sidecar.task_queue.work_control import canonical_json

        placeholders = ",".join("?" for _ in operation_ids)
        cursor = await self._db.execute(
            "SELECT operation_id FROM work_control_operations "
            "WHERE operation_id IN (" + placeholders + ") "
            "AND status = 'pending_ack' ORDER BY created_at, operation_id",
            tuple(operation_ids),
        )
        pending_ids = [
            str(row["operation_id"]) for row in await cursor.fetchall()
        ]
        if not pending_ids:
            return []
        placeholders = ",".join("?" for _ in pending_ids)
        acknowledged_at = _now_iso()
        changed = await self._db.execute(
            "UPDATE work_control_operations SET status = ?, "
            "to_job_status = ?, ack_details = ?, ack_authority = ?, "
            "acknowledged_at = ? WHERE operation_id IN ("
            + placeholders + ") AND status = 'pending_ack'",
            (
                status,
                to_job_status,
                canonical_json({
                    "outcome": acknowledgement_outcome,
                    "details": {"reason": reason},
                }),
                canonical_json(dict(ack_authority)),
                acknowledged_at,
                *pending_ids,
            ),
        )
        if changed.rowcount != len(pending_ids):
            raise RuntimeError("pending WorkControl terminalization raced")
        return pending_ids

    async def _seal_work_control_outcomes(
        self,
        target_id: str,
        operation_ids: List[str],
    ) -> int:
        """Bind terminal rows to the final target projection and append facts."""

        assert self._db is not None
        operation_ids = list(dict.fromkeys(operation_ids))
        if not operation_ids:
            return 0
        job = await self.get_job(target_id)
        if job is not None:
            projection = await self._work_control_projection_locked(job)
            placeholders = ",".join("?" for _ in operation_ids)
            await self._db.execute(
                "UPDATE work_control_operations SET result_revision = ?, "
                "result_state_digest = ? WHERE operation_id IN ("
                + placeholders + ")",
                (
                    projection["revision"], projection["state_digest"],
                    *operation_ids,
                ),
            )
        for operation_id in operation_ids:
            await self._append_work_control_receipt_event(
                operation_id, phase="outcome",
            )
        return len(operation_ids)

    async def _finalize_pending_controls_after_transition(
        self,
        job_id: str,
        claim_attempt_id: Optional[str],
        *,
        reason: str,
        exclude_operation_id: Optional[str] = None,
    ) -> int:
        """Finalize every command that lost its exact attempt lifecycle."""

        assert self._db is not None
        if not claim_attempt_id:
            return 0
        cursor = await self._db.execute(
            """SELECT operation_id FROM work_control_operations
               WHERE target_id = ? AND attempt_id = ?
                 AND status = 'pending_ack'
               ORDER BY created_at, operation_id""",
            (job_id, claim_attempt_id),
        )
        operation_ids = [
            str(row["operation_id"])
            for row in await cursor.fetchall()
            if str(row["operation_id"]) != str(exclude_operation_id or "")
        ]
        if not operation_ids:
            return 0
        job = await self.get_job(job_id)
        if (
            job is not None
            and job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            and job.claim_attempt_id == claim_attempt_id
        ):
            raise RuntimeError(
                "control supersession requires a losing attempt transition"
            )
        marked = await self._mark_pending_work_controls_terminal(
            operation_ids,
            status="superseded",
            acknowledgement_outcome="superseded",
            reason=reason,
            ack_authority={
                "authority_kind": "server_lifecycle_winner",
                "claim_attempt_id": claim_attempt_id,
            },
            to_job_status=(job.status.value if job is not None else None),
        )
        return await self._seal_work_control_outcomes(job_id, marked)

    @_serialized_mutation
    async def start_job(
        self,
        job_id: str,
        worker_id: str,
        claim_attempt_id: Optional[str] = None,
    ) -> bool:
        """Transition the exact, unexpired CLAIMED attempt to RUNNING."""
        assert self._db is not None
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        job = await self.get_job(job_id)
        if await self._attempt_has_pending_work_control(
            job_id, claim_attempt_id, stop_only=True,
        ):
            return False
        if (
            job is not None
            and job.status is JobStatus.RUNNING
            and job.claimed_by == worker_id
            and self._attempt_matches(job, claim_attempt_id)
        ):
            return True
        if (
            job is None
            or job.status is not JobStatus.CLAIMED
            or job.claimed_by != worker_id
            or not self._attempt_matches(job, claim_attempt_id)
        ):
            return False
        if job.deadline is not None and now_dt > job.deadline:
            await self.fail_job(
                job_id,
                worker_id,
                "server_deadline_exceeded",
                claim_attempt_id=claim_attempt_id,
            )
            return False
        if job.claim_expires_at is not None and now_dt > job.claim_expires_at:
            changed = await self._db.execute(
                """UPDATE jobs SET status = ?, claimed_by = NULL,
                          claimed_at = NULL, claim_attempt_id = NULL,
                          claim_expires_at = NULL, last_heartbeat = NULL
                   WHERE job_id = ? AND status = ? AND claimed_by = ?
                     AND claim_attempt_id IS ?""",
                (
                    JobStatus.QUEUED.value,
                    job_id,
                    JobStatus.CLAIMED.value,
                    worker_id,
                    job.claim_attempt_id,
                ),
            )
            if changed.rowcount == 1:
                await self._audit(
                    job_id,
                    JobStatus.CLAIMED.value,
                    JobStatus.QUEUED.value,
                    node_id=worker_id,
                    claim_attempt_id=job.claim_attempt_id,
                    reason="claim_start_lease_expired",
                )
                await self._finalize_pending_controls_after_transition(
                    job_id,
                    job.claim_attempt_id,
                    reason="claim_start_lease_expired",
                )
                await self._db.commit()
            return False
        runtime_hold = self._runtime_claim_hold_reason(job)
        if runtime_hold:
            # The source may have rolled back after claim but before the
            # worker began effects.  Preserve the immutable row and all
            # approval/evidence tags, but retire this exact lease/attempt.
            tags = dict(job.tags)
            tags.update({
                "hold_kind": _SOURCE_RUNTIME_HOLD_KIND,
                "blocked_reason": _SOURCE_RUNTIME_BLOCKED_REASON,
                "source_runtime_hold_reason": runtime_hold[:500],
                "source_runtime_held_at": _now_iso(),
            })
            changed = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, claimed_at = NULL,
                    claim_attempt_id = NULL, claim_expires_at = NULL,
                    last_heartbeat = NULL, tags = ?
                WHERE job_id = ? AND status = ? AND claimed_by = ?
                  AND claim_attempt_id IS ?
                """,
                (
                    JobStatus.BLOCKED.value,
                    json.dumps(tags),
                    job_id,
                    JobStatus.CLAIMED.value,
                    worker_id,
                    job.claim_attempt_id,
                ),
            )
            if changed.rowcount != 1:
                await self._db.rollback()
                return False
            await self._audit(
                job_id,
                JobStatus.CLAIMED.value,
                JobStatus.BLOCKED.value,
                node_id=worker_id,
                claim_attempt_id=job.claim_attempt_id,
                reason=f"{_SOURCE_RUNTIME_HOLD_KIND}:{runtime_hold[:500]}",
            )
            await self._finalize_pending_controls_after_transition(
                job_id,
                job.claim_attempt_id,
                reason="source_runtime_hold_before_start",
            )
            await self._db.commit()
            return False
        changed = await self._db.execute(
            """
            UPDATE jobs SET status = ?, last_heartbeat = ?, claim_expires_at = NULL
            WHERE job_id = ? AND status = ? AND claimed_by = ?
              AND claim_attempt_id IS ?
            """,
            (
                JobStatus.RUNNING.value,
                now,
                job_id,
                JobStatus.CLAIMED.value,
                worker_id,
                job.claim_attempt_id,
            ),
        )
        if changed.rowcount != 1:
            await self._db.rollback()
            return False
        await self._audit(
            job_id,
            JobStatus.CLAIMED.value,
            JobStatus.RUNNING.value,
            node_id=worker_id,
            claim_attempt_id=job.claim_attempt_id,
        )
        await self._db.commit()
        return True

    async def _server_started_at(
        self,
        job: Job,
        worker_id: str,
        now: datetime,
    ) -> Optional[datetime]:
        """Derive timing only from the durable server claim/start ledger."""

        assert self._db is not None
        cursor = await self._db.execute(
            """SELECT timestamp FROM job_audit
               WHERE job_id = ? AND to_status = ? AND node_id = ?
                 AND claim_attempt_id IS ?
               ORDER BY id DESC LIMIT 1""",
            (
                job.job_id,
                JobStatus.RUNNING.value,
                worker_id,
                job.claim_attempt_id,
            ),
        )
        row = await cursor.fetchone()
        started = _parse_dt(row["timestamp"]) if row is not None else None
        if started is None:
            started = job.claimed_at
        if started is None:
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        else:
            started = started.astimezone(timezone.utc)
        # A wall-clock adjustment must not create negative competence or CPI
        # evidence. The timestamp remains server-owned; only duration clamps.
        return min(started, now)

    def _audit_completion_report(
        self,
        job: Job,
        output: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the configured completion auditor before durable completion."""

        result: Dict[str, Any] = {
            "verdict": "unverified",
            "findings": [],
            "governor_outcome": "neutral",
            "outcome_reason": "governor_unavailable",
        }
        governor = self._worker_governor
        if governor is not None:
            try:
                audit = governor.audit_report(job, output)
                if not isinstance(audit, dict):
                    raise TypeError("completion audit must be a dictionary")
                verdict = audit.get("verdict")
                findings = audit.get("findings")
                if verdict not in {"clean", "violation", "unverified"}:
                    raise TypeError("invalid completion verdict")
                if not isinstance(findings, list) or any(
                    not isinstance(item, str) for item in findings
                ):
                    raise TypeError("invalid completion findings")
                outcome, outcome_reason = governor.classify_completion_outcome(
                    output, verdict)
                if outcome not in {"success", "failure", "neutral"}:
                    raise TypeError("invalid completion outcome")
                result.update({
                    "verdict": verdict,
                    "findings": findings,
                    "governor_outcome": outcome,
                    "outcome_reason": str(outcome_reason),
                })
            except Exception:
                logger.warning(
                    "central worker completion audit failed", exc_info=True
                )
                result["outcome_reason"] = "governor_audit_unavailable"
        if (
            isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
            and result["verdict"] != "violation"
            and result["governor_outcome"] != "failure"
        ):
            result["governor_outcome"] = "neutral"
            result["outcome_reason"] = "work_order_transport_only"
        return result

    @staticmethod
    def _job_governor_mode(job: Job) -> str:
        """Return the authority mode captured when this attempt was claimed."""

        captured = str((job.tags or {}).get("governor_mode") or "").lower()
        if captured in {"off", "shadow", "live"}:
            return captured
        # Legacy in-flight rows predate claim-mode evidence. Preserve their
        # historical behavior by using the mode active at terminalization.
        from colony_sidecar.task_queue.governor import workers_mode
        return workers_mode()

    async def _enqueue_worker_outcome(
        self,
        *,
        event_id: str,
        job_id: str,
        claim_attempt_id: str,
        report: Dict[str, Any],
        verdict: str,
        outcome: str,
        worker_mode: str,
        success_attested: bool,
        latency: Optional[float],
        attempts: int,
    ) -> None:
        """Insert one stable outcome event inside the job transaction."""

        assert self._db is not None
        await self._db.execute(
            """INSERT OR IGNORE INTO worker_outcome_outbox (
                   event_id, job_id, claim_attempt_id, report, verdict,
                   outcome, worker_mode, success_attested, latency, attempts,
                   state, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                event_id,
                job_id,
                claim_attempt_id,
                json.dumps(report),
                verdict,
                outcome,
                worker_mode,
                1 if success_attested else 0,
                latency,
                max(0, int(attempts or 0)),
                _now_iso(),
            ),
        )

    @_serialized_read
    async def pending_worker_outcomes(
        self,
        *,
        include_delivered: bool = False,
        event_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return bounded outbox metadata/report for reconciliation tests/status."""

        assert self._db is not None
        sql = "SELECT * FROM worker_outcome_outbox WHERE 1=1"
        params: List[Any] = []
        if not include_delivered:
            sql += " AND state = 'pending'"
        if event_id:
            sql += " AND event_id = ?"
            params.append(event_id)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        cursor = await self._db.execute(sql, tuple(params))
        rows = []
        for raw in await cursor.fetchall():
            row = dict(raw)
            try:
                row["report"] = json.loads(row.get("report") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["report"] = {}
            rows.append(row)
        return rows

    @_serialized_mutation
    async def _mark_worker_outcome(
        self,
        event_id: str,
        *,
        delivered: bool,
        error: str = "",
    ) -> None:
        assert self._db is not None
        if delivered:
            await self._db.execute(
                """UPDATE worker_outcome_outbox
                   SET state = 'delivered', delivered_at = ?,
                       delivery_attempts = delivery_attempts + 1,
                       last_error = NULL
                   WHERE event_id = ? AND state = 'pending'""",
                (_now_iso(), event_id),
            )
        else:
            await self._db.execute(
                """UPDATE worker_outcome_outbox
                   SET delivery_attempts = delivery_attempts + 1,
                       last_error = ?
                   WHERE event_id = ? AND state = 'pending'""",
                (str(error)[:1000], event_id),
            )
        await self._db.commit()

    async def drain_worker_outcomes(
        self,
        *,
        event_id: Optional[str] = None,
        limit: int = 100,
    ) -> int:
        """Deliver pending worker evidence; safe across retry and restart."""

        async with self._outcome_lock:
            governor = self._worker_governor
            if governor is None:
                return 0
            rows = await self.pending_worker_outcomes(
                event_id=event_id,
                limit=limit,
            )
            delivered = 0
            for row in rows:
                job = await self.get_job(str(row["job_id"]))
                if job is None:
                    await self._mark_worker_outcome(
                        str(row["event_id"]),
                        delivered=False,
                        error="outcome_job_missing",
                    )
                    continue
                try:
                    timeout = _positive_seconds(
                        os.environ.get(
                            "COLONY_WORKER_OUTCOME_DELIVERY_TIMEOUT_SECS",
                            "60",
                        ),
                        default=60.0,
                        field="COLONY_WORKER_OUTCOME_DELIVERY_TIMEOUT_SECS",
                    )
                    await asyncio.wait_for(
                        governor.record_outcome(
                            job,
                            dict(row["report"]),
                            str(row["verdict"]),
                            outcome=str(row["outcome"]),
                            latency=row.get("latency"),
                            attempts=int(row.get("attempts") or 0),
                            event_id=str(row["event_id"]),
                            event_mode=str(row.get("worker_mode") or "off"),
                            success_attested=bool(
                                row.get("success_attested")
                            ),
                        ),
                        timeout=timeout,
                    )
                except asyncio.CancelledError:
                    # The durable pending row is the retry contract.
                    raise
                except Exception as exc:
                    await self._mark_worker_outcome(
                        str(row["event_id"]),
                        delivered=False,
                        error=str(exc),
                    )
                    continue
                await self._mark_worker_outcome(
                    str(row["event_id"]), delivered=True,
                )
                delivered += 1
            return delivered

    def _schedule_worker_outcome_drain(
        self,
        event_id: Optional[str] = None,
    ) -> None:
        """Kick reconciliation without extending the completion critical path."""

        if self._worker_governor is None:
            return
        if any(not task.done() for task in self._outcome_tasks):
            return
        task = asyncio.create_task(
            self.drain_worker_outcomes(event_id=event_id)
        )
        self._outcome_tasks.add(task)

        def _done(completed: asyncio.Task) -> None:
            self._outcome_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.warning(
                    "background worker outcome drain failed",
                    exc_info=True,
                )

        task.add_done_callback(_done)

    @_serialized_mutation
    async def complete_job(
        self,
        job_id: str,
        worker_id: str,
        output: Dict[str, Any],
        started_at: Optional[datetime] = None,
        claim_attempt_id: Optional[str] = None,
        server_attested: bool = False,
    ) -> Dict[str, Any]:
        """Transition RUNNING → COMPLETED using server-derived timing.

        ``started_at`` remains a compatibility argument but is deliberately
        ignored because external workers are not evidence authorities.
        """
        assert self._db is not None
        now = datetime.now(timezone.utc)
        job = await self.get_job(job_id)
        if await self._attempt_has_pending_work_control(
            job_id, claim_attempt_id, stop_only=True,
        ):
            return {
                "verdict": "unverified",
                "findings": ["work_control_pending"],
                "governor_outcome": "neutral",
                "outcome_reason": "work_control_pending",
                "transitioned": False,
            }
        if (
            job is not None
            and job.status in {JobStatus.QUEUED, JobStatus.FAILED}
            and job.result is not None
            and job.result.status is JobStatus.FAILED
            and job.result.worker_node_id == worker_id
            and job.result.claim_attempt_id == claim_attempt_id
        ):
            return {
                "verdict": "unverified",
                "findings": ["claim_attempt_already_failed"],
                "governor_outcome": "failure",
                "outcome_reason": "claim_attempt_already_failed",
                "transitioned": False,
                "stale_attempt_rejected": True,
                "job_status": job.status.value,
            }
        if (
            job is not None
            and job.status in {
                JobStatus.COMPLETED, JobStatus.NEUTRAL, JobStatus.FAILED,
            }
            and job.result is not None
            and job.result.worker_node_id == worker_id
            and self._attempt_matches(job, claim_attempt_id)
            and str(job.tags.get("worker_completion_terminalized") or "")
            == "true"
        ):
            event_id = (
                f"worker-outcome:{job_id}:{job.claim_attempt_id}:complete"
            )
            self._schedule_worker_outcome_drain(event_id)
            return {
                "verdict": str(
                    job.tags.get("governor_verdict") or "unverified"
                ),
                "findings": [],
                "governor_outcome": str(
                    job.tags.get("governor_outcome") or "neutral"
                ),
                "outcome_reason": str(
                    job.tags.get("governor_outcome_reason") or "replayed"
                ),
                "transitioned": True,
                "replayed": True,
                "job_status": job.status.value,
            }
        if (
            job is None
            or job.claimed_by != worker_id
            or job.status != JobStatus.RUNNING
            or not self._attempt_matches(job, claim_attempt_id)
        ):
            return {
                "verdict": "unverified",
                "findings": [],
                "governor_outcome": "neutral",
                "outcome_reason": "claimant_or_state_mismatch",
                "transitioned": False,
            }
        started_at = await self._server_started_at(job, worker_id, now)
        deadline_expired = job.deadline is not None and now > job.deadline
        execution_timed_out = (
            started_at is not None
            and (now - started_at).total_seconds() > job.timeout_secs
        )
        if deadline_expired or execution_timed_out:
            reason = (
                "server_deadline_exceeded"
                if deadline_expired else "server_execution_timeout"
            )
            await self.fail_job(
                job_id,
                worker_id,
                reason,
                claim_attempt_id=claim_attempt_id,
            )
            return {
                "verdict": "unverified",
                "findings": [reason],
                "governor_outcome": "failure",
                "outcome_reason": reason,
                "transitioned": False,
                "stale_attempt_rejected": True,
            }
        audit = (
            self._audit_completion_report(job, output)
            if job is not None else {}
        )
        if (
            server_attested
            and audit["verdict"] != "violation"
            and audit["governor_outcome"] == "neutral"
            and audit["outcome_reason"] in {
                "audit_unverified",
                "completed_without_verification",
                "unknown_semantic_outcome",
            }
        ):
            audit["governor_outcome"] = "success"
            audit["outcome_reason"] = "server_attested_embedded_completion"
        duration = (
            max(0.0, (now - started_at).total_seconds())
            if started_at is not None else None
        )
        claim_mode = self._job_governor_mode(job)
        semantic_success = audit["governor_outcome"] == "success"
        semantic_failure = audit["governor_outcome"] == "failure"
        is_work_order = bool(
            isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
        )
        from colony_sidecar.task_queue.governor import job_declares_effect
        effectful = job_declares_effect(job)
        execution_result = (
            output.get("execution_result")
            if isinstance(output.get("execution_result"), Mapping) else {}
        )
        action_plane = (
            output.get("action_plane")
            if isinstance(output.get("action_plane"), Mapping) else {}
        )
        reported_terminal = str(
            output.get("status") or output.get("outcome")
            or execution_result.get("terminal_outcome") or ""
        ).strip().lower()
        reported_action_state = str(
            action_plane.get("state") or ""
        ).strip().lower()
        terminal_neutral = bool(
            reported_terminal in {
                "skipped", "skip", "cancelled", "canceled",
            }
            or reported_action_state in {
                "skipped", "skip", "cancelled", "canceled",
            }
        )
        terminal_failed = bool(
            reported_terminal in {
                "failed", "failure", "error", "errored", "denied",
            }
            or reported_action_state in {
                "failed", "failure", "error", "errored", "denied",
            }
        )
        ambiguous_semantic_effect = bool(
            effectful
            and (semantic_failure or terminal_neutral or terminal_failed)
        )
        work_order_needs_attestation = bool(
            is_work_order and not terminal_neutral and not semantic_failure
        )
        generic_effect_needs_attestation = bool(
            effectful
            and not is_work_order
            and not server_attested
            and not terminal_neutral
            and not semantic_failure
        )
        operational_completion = bool(
            not is_work_order
            and not effectful
            and audit["verdict"] == "clean"
            and audit["outcome_reason"] == "completed_without_verification"
        )
        if ambiguous_semantic_effect:
            # A semantic failure/skip/cancel is not proof that a started
            # mutation or disclosure did not land. Only independent exact-
            # attempt evidence may resolve this state.
            target_status = JobStatus.NEUTRAL
        elif semantic_failure:
            target_status = JobStatus.FAILED
        elif terminal_neutral:
            target_status = JobStatus.NEUTRAL
        elif work_order_needs_attestation or generic_effect_needs_attestation:
            # Executor transport completion is never independent evidence of
            # a WorkOrder or generic Action Plane effect. This is
            # mode-invariant: only the matching receipt attestation may
            # promote the result to COMPLETED.
            target_status = JobStatus.NEUTRAL
        elif claim_mode != "live" or (
            semantic_success and (server_attested or not is_work_order)
        ) or operational_completion:
            target_status = JobStatus.COMPLETED
        else:
            target_status = JobStatus.NEUTRAL
        terminal_error = None
        if ambiguous_semantic_effect:
            terminal_error = "ambiguous_prior_effects"
        elif target_status is JobStatus.FAILED:
            terminal_error = (
                "worker_scope_violation"
                if audit["verdict"] == "violation"
                else "worker_reported_failure"
            )
        result = JobResult(
            job_id=job_id,
            worker_node_id=worker_id,
            status=target_status,
            output=output,
            error=terminal_error,
            started_at=started_at,
            completed_at=now,
            duration_seconds=duration,
            claim_attempt_id=job.claim_attempt_id,
        )
        tags = dict(job.tags) if job is not None else {}
        tags.update({
            "governor_verdict": str(audit["verdict"]),
            "governor_outcome": str(audit["governor_outcome"]),
            "governor_outcome_reason": str(audit["outcome_reason"]),
            "worker_completion_terminalized": "true",
            "success_attested": str(bool(
                server_attested and not ambiguous_semantic_effect
            )).lower(),
        })
        if ambiguous_semantic_effect:
            tags.update({
                "verification_pending": "true",
                "ambiguous_prior_effects": "true",
                "hold_kind": "work_control",
                "blocked_reason": "ambiguous_prior_effects",
                "success_attestation_schema": (
                    "ExecutionResultV1"
                    if is_work_order else "ActionReceiptAttestationV1"
                ),
            })
        elif server_attested:
            tags.update({
                "success_verifier_identity": "colony:embedded-worker",
                "success_verifier_type": "server_executor",
            })
        elif work_order_needs_attestation:
            tags.update({
                "verification_pending": "true",
                "success_attestation_schema": "ExecutionResultV1",
            })
        elif generic_effect_needs_attestation:
            tags.update({
                "operational_completion_only": "true",
                "verification_pending": "true",
                "success_attestation_schema": (
                    "ActionReceiptAttestationV1"
                ),
            })
        elif target_status is JobStatus.COMPLETED:
            tags["operational_completion_only"] = "true"
        transitioned = await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, result = ?, claimed_by = NULL, claimed_at = NULL,
                last_heartbeat = NULL, claim_expires_at = NULL, tags = ?
            WHERE job_id = ? AND claimed_by = ? AND status = ?
              AND claim_attempt_id IS ?
            """,
            (
                target_status.value,
                _serialize_result(result),
                json.dumps(tags),
                job_id,
                worker_id,
                JobStatus.RUNNING.value,
                job.claim_attempt_id,
            ),
        )
        if transitioned.rowcount != 1:
            audit["transitioned"] = False
            await self._db.rollback()
            return audit
        audit["transitioned"] = True
        audit["job_status"] = target_status.value
        await self._audit(
            job_id,
            JobStatus.RUNNING.value,
            target_status.value,
            node_id=worker_id,
            claim_attempt_id=job.claim_attempt_id,
        )
        await self._finalize_pending_controls_after_transition(
            job_id, claim_attempt_id, reason="completion_won_race",
        )
        outcome_event_id = (
            f"worker-outcome:{job_id}:{job.claim_attempt_id}:complete"
        )
        # A worker's own clean success claim is an observation, not earned
        # competence.  Keep the first durable event neutral; an independent
        # verifier may later append the single attested success event.
        evidence_outcome = str(audit["governor_outcome"])
        if ambiguous_semantic_effect:
            evidence_outcome = "neutral"
        elif evidence_outcome == "success" and not server_attested:
            evidence_outcome = "neutral"
        await self._enqueue_worker_outcome(
            event_id=outcome_event_id,
            job_id=job_id,
            claim_attempt_id=str(job.claim_attempt_id),
            report=output,
            verdict=str(audit["verdict"]),
            outcome=evidence_outcome,
            worker_mode=claim_mode,
            success_attested=bool(server_attested),
            latency=duration,
            attempts=job.retry_count,
        )
        await self._db.commit()
        if target_status is JobStatus.COMPLETED:
            await self.unblock_ready_jobs()
        self._schedule_worker_outcome_drain(outcome_event_id)

        # Emit the semantic terminal event to the event bus (best-effort).
        if self._event_bus is not None:
            try:
                if target_status is JobStatus.COMPLETED:
                    from colony_sidecar.task_queue.events import JobCompletedEvent
                    event = JobCompletedEvent(
                        job_id=job_id,
                        worker_node_id=worker_id,
                        duration_seconds=result.duration_seconds,
                    )
                elif target_status is JobStatus.FAILED:
                    from colony_sidecar.task_queue.events import JobFailedEvent
                    event = JobFailedEvent(
                        job_id=job_id,
                        worker_node_id=worker_id,
                        error=terminal_error or "worker_completion_not_verified",
                        retry_count=job.retry_count,
                        will_retry=False,
                    )
                else:
                    from colony_sidecar.task_queue.events import JobNeutralEvent
                    event = JobNeutralEvent(
                        job_id=job_id,
                        worker_node_id=worker_id,
                        reason=str(audit["outcome_reason"]),
                    )
                self._event_bus.emit(event)
            except Exception:
                pass
        return audit

    @_serialized_mutation
    async def attest_action_success(
        self,
        job_id: str,
        *,
        attestation: Any,
        verifier_identity: str,
        verifier_type: str = "scoped_receipt_verifier",
    ) -> Optional[Dict[str, Any]]:
        """Promote one generic Action Plane effect after exact verification.

        The executor's completion remains observational. A separate scoped
        verifier must bind its evidence to the immutable action digest and
        exact server-minted claim attempt. Raw receipts are not persisted in
        queue tags; only their canonical digest is retained.
        """

        assert self._db is not None
        from colony_sidecar.task_queue.action_receipts import (
            ActionReceiptAttestationV1,
        )

        if type(attestation) is not ActionReceiptAttestationV1:
            return None
        job = await self.get_job(job_id)
        attempt = attestation.claim_attempt_id
        supplied_digest = attestation.action_digest
        identity = str(verifier_identity or "").strip()
        kind = str(verifier_type or "").strip()
        if (
            job is None
            or job.job_type is not JobType.AGENT_ACTION
            or job.payload.get("schema") == "WorkOrderV1"
            or attestation.job_id != job_id
            or not identity
            or not kind
        ):
            return None

        evidence_digest = attestation.evidence_sha256(
            verifier_identity=identity,
            verifier_type=kind,
        )
        refs_digest = hashlib.sha256(json.dumps(
            list(attestation.receipt_refs),
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")).hexdigest()

        if (
            job.status is JobStatus.COMPLETED
            and job.tags.get("success_attested") == "true"
        ):
            replayed = bool(
                job.tags.get("action_digest") == supplied_digest
                and job.tags.get("success_evidence_digest")
                == evidence_digest
                and job.tags.get("success_receipt_refs_digest")
                == refs_digest
                and job.tags.get("success_verifier_identity") == identity
                and job.tags.get("success_verifier_type") == kind
                and job.result is not None
                and job.result.claim_attempt_id == attempt
            )
            if not replayed:
                return None
            return {
                "replayed": True,
                "job_status": JobStatus.COMPLETED.value,
                "evidence_sha256": evidence_digest,
                "receipt_refs_sha256": str(
                    job.tags.get("success_receipt_refs_digest") or ""
                ),
            }

        from colony_sidecar.initiatives.approval_authority import (
            build_action_binding,
        )
        from colony_sidecar.initiatives.action_registry import (
            RiskTier,
            get_action,
        )
        from colony_sidecar.task_queue.governor import job_declares_effect

        binding = build_action_binding(
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
        )
        action_spec = get_action(str(job.payload.get("action_hint") or ""))
        expected_effect = (
            "disclosure"
            if action_spec is not None and action_spec.risk is RiskTier.OUTBOUND
            else "mutation"
        )
        try:
            clock_skew = float(os.environ.get(
                "COLONY_ACTION_RECEIPT_CLOCK_SKEW_SECS", "300",
            ))
        except (TypeError, ValueError):
            clock_skew = 300.0
        clock_skew = max(0.0, min(clock_skew, 900.0))
        observed_at = datetime.fromisoformat(attestation.observed_at)
        completed_at = job.result.completed_at if job.result is not None else None
        chronology_valid = bool(
            completed_at is not None
            and observed_at >= completed_at - timedelta(seconds=clock_skew)
            and observed_at
            <= datetime.now(timezone.utc) + timedelta(seconds=clock_skew)
        )
        ambiguous_attempt = bool(
            str(job.tags.get("ambiguous_prior_effects") or "").lower()
            == "true"
            and str(job.tags.get("verification_pending") or "").lower()
            == "true"
        )
        reconciled_applied = bool(
            job is not None
            and str(
                job.tags.get("effect_reconciliation_finding") or ""
            ).lower() == "applied"
        )
        if (
            not job_declares_effect(job)
            or action_spec is None
            or action_spec.risk is RiskTier.READ_ONLY
            or attestation.effect_class != expected_effect
            or not chronology_valid
            or job.status is not JobStatus.NEUTRAL
            or job.result is None
            or job.result.status is not JobStatus.NEUTRAL
            or job.result.claim_attempt_id != attempt
            or job.result.worker_node_id == identity
            or (
                str(job.tags.get("worker_completion_terminalized") or "")
                != "true"
                and not ambiguous_attempt
                and not reconciled_applied
            )
            or str(job.tags.get("governor_verdict") or "") == "violation"
            or str(job.tags.get("action_digest") or "")
            != binding.action_digest
            or supplied_digest != binding.action_digest
        ):
            return None

        result = JobResult(
            job_id=job.result.job_id,
            worker_node_id=job.result.worker_node_id,
            status=JobStatus.COMPLETED,
            output=job.result.output,
            error=None,
            started_at=job.result.started_at,
            completed_at=job.result.completed_at,
            duration_seconds=job.result.duration_seconds,
            claim_attempt_id=job.result.claim_attempt_id,
        )
        tags = dict(job.tags)
        for stale in (
            "operational_completion_only",
            "verification_pending",
            "ambiguous_prior_effects",
            "hold_kind",
            "blocked_reason",
            "memory_synced",
            "memory_sync_attempts",
        ):
            tags.pop(stale, None)
        tags.update({
            "success_attested": "true",
            "success_verifier_identity": identity[:256],
            "success_verifier_type": kind[:128],
            "success_evidence_digest": evidence_digest,
            "success_receipt_refs_digest": refs_digest,
            "success_attestation_schema": "ActionReceiptAttestationV1",
            "governor_outcome": "success",
            "governor_outcome_reason": (
                "independently_verified_action_completion"
            ),
        })
        if ambiguous_attempt or reconciled_applied:
            tags.update({
                "effect_reconciliation_finding": "applied",
                "effect_reconciliation_evidence_digest": evidence_digest,
                "effect_reconciliation_verifier": identity[:256],
                "effect_reconciliation_verifier_type": kind[:128],
            })
        if reconciled_applied:
            tags.pop("semantic_attestation_pending", None)
            tags["effect_reconciliation_consumed_at"] = _now_iso()
        changed = await self._db.execute(
            """UPDATE jobs SET status = ?, result = ?, tags = ?
               WHERE job_id = ? AND status = ?
                 AND claim_attempt_id IS ?""",
            (
                JobStatus.COMPLETED.value,
                _serialize_result(result),
                json.dumps(tags),
                job_id,
                JobStatus.NEUTRAL.value,
                attempt,
            ),
        )
        if changed.rowcount != 1:
            await self._db.rollback()
            return None
        await self._audit(
            job_id,
            JobStatus.NEUTRAL.value,
            JobStatus.COMPLETED.value,
            node_id=job.result.worker_node_id,
            claim_attempt_id=attempt,
            reason="independently_verified_action_completion",
            details={
                "action_digest": supplied_digest,
                "verifier_identity": identity[:256],
                "verifier_type": kind[:128],
                "evidence_digest": evidence_digest,
                "receipt_refs_digest": refs_digest,
            },
        )
        attestation_report = {
            "status": "verified",
            "action_receipt": {
                **attestation.payload(),
                "evidence_sha256": evidence_digest,
                "receipt_refs_sha256": refs_digest,
                "verifier_identity": identity[:256],
                "verifier_type": kind[:128],
            },
        }
        event_id = f"worker-action-attestation:{job_id}:{attempt}:success"
        await self._enqueue_worker_outcome(
            event_id=event_id,
            job_id=job_id,
            claim_attempt_id=attempt,
            report=attestation_report,
            verdict="clean",
            outcome="success",
            worker_mode=self._job_governor_mode(job),
            success_attested=True,
            latency=job.result.duration_seconds,
            attempts=job.retry_count,
        )
        await self._db.commit()
        await self.unblock_ready_jobs()
        self._schedule_worker_outcome_drain(event_id)
        if self._event_bus is not None:
            try:
                from colony_sidecar.task_queue.events import JobCompletedEvent

                self._event_bus.emit(JobCompletedEvent(
                    job_id=job_id,
                    worker_node_id=job.result.worker_node_id,
                    duration_seconds=job.result.duration_seconds,
                ))
            except Exception:
                pass
        return {
            "replayed": False,
            "job_status": JobStatus.COMPLETED.value,
            "evidence_sha256": evidence_digest,
            "receipt_refs_sha256": refs_digest,
        }

    @_serialized_mutation
    async def attest_job_success(
        self,
        job_id: str,
        *,
        report: Dict[str, Any],
        verifier_identity: str,
        verifier_type: str = "independent_receipt",
    ) -> bool:
        """Attest one exact terminal attempt after independent verification.

        The verifier report is intentionally narrow: only a bound,
        ``ExecutionResultV1`` success with a matching verifier identity may
        promote a live ``NEUTRAL`` row (or attest a shadow ``COMPLETED`` row).
        A skipped/failed/malformed report can never be relabelled as success.
        """

        assert self._db is not None
        job = await self.get_job(job_id)
        if (
            job is not None
            and job.status is JobStatus.COMPLETED
            and job.tags.get("success_attested") == "true"
        ):
            return True
        identity = str(verifier_identity or "").strip()
        kind = str(verifier_type or "").strip()
        attested = (
            report.get("execution_result")
            if isinstance(report, Mapping) else None
        )
        if not isinstance(attested, Mapping):
            return False
        ambiguous_attempt = bool(
            job is not None
            and str(job.tags.get("ambiguous_prior_effects") or "").lower()
            == "true"
            and str(job.tags.get("verification_pending") or "").lower()
            == "true"
        )
        reconciled_applied = bool(
            job is not None
            and str(
                job.tags.get("effect_reconciliation_finding") or ""
            ).lower() == "applied"
        )
        if (
            str(report.get("status") or "").strip().lower() != "verified"
            or str(attested.get("schema") or "") != "ExecutionResultV1"
            or type(attested.get("version")) is not int
            or int(attested.get("version")) != 1
            or str(attested.get("work_order_id") or "") != job_id
            or str(attested.get("terminal_outcome") or "").lower()
            != "succeeded"
            or str(attested.get("verification_result") or "").lower()
            != "verified"
            or str(attested.get("verifier_identity") or "").strip()
            != identity
        ):
            return False
        if (
            job is None
            or job.status not in {JobStatus.NEUTRAL, JobStatus.COMPLETED}
            or job.result is None
            or not job.result.claim_attempt_id
            or not identity
            or not kind
            or job.result.worker_node_id == identity
            or (
                (ambiguous_attempt or reconciled_applied)
                and str(report.get("claim_attempt_id") or "")
                != job.result.claim_attempt_id
            )
            or str(job.payload.get("work_order_digest") or "")
            != str(attested.get("work_order_digest") or "")
            or not (
                ambiguous_attempt
                or reconciled_applied
                or str(job.tags.get("governor_outcome") or "") == "success"
                or (
                    str(job.tags.get("governor_outcome") or "") == "neutral"
                    and str(
                        job.tags.get("governor_outcome_reason") or ""
                    ) == "work_order_transport_only"
                )
            )
        ):
            return False
        old_status = job.status
        result = JobResult(
            job_id=job.result.job_id,
            worker_node_id=job.result.worker_node_id,
            status=JobStatus.COMPLETED,
            output=job.result.output,
            error=None,
            started_at=job.result.started_at,
            completed_at=job.result.completed_at,
            duration_seconds=job.result.duration_seconds,
            claim_attempt_id=job.result.claim_attempt_id,
        )
        tags = dict(job.tags)
        for stale in (
            "operational_completion_only",
            "verification_pending",
            "ambiguous_prior_effects",
            "hold_kind",
            "blocked_reason",
            "memory_synced",
            "memory_sync_attempts",
        ):
            tags.pop(stale, None)
        tags.update({
            "success_attested": "true",
            "success_verifier_identity": identity[:256],
            "success_verifier_type": kind[:128],
            "governor_outcome": "success",
            "governor_outcome_reason": "independently_verified_completion",
        })
        evidence_digest = hashlib.sha256(
            json.dumps(
                report,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        tags["success_evidence_digest"] = evidence_digest
        if ambiguous_attempt or reconciled_applied:
            tags.update({
                "effect_reconciliation_finding": "applied",
                "effect_reconciliation_evidence_digest": evidence_digest,
                "effect_reconciliation_verifier": identity[:256],
                "effect_reconciliation_verifier_type": kind[:128],
            })
        if reconciled_applied:
            tags.pop("semantic_attestation_pending", None)
            tags["effect_reconciliation_consumed_at"] = _now_iso()
        changed = await self._db.execute(
            """UPDATE jobs SET status = ?, result = ?, tags = ?
               WHERE job_id = ? AND status = ?
                 AND claim_attempt_id IS ?""",
            (
                JobStatus.COMPLETED.value,
                _serialize_result(result),
                json.dumps(tags),
                job_id,
                old_status.value,
                job.result.claim_attempt_id,
            ),
        )
        if changed.rowcount != 1:
            await self._db.rollback()
            return False
        await self._audit(
            job_id,
            old_status.value,
            JobStatus.COMPLETED.value,
            node_id=job.result.worker_node_id,
            claim_attempt_id=job.result.claim_attempt_id,
            reason="independently_verified_completion",
            details={
                "verifier_identity": identity[:256],
                "verifier_type": kind[:128],
                "evidence_digest": evidence_digest,
            },
        )
        event_id = (
            f"worker-attestation:{job_id}:"
            f"{job.result.claim_attempt_id}:success"
        )
        await self._enqueue_worker_outcome(
            event_id=event_id,
            job_id=job_id,
            claim_attempt_id=job.result.claim_attempt_id,
            report=dict(report),
            verdict="clean",
            outcome="success",
            worker_mode=self._job_governor_mode(job),
            success_attested=True,
            latency=job.result.duration_seconds,
            attempts=job.retry_count,
        )
        await self._db.commit()
        await self.unblock_ready_jobs()
        self._schedule_worker_outcome_drain(event_id)
        if old_status is JobStatus.NEUTRAL and self._event_bus is not None:
            try:
                from colony_sidecar.task_queue.events import JobCompletedEvent
                self._event_bus.emit(JobCompletedEvent(
                    job_id=job_id,
                    worker_node_id=job.result.worker_node_id,
                    duration_seconds=job.result.duration_seconds,
                ))
            except Exception:
                pass
        return True

    @_serialized_read
    async def completed_durations(
        self, since_iso: str, until_iso: str, limit: int = 1000,
    ) -> List[float]:
        """Durations (seconds) of ALL jobs completed inside the window,
        claimed or not (selfhood benchmark; get_completed_jobs_since
        deliberately excludes claimed jobs). Result-payload timestamps may
        be naive UTC, so the window compares the shared ISO prefix."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT result FROM jobs WHERE status = ?"
            " ORDER BY rowid DESC LIMIT ?",
            (JobStatus.COMPLETED.value, limit))
        rows = await cursor.fetchall()
        out: List[float] = []
        for row in rows:
            try:
                r = json.loads(row["result"]) if row["result"] else {}
            except (json.JSONDecodeError, TypeError):
                continue
            done = str(r.get("completed_at") or "")
            dur = r.get("duration_seconds")
            if (done and dur is not None
                    and since_iso[:19] <= done[:19] < until_iso[:19]):
                try:
                    value = float(dur)
                except (TypeError, ValueError):
                    continue
                # Old databases can contain client-derived negative or
                # non-finite durations.  They are retained for forensics but
                # must never become benchmark or competence evidence.
                if math.isfinite(value) and value >= 0:
                    out.append(value)
        return out

    @_serialized_read
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

    @_serialized_mutation
    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        started_at: Optional[datetime] = None,
        claim_attempt_id: Optional[str] = None,
    ) -> bool:
        """Fail a claimed job with timing derived from the server ledger."""
        assert self._db is not None
        now = datetime.now(timezone.utc)
        job = await self.get_job(job_id)
        if await self._attempt_has_pending_work_control(
            job_id, claim_attempt_id, stop_only=True,
        ):
            return False
        if self._ambiguous_attempt_replay(
            job, worker_id, claim_attempt_id,
        ):
            return True
        if (
            job is not None
            and job.status in {JobStatus.QUEUED, JobStatus.FAILED}
            and job.result is not None
            and job.result.status is JobStatus.FAILED
            and job.result.worker_node_id == worker_id
            and job.result.claim_attempt_id == claim_attempt_id
        ):
            return True
        if (
            job is None
            or job.claimed_by != worker_id
            or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or not self._attempt_matches(job, claim_attempt_id)
        ):
            return False
        old_status = job.status.value
        started_at = await self._server_started_at(job, worker_id, now)
        duration = (
            max(0.0, (now - started_at).total_seconds())
            if started_at is not None else None
        )

        result = JobResult(
            job_id=job_id,
            worker_node_id=worker_id,
            status=JobStatus.FAILED,
            error=error,
            started_at=started_at,
            completed_at=now,
            duration_seconds=duration,
            claim_attempt_id=job.claim_attempt_id,
        )
        deadline_expired = job.deadline is not None and now > job.deadline
        execution_timed_out = (
            job.status is JobStatus.RUNNING
            and started_at is not None
            and (now - started_at).total_seconds() > job.timeout_secs
        )
        if deadline_expired:
            error = "server_deadline_exceeded"
            result.error = error
        elif execution_timed_out:
            error = "server_execution_timeout"
            result.error = error
        from colony_sidecar.task_queue.governor import job_declares_effect

        started_effectful = bool(
            job.status is JobStatus.RUNNING
            and started_at is not None
            and job_declares_effect(job)
        )
        if started_effectful:
            # A worker failure, timeout, heartbeat loss, or shutdown after a
            # mutation/disclosure began is not evidence that nothing happened.
            # Never turn that ambiguity into an automatic duplicate attempt.
            result.status = JobStatus.NEUTRAL
            result.output = {
                "status": "ambiguous",
                "reason": "ambiguous_prior_effects",
            }
            tags = dict(job.tags or {})
            tags.update({
                "verification_pending": "true",
                "ambiguous_prior_effects": "true",
                "hold_kind": "work_control",
                "blocked_reason": "ambiguous_prior_effects",
            })
            changed = await self._db.execute(
                """UPDATE jobs
                   SET status = ?, result = ?, tags = ?,
                       claimed_by = NULL, claimed_at = NULL,
                       claim_expires_at = NULL,
                       last_heartbeat = NULL
                   WHERE job_id = ? AND claimed_by = ? AND status = ?
                     AND claim_attempt_id IS ?""",
                (
                    JobStatus.NEUTRAL.value, _serialize_result(result),
                    json.dumps(tags), job_id, worker_id,
                    JobStatus.RUNNING.value, job.claim_attempt_id,
                ),
            )
            if changed.rowcount != 1:
                await self._db.rollback()
                return False
            await self._audit(
                job_id, old_status, JobStatus.NEUTRAL.value,
                node_id=worker_id,
                reason=f"ambiguous_prior_effects:{error}",
                claim_attempt_id=job.claim_attempt_id,
                details={"automatic_retry_forbidden": True},
            )
            await self._finalize_pending_controls_after_transition(
                job_id, claim_attempt_id, reason="failure_won_race",
            )
            outcome_event_id = (
                f"worker-outcome:{job_id}:{job.claim_attempt_id}:ambiguous"
            )
            await self._enqueue_worker_outcome(
                event_id=outcome_event_id,
                job_id=job_id,
                claim_attempt_id=str(job.claim_attempt_id),
                report={
                    "status": "ambiguous",
                    "summary": error,
                    "reason": "ambiguous_prior_effects",
                },
                verdict="unverified",
                outcome="neutral",
                worker_mode=self._job_governor_mode(job),
                success_attested=False,
                latency=duration,
                attempts=job.retry_count,
            )
            await self._db.commit()
            self._schedule_worker_outcome_drain(outcome_event_id)
            return True
        new_retry = job.retry_count + 1
        if job.retry_count < job.max_retries and not deadline_expired:
            # Re-queue
            changed = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, claimed_by = NULL, claimed_at = NULL,
                    claim_attempt_id = NULL, claim_expires_at = NULL,
                    last_heartbeat = NULL, result = ?
                WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
                  AND claim_attempt_id IS ?
                """,
                (
                    JobStatus.QUEUED.value, new_retry, _serialize_result(result),
                    job_id, worker_id, JobStatus.CLAIMED.value,
                    JobStatus.RUNNING.value,
                    job.claim_attempt_id,
                ),
            )
            if changed.rowcount != 1:
                await self._db.rollback()
                return False
            await self._audit(
                job_id, old_status, JobStatus.QUEUED.value,
                node_id=worker_id, reason=f"retry {new_retry}/{job.max_retries}: {error}",
                claim_attempt_id=job.claim_attempt_id,
            )
        else:
            changed = await self._db.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, result = ?,
                    claimed_by = NULL, claimed_at = NULL,
                    claim_expires_at = NULL, last_heartbeat = NULL
                WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
                  AND claim_attempt_id IS ?
                """,
                (
                    JobStatus.FAILED.value, new_retry, _serialize_result(result),
                    job_id, worker_id, JobStatus.CLAIMED.value,
                    JobStatus.RUNNING.value,
                    job.claim_attempt_id,
                ),
            )
            if changed.rowcount != 1:
                await self._db.rollback()
                return False
            await self._audit(
                job_id, old_status, JobStatus.FAILED.value,
                node_id=worker_id, reason=error,
                claim_attempt_id=job.claim_attempt_id,
            )
        await self._finalize_pending_controls_after_transition(
            job_id, claim_attempt_id, reason="failure_won_race",
        )
        outcome_event_id = (
            f"worker-outcome:{job_id}:{job.claim_attempt_id}:failure"
        )
        await self._enqueue_worker_outcome(
            event_id=outcome_event_id,
            job_id=job_id,
            claim_attempt_id=str(job.claim_attempt_id),
            report={"status": "failed", "summary": error},
            verdict="clean",
            outcome="failure",
            worker_mode=self._job_governor_mode(job),
            success_attested=False,
            latency=duration,
            attempts=job.retry_count,
        )
        await self._db.commit()
        self._schedule_worker_outcome_drain(outcome_event_id)
        return True

    @_serialized_read
    async def worker_for_claim_attempt(
        self,
        job_id: str,
        claim_attempt_id: Optional[str],
    ) -> Optional[str]:
        """Resolve the server-recorded worker for a lifecycle replay."""

        assert self._db is not None
        cursor = await self._db.execute(
            """SELECT node_id FROM job_audit
               WHERE job_id = ? AND claim_attempt_id IS ?
                 AND node_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (job_id, claim_attempt_id),
        )
        row = await cursor.fetchone()
        return str(row["node_id"]) if row is not None else None

    @_serialized_mutation
    async def release_job(
        self,
        job_id: str,
        worker_id: str,
        claim_attempt_id: Optional[str] = None,
    ) -> bool:
        """Transition CLAIMED/RUNNING → QUEUED, clearing the worker claim."""
        assert self._db is not None
        job = await self.get_job(job_id)
        if await self._attempt_has_pending_work_control(
            job_id, claim_attempt_id, stop_only=True,
        ):
            return False
        if self._ambiguous_attempt_replay(
            job, worker_id, claim_attempt_id,
        ):
            return True
        if (
            job is not None
            and job.status is JobStatus.QUEUED
            and job.claimed_by is None
            and claim_attempt_id is not None
        ):
            cursor = await self._db.execute(
                """SELECT 1 FROM job_audit
                   WHERE job_id = ? AND node_id = ?
                     AND claim_attempt_id = ? AND to_status = ?
                     AND reason = 'released_by_worker'
                   ORDER BY id DESC LIMIT 1""",
                (
                    job_id, worker_id, claim_attempt_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if await cursor.fetchone() is not None:
                return True
        if (
            job is None
            or job.claimed_by != worker_id
            or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or not self._attempt_matches(job, claim_attempt_id)
        ):
            return False
        if job.status is JobStatus.RUNNING:
            from colony_sidecar.task_queue.governor import job_declares_effect

            if job_declares_effect(job):
                return await self.fail_job(
                    job_id,
                    worker_id,
                    "worker_release_after_effectful_start",
                    claim_attempt_id=claim_attempt_id,
                )
        changed = await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = NULL, claimed_at = NULL,
                claim_attempt_id = NULL, claim_expires_at = NULL,
                last_heartbeat = NULL
            WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
              AND claim_attempt_id IS ?
            """,
            (
                JobStatus.QUEUED.value, job_id, worker_id,
                JobStatus.CLAIMED.value, JobStatus.RUNNING.value,
                job.claim_attempt_id,
            ),
        )
        if changed.rowcount != 1:
            await self._db.rollback()
            return False
        await self._audit(
            job_id, job.status.value, JobStatus.QUEUED.value,
            node_id=worker_id, reason="released_by_worker",
            claim_attempt_id=job.claim_attempt_id,
        )
        await self._finalize_pending_controls_after_transition(
            job_id, claim_attempt_id, reason="release_won_race",
        )
        await self._db.commit()
        return True

    @_serialized_mutation
    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        reason: str = "",
        tags: Optional[Dict[str, str]] = None,
        remove_tags: Optional[List[str]] = None,
    ) -> bool:
        """Update a job's status and optionally merge new tags.

        Returns True if the job was found and updated.
        """
        assert self._db is not None
        job = await self.get_job(job_id)
        if (
            job is None
            or job.is_terminal()
            or job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or str(job.tags.get("verification_pending") or "").lower()
            == "true"
            or str(job.tags.get("ambiguous_prior_effects") or "").lower()
            == "true"
        ):
            return False
        old_status = job.status.value
        for key in remove_tags or []:
            job.tags.pop(str(key), None)
        if tags:
            job.tags.update(tags)
        if tags or remove_tags:
            changed = await self._db.execute(
                "UPDATE jobs SET status = ?, tags = ? "
                "WHERE job_id = ? AND status = ?",
                (status.value, json.dumps(job.tags), job_id, old_status),
            )
        else:
            changed = await self._db.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ? AND status = ?",
                (status.value, job_id, old_status),
            )
        if changed.rowcount != 1:
            await self._db.rollback()
            return False
        await self._audit(job_id, old_status, status.value, reason=reason)
        await self._db.commit()
        return True

    @_serialized_mutation
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

    @_serialized_mutation
    async def cancel_job(self, job_id: str, reason: str = "") -> bool:
        """Cancel inactive work; running work requires WorkControl cooperation.

        The legacy method remains source/API compatible, but it must never
        claim that a running handler stopped merely because a SQLite status
        changed beneath it.
        """
        assert self._db is not None
        job = await self.get_job(job_id)
        if (
            job is None
            or job.is_terminal()
            or job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
        ):
            return False
        old_status = job.status.value
        changed = await self._db.execute(
            """UPDATE jobs SET status = ?, claimed_by = NULL,
                      claimed_at = NULL, claim_attempt_id = NULL,
                      claim_expires_at = NULL, last_heartbeat = NULL
               WHERE job_id = ? AND status = ?""",
            (JobStatus.CANCELLED.value, job_id, old_status),
        )
        if changed.rowcount != 1:
            await self._db.rollback()
            return False
        await self._audit(job_id, old_status, JobStatus.CANCELLED.value, reason=reason)
        await self._db.commit()
        return True

    # ------------------------------------------------------------------
    # WorkControlV1 — operator/agent steering over real queue work
    # ------------------------------------------------------------------

    async def _work_control_target_row(self, job: Job) -> aiosqlite.Row:
        """Return (and if needed create) one stable queue-job run binding."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import digest_json

        target_id = job.job_id
        if (
            isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
        ):
            from colony_sidecar.work_orders import WorkOrderV1

            authority_digest = WorkOrderV1.from_payload(
                job.payload,
            ).work_order_digest
        else:
            action_digest = ""
            try:
                from colony_sidecar.initiatives.approval_authority import (
                    build_action_binding,
                )

                action_digest = build_action_binding(
                    job_id=job.job_id,
                    job_type=job.job_type.value,
                    payload=job.payload,
                ).action_digest
            except Exception:
                # Non-action queue jobs have no registered action binding;
                # their complete immutable execution envelope is hashed below.
                action_digest = ""
            authority_digest = digest_json({
                "schema": "QueueJobAuthorityV1",
                "job_id": job.job_id,
                "job_type": job.job_type.value,
                "payload": job.payload,
                "priority": job.priority.value,
                "capabilities": [
                    {
                        "name": item.name,
                        "minimum": item.minimum,
                        "preferred": item.preferred,
                    }
                    for item in job.capabilities
                ],
                "deadline": job.deadline.isoformat() if job.deadline else None,
                "max_retries": job.max_retries,
                "timeout_secs": job.timeout_secs,
                "depends_on": job.depends_on,
                "posted_by": job.posted_by,
                "posted_at": job.posted_at.isoformat(),
                "action_digest": action_digest,
            })
        cursor = await self._db.execute(
            "SELECT * FROM work_control_targets WHERE target_id = ?",
            (target_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            if str(row["authority_digest"]) != authority_digest:
                from colony_sidecar.task_queue.work_control import WorkControlError

                raise WorkControlError(
                    "work_authority_drift",
                    "queue execution authority changed after control binding",
                )
            return row
        run_id = "run-" + _uuid_module.uuid5(
            _uuid_module.NAMESPACE_URL,
            f"colony:queue-job:{target_id}",
        ).hex
        now = _now_iso()
        await self._db.execute(
            """INSERT OR IGNORE INTO work_control_targets (
                   target_id, run_id, authority_digest, revision,
                   state_digest, projected_at
               ) VALUES (?, ?, ?, 0, '', ?)""",
            (target_id, run_id, authority_digest, now),
        )
        cursor = await self._db.execute(
            "SELECT * FROM work_control_targets WHERE target_id = ?",
            (target_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("WorkControl target could not be materialized")
        return row

    async def _work_control_attempt_truth(
        self,
        job: Job,
    ) -> Dict[str, Any]:
        """Derive aggregate prior-effect truth from every server-ledger attempt.

        A newer claim that never reached ``RUNNING`` cannot erase the effect
        uncertainty of an older started attempt.  Retry is therefore safe
        only when every historical started effect attempt has its own exact
        ``not_applied`` reconciliation.  One ``applied`` finding dominates
        all negative findings, while any unresolved started attempt keeps the
        target quarantined.
        """

        assert self._db is not None
        cursor = await self._db.execute(
            """SELECT id, timestamp, from_status, to_status, node_id,
                      claim_attempt_id, reason, details
               FROM job_audit
               WHERE job_id = ? AND claim_attempt_id IS NOT NULL
                     AND claim_attempt_id != ''
               ORDER BY id DESC""",
            (job.job_id,),
        )
        rows = await cursor.fetchall()
        active = (
            str(job.claim_attempt_id)
            if job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            and job.claim_attempt_id else None
        )
        ordered_attempt_ids: List[str] = []
        attempt_rows: Dict[str, List[aiosqlite.Row]] = {}
        if active:
            ordered_attempt_ids.append(active)
            attempt_rows[active] = []
        for row in rows:
            attempt_id = str(row["claim_attempt_id"])
            if attempt_id not in attempt_rows:
                ordered_attempt_ids.append(attempt_id)
                attempt_rows[attempt_id] = []
            attempt_rows[attempt_id].append(row)
        last_attempt = (
            ordered_attempt_ids[0] if ordered_attempt_ids else None
        )

        reconciliation_cursor = await self._db.execute(
            """SELECT attempt_id, finding, reconciliation_id,
                      evidence_digest, verifier_identity, verifier_type,
                      created_at
               FROM work_effect_reconciliations
               WHERE target_id = ?""",
            (job.job_id,),
        )
        reconciliations = {
            str(row["attempt_id"]): row
            for row in await reconciliation_cursor.fetchall()
        }
        from colony_sidecar.task_queue.governor import job_declares_effect

        effectful = bool(job_declares_effect(job))
        attempts: List[Dict[str, Any]] = []
        for attempt_id in ordered_attempt_ids:
            history = attempt_rows.get(attempt_id, [])
            worker_id = next(
                (
                    str(row["node_id"])
                    for row in history if row["node_id"]
                ),
                None,
            )
            if attempt_id == active and job.claimed_by:
                worker_id = str(job.claimed_by)
            started = any(
                row["to_status"] == JobStatus.RUNNING.value
                for row in history
            )
            server_started_at = next(
                (
                    str(row["timestamp"])
                    for row in reversed(history)
                    if row["to_status"] == JobStatus.RUNNING.value
                ),
                None,
            )
            terminal_observed_at: Optional[str] = None
            if started:
                for row in history:
                    # A started attempt first becomes independently
                    # reconcilable when the server ledger observes it leave
                    # RUNNING.  This also recognizes P6-era RUNNING->QUEUED
                    # rows whose reason did not yet call the state ambiguous.
                    if (
                        row["from_status"] == JobStatus.RUNNING.value
                        and row["to_status"]
                        not in {
                            JobStatus.CLAIMED.value,
                            JobStatus.RUNNING.value,
                        }
                    ):
                        terminal_observed_at = str(row["timestamp"])
                        break
            reconciliation = reconciliations.get(attempt_id)
            finding = (
                str(reconciliation["finding"])
                if reconciliation is not None else None
            )
            if not started:
                attempt_disposition = "not_started"
            elif not effectful:
                attempt_disposition = "read_only"
            elif finding == "not_applied":
                attempt_disposition = "verified_not_applied"
            elif finding == "applied":
                attempt_disposition = "verified_applied"
            else:
                attempt_disposition = "ambiguous_prior_effects"
            attempts.append({
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "started": started,
                "server_started_at": server_started_at,
                "terminal_ambiguity_observed_at": terminal_observed_at,
                "effect_disposition": attempt_disposition,
                "reconciliation_finding": finding,
                "reconciliation_id": (
                    str(reconciliation["reconciliation_id"])
                    if reconciliation is not None else None
                ),
                "reconciliation_evidence_digest": (
                    str(reconciliation["evidence_digest"])
                    if reconciliation is not None else None
                ),
                "reconciliation_verifier_identity": (
                    str(reconciliation["verifier_identity"])
                    if reconciliation is not None else None
                ),
                "reconciliation_verifier_type": (
                    str(reconciliation["verifier_type"])
                    if reconciliation is not None else None
                ),
                "reconciled_at": (
                    str(reconciliation["created_at"])
                    if reconciliation is not None else None
                ),
            })

        started_attempts = [item for item in attempts if item["started"]]
        applied_attempts = [
            item for item in started_attempts
            if item["effect_disposition"] == "verified_applied"
        ]
        unresolved_attempts = [
            item for item in started_attempts
            if item["effect_disposition"] == "ambiguous_prior_effects"
        ]
        not_applied_attempts = [
            item for item in started_attempts
            if item["effect_disposition"] == "verified_not_applied"
        ]
        if not started_attempts:
            disposition = "not_started"
        elif not effectful:
            disposition = "read_only"
        elif unresolved_attempts:
            disposition = "ambiguous_prior_effects"
        elif applied_attempts:
            disposition = "verified_applied"
        else:
            # Every historical started effect has its own negative proof.
            disposition = "verified_not_applied"

        blocking = (
            unresolved_attempts[0]
            if unresolved_attempts else (
                applied_attempts[0] if applied_attempts else None
            )
        )
        last_record = attempts[0] if attempts else None
        reconciliation_finding: Optional[str]
        if applied_attempts:
            reconciliation_finding = "applied"
        elif started_attempts and len(not_applied_attempts) == len(
            started_attempts
        ):
            reconciliation_finding = "not_applied"
        else:
            reconciliation_finding = None
        return {
            "active_attempt_id": active,
            "last_attempt_id": last_attempt,
            "last_worker_id": (
                last_record["worker_id"] if last_record else None
            ),
            "last_attempt_started": bool(
                last_record and last_record["started"]
            ),
            "effectful": effectful,
            "effect_disposition": disposition,
            "reconciliation_finding": reconciliation_finding,
            "blocking_attempt_id": (
                blocking["attempt_id"] if blocking else None
            ),
            "blocking_worker_id": (
                blocking["worker_id"] if blocking else None
            ),
            "unresolved_attempt_ids": [
                item["attempt_id"] for item in unresolved_attempts
            ],
            "applied_attempt_ids": [
                item["attempt_id"] for item in applied_attempts
            ],
            "not_applied_attempt_ids": [
                item["attempt_id"] for item in not_applied_attempts
            ],
            "attempts": attempts,
        }

    async def _quarantine_queued_effect_truth(
        self,
        job: Job,
        truth: Mapping[str, Any],
        *,
        reason: str,
    ) -> Optional[str]:
        """Fence one persisted effect retry before another claim can exist.

        P6 could persist an effectful RUNNING attempt as QUEUED after failure,
        clearing the live claim while retaining its exact attempt in the
        server audit/result.  That row is not safe to execute again unless an
        independent reconciler proves ``not_applied``.  An existing
        ``applied`` reconciliation is also terminal effect truth and must not
        be overwritten or reclaimed.
        """

        assert self._db is not None
        disposition = str(truth.get("effect_disposition") or "")
        if (
            job.status is not JobStatus.QUEUED
            or disposition not in {
                "ambiguous_prior_effects", "verified_applied",
            }
        ):
            return None
        attempt_id = str(
            truth.get("blocking_attempt_id")
            or truth.get("last_attempt_id")
            or ""
        ).strip()
        if not attempt_id:
            raise RuntimeError(
                "effect retry quarantine requires exact prior attempt"
            )
        previous = job.result
        exact_record = next(
            (
                item for item in truth.get("attempts", [])
                if item.get("attempt_id") == attempt_id
            ),
            None,
        )
        previous_is_exact = bool(
            previous is not None
            and previous.claim_attempt_id == attempt_id
        )
        worker_id = str(
            truth.get("blocking_worker_id")
            or truth.get("last_worker_id")
            or (previous.worker_node_id if previous is not None else "")
        ).strip()
        now = datetime.now(timezone.utc)
        started_at = (
            previous.started_at if previous_is_exact else _parse_dt(
                exact_record.get("server_started_at")
                if exact_record is not None else None
            )
        )
        completed_at = (
            previous.completed_at
            if previous_is_exact and previous.completed_at is not None
            else (
                _parse_dt(exact_record.get(
                    "terminal_ambiguity_observed_at"
                )) if exact_record is not None else None
            )
        ) or now
        if started_at is not None:
            started_at = (
                started_at.replace(tzinfo=timezone.utc)
                if started_at.tzinfo is None
                else started_at.astimezone(timezone.utc)
            )
        completed_at = (
            completed_at.replace(tzinfo=timezone.utc)
            if completed_at.tzinfo is None
            else completed_at.astimezone(timezone.utc)
        )
        output = (
            dict(previous.output or {}) if previous_is_exact else {}
        )
        tags = dict(job.tags or {})
        if disposition == "ambiguous_prior_effects":
            output.update({
                "status": "ambiguous",
                "reason": "ambiguous_prior_effects",
            })
            error = "legacy_retry_after_effectful_attempt_started"
            tags.update({
                "verification_pending": "true",
                "ambiguous_prior_effects": "true",
                "hold_kind": "work_control",
                "blocked_reason": "ambiguous_prior_effects",
            })
        else:
            # Independent positive evidence proves the effect happened, but
            # does not manufacture semantic success. Keep it NEUTRAL for the
            # existing richer action/WorkOrder attester.
            error = "verified_applied_without_semantic_success"
            for key in (
                "verification_pending", "ambiguous_prior_effects",
                "hold_kind", "blocked_reason",
            ):
                tags.pop(key, None)
            tags["semantic_attestation_pending"] = "true"
        result = JobResult(
            job_id=job.job_id,
            worker_node_id=worker_id,
            status=JobStatus.NEUTRAL,
            output=output,
            error=error,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=(
                previous.duration_seconds
                if previous_is_exact else (
                    max(0.0, (completed_at - started_at).total_seconds())
                    if started_at is not None else None
                )
            ),
            claim_attempt_id=attempt_id,
        )
        changed = await self._db.execute(
            """UPDATE jobs
               SET status = ?, result = ?, tags = ?,
                   claimed_by = NULL, claimed_at = NULL,
                   claim_attempt_id = ?, claim_expires_at = NULL,
                   last_heartbeat = NULL
               WHERE job_id = ? AND status = ?""",
            (
                JobStatus.NEUTRAL.value, _serialize_result(result),
                json.dumps(tags), attempt_id, job.job_id,
                JobStatus.QUEUED.value,
            ),
        )
        if changed.rowcount != 1:
            return None
        await self._audit(
            job.job_id,
            JobStatus.QUEUED.value,
            JobStatus.NEUTRAL.value,
            node_id=worker_id or None,
            claim_attempt_id=attempt_id,
            reason=f"{disposition}:{reason}",
            details={"automatic_retry_forbidden": True},
        )
        await self._finalize_pending_controls_after_transition(
            job.job_id,
            attempt_id,
            reason=reason,
        )
        if disposition != "ambiguous_prior_effects":
            return None
        event_id = f"worker-outcome:{job.job_id}:{attempt_id}:ambiguous"
        await self._enqueue_worker_outcome(
            event_id=event_id,
            job_id=job.job_id,
            claim_attempt_id=attempt_id,
            report={
                "status": "ambiguous",
                "summary": "persisted effect retry quarantined",
                "reason": "ambiguous_prior_effects",
            },
            verdict="unverified",
            outcome="neutral",
            worker_mode=self._job_governor_mode(job),
            success_attested=False,
            latency=result.duration_seconds,
            attempts=job.retry_count,
        )
        return event_id

    @_serialized_mutation
    async def reconcile_legacy_effect_retries(self) -> int:
        """Quarantine P6-era effect retries transactionally at startup."""

        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY rowid",
            (JobStatus.QUEUED.value,),
        )
        changed = 0
        event_ids: List[str] = []
        for row in await cursor.fetchall():
            job = _job_from_row(row)
            truth = await self._work_control_attempt_truth(job)
            if truth["effect_disposition"] not in {
                "ambiguous_prior_effects", "verified_applied",
            }:
                continue
            event_id = await self._quarantine_queued_effect_truth(
                job,
                truth,
                reason="legacy_queued_retry_migration",
            )
            changed += 1
            if event_id:
                event_ids.append(event_id)
        if changed:
            await self._db.commit()
        for event_id in event_ids:
            self._schedule_worker_outcome_drain(event_id)
        return changed

    async def _work_control_worker_capabilities(
        self,
        worker_id: Optional[str],
    ) -> set[str]:
        assert self._db is not None
        if not worker_id:
            return set()
        cursor = await self._db.execute(
            "SELECT capabilities FROM workers WHERE node_id = ?",
            (worker_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return set()
        try:
            values = json.loads(row["capabilities"] or "[]")
        except (TypeError, ValueError):
            return set()
        return {
            str(item) for item in values
            if isinstance(item, str) and item
        }

    async def _work_control_pending_rows(
        self,
        target_id: str,
    ) -> List[aiosqlite.Row]:
        assert self._db is not None
        cursor = await self._db.execute(
            """SELECT * FROM work_control_operations
               WHERE target_id = ? AND status = 'pending_ack'
               ORDER BY created_at ASC, operation_id ASC""",
            (target_id,),
        )
        return list(await cursor.fetchall())

    async def _append_work_control_receipt_event(
        self,
        operation_id: str,
        *,
        phase: str,
    ) -> Dict[str, Any]:
        """Append one immutable accepted/outcome receipt fact."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import digest_json

        cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (operation_id,),
        )
        raw = await cursor.fetchone()
        if raw is None:
            raise RuntimeError("WorkControl operation missing for receipt event")
        row = dict(raw)
        if phase not in {"accepted", "outcome"}:
            raise ValueError("WorkControl receipt phase is invalid")
        event = {
            "schema": "WorkControlReceiptEventV1",
            "version": 1,
            "phase": phase,
            "operation_id": row["operation_id"],
            "operation": row["operation_type"],
            "target_id": row["target_id"],
            "run_id": row["run_id"],
            "authority_digest": row["authority_digest"],
            "request_digest": row["request_digest"],
            "requested_by": row["requested_by"],
            "request_authority": json.loads(
                row["request_authority"] or "{}"
            ),
            "attempt_id": row["attempt_id"],
            "worker_id": row["worker_id"],
            "status": row["status"],
            "accepted_revision": row["accepted_revision"],
            "result_revision": row["result_revision"],
            "result_state_digest": row["result_state_digest"],
            "from_job_status": row["from_job_status"],
            "to_job_status": row["to_job_status"],
            "effect_disposition": row["effect_disposition"],
            "acknowledgement": json.loads(row["ack_details"] or "{}"),
            "ack_authority": json.loads(row["ack_authority"] or "{}"),
            "ack_deadline": row["ack_deadline"],
            "recorded_at": (
                row["created_at"]
                if phase == "accepted"
                else row["acknowledged_at"] or _now_iso()
            ),
        }
        receipt_digest = digest_json(event)
        receipt_id = "wcre-" + hashlib.sha256(
            f"{operation_id}:{phase}".encode("utf-8")
        ).hexdigest()[:32]
        existing_cursor = await self._db.execute(
            "SELECT * FROM work_control_receipts WHERE receipt_id = ?",
            (receipt_id,),
        )
        existing = await existing_cursor.fetchone()
        if existing is not None:
            persisted = dict(existing)
            if (
                persisted["receipt_digest"] != receipt_digest
                or persisted["payload_json"] != json.dumps(
                    event, sort_keys=True, separators=(",", ":"),
                )
            ):
                raise RuntimeError("immutable WorkControl receipt event drift")
            return {
                **json.loads(persisted["payload_json"]),
                "receipt_id": persisted["receipt_id"],
                "receipt_digest": persisted["receipt_digest"],
            }
        payload_json = json.dumps(
            event, sort_keys=True, separators=(",", ":"),
        )
        await self._db.execute(
            """INSERT INTO work_control_receipts (
                   receipt_id, operation_id, target_id, run_id,
                   authority_digest, phase, payload_json, receipt_digest,
                   created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, operation_id, row["target_id"], row["run_id"],
                row["authority_digest"], phase, payload_json,
                receipt_digest, event["recorded_at"],
            ),
        )
        return {
            **event,
            "receipt_id": receipt_id,
            "receipt_digest": receipt_digest,
        }

    async def _work_control_receipt_projection(
        self,
        row: Mapping[str, Any],
        *,
        replayed: bool = False,
    ) -> Dict[str, Any]:
        from colony_sidecar.task_queue.work_control import (
            build_receipt,
            canonical_json,
            digest_json,
        )

        assert self._db is not None
        projection = build_receipt(dict(row), replayed=replayed)
        cursor = await self._db.execute(
            """SELECT receipt_id, phase, payload_json, receipt_digest
               FROM work_control_receipts
               WHERE operation_id = ?
               ORDER BY CASE phase WHEN 'accepted' THEN 0 ELSE 1 END,
                        created_at ASC, receipt_id ASC""",
            (row["operation_id"],),
        )
        history = []
        outcome_events: List[Dict[str, Any]] = []
        for event_row in await cursor.fetchall():
            try:
                event = json.loads(event_row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "durable WorkControl receipt event is malformed"
                ) from exc
            if (
                not isinstance(event, dict)
                or digest_json(event) != str(event_row["receipt_digest"])
                or str(event.get("operation_id") or "")
                != str(row["operation_id"])
                or str(event.get("target_id") or "")
                != str(row["target_id"])
                or str(event.get("run_id") or "") != str(row["run_id"])
                or str(event.get("authority_digest") or "")
                != str(row["authority_digest"])
            ):
                raise RuntimeError(
                    "durable WorkControl receipt event integrity failure"
                )
            projected_event = {
                **event,
                "receipt_id": event_row["receipt_id"],
                "receipt_digest": event_row["receipt_digest"],
            }
            history.append(projected_event)
            if event.get("phase") == "outcome":
                outcome_events.append(projected_event)

        # A terminal acknowledgement is replayed from its immutable receipt,
        # never reconstructed from whichever credential happens to be in the
        # current keyring after rotation/restart. Cross-check the mutable row
        # projection against that append-only fact before returning it.
        if str(row["status"]) != "pending_ack":
            if len(outcome_events) != 1:
                raise RuntimeError(
                    "terminal WorkControl receipt has no unique durable outcome"
                )
            outcome_event = outcome_events[0]
            if (
                canonical_json(outcome_event.get("ack_authority") or {})
                != canonical_json(projection.get("ack_authority") or {})
                or canonical_json(outcome_event.get("acknowledgement") or {})
                != canonical_json(projection.get("acknowledgement") or {})
                or str(outcome_event.get("status") or "")
                != str(projection["status"])
            ):
                raise RuntimeError(
                    "durable WorkControl acknowledgement authority drift"
                )
        projection["receipt_events"] = history
        return projection

    async def _work_control_projection_locked(
        self,
        job: Job,
    ) -> Dict[str, Any]:
        """Synchronize and return the CAS projection for *job*."""

        from colony_sidecar.task_queue.work_control import (
            WORK_CONTROL_SCHEMA,
            WORK_CONTROL_VERSION,
            digest_json,
            interrupt_capability,
            steer_capability,
            work_control_mode,
        )

        target_row = await self._work_control_target_row(job)
        mode = work_control_mode()
        run_id = str(target_row["run_id"])
        authority_digest = str(target_row["authority_digest"])
        truth = await self._work_control_attempt_truth(job)
        worker_caps = await self._work_control_worker_capabilities(
            job.claimed_by,
        )
        pending_rows = await self._work_control_pending_rows(job.job_id)
        pending = [
            {
                "operation_id": str(row["operation_id"]),
                "operation": str(row["operation_type"]),
                "attempt_id": row["attempt_id"],
                "worker_id": row["worker_id"],
                "created_at": str(row["created_at"]),
                "ack_deadline": row["ack_deadline"],
            }
            for row in pending_rows
        ]
        allowed: List[Dict[str, Any]] = []
        status = job.status
        active = status in {JobStatus.CLAIMED, JobStatus.RUNNING}
        interrupted = bool(
            status is JobStatus.BLOCKED
            and str(job.tags.get("hold_kind") or "") == "work_control"
            and str(job.tags.get("blocked_reason") or "") == "interrupted"
        )
        retry_blockers: List[str] = []
        hold_kind = str(job.tags.get("hold_kind") or "")
        if hold_kind and not interrupted:
            retry_blockers.append(f"hold:{hold_kind}")
        for dependency_id in job.depends_on:
            dependency = await self.get_job(dependency_id)
            if dependency is None:
                retry_blockers.append(f"dependency_missing:{dependency_id}")
            elif dependency.status is not JobStatus.COMPLETED:
                retry_blockers.append(
                    f"dependency_not_completed:{dependency_id}:{dependency.status.value}"
                )
        if truth["effectful"] and not self._server_approval_provenance_valid(job):
            retry_blockers.append("effect_approval_not_valid")
        if (
            isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
            and job.retry_count >= int(job.payload.get("max_attempts", 1)) - 1
        ):
            retry_blockers.append("work_order_attempt_budget_exhausted")
        pending_types = {item["operation"] for item in pending}
        exact_interrupt = interrupt_capability(job.job_type) in worker_caps
        if pending:
            # Emergency precedence is strict: cancel > interrupt > steer.
            # A stop command can therefore supersede guidance that a worker
            # has not acknowledged instead of being trapped behind it.
            if active and truth["active_attempt_id"]:
                if pending_types <= {"steer"} and exact_interrupt:
                    allowed.extend((
                        {
                            "operation": "interrupt",
                            "attempt_id_required": True,
                            "worker_ack_required": True,
                        },
                        {
                            "operation": "cancel",
                            "attempt_id_required": True,
                            "worker_ack_required": True,
                        },
                    ))
                elif "cancel" not in pending_types and exact_interrupt:
                    allowed.append({
                        "operation": "cancel",
                        "attempt_id_required": True,
                        "worker_ack_required": True,
                    })
                if (
                    status is JobStatus.CLAIMED
                    and "cancel" not in pending_types
                    and not any(
                        item["operation"] == "cancel" for item in allowed
                    )
                ):
                    allowed.append({
                        "operation": "cancel",
                        "attempt_id_required": True,
                        "worker_ack_required": False,
                    })
        else:
            if active and truth["active_attempt_id"]:
                exact_steer = steer_capability(job.job_type)
                steer_before_deadline = bool(
                    job.deadline is None
                    or datetime.now(timezone.utc) < job.deadline
                )
                if exact_steer in worker_caps and steer_before_deadline:
                    allowed.append({
                        "operation": "steer",
                        "attempt_id_required": True,
                        "worker_ack_required": True,
                    })
                if exact_interrupt:
                    allowed.append(
                        {
                            "operation": "interrupt",
                            "attempt_id_required": True,
                            "worker_ack_required": True,
                        },
                    )
                if status is JobStatus.CLAIMED or exact_interrupt:
                    allowed.append({
                            "operation": "cancel",
                            "attempt_id_required": True,
                            "worker_ack_required": (
                                status is JobStatus.RUNNING
                            ),
                        })
            elif status in {
                JobStatus.QUEUED, JobStatus.BLOCKED, JobStatus.ABANDONED,
            }:
                allowed.append({
                    "operation": "cancel",
                    "attempt_id_required": False,
                    "worker_ack_required": False,
                })

            retry_state = status in {
                JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.ABANDONED,
            } or interrupted
            retry_safe = truth["effect_disposition"] in {
                "not_started", "read_only", "verified_not_applied",
            }
            retry_budget = job.retry_count < job.max_retries
            unexpired = not job.is_expired()
            if (
                retry_state and retry_safe and retry_budget and unexpired
                and not retry_blockers
            ):
                allowed.append({
                    "operation": "retry",
                    "attempt_id_required": bool(truth["last_attempt_id"]),
                    "worker_ack_required": False,
                })

        target_kind = (
            "work_order"
            if isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
            else "queue_job"
        )
        state = {
            "target_kind": target_kind,
            "target_id": job.job_id,
            "run_id": run_id,
            "authority_digest": authority_digest,
            "job_type": job.job_type.value,
            "job_status": job.status.value,
            "active_attempt_id": truth["active_attempt_id"],
            "last_attempt_id": truth["last_attempt_id"],
            "claimed_by": job.claimed_by,
            "retry_count": job.retry_count,
            "max_retries": job.max_retries,
            "deadline": job.deadline.isoformat() if job.deadline else None,
            "effectful": truth["effectful"],
            "effect_disposition": truth["effect_disposition"],
            "effect_reconciliation": truth["reconciliation_finding"],
            "retry_blockers": retry_blockers,
            "pending_operations": pending,
        }
        would_allow = allowed
        allowed = would_allow if mode == "live" else []
        state_digest = digest_json({
            "schema": WORK_CONTROL_SCHEMA,
            "version": WORK_CONTROL_VERSION,
            "mode": mode,
            "state": state,
            "allowed_operations": allowed,
            "would_allow_operations": would_allow,
        })
        revision = int(target_row["revision"])
        if state_digest != str(target_row["state_digest"] or ""):
            revision += 1
            await self._db.execute(
                """UPDATE work_control_targets
                   SET revision = ?, state_digest = ?, projected_at = ?
                   WHERE target_id = ?""",
                (revision, state_digest, _now_iso(), job.job_id),
            )
        return {
            "schema": WORK_CONTROL_SCHEMA,
            "version": WORK_CONTROL_VERSION,
            "mode": mode,
            "target_id": job.job_id,
            "run_id": run_id,
            "authority_digest": authority_digest,
            "revision": revision,
            "state_digest": state_digest,
            "state": state,
            "allowed_operations": allowed,
            "would_allow_operations": would_allow,
        }

    @_serialized_mutation
    async def get_work_control_target(self, target_id: str) -> Dict[str, Any]:
        """Read one durable WorkControlV1 CAS projection."""

        assert self._db is not None
        job = await self.get_job(target_id)
        if job is None:
            from colony_sidecar.task_queue.work_control import WorkControlError

            raise WorkControlError(
                "work_target_not_found", "queue work target does not exist",
                status_code=404,
            )
        projection = await self._work_control_projection_locked(job)
        await self._db.commit()
        return projection

    @_serialized_mutation
    async def reconcile_work_effect(
        self,
        *,
        reconciliation_id: str,
        target_id: str,
        attempt_id: str,
        authority_digest: str,
        finding: str,
        evidence_refs: List[str],
        observed_at: str,
        summary: str,
        verifier_identity: str,
        verifier_type: str,
        verifier_authority: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve one ambiguous effect from independent exact-attempt proof."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import (
            WorkControlError,
            canonical_json,
            digest_json,
            validate_digest,
            validate_operation_id,
        )

        reconciliation_id = validate_operation_id(reconciliation_id)
        target_id = str(target_id or "").strip()
        attempt_id = str(attempt_id or "").strip()
        authority_digest = validate_digest(
            authority_digest, field="authority_digest",
        )
        finding = str(finding or "").strip().lower()
        verifier_identity = str(verifier_identity or "").strip()
        verifier_type = str(verifier_type or "").strip()
        summary = str(summary or "").strip()
        if finding not in {"applied", "not_applied"}:
            raise WorkControlError(
                "invalid_effect_finding",
                "finding must be applied or not_applied",
                status_code=422,
            )
        if (
            not target_id or len(target_id) > 192
            or not attempt_id or len(attempt_id) > 128
            or not verifier_identity or len(verifier_identity) > 256
            or not verifier_type or len(verifier_type) > 128
            or len(summary) > 1000
        ):
            raise WorkControlError(
                "invalid_effect_reconciliation",
                "reconciliation identity, attempt, verifier, or summary is invalid",
                status_code=422,
            )
        if (
            not isinstance(evidence_refs, list)
            or not 1 <= len(evidence_refs) <= 32
        ):
            raise WorkControlError(
                "effect_evidence_required",
                "one to 32 independent evidence references are required",
                status_code=422,
            )
        normalized_refs: List[str] = []
        for value in evidence_refs:
            ref = str(value or "").strip()
            if not ref or len(ref) > 512:
                raise WorkControlError(
                    "invalid_effect_evidence_ref",
                    "each evidence reference must be 1..512 characters",
                    status_code=422,
                )
            normalized_refs.append(ref)
        normalized_refs = list(dict.fromkeys(normalized_refs))
        try:
            observed = datetime.fromisoformat(str(observed_at))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed = observed.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_effect_observed_at",
                "observed_at must be an ISO-8601 timestamp",
                status_code=422,
            ) from exc
        if observed > datetime.now(timezone.utc) + timedelta(minutes=15):
            raise WorkControlError(
                "invalid_effect_observed_at",
                "observed_at is implausibly in the future",
                status_code=422,
            )
        authority = dict(verifier_authority or {
            "authority_kind": "trusted_internal_verifier",
        })
        try:
            authority_json = canonical_json(authority)
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_verifier_authority",
                "verifier authority must be finite JSON",
                status_code=422,
            ) from exc
        evidence = {
            "schema": "WorkEffectReconciliationV1",
            "version": 1,
            "reconciliation_id": reconciliation_id,
            "target_id": target_id,
            "attempt_id": attempt_id,
            "authority_digest": authority_digest,
            "finding": finding,
            "evidence_refs": normalized_refs,
            "observed_at": observed.isoformat(),
            "summary": summary,
        }
        evidence_json = canonical_json(evidence)
        evidence_digest = digest_json(evidence)
        replay_cursor = await self._db.execute(
            """SELECT * FROM work_effect_reconciliations
               WHERE reconciliation_id = ?""",
            (reconciliation_id,),
        )
        replay = await replay_cursor.fetchone()
        if replay is not None:
            if (
                replay["evidence_digest"] != evidence_digest
                or replay["verifier_identity"] != verifier_identity
            ):
                raise WorkControlError(
                    "reconciliation_id_conflict",
                    "reconciliation_id already binds different evidence",
                )
            return {
                "schema": "WorkEffectReconciliationReceiptV1",
                "version": 1,
                "reconciliation_id": reconciliation_id,
                "target_id": target_id,
                "attempt_id": attempt_id,
                "finding": str(replay["finding"]),
                "evidence_digest": str(replay["evidence_digest"]),
                "verifier_identity": str(replay["verifier_identity"]),
                "created_at": str(replay["created_at"]),
                "replayed": True,
            }
        existing_cursor = await self._db.execute(
            """SELECT reconciliation_id FROM work_effect_reconciliations
               WHERE target_id = ? AND attempt_id = ?""",
            (target_id, attempt_id),
        )
        if await existing_cursor.fetchone() is not None:
            raise WorkControlError(
                "effect_attempt_already_reconciled",
                "this exact attempt already has a reconciliation finding",
            )
        job = await self.get_job(target_id)
        if (
            job is None
            or job.status is not JobStatus.NEUTRAL
            or job.result is None
            or job.result.status is not JobStatus.NEUTRAL
            or str(job.tags.get("ambiguous_prior_effects") or "").lower()
            != "true"
            or str(job.tags.get("verification_pending") or "").lower()
            != "true"
        ):
            raise WorkControlError(
                "effect_attempt_not_ambiguous",
                "target is not the exact unresolved ambiguous attempt",
            )
        truth_before = await self._work_control_attempt_truth(job)
        exact_attempt = next(
            (
                item for item in truth_before["attempts"]
                if item["attempt_id"] == attempt_id
            ),
            None,
        )
        if (
            exact_attempt is None
            or exact_attempt["effect_disposition"]
            != "ambiguous_prior_effects"
        ):
            raise WorkControlError(
                "effect_attempt_not_ambiguous",
                "target is not the exact unresolved ambiguous attempt",
            )
        terminal_observed_raw = exact_attempt.get(
            "terminal_ambiguity_observed_at"
        )
        terminal_observed = _parse_dt(terminal_observed_raw)
        if terminal_observed is None:
            raise WorkControlError(
                "effect_ambiguity_ledger_missing",
                "the exact attempt has no server-ledger terminal ambiguity observation",
            )
        if terminal_observed.tzinfo is None:
            terminal_observed = terminal_observed.replace(tzinfo=timezone.utc)
        else:
            terminal_observed = terminal_observed.astimezone(timezone.utc)
        if observed < terminal_observed:
            raise WorkControlError(
                "effect_evidence_predates_ambiguity",
                "observed_at must be at or after the exact attempt's server-ledger ambiguity observation",
                status_code=422,
            )
        exact_worker = str(exact_attempt.get("worker_id") or "").strip()
        if exact_worker == verifier_identity:
            raise WorkControlError(
                "independent_verifier_required",
                "the executor cannot reconcile its own ambiguous effect",
                status_code=403,
            )
        target_row = await self._work_control_target_row(job)
        if str(target_row["authority_digest"]) != authority_digest:
            raise WorkControlError(
                "work_authority_mismatch",
                "reconciliation must bind the exact work authority digest",
            )
        created_at = _now_iso()
        await self._db.execute(
            """INSERT INTO work_effect_reconciliations (
                   reconciliation_id, target_id, attempt_id,
                   authority_digest, finding, evidence_json, evidence_digest,
                   verifier_identity, verifier_type, verifier_authority,
                   created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reconciliation_id, target_id, attempt_id, authority_digest,
                finding, evidence_json, evidence_digest, verifier_identity,
                verifier_type, authority_json, created_at,
            ),
        )
        truth_after = await self._work_control_attempt_truth(job)
        tags = dict(job.tags or {})
        aggregate_disposition = str(truth_after["effect_disposition"])
        remaining_unresolved = list(
            truth_after["unresolved_attempt_ids"]
        )
        next_blocking = next(
            (
                item for item in truth_after["attempts"]
                if item["attempt_id"]
                == truth_after.get("blocking_attempt_id")
            ),
            None,
        )
        exact_after = next(
            (
                item for item in truth_after["attempts"]
                if item["attempt_id"] == attempt_id
            ),
            None,
        )
        tag_evidence = exact_after
        if aggregate_disposition == "verified_applied":
            tag_evidence = next(
                (
                    item for item in truth_after["attempts"]
                    if item["effect_disposition"] == "verified_applied"
                ),
                exact_after,
            )
        if tag_evidence is None:
            raise RuntimeError("exact reconciliation evidence disappeared")
        tags.update({
            "effect_reconciliation_finding": str(
                tag_evidence.get("reconciliation_finding") or finding
            ),
            "effect_reconciliation_id": str(
                tag_evidence.get("reconciliation_id") or reconciliation_id
            ),
            "effect_reconciliation_evidence_digest": str(
                tag_evidence.get("reconciliation_evidence_digest")
                or evidence_digest
            ),
            "effect_reconciliation_verifier": str(
                tag_evidence.get("reconciliation_verifier_identity")
                or verifier_identity
            )[:256],
            "effect_reconciliation_verifier_type": str(
                tag_evidence.get("reconciliation_verifier_type")
                or verifier_type
            )[:128],
            "effect_reconciliation_attempt_id": str(
                tag_evidence["attempt_id"]
            ),
            "effect_reconciliation_at": str(
                tag_evidence.get("reconciled_at") or created_at
            ),
        })
        if aggregate_disposition == "ambiguous_prior_effects":
            target_status = JobStatus.NEUTRAL
            tags.update({
                "verification_pending": "true",
                "ambiguous_prior_effects": "true",
                "hold_kind": "work_control",
                "blocked_reason": "ambiguous_prior_effects",
            })
            tags.pop("semantic_attestation_pending", None)
            tags.pop("effect_reconciliation_retry_pending", None)
        else:
            for key in (
                "verification_pending", "ambiguous_prior_effects",
                "hold_kind", "blocked_reason",
                "work_control_interrupted",
            ):
                tags.pop(key, None)
            if aggregate_disposition == "verified_applied":
                # Applied proves effect disposition, not semantic success.
                # Keep the evidence live until a richer action/WorkOrder
                # attester consumes it.
                target_status = JobStatus.NEUTRAL
                tags["semantic_attestation_pending"] = "true"
                tags.pop("effect_reconciliation_retry_pending", None)
            elif aggregate_disposition == "verified_not_applied":
                target_status = JobStatus.CANCELLED
                tags["effect_reconciliation_retry_pending"] = "true"
                tags.pop("semantic_attestation_pending", None)
            else:
                raise RuntimeError(
                    "effect reconciliation produced unsafe aggregate truth"
                )
        reconciled_output = dict(job.result.output or {})
        reconciled_output["effect_reconciliation"] = {
            "attempt_id": str(tag_evidence["attempt_id"]),
            "finding": str(
                tag_evidence.get("reconciliation_finding") or finding
            ),
            "evidence_digest": str(
                tag_evidence.get("reconciliation_evidence_digest")
                or evidence_digest
            ),
            "verifier_identity": str(
                tag_evidence.get("reconciliation_verifier_identity")
                or verifier_identity
            )[:256],
            "remaining_unresolved_attempt_ids": remaining_unresolved,
        }
        if str(tag_evidence["attempt_id"]) != attempt_id:
            reconciled_output["latest_effect_reconciliation"] = {
                "attempt_id": attempt_id,
                "finding": finding,
                "evidence_digest": evidence_digest,
                "verifier_identity": verifier_identity[:256],
            }
        result_attempt_id = str(
            (
                next_blocking["attempt_id"]
                if next_blocking is not None else attempt_id
            )
        )
        result_worker_id = str(
            (
                next_blocking.get("worker_id")
                if next_blocking is not None else exact_worker
            )
            or job.result.worker_node_id
            or ""
        )
        result_started_at = job.result.started_at
        result_completed_at = job.result.completed_at
        result_duration = job.result.duration_seconds
        if next_blocking is not None:
            ledger_started_at = _parse_dt(
                next_blocking.get("server_started_at")
            )
            if ledger_started_at is not None:
                result_started_at = ledger_started_at
            ledger_completed_at = _parse_dt(
                next_blocking.get("terminal_ambiguity_observed_at")
            )
            if ledger_completed_at is not None:
                result_completed_at = ledger_completed_at
            if (
                result_started_at is not None
                and result_completed_at is not None
            ):
                normalized_started = (
                    result_started_at.replace(tzinfo=timezone.utc)
                    if result_started_at.tzinfo is None
                    else result_started_at.astimezone(timezone.utc)
                )
                normalized_completed = (
                    result_completed_at.replace(tzinfo=timezone.utc)
                    if result_completed_at.tzinfo is None
                    else result_completed_at.astimezone(timezone.utc)
                )
                result_started_at = normalized_started
                result_completed_at = normalized_completed
                result_duration = max(
                    0.0,
                    (normalized_completed - normalized_started).total_seconds(),
                )
        result = JobResult(
            job_id=job.result.job_id,
            worker_node_id=result_worker_id,
            status=JobStatus.NEUTRAL,
            output=reconciled_output,
            error=(
                "ambiguous_prior_effects"
                if aggregate_disposition == "ambiguous_prior_effects"
                else (
                    "verified_applied_without_semantic_success"
                    if aggregate_disposition == "verified_applied"
                    else "verified_not_applied"
                )
            ),
            started_at=result_started_at,
            completed_at=result_completed_at,
            duration_seconds=result_duration,
            claim_attempt_id=result_attempt_id,
        )
        changed = await self._db.execute(
            """UPDATE jobs SET status = ?, result = ?, tags = ?,
                              claim_attempt_id = ?
               WHERE job_id = ? AND status = ? AND claim_attempt_id IS ?""",
            (
                target_status.value, _serialize_result(result),
                json.dumps(tags), result_attempt_id, target_id,
                JobStatus.NEUTRAL.value, job.claim_attempt_id,
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("effect reconciliation transition raced")
        await self._audit(
            target_id,
            JobStatus.NEUTRAL.value,
            target_status.value,
            node_id=exact_worker or None,
            claim_attempt_id=attempt_id,
            reason=f"independently_verified_{finding}",
            details={
                "reconciliation_id": reconciliation_id,
                "evidence_digest": evidence_digest,
                "verifier_identity": verifier_identity[:256],
                "verifier_type": verifier_type[:128],
                "aggregate_effect_disposition": aggregate_disposition,
                "remaining_unresolved_attempt_ids": remaining_unresolved,
            },
        )
        await self._db.commit()
        return {
            "schema": "WorkEffectReconciliationReceiptV1",
            "version": 1,
            "reconciliation_id": reconciliation_id,
            "target_id": target_id,
            "attempt_id": attempt_id,
            "finding": finding,
            "evidence_digest": evidence_digest,
            "verifier_identity": verifier_identity,
            "created_at": created_at,
            "job_status": target_status.value,
            "effect_disposition": aggregate_disposition,
            "remaining_unresolved_attempt_ids": remaining_unresolved,
            "replayed": False,
        }

    @_serialized_mutation
    async def apply_work_control_operation(
        self,
        *,
        operation_id: str,
        operation: str,
        target_id: str,
        run_id: str,
        attempt_id: Optional[str],
        expected_revision: int,
        expected_state_digest: str,
        parameters: Optional[Mapping[str, Any]] = None,
        reason: str = "",
        requested_by: str = "trusted-internal",
        request_authority: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """CAS-apply one idempotent WorkControlV1 operation."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import (
            WorkControlError,
            build_receipt,
            normalize_operation_request,
            operation_request_digest,
            work_control_ack_timeout_secs,
        )

        request = normalize_operation_request(
            operation_id=operation_id,
            operation=operation,
            target_id=target_id,
            run_id=run_id,
            attempt_id=attempt_id,
            expected_revision=expected_revision,
            expected_state_digest=expected_state_digest,
            parameters=parameters,
            reason=reason,
        )
        request_digest = operation_request_digest(request)
        requested_by = str(requested_by or "").strip()
        if not requested_by or len(requested_by) > 256:
            raise WorkControlError(
                "invalid_request_principal",
                "requested_by must be a non-empty principal identifier",
                status_code=422,
            )
        authority_evidence = dict(request_authority or {
            "authority_kind": "trusted_internal",
        })
        try:
            authority_evidence_json = json.dumps(
                authority_evidence,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_request_authority",
                "request authority evidence must be finite JSON",
                status_code=422,
            ) from exc
        if len(authority_evidence_json.encode("utf-8")) > 4096:
            raise WorkControlError(
                "invalid_request_authority",
                "request authority evidence exceeds 4 KiB",
                status_code=422,
            )
        replay_cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (request["operation_id"],),
        )
        replay_row = await replay_cursor.fetchone()
        if replay_row is not None:
            replay = dict(replay_row)
            if (
                replay["request_digest"] != request_digest
                or replay["target_id"] != request["target_id"]
                or replay["requested_by"] != requested_by
            ):
                raise WorkControlError(
                    "operation_id_conflict",
                    "operation_id was already used for a different request",
                )
            return await self._work_control_receipt_projection(
                replay, replayed=True,
            )

        job = await self.get_job(request["target_id"])
        if job is None:
            raise WorkControlError(
                "work_target_not_found", "queue work target does not exist",
                status_code=404,
            )
        projection = await self._work_control_projection_locked(job)
        if projection["mode"] != "live":
            raise WorkControlError(
                "work_control_not_live",
                "WorkControl mutations require COLONY_WORK_CONTROL_MODE=live",
                status_code=503,
            )
        if request["run_id"] != projection["run_id"]:
            raise WorkControlError(
                "run_identity_mismatch",
                "run_id does not identify this durable work target",
            )
        if (
            request["expected_revision"] != projection["revision"]
            or request["expected_state_digest"] != projection["state_digest"]
        ):
            raise WorkControlError(
                "stale_work_revision",
                "work state changed; read the target and retry with its exact revision and digest",
            )
        allowed = {
            item["operation"]: item
            for item in projection["allowed_operations"]
        }
        selected = allowed.get(request["operation"])
        if selected is None:
            if (
                request["operation"] == "retry"
                and projection["state"]["effect_disposition"]
                == "ambiguous_prior_effects"
            ):
                raise WorkControlError(
                    "ambiguous_prior_effects",
                    "retry is forbidden because the prior effectful attempt started without independent no-effect reconciliation",
                )
            raise WorkControlError(
                "operation_not_allowed",
                "operation is not allowed in the current work state",
            )
        if (
            request["operation"] == "steer"
            and job.deadline is not None
            and datetime.now(timezone.utc) >= job.deadline
        ):
            raise WorkControlError(
                "work_deadline_elapsed",
                "steer authority ends at the job deadline",
            )
        expected_attempt = (
            projection["state"]["active_attempt_id"]
            if request["operation"] in {"steer", "interrupt", "cancel"}
            and projection["state"]["active_attempt_id"]
            else projection["state"]["last_attempt_id"]
            if request["operation"] == "retry" else None
        )
        if selected["attempt_id_required"]:
            if not request["attempt_id"] or request["attempt_id"] != expected_attempt:
                raise WorkControlError(
                    "attempt_identity_mismatch",
                    "operation must bind the exact current/prior attempt",
                )
        elif request["attempt_id"] is not None:
            raise WorkControlError(
                "unexpected_attempt_id",
                "this operation state does not accept an attempt_id",
            )

        superseded_operation_ids: List[str] = []
        if request["operation"] in {"interrupt", "cancel"} and expected_attempt:
            lower_types = (
                ("steer",)
                if request["operation"] == "interrupt"
                else ("steer", "interrupt")
            )
            placeholders = ",".join("?" for _ in lower_types)
            cursor = await self._db.execute(
                "SELECT operation_id FROM work_control_operations "
                "WHERE target_id = ? AND attempt_id = ? "
                "AND status = 'pending_ack' AND operation_type IN ("
                + placeholders + ") ORDER BY created_at, operation_id",
                (job.job_id, expected_attempt, *lower_types),
            )
            superseded_operation_ids = [
                str(row["operation_id"]) for row in await cursor.fetchall()
            ]
            if superseded_operation_ids:
                now = _now_iso()
                acknowledgement = json.dumps({
                    "outcome": "superseded",
                    "details": {
                        "reason": f"superseded_by_{request['operation']}",
                        "operation_id": request["operation_id"],
                    },
                }, sort_keys=True)
                old_placeholders = ",".join(
                    "?" for _ in superseded_operation_ids
                )
                superseded = await self._db.execute(
                    "UPDATE work_control_operations SET status = 'superseded', "
                    "ack_details = ?, ack_authority = ?, acknowledged_at = ? "
                    "WHERE operation_id IN ("
                    + old_placeholders + ") AND status = 'pending_ack'",
                    (
                        acknowledgement,
                        json.dumps({
                            "authority_kind": "server_control_precedence",
                            "superseding_operation_id": request[
                                "operation_id"
                            ],
                            "superseding_operation_type": request[
                                "operation"
                            ],
                        }, sort_keys=True),
                        now,
                        *superseded_operation_ids,
                    ),
                )
                if superseded.rowcount != len(superseded_operation_ids):
                    raise RuntimeError(
                        "control precedence supersession raced"
                    )

        old_status = job.status.value
        worker_id = (
            job.claimed_by
            if selected["worker_ack_required"] else None
        )
        receipt_status = (
            "pending_ack" if selected["worker_ack_required"] else "applied"
        )
        to_status = old_status
        tags = dict(job.tags or {})
        if request["operation"] == "cancel" and not selected["worker_ack_required"]:
            to_status = JobStatus.CANCELLED.value
            changed = await self._db.execute(
                """UPDATE jobs SET status = ?, claimed_by = NULL,
                          claimed_at = NULL, claim_attempt_id = NULL,
                          claim_expires_at = NULL, last_heartbeat = NULL
                   WHERE job_id = ? AND status = ?""",
                (to_status, job.job_id, old_status),
            )
            if changed.rowcount != 1:
                raise WorkControlError(
                    "work_transition_raced",
                    "work state changed during cancel",
                )
            await self._audit(
                job.job_id, old_status, to_status,
                reason=f"work_control_cancel:{request['operation_id']}",
                details={"operation_id": request["operation_id"]},
            )
        elif request["operation"] == "retry":
            if projection["state"].get("retry_blockers"):
                raise WorkControlError(
                    "retry_prerequisites_not_met",
                    "retry prerequisites changed or remain unsatisfied",
                )
            current_hold = str(tags.get("hold_kind") or "")
            current_reason = str(tags.get("blocked_reason") or "")
            if current_hold:
                if not (
                    current_hold == "work_control"
                    and current_reason == "interrupted"
                ):
                    raise WorkControlError(
                        "retry_hold_not_releasable",
                        "retry cannot clear a dependency, approval, boundary, or governance hold",
                    )
                tags.pop("hold_kind", None)
                tags.pop("blocked_reason", None)
                tags.pop("work_control_interrupted", None)
            for key in list(tags):
                if str(key).startswith("effect_reconciliation_"):
                    tags.pop(key, None)
            if (
                projection["state"].get("effect_disposition")
                == "verified_not_applied"
            ):
                # The exact negative proof has now been consumed by a retry.
                # Retain that lifecycle anchor even though the presentation
                # tags for the reconciliation itself are cleared.
                tags["effect_reconciliation_consumed_at"] = _now_iso()
            to_status = JobStatus.QUEUED.value
            changed = await self._db.execute(
                """UPDATE jobs SET status = ?, retry_count = ?, tags = ?,
                          claimed_by = NULL, claimed_at = NULL,
                          claim_attempt_id = NULL, claim_expires_at = NULL,
                          last_heartbeat = NULL
                   WHERE job_id = ? AND status = ?""",
                (
                    to_status, job.retry_count + 1, json.dumps(tags),
                    job.job_id, old_status,
                ),
            )
            if changed.rowcount != 1:
                raise WorkControlError(
                    "work_transition_raced",
                    "work state changed during retry",
                )
            await self._audit(
                job.job_id, old_status, to_status,
                reason=f"work_control_retry:{request['operation_id']}",
                claim_attempt_id=expected_attempt,
                details={"operation_id": request["operation_id"]},
            )

        created_dt = datetime.now(timezone.utc)
        created_at = created_dt.isoformat()
        ack_deadline = (
            min(
                created_dt
                + timedelta(seconds=work_control_ack_timeout_secs()),
                job.deadline
                if request["operation"] == "steer"
                and job.deadline is not None
                else datetime.max.replace(tzinfo=timezone.utc),
            ).isoformat()
            if receipt_status == "pending_ack" else None
        )
        # The accepted revision is the next synchronized projection.  The
        # operation or job transition always changes the digest exactly once.
        accepted_revision = int(projection["revision"]) + 1
        await self._db.execute(
            """INSERT INTO work_control_operations (
                   operation_id, target_id, run_id, authority_digest,
                   operation_type,
                   request_digest, request_json, requested_by,
                   request_authority, status,
                   expected_revision, expected_state_digest,
                   accepted_revision, result_revision, result_state_digest,
                   attempt_id, worker_id, from_job_status, to_job_status,
                   effect_disposition, ack_details, created_at,
                   ack_deadline, acknowledged_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                         ?, ?, ?, ?, ?, '{}', ?, ?, NULL)""",
            (
                request["operation_id"], job.job_id, projection["run_id"],
                projection["authority_digest"], request["operation"],
                request_digest,
                json.dumps(request, sort_keys=True), requested_by,
                authority_evidence_json, receipt_status,
                projection["revision"], projection["state_digest"],
                accepted_revision, expected_attempt, worker_id,
                old_status, to_status,
                projection["state"]["effect_disposition"], created_at,
                ack_deadline,
            ),
        )
        updated_job = await self.get_job(job.job_id)
        if updated_job is None:
            raise RuntimeError("WorkControl target disappeared during operation")
        result_projection = await self._work_control_projection_locked(updated_job)
        if result_projection["revision"] != accepted_revision:
            # This is an invariant failure, not a stale client.  Rolling back
            # prevents a receipt from lying about its CAS revision.
            raise RuntimeError("WorkControl accepted revision invariant failed")
        if superseded_operation_ids:
            placeholders = ",".join("?" for _ in superseded_operation_ids)
            await self._db.execute(
                "UPDATE work_control_operations SET result_revision = ?, "
                "result_state_digest = ? WHERE operation_id IN ("
                + placeholders + ")",
                (
                    result_projection["revision"],
                    result_projection["state_digest"],
                    *superseded_operation_ids,
                ),
            )
        if receipt_status == "applied":
            await self._db.execute(
                """UPDATE work_control_operations
                   SET result_revision = ?, result_state_digest = ?,
                       ack_authority = ?, acknowledged_at = ?
                   WHERE operation_id = ?""",
                (
                    result_projection["revision"],
                    result_projection["state_digest"],
                    json.dumps({
                        "authority_kind": "server_atomic_operation",
                    }, sort_keys=True), created_at,
                    request["operation_id"],
                ),
            )
        await self._append_work_control_receipt_event(
            request["operation_id"], phase="accepted",
        )
        if receipt_status == "applied":
            await self._append_work_control_receipt_event(
                request["operation_id"], phase="outcome",
            )
        for superseded_id in superseded_operation_ids:
            await self._append_work_control_receipt_event(
                superseded_id, phase="outcome",
            )
        await self._db.commit()
        row_cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (request["operation_id"],),
        )
        row = await row_cursor.fetchone()
        if row is None:
            raise RuntimeError("WorkControl receipt was not durably written")
        return await self._work_control_receipt_projection(dict(row))

    @_serialized_read
    async def get_work_control_receipt(
        self,
        target_id: str,
        operation_id: str,
    ) -> Dict[str, Any]:
        """Read back one durable operation receipt."""

        from colony_sidecar.task_queue.work_control import (
            WorkControlError,
            build_receipt,
            validate_operation_id,
        )

        assert self._db is not None
        operation_id = validate_operation_id(operation_id)
        cursor = await self._db.execute(
            """SELECT * FROM work_control_operations
               WHERE target_id = ? AND operation_id = ?""",
            (target_id, operation_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise WorkControlError(
                "work_control_receipt_not_found",
                "operation receipt does not exist for this target",
                status_code=404,
            )
        return await self._work_control_receipt_projection(dict(row))

    @_serialized_read
    async def get_work_control_worker_outcome(
        self,
        *,
        worker_id: str,
        operation_id: str,
        attempt_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Read one exact durable steer outcome before any handler replay."""

        assert self._db is not None
        cursor = await self._db.execute(
            """SELECT * FROM work_control_worker_outcomes
               WHERE operation_id = ? AND worker_id = ? AND attempt_id = ?""",
            (operation_id, worker_id, attempt_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "operation_id": str(row["operation_id"]),
            "target_id": str(row["target_id"]),
            "attempt_id": str(row["attempt_id"]),
            "worker_id": str(row["worker_id"]),
            "authority_digest": str(row["authority_digest"]),
            "outcome": str(row["outcome"]),
            "details": json.loads(row["details_json"] or "{}"),
            "outcome_digest": str(row["outcome_digest"]),
            "recorded_at": str(row["recorded_at"]),
        }

    @_serialized_mutation
    async def record_work_control_worker_outcome(
        self,
        *,
        worker_id: str,
        operation_id: str,
        attempt_id: str,
        outcome: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Durably bind a steer handler outcome before acknowledgement.

        A handler must also advertise durable ``operation_id`` idempotency;
        this ledger closes response loss and process restart after the record
        commits, while that handler contract closes the preceding crash
        window.
        """

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import (
            WorkControlError,
            canonical_json,
            digest_json,
            validate_operation_id,
            work_control_mode,
        )

        operation_id = validate_operation_id(operation_id)
        worker_id = str(worker_id or "").strip()
        attempt_id = str(attempt_id or "").strip()
        outcome = str(outcome or "").strip().lower()
        if not worker_id or not attempt_id or outcome not in {
            "applied", "rejected",
        }:
            raise WorkControlError(
                "invalid_worker_outcome",
                "durable steer outcome requires worker, attempt, and applied/rejected",
                status_code=422,
            )
        detail_payload = dict(details or {})
        try:
            details_json = canonical_json(detail_payload)
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_acknowledgement_details",
                "worker outcome details must be finite JSON",
                status_code=422,
            ) from exc
        if len(details_json.encode("utf-8")) > 16 * 1024:
            raise WorkControlError(
                "acknowledgement_details_too_large",
                "worker outcome details exceed 16 KiB",
                status_code=422,
            )
        cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (operation_id,),
        )
        operation = await cursor.fetchone()
        if operation is None:
            raise WorkControlError(
                "work_control_receipt_not_found",
                "operation receipt does not exist",
                status_code=404,
            )
        if work_control_mode() != "live":
            await self.reconcile_inactive_work_controls()
            raise WorkControlError(
                "work_control_not_live",
                "worker outcomes are inactive unless WorkControl is live",
                status_code=503,
            )
        if (
            operation["operation_type"] != "steer"
            or operation["worker_id"] != worker_id
            or operation["attempt_id"] != attempt_id
        ):
            raise WorkControlError(
                "worker_attempt_mismatch",
                "durable steer outcome is not bound to this worker attempt",
            )
        observed = datetime.now(timezone.utc)
        deadline = _parse_dt(operation["ack_deadline"])
        if deadline is None:
            raise WorkControlError(
                "worker_outcome_lease_missing",
                "steer operation has no acknowledgement lease",
            )
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if observed >= deadline.astimezone(timezone.utc):
            await self.reconcile_expired_work_controls(observed)
            raise WorkControlError(
                "worker_outcome_lease_elapsed",
                "steer outcome arrived after its authority lease",
            )
        job = await self.get_job(str(operation["target_id"]))
        if (
            job is None
            or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
            or job.claimed_by != worker_id
            or job.claim_attempt_id != attempt_id
            or (
                job.deadline is not None
                and observed >= job.deadline
            )
        ):
            raise WorkControlError(
                "worker_outcome_superseded",
                "steer attempt is no longer active under this authority",
            )
        payload = {
            "schema": "WorkControlWorkerOutcomeV1",
            "version": 1,
            "operation_id": operation_id,
            "target_id": str(operation["target_id"]),
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "authority_digest": str(operation["authority_digest"]),
            "outcome": outcome,
            "details": detail_payload,
        }
        outcome_digest = digest_json(payload)
        existing_cursor = await self._db.execute(
            "SELECT * FROM work_control_worker_outcomes WHERE operation_id = ?",
            (operation_id,),
        )
        existing = await existing_cursor.fetchone()
        if existing is not None:
            if str(existing["outcome_digest"]) != outcome_digest:
                raise WorkControlError(
                    "worker_outcome_conflict",
                    "operation already has a different durable worker outcome",
                )
            return await self.get_work_control_worker_outcome(
                worker_id=worker_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
            )
        if operation["status"] != "pending_ack":
            raise WorkControlError(
                "worker_outcome_superseded",
                "operation is no longer awaiting a worker outcome",
            )
        recorded_at = _now_iso()
        await self._db.execute(
            """INSERT INTO work_control_worker_outcomes (
                   operation_id, target_id, attempt_id, worker_id,
                   authority_digest, outcome, details_json, outcome_digest,
                   recorded_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id, operation["target_id"], attempt_id, worker_id,
                operation["authority_digest"], outcome, details_json,
                outcome_digest, recorded_at,
            ),
        )
        await self._db.commit()
        result = await self.get_work_control_worker_outcome(
            worker_id=worker_id,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        if result is None:
            raise RuntimeError("durable worker outcome disappeared")
        return result

    @_serialized_mutation
    async def reconcile_inactive_work_controls(self) -> int:
        """Terminalize every pending control when delivery is not live."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import work_control_mode

        mode = work_control_mode()
        if mode == "live":
            return 0
        cursor = await self._db.execute(
            """SELECT operation_id, target_id
               FROM work_control_operations
               WHERE status = 'pending_ack'
               ORDER BY target_id, created_at, operation_id"""
        )
        by_target: Dict[str, List[str]] = {}
        for row in await cursor.fetchall():
            by_target.setdefault(str(row["target_id"]), []).append(
                str(row["operation_id"])
            )
        count = 0
        for target_id, operation_ids in by_target.items():
            job = await self.get_job(target_id)
            marked = await self._mark_pending_work_controls_terminal(
                operation_ids,
                status="superseded",
                acknowledgement_outcome="inactive",
                reason=f"work_control_mode_{mode}",
                ack_authority={
                    "authority_kind": "server_control_mode_reconciler",
                    "observed_mode": mode,
                },
                to_job_status=(job.status.value if job is not None else None),
            )
            count += await self._seal_work_control_outcomes(
                target_id, marked,
            )
        if count:
            await self._db.commit()
        return count

    @_serialized_mutation
    async def reconcile_stale_work_controls(
        self,
        now: Optional[datetime] = None,
    ) -> int:
        """Durably retire controls whose exact attempt is no longer active."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import work_control_mode

        if work_control_mode() != "live":
            return await self.reconcile_inactive_work_controls()
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        expired = await self.reconcile_expired_work_controls(observed)
        cursor = await self._db.execute(
            """SELECT operation.operation_id, operation.target_id
               FROM work_control_operations AS operation
               LEFT JOIN jobs AS job ON job.job_id = operation.target_id
               WHERE operation.status = 'pending_ack'
                 AND (
                    job.job_id IS NULL
                    OR job.status NOT IN ('claimed', 'running')
                    OR job.claimed_by IS NOT operation.worker_id
                    OR job.claim_attempt_id IS NOT operation.attempt_id
                 )
               ORDER BY operation.target_id, operation.created_at,
                        operation.operation_id"""
        )
        by_target: Dict[str, List[str]] = {}
        for row in await cursor.fetchall():
            by_target.setdefault(str(row["target_id"]), []).append(
                str(row["operation_id"])
            )
        stale = 0
        for target_id, operation_ids in by_target.items():
            job = await self.get_job(target_id)
            marked = await self._mark_pending_work_controls_terminal(
                operation_ids,
                status="superseded",
                acknowledgement_outcome="superseded",
                reason="worker_attempt_no_longer_active",
                ack_authority={
                    "authority_kind": "server_attempt_reconciler",
                },
                to_job_status=(job.status.value if job is not None else None),
            )
            stale += await self._seal_work_control_outcomes(
                target_id, marked,
            )
        if stale:
            await self._db.commit()
        return expired + stale

    async def reconcile_work_control_delivery_state(
        self,
        now: Optional[datetime] = None,
    ) -> int:
        """Converge mode, deadline, and exact-attempt delivery authority."""

        return await self.reconcile_stale_work_controls(now)

    @_serialized_mutation
    async def pending_work_control_operations(
        self,
        worker_id: str,
    ) -> List[Dict[str, Any]]:
        """Return only unexpired controls bound to exact worker attempts."""

        assert self._db is not None
        observed = datetime.now(timezone.utc)
        # Delivery is itself an authority boundary. Reconcile mode, deadline,
        # and exact-attempt state first; retain every condition in SQL as an
        # independent fence against scheduler delay and lifecycle races.
        await self.reconcile_work_control_delivery_state(observed)
        from colony_sidecar.task_queue.work_control import work_control_mode

        if work_control_mode() != "live":
            return []
        cursor = await self._db.execute(
            """SELECT operation.*
               FROM work_control_operations AS operation
               JOIN jobs AS job ON job.job_id = operation.target_id
               WHERE operation.worker_id = ?
                 AND operation.status = 'pending_ack'
                 AND operation.ack_deadline IS NOT NULL
                 AND operation.ack_deadline > ?
                 AND job.status IN ('claimed', 'running')
                 AND job.claimed_by IS operation.worker_id
                 AND job.claim_attempt_id IS operation.attempt_id
               ORDER BY operation.created_at ASC, operation.operation_id ASC""",
            (worker_id, observed.isoformat()),
        )
        rows = await cursor.fetchall()
        controls: List[Dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            request = json.loads(row["request_json"])
            controls.append({
                "schema": "WorkControlDeliveryV1",
                "version": 1,
                "operation_id": row["operation_id"],
                "operation": row["operation_type"],
                "target_id": row["target_id"],
                "run_id": row["run_id"],
                "authority_digest": row["authority_digest"],
                "attempt_id": row["attempt_id"],
                "request_digest": row["request_digest"],
                "parameters": request.get("parameters") or {},
                "reason": request.get("reason") or "",
                "created_at": row["created_at"],
                "ack_deadline": row["ack_deadline"],
            })
        return controls

    @_serialized_mutation
    async def acknowledge_work_control_operation(
        self,
        *,
        worker_id: str,
        operation_id: str,
        attempt_id: str,
        outcome: str,
        details: Optional[Dict[str, Any]] = None,
        ack_authority: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Acknowledge cooperative steer/stop for one exact worker attempt."""

        assert self._db is not None
        from colony_sidecar.task_queue.work_control import (
            WorkControlError,
            build_receipt,
            canonical_json,
            validate_operation_id,
            work_control_mode,
        )

        operation_id = validate_operation_id(operation_id)
        worker_id = str(worker_id or "").strip()
        attempt_id = str(attempt_id or "").strip()
        outcome = str(outcome or "").strip().lower()
        if not worker_id or not attempt_id:
            raise WorkControlError(
                "invalid_worker_acknowledgement",
                "worker_id and attempt_id are required",
                status_code=422,
            )
        if outcome not in {"applied", "stopped", "rejected"}:
            raise WorkControlError(
                "invalid_worker_outcome",
                "outcome must be applied, stopped, or rejected",
                status_code=422,
            )
        ack = dict(details or {})
        try:
            encoded_ack = canonical_json(ack)
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_acknowledgement_details",
                "acknowledgement details must be finite JSON",
                status_code=422,
            ) from exc
        if len(encoded_ack.encode("utf-8")) > 16 * 1024:
            raise WorkControlError(
                "acknowledgement_details_too_large",
                "acknowledgement details exceed 16 KiB",
                status_code=422,
            )
        authority_evidence = dict(ack_authority or {
            "authority_kind": "trusted_internal_worker",
            "worker_id": worker_id,
        })
        try:
            authority_json = canonical_json(authority_evidence)
        except (TypeError, ValueError) as exc:
            raise WorkControlError(
                "invalid_acknowledgement_authority",
                "acknowledgement authority must be finite JSON",
                status_code=422,
            ) from exc
        if len(authority_json.encode("utf-8")) > 4096:
            raise WorkControlError(
                "invalid_acknowledgement_authority",
                "acknowledgement authority exceeds 4 KiB",
                status_code=422,
            )
        cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (operation_id,),
        )
        raw = await cursor.fetchone()
        if raw is None:
            raise WorkControlError(
                "work_control_receipt_not_found",
                "operation receipt does not exist",
                status_code=404,
            )
        if work_control_mode() != "live":
            await self.reconcile_inactive_work_controls()
            cursor = await self._db.execute(
                "SELECT * FROM work_control_operations WHERE operation_id = ?",
                (operation_id,),
            )
            inactive = await cursor.fetchone()
            if inactive is None:
                raise RuntimeError(
                    "inactive WorkControl receipt disappeared"
                )
            return await self._work_control_receipt_projection(
                dict(inactive), replayed=True,
            )
        row = dict(raw)
        if row["worker_id"] != worker_id or row["attempt_id"] != attempt_id:
            raise WorkControlError(
                "worker_attempt_mismatch",
                "acknowledgement is not bound to this worker attempt",
            )

        # The acknowledgement lease is server authority, not a scheduler
        # hint.  Enforce it on the acknowledgement path itself so a delayed
        # scheduler tick can never make a late worker response authoritative.
        # Reconciliation is task-reentrant under the queue mutation lock and
        # durably records the terminal receipt before it is returned here.
        ack_deadline = _parse_dt(row.get("ack_deadline"))
        observed = datetime.now(timezone.utc)
        if ack_deadline is not None:
            if ack_deadline.tzinfo is None:
                ack_deadline = ack_deadline.replace(tzinfo=timezone.utc)
            if (
                row["status"] == "pending_ack"
                and observed >= ack_deadline.astimezone(timezone.utc)
            ):
                await self.reconcile_expired_work_controls(observed)
                cursor = await self._db.execute(
                    "SELECT * FROM work_control_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                )
                expired = await cursor.fetchone()
                if expired is None:
                    raise RuntimeError(
                        "WorkControl acknowledgement receipt disappeared"
                    )
                expired_row = dict(expired)
                if expired_row["status"] == "pending_ack":
                    raise RuntimeError(
                        "WorkControl acknowledgement deadline did not reconcile"
                    )
                return await self._work_control_receipt_projection(
                    expired_row, replayed=True,
                )

        if row["status"] != "pending_ack":
            prior = await self._work_control_receipt_projection(
                row, replayed=True,
            )
            prior_ack = prior.get("acknowledgement") or {}
            if (
                str(prior_ack.get("outcome") or "") == outcome
                and prior_ack.get("details") == ack
            ):
                return prior
            raise WorkControlError(
                "acknowledgement_conflict",
                "operation was already acknowledged with a different outcome",
            )

        job = await self.get_job(row["target_id"])
        if job is None:
            raise WorkControlError(
                "work_target_not_found", "queue work target does not exist",
                status_code=404,
            )
        before = await self._work_control_projection_locked(job)
        operation = str(row["operation_type"])
        if operation == "steer" and outcome == "applied":
            durable_outcome = await self.get_work_control_worker_outcome(
                worker_id=worker_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
            )
            if (
                durable_outcome is None
                or durable_outcome["outcome"] != "applied"
                or durable_outcome["details"] != ack
            ):
                raise WorkControlError(
                    "durable_steer_outcome_required",
                    "steer acknowledgement requires its exact durable worker outcome",
                )
        exact_active = bool(
            job.claimed_by == worker_id
            and job.claim_attempt_id == attempt_id
            and job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
        )
        final_status = "rejected"
        to_status = job.status.value
        ambiguity_outcome_event_id: Optional[str] = None
        if outcome == "rejected":
            final_status = "rejected"
        elif operation == "steer" and outcome == "applied" and exact_active:
            final_status = "applied"
        elif operation in {"interrupt", "cancel"} and outcome == "stopped":
            if exact_active:
                old_status = job.status.value
                tags = dict(job.tags or {})
                ambiguous_effect = bool(
                    job.status is JobStatus.RUNNING
                    and before["state"]["effect_disposition"]
                    == "ambiguous_prior_effects"
                )
                stored_result = (
                    _serialize_result(job.result) if job.result else None
                )
                if ambiguous_effect:
                    new_status = JobStatus.NEUTRAL
                    now_dt = datetime.now(timezone.utc)
                    started_at = await self._server_started_at(
                        job, worker_id, now_dt,
                    )
                    stored_result = _serialize_result(JobResult(
                        job_id=job.job_id,
                        worker_node_id=worker_id,
                        status=JobStatus.NEUTRAL,
                        output={
                            "status": "ambiguous",
                            "reason": "ambiguous_prior_effects",
                        },
                        error="stopped_after_effectful_attempt_started",
                        started_at=started_at,
                        completed_at=now_dt,
                        duration_seconds=(
                            max(0.0, (now_dt - started_at).total_seconds())
                            if started_at is not None else None
                        ),
                        claim_attempt_id=attempt_id,
                    ))
                    tags.update({
                        "verification_pending": "true",
                        "ambiguous_prior_effects": "true",
                        "hold_kind": "work_control",
                        "blocked_reason": "ambiguous_prior_effects",
                    })
                elif operation == "interrupt":
                    new_status = JobStatus.BLOCKED
                    tags.update({
                        "hold_kind": "work_control",
                        "blocked_reason": "interrupted",
                        "work_control_interrupted": "true",
                    })
                else:
                    new_status = JobStatus.CANCELLED
                changed = await self._db.execute(
                    """UPDATE jobs SET status = ?, tags = ?, result = ?,
                              claimed_by = NULL, claimed_at = NULL,
                              claim_attempt_id = ?,
                              claim_expires_at = NULL, last_heartbeat = NULL
                       WHERE job_id = ? AND claimed_by = ?
                         AND claim_attempt_id = ? AND status IN (?, ?)""",
                    (
                        new_status.value, json.dumps(tags), stored_result,
                        attempt_id if ambiguous_effect else None,
                        job.job_id,
                        worker_id, attempt_id, JobStatus.CLAIMED.value,
                        JobStatus.RUNNING.value,
                    ),
                )
                if changed.rowcount == 1:
                    to_status = new_status.value
                    final_status = "applied"
                    await self._audit(
                        job.job_id, old_status, new_status.value,
                        node_id=worker_id, claim_attempt_id=attempt_id,
                        reason=f"work_control_{operation}:{operation_id}",
                        details={
                            "operation_id": operation_id,
                            "ambiguous_prior_effects": ambiguous_effect,
                        },
                    )
                    if ambiguous_effect:
                        ambiguity_outcome_event_id = (
                            f"worker-outcome:{job.job_id}:{attempt_id}:ambiguous"
                        )
                        await self._enqueue_worker_outcome(
                            event_id=ambiguity_outcome_event_id,
                            job_id=job.job_id,
                            claim_attempt_id=attempt_id,
                            report={
                                "status": "ambiguous",
                                "summary": "cooperative stop after effectful start",
                                "reason": "ambiguous_prior_effects",
                            },
                            verdict="unverified",
                            outcome="neutral",
                            worker_mode=self._job_governor_mode(job),
                            success_attested=False,
                            latency=None,
                            attempts=job.retry_count,
                        )
                else:
                    final_status = "superseded"
            else:
                final_status = "superseded"
        else:
            raise WorkControlError(
                "worker_outcome_mismatch",
                f"{operation} cannot be acknowledged with {outcome}",
                status_code=422,
            )

        lifecycle_loser_ids: List[str] = []
        if (
            final_status == "applied"
            and operation in {"interrupt", "cancel"}
            and outcome == "stopped"
        ):
            cursor = await self._db.execute(
                """SELECT operation_id FROM work_control_operations
                   WHERE target_id = ? AND attempt_id = ?
                     AND status = 'pending_ack' AND operation_id != ?
                   ORDER BY created_at, operation_id""",
                (job.job_id, attempt_id, operation_id),
            )
            lifecycle_loser_ids = await self._mark_pending_work_controls_terminal(
                [
                    str(item["operation_id"])
                    for item in await cursor.fetchall()
                ],
                status="superseded",
                acknowledgement_outcome="superseded",
                reason=f"work_control_{operation}_won_race",
                ack_authority={
                    "authority_kind": "server_lifecycle_winner",
                    "claim_attempt_id": attempt_id,
                    "winning_operation_id": operation_id,
                },
                to_job_status=to_status,
            )

        acknowledged_at = _now_iso()
        ack_payload = {"outcome": outcome, "details": ack}
        acknowledged = await self._db.execute(
            """UPDATE work_control_operations
               SET status = ?, to_job_status = ?, ack_details = ?,
                   ack_authority = ?, acknowledged_at = ?
               WHERE operation_id = ? AND status = 'pending_ack'""",
            (
                final_status, to_status, canonical_json(ack_payload),
                authority_json, acknowledged_at, operation_id,
            ),
        )
        if acknowledged.rowcount != 1:
            raise RuntimeError("WorkControl acknowledgement raced")
        updated_job = await self.get_job(job.job_id)
        if updated_job is None:
            raise RuntimeError("WorkControl target disappeared during ack")
        after = await self._work_control_projection_locked(updated_job)
        if after["revision"] != before["revision"] + 1:
            raise RuntimeError("WorkControl acknowledgement revision invariant failed")
        outcome_ids = [operation_id, *lifecycle_loser_ids]
        placeholders = ",".join("?" for _ in outcome_ids)
        await self._db.execute(
            "UPDATE work_control_operations SET result_revision = ?, "
            "result_state_digest = ? WHERE operation_id IN ("
            + placeholders + ")",
            (after["revision"], after["state_digest"], *outcome_ids),
        )
        for losing_operation_id in lifecycle_loser_ids:
            await self._append_work_control_receipt_event(
                losing_operation_id, phase="outcome",
            )
        await self._append_work_control_receipt_event(
            operation_id, phase="outcome",
        )
        await self._db.commit()
        if ambiguity_outcome_event_id:
            self._schedule_worker_outcome_drain(
                ambiguity_outcome_event_id,
            )
        cursor = await self._db.execute(
            "SELECT * FROM work_control_operations WHERE operation_id = ?",
            (operation_id,),
        )
        final = await cursor.fetchone()
        if final is None:
            raise RuntimeError("WorkControl acknowledgement receipt disappeared")
        return await self._work_control_receipt_projection(dict(final))

    @_serialized_mutation
    async def reconcile_expired_work_controls(
        self,
        now: Optional[datetime] = None,
    ) -> int:
        """Expire unacknowledged command leases without claiming a stop.

        Once the pending row is terminal, ordinary timeout/heartbeat handling
        can safely close the exact attempt. Started effectful failures then
        follow ``fail_job`` into NEUTRAL ambiguity, never an automatic retry.
        A CLAIMED stop is server-safe because ``start_job`` was gated while
        the command was pending, so it can be resolved without a handler.
        """

        assert self._db is not None
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        cursor = await self._db.execute(
            """SELECT * FROM work_control_operations
               WHERE status = 'pending_ack' AND ack_deadline IS NOT NULL
                 AND ack_deadline <= ?
               ORDER BY ack_deadline ASC, operation_id ASC""",
            (observed.astimezone(timezone.utc).isoformat(),),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if not rows:
            return 0
        for row in rows:
            job = await self.get_job(row["target_id"])
            exact_active = bool(
                job is not None
                and job.claimed_by == row["worker_id"]
                and job.claim_attempt_id == row["attempt_id"]
                and job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            )
            status = "expired" if exact_active else "superseded"
            to_status = job.status.value if job is not None else None
            if (
                exact_active
                and job is not None
                and job.status is JobStatus.CLAIMED
                and row["operation_type"] in {"interrupt", "cancel"}
            ):
                old_status = job.status.value
                tags = dict(job.tags or {})
                if row["operation_type"] == "interrupt":
                    new_status = JobStatus.BLOCKED
                    tags.update({
                        "hold_kind": "work_control",
                        "blocked_reason": "interrupted",
                        "work_control_interrupted": "true",
                    })
                else:
                    new_status = JobStatus.CANCELLED
                changed = await self._db.execute(
                    """UPDATE jobs SET status = ?, tags = ?,
                              claimed_by = NULL, claimed_at = NULL,
                              claim_attempt_id = NULL,
                              claim_expires_at = NULL, last_heartbeat = NULL
                       WHERE job_id = ? AND status = ? AND claimed_by = ?
                         AND claim_attempt_id = ?""",
                    (
                        new_status.value, json.dumps(tags), job.job_id,
                        JobStatus.CLAIMED.value, row["worker_id"],
                        row["attempt_id"],
                    ),
                )
                if changed.rowcount == 1:
                    status = "applied"
                    to_status = new_status.value
                    await self._audit(
                        job.job_id, old_status, new_status.value,
                        node_id=row["worker_id"],
                        claim_attempt_id=row["attempt_id"],
                        reason=(
                            f"work_control_{row['operation_type']}:"
                            f"{row['operation_id']}:server_prestart"
                        ),
                    )
                    await self._finalize_pending_controls_after_transition(
                        job.job_id,
                        row["attempt_id"],
                        reason="server_prestart_control_won_race",
                        exclude_operation_id=row["operation_id"],
                    )
            acknowledged_at = observed.astimezone(timezone.utc).isoformat()
            acknowledgement = json.dumps({
                "outcome": status,
                "details": {"reason": "worker_ack_deadline_elapsed"},
            }, sort_keys=True)
            await self._db.execute(
                """UPDATE work_control_operations
                   SET status = ?, to_job_status = ?, ack_details = ?,
                       ack_authority = ?, acknowledged_at = ?
                   WHERE operation_id = ? AND status = 'pending_ack'""",
                (
                    status, to_status, acknowledgement,
                    json.dumps({
                        "authority_kind": "server_deadline_reconciler",
                    }, sort_keys=True), acknowledged_at,
                    row["operation_id"],
                ),
            )
            refreshed = await self.get_job(row["target_id"])
            if refreshed is not None:
                projection = await self._work_control_projection_locked(
                    refreshed,
                )
                await self._db.execute(
                    """UPDATE work_control_operations
                       SET result_revision = ?, result_state_digest = ?
                       WHERE operation_id = ?""",
                    (
                        projection["revision"], projection["state_digest"],
                        row["operation_id"],
                    ),
                )
            await self._append_work_control_receipt_event(
                row["operation_id"], phase="outcome",
            )
        await self._db.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    @_serialized_mutation
    async def send_heartbeat(
        self,
        worker_id: str,
        job_ids: List[str],
        progress: Optional[Dict[str, float]] = None,
        claim_attempt_ids: Optional[Dict[str, str]] = None,
    ) -> int:
        """Update last_heartbeat for listed jobs and worker last_seen."""
        assert self._db is not None
        now = self._worker_now_iso()
        updated = 0
        for job_id in dict.fromkeys(job_ids):
            pct = (progress or {}).get(job_id)
            attempt = (claim_attempt_ids or {}).get(job_id)
            if not attempt:
                continue
            changed = await self._db.execute(
                """
                UPDATE jobs SET last_heartbeat = ?
                WHERE job_id = ? AND claimed_by = ?
                  AND claim_attempt_id = ?
                """,
                (now, job_id, worker_id, attempt),
            )
            if changed.rowcount == 1:
                updated += 1
                await self._db.execute(
                    """
                    INSERT OR REPLACE INTO heartbeats (
                        node_id, job_id, timestamp, claim_attempt_id, progress
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (worker_id, job_id, now, attempt, pct),
                )
        await self._db.execute(
            "UPDATE workers SET last_seen = ? WHERE node_id = ?",
            (now, worker_id),
        )
        await self._db.commit()
        return updated

    # ------------------------------------------------------------------
    # Scheduler phases
    # ------------------------------------------------------------------

    @_serialized_mutation
    async def expire_past_deadlines(self, now: datetime) -> int:
        """Transition expired QUEUED/CLAIMED/RUNNING jobs → FAILED. Returns count."""
        assert self._db is not None
        now_iso = now.isoformat()
        cur = await self._db.execute(
            """
            SELECT * FROM jobs
            WHERE deadline IS NOT NULL
              AND deadline < ?
              AND status IN ('queued', 'claimed', 'running', 'blocked')
            """,
            (now_iso,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            job = _job_from_row(row)
            jid, old_status = job.job_id, job.status.value
            if (
                job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
                and job.claimed_by
                and job.claim_attempt_id
            ):
                if await self.fail_job(
                    jid,
                    job.claimed_by,
                    "server_deadline_exceeded",
                    claim_attempt_id=job.claim_attempt_id,
                ):
                    count += 1
                continue
            await self._db.execute(
                """UPDATE jobs SET status = ?, claimed_by = NULL,
                          claimed_at = NULL, claim_expires_at = NULL,
                          last_heartbeat = NULL
                   WHERE job_id = ?""",
                (JobStatus.FAILED.value, jid),
            )
            await self._audit(jid, old_status, JobStatus.FAILED.value, reason="deadline_expired")
            count += 1
        if count:
            await self._db.commit()
        return count

    @_serialized_mutation
    async def expire_execution_timeouts(self, now: datetime) -> int:
        """Fail/requeue RUNNING attempts past their server-owned timeout."""

        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?",
            (JobStatus.RUNNING.value,),
        )
        expired = 0
        for row in await cursor.fetchall():
            job = _job_from_row(row)
            if not job.claimed_by or not job.claim_attempt_id:
                continue
            started = await self._server_started_at(job, job.claimed_by, now)
            if (
                started is None
                or (now - started).total_seconds() <= job.timeout_secs
            ):
                continue
            if await self.fail_job(
                job.job_id,
                job.claimed_by,
                "server_execution_timeout",
                claim_attempt_id=job.claim_attempt_id,
            ):
                expired += 1
        return expired

    @_serialized_mutation
    async def abandon_silent_jobs(
        self,
        now: datetime,
        timeout_secs: float,
        claim_timeout_secs: Optional[float] = None,
    ) -> int:
        """Abandon stale RUNNING heartbeats and unstarted claim leases."""
        assert self._db is not None
        heartbeat_cutoff = (
            now - timedelta(seconds=timeout_secs)
        ).isoformat()
        claim_cutoff = (
            now - timedelta(seconds=(
                claim_timeout_secs
                if claim_timeout_secs is not None
                else self._claim_timeout_secs
            ))
        ).isoformat()
        cur = await self._db.execute(
            """
            SELECT job_id, status, claimed_by, claim_attempt_id FROM jobs
            WHERE (
                status = 'running'
                AND (last_heartbeat IS NULL OR last_heartbeat < ?)
            ) OR (
                status = 'claimed'
                AND (
                    (claim_expires_at IS NOT NULL AND claim_expires_at < ?)
                    OR claimed_at < ?
                )
            )
            """,
            (heartbeat_cutoff, now.isoformat(), claim_cutoff),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid, old_status, node = row["job_id"], row["status"], row["claimed_by"]
            if old_status == JobStatus.CLAIMED.value:
                changed = await self._db.execute(
                    """UPDATE jobs SET status = ?, claimed_by = NULL,
                              claimed_at = NULL, claim_attempt_id = NULL,
                              claim_expires_at = NULL, last_heartbeat = NULL
                       WHERE job_id = ? AND status = ?
                         AND claim_attempt_id IS ?""",
                    (
                        JobStatus.QUEUED.value,
                        jid,
                        JobStatus.CLAIMED.value,
                        row["claim_attempt_id"],
                    ),
                )
                if changed.rowcount != 1:
                    continue
                await self._audit(
                    jid,
                    JobStatus.CLAIMED.value,
                    JobStatus.QUEUED.value,
                    node_id=node,
                    claim_attempt_id=row["claim_attempt_id"],
                    reason="claim_start_lease_expired",
                )
                await self._finalize_pending_controls_after_transition(
                    jid,
                    row["claim_attempt_id"],
                    reason="claim_start_lease_expired",
                )
                count += 1
                continue
            if node and await self.fail_job(
                jid,
                node,
                "worker_heartbeat_timeout",
                claim_attempt_id=row["claim_attempt_id"],
            ):
                count += 1
        if count:
            await self._db.commit()
        return count

    @_serialized_mutation
    async def requeue_retryable_jobs(self, now: datetime) -> int:
        """Move ABANDONED jobs with remaining retries back to QUEUED. Returns count."""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT * FROM jobs WHERE status = 'abandoned'
            """,
        )
        rows = await cur.fetchall()
        count = 0
        ambiguity_events: List[str] = []
        for row in rows:
            job = _job_from_row(row)
            jid = job.job_id
            retry = job.retry_count
            max_r = job.max_retries
            deadline = job.deadline
            expired = deadline is not None and now > deadline

            # Older writers and persisted databases may still contain
            # ABANDONED rows. A server-ledger RUNNING attempt that declared a
            # mutation/disclosure cannot be assumed not-applied just because
            # its worker disappeared. Quarantine it for independent
            # reconciliation instead of creating a duplicate effect.
            truth = await self._work_control_attempt_truth(job)
            disposition = str(truth["effect_disposition"])
            if disposition in {
                "ambiguous_prior_effects", "verified_applied",
            }:
                attempt_id = str(
                    truth.get("blocking_attempt_id")
                    or truth["last_attempt_id"]
                    or ""
                )
                exact_record = next(
                    (
                        item for item in truth.get("attempts", [])
                        if item.get("attempt_id") == attempt_id
                    ),
                    None,
                )
                completed_at = _parse_dt(
                    exact_record.get("terminal_ambiguity_observed_at")
                    if exact_record is not None else None
                ) or now
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
                else:
                    completed_at = completed_at.astimezone(timezone.utc)
                previous_is_exact = bool(
                    job.result is not None
                    and job.result.claim_attempt_id == attempt_id
                )
                started_at = (
                    job.result.started_at
                    if previous_is_exact else _parse_dt(
                        exact_record.get("server_started_at")
                        if exact_record is not None else None
                    )
                )
                if started_at is not None:
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    else:
                        started_at = started_at.astimezone(timezone.utc)
                worker_id = str(
                    truth.get("blocking_worker_id")
                    or truth["last_worker_id"]
                    or job.claimed_by
                    or (job.result.worker_node_id if job.result else "")
                )
                output = (
                    dict(job.result.output or {})
                    if previous_is_exact else {}
                )
                tags = dict(job.tags or {})
                if disposition == "ambiguous_prior_effects":
                    output.update({
                        "status": "ambiguous",
                        "reason": "ambiguous_prior_effects",
                    })
                    error = "abandoned_after_effectful_attempt_started"
                    tags.update({
                        "verification_pending": "true",
                        "ambiguous_prior_effects": "true",
                        "hold_kind": "work_control",
                        "blocked_reason": "ambiguous_prior_effects",
                    })
                else:
                    error = "verified_applied_without_semantic_success"
                    for key in (
                        "verification_pending", "ambiguous_prior_effects",
                        "hold_kind", "blocked_reason",
                    ):
                        tags.pop(key, None)
                    tags["semantic_attestation_pending"] = "true"
                result = JobResult(
                    job_id=jid,
                    worker_node_id=worker_id,
                    status=JobStatus.NEUTRAL,
                    output=output,
                    error=error,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=(
                        max(0.0, (completed_at - started_at).total_seconds())
                        if started_at is not None else None
                    ),
                    claim_attempt_id=attempt_id or None,
                )
                changed = await self._db.execute(
                    """UPDATE jobs
                       SET status = ?, result = ?, tags = ?,
                           claimed_by = NULL, claimed_at = NULL,
                           claim_attempt_id = ?,
                           claim_expires_at = NULL, last_heartbeat = NULL
                       WHERE job_id = ? AND status = ?""",
                    (
                        JobStatus.NEUTRAL.value, _serialize_result(result),
                        json.dumps(tags), attempt_id or None,
                        jid, JobStatus.ABANDONED.value,
                    ),
                )
                if changed.rowcount != 1:
                    continue
                await self._audit(
                    jid,
                    JobStatus.ABANDONED.value,
                    JobStatus.NEUTRAL.value,
                    node_id=worker_id or None,
                    claim_attempt_id=attempt_id or None,
                    reason=f"{disposition}:abandoned_reconciliation",
                    details={"automatic_retry_forbidden": True},
                )
                await self._finalize_pending_controls_after_transition(
                    jid,
                    attempt_id or None,
                    reason="abandoned_effect_reconciliation",
                )
                if disposition == "ambiguous_prior_effects":
                    event_id = f"worker-outcome:{jid}:{attempt_id}:ambiguous"
                    await self._enqueue_worker_outcome(
                        event_id=event_id,
                        job_id=jid,
                        claim_attempt_id=attempt_id,
                        report={
                            "status": "ambiguous",
                            "summary": "abandoned effectful attempt",
                            "reason": "ambiguous_prior_effects",
                        },
                        verdict="unverified",
                        outcome="neutral",
                        worker_mode=self._job_governor_mode(job),
                        success_attested=False,
                        latency=result.duration_seconds,
                        attempts=job.retry_count,
                    )
                    ambiguity_events.append(event_id)
                continue

            if retry < max_r and not expired:
                new_retry = retry + 1
                await self._db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, retry_count = ?, claimed_by = NULL,
                        claimed_at = NULL, claim_attempt_id = NULL,
                        claim_expires_at = NULL, last_heartbeat = NULL
                    WHERE job_id = ?
                    """,
                    (JobStatus.QUEUED.value, new_retry, jid),
                )
                await self._audit(
                    jid, JobStatus.ABANDONED.value, JobStatus.QUEUED.value,
                    reason=f"retry {new_retry}/{max_r}",
                )
                await self._finalize_pending_controls_after_transition(
                    jid,
                    truth["last_attempt_id"],
                    reason="abandoned_retry_transition",
                )
                count += 1
            else:
                await self._db.execute(
                    """UPDATE jobs SET status = ?, claimed_by = NULL,
                              claimed_at = NULL, claim_expires_at = NULL,
                              last_heartbeat = NULL
                       WHERE job_id = ?""",
                    (JobStatus.FAILED.value, jid),
                )
                await self._audit(
                    jid, JobStatus.ABANDONED.value, JobStatus.FAILED.value,
                    reason="max_retries_exceeded",
                )
                await self._finalize_pending_controls_after_transition(
                    jid,
                    truth["last_attempt_id"],
                    reason="abandoned_terminal_transition",
                )
        if count or rows:
            await self._db.commit()
        for event_id in ambiguity_events:
            self._schedule_worker_outcome_drain(event_id)
        return count

    @_serialized_mutation
    async def unblock_ready_jobs(self) -> int:
        """Transition BLOCKED jobs to QUEUED when all deps completed. Returns count."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT job_id, depends_on, tags FROM jobs WHERE status = 'blocked'"
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            jid = row["job_id"]
            deps = json.loads(row["depends_on"] or "[]")
            try:
                tags = json.loads(row["tags"] or "{}")
            except (TypeError, json.JSONDecodeError):
                tags = {}
            hold_kind = str(tags.get("hold_kind") or "")
            blocked_reason = str(tags.get("blocked_reason") or "")
            # Legacy dependency-blocked rows had no hold_kind. Explicit
            # approval/boundary/governor holds must never be released merely
            # because a separate dependency completed.
            if (
                hold_kind not in {"", "dependency"}
                or blocked_reason in {
                    "awaiting_owner_approval", "boundary",
                    "boundary_refused", "governor_unavailable",
                }
                or str(tags.get("awaiting_owner_approval") or "").lower()
                in {"1", "true", "yes"}
            ):
                continue
            if not deps:
                continue
            # Check if any dep failed/cancelled
            any_failed = False
            dependent_status = JobStatus.FAILED
            fail_reason = ""
            all_complete = True
            for dep_id in deps:
                dep = await self.get_job(dep_id)
                if dep is None:
                    any_failed = True
                    fail_reason = f"dependency {dep_id} missing"
                    break
                dep_output = (
                    dep.result.output
                    if dep.result is not None
                    and isinstance(dep.result.output, Mapping) else {}
                )
                dep_execution_result = (
                    dep_output.get("execution_result")
                    if isinstance(
                        dep_output.get("execution_result"), Mapping,
                    ) else {}
                )
                dep_action_plane = (
                    dep_output.get("action_plane")
                    if isinstance(dep_output.get("action_plane"), Mapping)
                    else {}
                )
                dep_terminal = str(
                    dep_output.get("status") or dep_output.get("outcome")
                    or dep_execution_result.get("terminal_outcome") or ""
                ).strip().lower()
                dep_action_state = str(
                    dep_action_plane.get("state") or ""
                ).strip().lower()
                dep_terminal_neutral = bool(
                    dep_terminal in {
                        "skipped", "skip", "cancelled", "canceled",
                    }
                    or dep_action_state in {
                        "skipped", "skip", "cancelled", "canceled",
                    }
                )
                verification_pending = bool(
                    dep.status is JobStatus.NEUTRAL
                    and (
                        str(dep.tags.get("verification_pending") or "").lower()
                        == "true"
                        or (
                            not dep_terminal_neutral
                            and str(
                                dep.tags.get("governor_outcome_reason") or ""
                            ) == "work_order_transport_only"
                        )
                    )
                )
                if verification_pending:
                    all_complete = False
                    continue
                if dep.status in {
                    JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.NEUTRAL,
                }:
                    any_failed = True
                    fail_reason = f"dependency {dep_id} {dep.status.value}"
                    if dep.status is JobStatus.NEUTRAL:
                        dependent_status = JobStatus.NEUTRAL
                    break
                if dep.status != JobStatus.COMPLETED:
                    all_complete = False

            if any_failed:
                tags.pop("hold_kind", None)
                tags.pop("blocked_reason", None)
                await self._db.execute(
                    "UPDATE jobs SET status = ?, tags = ? WHERE job_id = ?",
                    (dependent_status.value, json.dumps(tags), jid),
                )
                await self._audit(
                    jid, JobStatus.BLOCKED.value, dependent_status.value,
                    reason=fail_reason,
                )
                count += 1
            elif all_complete:
                tags.pop("hold_kind", None)
                tags.pop("blocked_reason", None)
                await self._db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, claimed_by = NULL, claimed_at = NULL,
                        last_heartbeat = NULL, tags = ?
                    WHERE job_id = ?
                    """,
                    (JobStatus.QUEUED.value, json.dumps(tags), jid),
                )
                await self._audit(
                    jid, JobStatus.BLOCKED.value, JobStatus.QUEUED.value,
                    reason="dependencies_met",
                )
                count += 1
        if count:
            await self._db.commit()
        return count

    @_serialized_mutation
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
        cutoff = now - timedelta(hours=timeout_hours)
        cur = await self._db.execute(
            """
            SELECT job_id, tags, posted_at FROM jobs
            WHERE status = 'blocked'
            """,
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            tags = json.loads(row["tags"] or "{}")
            if tags.get("blocked_reason") != "awaiting_owner_approval":
                continue
            canonical_expiry = _parse_dt(tags.get("approval_expires_at"))
            if canonical_expiry is not None:
                if canonical_expiry > now:
                    continue
                jid = row["job_id"]
                await self._db.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (JobStatus.FAILED.value, jid),
                )
                await self._audit(
                    jid,
                    JobStatus.BLOCKED.value,
                    JobStatus.FAILED.value,
                    reason="canonical_approval_expired",
                )
                count += 1
                continue
            requested_at = _parse_dt(
                tags.get("approval_requested_at") or row["posted_at"]
            )
            if requested_at is not None and requested_at >= cutoff:
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

    @_serialized_mutation
    async def reconcile_blocked_approval_authority(self) -> int:
        """Repair legacy/partial approval holds without a read-side effect."""

        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?",
            (JobStatus.BLOCKED.value,),
        )
        rows = await cursor.fetchall()
        changed_count = 0
        for row in rows:
            job = _job_from_row(row)
            if job.tags.get("blocked_reason") != "awaiting_owner_approval":
                continue
            before_status = job.status
            before_tags = dict(job.tags)
            self._materialize_effect_approval(job)
            if job.status is JobStatus.QUEUED and job.depends_on:
                dependency_pending = False
                for dependency_id in job.depends_on:
                    dependency = await self._db.execute(
                        "SELECT status FROM jobs WHERE job_id = ?",
                        (dependency_id,),
                    )
                    dependency_row = await dependency.fetchone()
                    if (
                        dependency_row is None
                        or dependency_row["status"] != JobStatus.COMPLETED.value
                    ):
                        dependency_pending = True
                        break
                if dependency_pending:
                    job.status = JobStatus.BLOCKED
                    job.tags.update({
                        "hold_kind": "dependency",
                        "blocked_reason": "dependencies_pending",
                    })
            if job.status is before_status and job.tags == before_tags:
                continue
            updated = await self._db.execute(
                "UPDATE jobs SET status=?, tags=? WHERE job_id=? AND status=?",
                (
                    job.status.value,
                    json.dumps(job.tags),
                    job.job_id,
                    JobStatus.BLOCKED.value,
                ),
            )
            if updated.rowcount != 1:
                continue
            reason = (
                "approval_authority_materialized"
                if job.status is JobStatus.BLOCKED
                else "approval_authority_reconciled"
            )
            await self._audit(
                job.job_id,
                before_status.value,
                job.status.value,
                reason=reason,
            )
            changed_count += 1
        if changed_count:
            await self._db.commit()
        return changed_count

    @_serialized_mutation
    async def abandon_jobs_for_node(self, node_id: str) -> List[str]:
        """Release unstarted claims and durably fail RUNNING node attempts."""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT job_id, status, claim_attempt_id FROM jobs
            WHERE claimed_by = ? AND status IN ('claimed', 'running')
            """,
            (node_id,),
        )
        rows = await cur.fetchall()
        abandoned = []
        for row in rows:
            jid, old_status = row["job_id"], row["status"]
            if old_status == JobStatus.CLAIMED.value:
                changed = await self.release_job(
                    jid,
                    node_id,
                    claim_attempt_id=row["claim_attempt_id"],
                )
            else:
                changed = await self.fail_job(
                    jid,
                    node_id,
                    "worker_node_declared_dead",
                    claim_attempt_id=row["claim_attempt_id"],
                )
            if changed:
                abandoned.append(jid)
        return abandoned

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @_serialized_read
    async def get_job(self, job_id: str) -> Optional[Job]:
        """Retrieve a job by ID."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        )
        row = await cur.fetchone()
        return _job_from_row(row) if row else None

    # -- v0.13.0 digest helpers ------------------------------------------------

    @_serialized_read
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

        cur = await self._db.execute(
            """SELECT job_id, job_type, payload, result, tags, posted_at
               FROM jobs
               WHERE status = 'neutral' AND posted_at > ?
               ORDER BY posted_at DESC LIMIT ?""",
            (since_iso, limit),
        )
        neutral = []
        for row in await cur.fetchall():
            result_dict = {}
            try:
                result_dict = json.loads(row["result"] or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            neutral.append({
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "payload": json.loads(row["payload"] or "{}"),
                "result": result_dict,
                "tags": json.loads(row["tags"] or "{}"),
                "status": "needs_verification",
            })

        return {"completed": completed, "neutral": neutral, "failed": failed}

    @_serialized_read
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

    @_serialized_read
    async def current_work(self, limit: int = 20) -> dict:
        """Bounded canonical worker work, with no payload/result disclosure."""
        assert self._db is not None
        bounded = max(1, min(limit, 100))
        cur = await self._db.execute("""SELECT job_id,job_type,status,claimed_by,
            claim_attempt_id,claim_expires_at,last_heartbeat,payload FROM jobs
            WHERE status IN ('claimed','running') ORDER BY posted_at DESC LIMIT ?""", (bounded + 1,))
        rows = await cur.fetchall()
        now = self._worker_now()
        items = []
        for row in rows[:bounded]:
            payload = json.loads(row['payload'] or '{}')
            description = next((payload[key] for key in ('title', 'goal', 'description', 'instruction', 'task')
                                if isinstance(payload.get(key), str) and payload[key].strip()), row['job_type'])
            age = None
            if row['last_heartbeat']:
                try:
                    age = max(0, (now - datetime.fromisoformat(row['last_heartbeat'])).total_seconds())
                except (TypeError, ValueError):
                    pass
            items.append({'job_id': row['job_id'], 'job_type': row['job_type'], 'state': row['status'],
                'worker_id': row['claimed_by'], 'claim_attempt_id': row['claim_attempt_id'],
                'description': description[:240], 'heartbeat_age_seconds': round(age, 1) if age is not None else None,
                'liveness': 'recently_observed' if age is not None and age < self._worker_heartbeat_ttl_secs else 'unknown'})
        return {'items': items, 'truncated': len(rows) > bounded, 'source': 'canonical_task_queue'}

    @_serialized_read
    async def get_jobs_by_status(self, status: JobStatus) -> List[Job]:
        """Return all jobs with a given status."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM jobs WHERE status = ?", (status.value,)
        )
        rows = await cur.fetchall()
        return [_job_from_row(r) for r in rows]

    @_serialized_read
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

        worker_truth = await self._worker_truth_snapshot()

        return QueueStats(
            by_status=by_status,
            by_type=by_type,
            # Compatibility: total_workers continues to mean durable
            # registrations. New fields make liveness explicit.
            total_workers=worker_truth["registered_workers"],
            available_workers=worker_truth["available_workers"],
            registered_workers=worker_truth["registered_workers"],
            active_workers=worker_truth["active_workers"],
            stale_workers=worker_truth["stale_workers"],
            worker_heartbeat_ttl_secs=(
                worker_truth["worker_heartbeat_ttl_secs"]
            ),
        )

    @_serialized_read
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

    @_serialized_mutation
    async def register_worker(self, caps: WorkerCapabilities) -> None:
        """Register or update a worker node."""
        assert self._db is not None
        now = self._worker_now_iso()
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

    @_serialized_mutation
    async def deregister_worker(self, node_id: str) -> None:
        """Remove a worker from the registry."""
        assert self._db is not None
        await self._db.execute("DELETE FROM workers WHERE node_id = ?", (node_id,))
        await self._db.commit()

    @_serialized_mutation
    async def update_worker_load(self, node_id: str, load: float) -> None:
        """Update the load factor for a worker."""
        assert self._db is not None
        now = self._worker_now_iso()
        await self._db.execute(
            "UPDATE workers SET load = ?, last_seen = ? WHERE node_id = ?",
            (load, now, node_id),
        )
        await self._db.commit()

    @_serialized_read
    async def get_available_workers(self) -> List[WorkerCapabilities]:
        """Return fresh workers that are available and not at capacity."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM workers WHERE available = 1 AND load < 1.0"
        )
        rows = await cur.fetchall()
        now = self._worker_now()
        return [
            _worker_from_row(row)
            for row in rows
            if self._worker_last_seen_is_fresh(row["last_seen"], now=now)
        ]

    @_serialized_read
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

    @_serialized_mutation
    async def prune_old_jobs(
        self,
        completed_retention_days: int = 30,
        failed_retention_days: int = 7,
    ) -> int:
        """Retire old terminal jobs behind permanent identity tombstones."""
        assert self._db is not None
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        c_cutoff = (now - timedelta(days=completed_retention_days)).isoformat()
        f_cutoff = (now - timedelta(days=failed_retention_days)).isoformat()

        def _prune_dt(value: Any) -> Optional[datetime]:
            parsed = (
                value if isinstance(value, datetime)
                else (_parse_dt(value) if isinstance(value, str) else None)
            )
            if parsed is None:
                return None
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )

        cur = await self._db.execute(
            """
            SELECT * FROM jobs
            WHERE (
                    (status = 'completed' AND posted_at < ?)
                 OR (status IN ('neutral', 'failed', 'cancelled', 'abandoned') AND posted_at < ?)
            )
            ORDER BY posted_at, job_id
            """,
            (c_cutoff, f_cutoff),
        )
        rows = await cur.fetchall()
        retired = 0
        for raw in rows:
            row = dict(raw)
            try:
                tags = json.loads(row.get("tags") or "{}")
            except (TypeError, json.JSONDecodeError):
                tags = {}
            if (
                row["status"] == JobStatus.NEUTRAL.value
                and (
                    str(tags.get("verification_pending") or "").lower()
                    == "true"
                    or str(tags.get("ambiguous_prior_effects") or "").lower()
                    == "true"
                )
            ):
                continue
            reconciliation_cursor = await self._db.execute(
                """SELECT finding, created_at
                   FROM work_effect_reconciliations
                   WHERE target_id = ?""",
                (row["job_id"],),
            )
            reconciliations = await reconciliation_cursor.fetchall()
            if reconciliations:
                findings = {
                    str(item["finding"]) for item in reconciliations
                }
                latest_reconciliation = max(
                    (
                        value for value in (
                            _prune_dt(item["created_at"])
                            for item in reconciliations
                        ) if value is not None
                    ),
                    default=None,
                )

                # Positive effect evidence is not semantic success.  Its
                # live job/evidence row must remain available to the richer
                # action or WorkOrder attester; pruning cannot silently turn
                # an unresolved semantic outcome into a tombstone.
                if "applied" in findings and not (
                    row["status"] == JobStatus.COMPLETED.value
                    and str(tags.get("success_attested") or "").lower()
                    == "true"
                    and str(
                        tags.get("semantic_attestation_pending") or ""
                    ).lower() != "true"
                ):
                    continue

                # A negative finding is a consumable retry authorization.
                # Keep it until execution consumes it or its immutable
                # budget/deadline makes the target terminal. Approval and
                # dependency holds are deliberately ignored here: their
                # temporary absence must not destroy the safe retry path.
                retry_state = row["status"] in {
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.ABANDONED.value,
                }
                retry_budget = int(row["retry_count"]) < int(
                    row["max_retries"]
                )
                try:
                    payload = json.loads(row.get("payload") or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if (
                    isinstance(payload, dict)
                    and payload.get("schema") == "WorkOrderV1"
                ):
                    retry_budget = retry_budget and (
                        int(row["retry_count"])
                        < int(payload.get("max_attempts", 1)) - 1
                    )
                deadline = _prune_dt(row.get("deadline"))
                retry_unexpired = deadline is None or now <= deadline
                reconciliation_consumed_at = _prune_dt(
                    tags.get("effect_reconciliation_consumed_at")
                )
                if (
                    "not_applied" in findings
                    and "applied" not in findings
                    and reconciliation_consumed_at is None
                    and retry_state
                    and retry_budget
                    and retry_unexpired
                ):
                    continue

                # Once the reconciliation has been consumed (or the job is
                # terminal), retention starts from the newest lifecycle
                # anchor, never from the original posted_at.  This prevents
                # an old job from being tombstoned immediately after fresh
                # applied/not-applied evidence arrives.
                anchors = [
                    value for value in (
                        _prune_dt(row.get("posted_at")),
                        latest_reconciliation,
                        reconciliation_consumed_at,
                        (
                            deadline
                            if deadline is not None and now > deadline
                            else None
                        ),
                    ) if value is not None
                ]
                try:
                    result_payload = json.loads(row.get("result") or "{}")
                except (TypeError, json.JSONDecodeError):
                    result_payload = {}
                completed_anchor = _prune_dt(
                    result_payload.get("completed_at")
                    if isinstance(result_payload, dict) else None
                )
                if completed_anchor is not None:
                    anchors.append(completed_anchor)
                retention_cutoff = (
                    datetime.fromisoformat(c_cutoff)
                    if row["status"] == JobStatus.COMPLETED.value
                    else datetime.fromisoformat(f_cutoff)
                )
                if (
                    anchors
                    and max(anchors) >= retention_cutoff
                ):
                    continue
            pending_cursor = await self._db.execute(
                """SELECT 1 FROM worker_outcome_outbox
                   WHERE job_id = ? AND state = 'pending' LIMIT 1""",
                (row["job_id"],),
            )
            if await pending_cursor.fetchone() is not None:
                continue
            job_digest = hashlib.sha256(json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")).hexdigest()
            pruned_at = _now_iso()
            await self._db.execute(
                """INSERT INTO job_tombstones (
                       job_id, final_status, job_digest, pruned_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    row["job_id"], row["status"], job_digest, pruned_at,
                ),
            )
            deleted = await self._db.execute(
                "DELETE FROM jobs WHERE job_id = ? AND status = ?",
                (row["job_id"], row["status"]),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("job tombstone/delete transaction raced")
            retired += 1
        await self._db.commit()
        return retired

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _audit(
        self,
        job_id: str,
        from_status: Optional[str],
        to_status: str,
        node_id: Optional[str] = None,
        claim_attempt_id: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict] = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO job_audit (
                job_id, timestamp, from_status, to_status, node_id,
                claim_attempt_id, reason, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                _now_iso(),
                from_status,
                to_status,
                node_id,
                claim_attempt_id,
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

    def __init__(
        self,
        db_path: Path,
        event_bus: Optional[Any] = None,
        claim_timeout_secs: float = 30.0,
        worker_heartbeat_ttl_secs: float = (
            DEFAULT_WORKER_HEARTBEAT_TTL_SECS
        ),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.queue = QueueManager(
            db_path=db_path,
            event_bus=event_bus,
            claim_timeout_secs=claim_timeout_secs,
            worker_heartbeat_ttl_secs=worker_heartbeat_ttl_secs,
            clock=clock,
        )

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
        claim_timeout_secs: Optional[float] = None,
        worker_heartbeat_ttl_secs: Optional[float] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> "TaskQueueManager":
        """Create, start, and register the singleton TaskQueueManager."""
        import os

        if db_path is None:
            colony_home = Path(
                os.environ.get("COLONY_HOME", "~/.colony")
            ).expanduser()
            task_db_env = os.environ.get("COLONY_TASK_DB_PATH", "")
            db_path = Path(task_db_env) if task_db_env else colony_home / "task_queue.db"

        if claim_timeout_secs is None:
            claim_timeout_secs = _positive_seconds(
                os.environ.get("COLONY_QUEUE_CLAIM_TIMEOUT_SECS", "30"),
                default=30.0,
                field="COLONY_QUEUE_CLAIM_TIMEOUT_SECS",
            )
        if worker_heartbeat_ttl_secs is None:
            worker_heartbeat_ttl_secs = validate_worker_heartbeat_ttl(
                os.environ.get(
                    "COLONY_QUEUE_HEARTBEAT_TIMEOUT_SECS",
                    str(DEFAULT_WORKER_HEARTBEAT_TTL_SECS),
                )
            )
        instance = cls(
            db_path=db_path,
            event_bus=event_bus,
            claim_timeout_secs=claim_timeout_secs,
            worker_heartbeat_ttl_secs=worker_heartbeat_ttl_secs,
            clock=clock,
        )
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
                "neutral": JobStatus.NEUTRAL,
                "needs_verification": JobStatus.NEUTRAL,
                "failed": JobStatus.FAILED,
                "cancelled": JobStatus.CANCELLED,
            }
            for s in statuses:
                js = _status_map.get(s)
                if js is not None:
                    all_jobs.extend(await self.queue.get_jobs_by_status(js))
        else:
            for js in (
                JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COMPLETED,
                JobStatus.NEUTRAL, JobStatus.FAILED, JobStatus.CANCELLED,
            ):
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
                    JobStatus.NEUTRAL: "needs_verification",
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

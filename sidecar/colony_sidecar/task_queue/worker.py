"""Distributed Task Queue — Worker Node.

A WorkerNode polls the queue for eligible jobs, executes them via
registered handlers, and sends periodic heartbeats.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

if TYPE_CHECKING:
    from colony_sidecar.skills.learning.triggers import SkillLearningService

from colony_sidecar.task_queue.models import (
    Job,
    JobResult,
    JobStatus,
    JobType,
    WorkerCapabilities,
)

logger = logging.getLogger(__name__)


def _prepare_embedded_report(
    job: Job,
    output: Any,
) -> tuple[Dict[str, Any], bool]:
    """Normalize a trusted handler result and decide server attestation.

    Local execution is not itself proof of success.  The handler must return
    an unambiguous completed contract with no failure/skip markers.  WorkOrder
    success always remains transport-only until its independent verifier runs.
    """

    if not isinstance(output, dict):
        raise TypeError("embedded job handler must return a dictionary")
    report = dict(output)
    action_plane = (
        dict(report.get("action_plane"))
        if isinstance(report.get("action_plane"), dict) else {}
    )
    semantic = str(report.get("status") or "").strip().lower()
    action_state = str(action_plane.get("state") or "").strip().lower()
    failure = bool(
        report.get("success") is False
        or report.get("errors")
        or report.get("alerts")
        or semantic in {"failed", "failure", "error"}
        or action_state in {"failed", "failure", "error"}
    )
    skipped = bool(
        report.get("gate_blocked") is True
        or semantic in {"skipped", "cancelled", "canceled"}
        or action_state in {"skipped", "cancelled", "canceled"}
    )
    if failure:
        report["status"] = "failed"
        report["action_plane"] = {**action_plane, "state": "failed"}
        report.setdefault(
            "summary",
            str(report.get("error") or "embedded handler reported failure"),
        )
        return report, False
    if skipped:
        report["status"] = "skipped"
        report["action_plane"] = {**action_plane, "state": "skipped"}
        return report, False
    attested = bool(
        semantic in {"completed", "verified"}
        and action_state in {"completed", "verified"}
        and not (
            isinstance(job.payload, dict)
            and job.payload.get("schema") == "WorkOrderV1"
        )
    )
    return report, attested


def detect_local_capabilities() -> Dict[str, Any]:
    """Probe the local host and return a capabilities dict.

    Detects:
    - GPU presence and VRAM (nvidia-smi / Metal)
    - CPU core count and architecture
    - Available RAM (psutil if available)
    - macOS/Linux platform
    - Installed tools (docker, ollama, ffmpeg, git, etc.)
    - Apple Silicon identifier
    """
    caps: Dict[str, Any] = {}
    capacity: Dict[str, float] = {}

    # Platform
    system = platform.system().lower()
    machine = platform.machine().lower()
    caps["os_" + system] = True
    if "arm" in machine or "aarch64" in machine:
        caps["arm64"] = True
    if system == "darwin" and ("arm" in machine or "aarch64" in machine):
        caps["apple_silicon"] = True
        caps["metal"] = True

    # CPU
    cpu_count = os.cpu_count() or 1
    capacity["cpu_cores"] = float(cpu_count)

    # RAM
    try:
        import psutil
        ram_bytes = psutil.virtual_memory().total
        capacity["ram_gb"] = round(ram_bytes / (1024 ** 3), 1)
    except ImportError:
        pass

    # GPU (NVIDIA)
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                gpus = result.stdout.strip().splitlines()
                caps["gpu"] = True
                caps["cuda"] = True
                capacity["gpu_count"] = float(len(gpus))
                total_vram = 0.0
                for line in gpus:
                    if "," not in line:
                        continue
                    raw = line.split(",")[1].strip()
                    try:
                        total_vram += float(raw)
                    except ValueError:
                        pass  # nvidia-smi returned "[N/A]" or similar non-numeric value
                capacity["gpu_vram_gb"] = round(total_vram / 1024, 1)
        except Exception:
            logger.warning("GPU detection failed; treating node as CPU-only", exc_info=True)

    # Installed tools
    for tool in ["docker", "ollama", "ffmpeg", "git", "python3", "node"]:
        if shutil.which(tool):
            caps[tool] = True

    return {"capabilities": set(caps.keys()), "capacity": capacity}


class JobHandler:
    """Base class for job type handlers registered on a worker node.

    Subclass and implement ``execute`` to handle a specific JobType.
    """

    # Opt in only when cancelling the owning asyncio task really stops all
    # work. Handlers that spawn subprocesses/background effects need their own
    # wrapper and must leave this false.
    work_control_interrupt_safe = False
    # Steering can cross a response-loss/restart boundary. An override is not
    # advertised unless the handler guarantees that ``operation_id`` is a
    # durable idempotency key, so replay after the tiny handler/ledger crash
    # window cannot apply guidance twice.
    work_control_steer_idempotent = False

    async def execute(self, job: Job) -> Dict[str, Any]:
        """Execute the job and return an output dict.

        Raise any exception on failure. WorkerNode catches all exceptions
        and transitions the job to FAILED.
        """
        raise NotImplementedError

    async def apply_steer(
        self,
        job: Job,
        *,
        directive: str,
        context_refs: list[str],
        operation_id: str,
        authority_digest: str,
    ) -> Dict[str, Any]:
        """Cooperatively apply a directive to an in-flight handler.

        Handlers that can truthfully change an ongoing execution override
        this method.  Worker registration advertises steer support per job
        type only for an override, so this default is never presented to a
        controller as usable capability. Steering is subordinate guidance,
        never new authority: it may not expand the durable payload,
        capabilities, recipient, effect/risk class, approval, deadline, or
        attempt budget. An expansion requires a new governed queue job.

        An implementation must return the exact ``authority_digest`` and
        ``authority_expanded=False``. The worker rejects any other report.
        """

        raise NotImplementedError("handler does not support live steering")


class WorkerNode:
    """A mesh node that polls and executes jobs from the distributed queue.

    Usage::

        worker = WorkerNode(
            node_id="node-1",
            queue=queue_manager,
            handlers={JobType.INFERENCE: MyInferenceHandler()},
        )
        await worker.start()
    """

    def __init__(
        self,
        node_id: str,
        queue: Any,  # QueueManager — avoid circular import
        handlers: Optional[Dict[JobType, JobHandler]] = None,
        capabilities: Optional[WorkerCapabilities] = None,
        poll_interval_secs: float = 5.0,
        heartbeat_interval_secs: float = 15.0,
        skill_learning_service: "Optional[SkillLearningService]" = None,
    ) -> None:
        self.node_id = node_id
        self._queue = queue
        self._handlers: Dict[JobType, JobHandler] = handlers or {}
        self._capabilities = capabilities or self._build_capabilities()
        # Advertise ONLY the job types we can actually run. Handlers passed via the
        # constructor (build_default_handlers) never touched job_types — only
        # register_handler did — so an embedded worker left job_types empty, which
        # WorkerCapabilities.can_accept treats as "accept ALL types". It then claimed
        # AGENT_ACTION jobs it has no handler for and failed every one ("No handler
        # registered for job_type=..."). AGENT_ACTION is executed by a separate
        # agent-backed worker; keep this node scoped to the types it handles.
        if self._handlers and not self._capabilities.job_types:
            self._capabilities.job_types = set(self._handlers.keys())
        if JobType.THOUGHT in self._handlers:
            # Only a node that deliberately loaded the strict thought handler
            # may claim owner-private cognition jobs. Job-type matching alone
            # is insufficient because an empty advertised set means "all".
            self._configure_thought_handler_capabilities()
        self._rebuild_work_control_capabilities()
        self._poll_interval = poll_interval_secs
        self._heartbeat_interval = heartbeat_interval_secs
        self._running_jobs: Dict[str, asyncio.Task] = {}
        self._skill_tasks: set[asyncio.Task] = set()
        self._job_attempt_ids: Dict[str, str] = {}
        self._job_start_times: Dict[str, datetime] = {}
        self._running = False
        self._registered = False
        self._loop_tasks: set[asyncio.Task] = set()
        self._stop_in_progress = False
        self._stop_cleanup_done = asyncio.Event()
        self._stop_cleanup_done.set()
        self._skill_learning: Optional[Any] = skill_learning_service

    def _build_capabilities(self) -> WorkerCapabilities:
        detected = detect_local_capabilities()
        return WorkerCapabilities(
            node_id=self.node_id,
            capabilities=detected["capabilities"],
            capacity=detected["capacity"],
        )

    def register_handler(self, job_type: JobType, handler: JobHandler) -> None:
        """Register a handler for a specific job type."""
        if self._running or self._registered:
            raise RuntimeError(
                "handlers must be registered before the worker starts"
            )
        self._handlers[job_type] = handler
        self._capabilities.job_types.add(job_type)
        if job_type is JobType.THOUGHT:
            self._configure_thought_handler_capabilities()
        self._rebuild_work_control_capabilities()

    @staticmethod
    def _handler_supports_steer(handler: JobHandler) -> bool:
        implementation = getattr(type(handler), "apply_steer", None)
        return bool(
            implementation is not None
            and implementation is not JobHandler.apply_steer
            and getattr(
                handler, "work_control_steer_idempotent", False,
            ) is True
        )

    def _rebuild_work_control_capabilities(self) -> None:
        """Derive every WorkControl capability from the final handler map.

        Caller-preseeded values and capabilities from a replaced handler are
        removed first. This makes registration an attestation of current code,
        never a sticky string supplied by configuration.
        """

        from colony_sidecar.task_queue.work_control import (
            interrupt_capability,
            steer_capability,
            work_control_mode,
        )

        capabilities = {
            str(value)
            for value in (self._capabilities.capabilities or ())
            if not str(value).startswith("work_control:")
        }
        if work_control_mode() != "off":
            for job_type, handler in self._handlers.items():
                if getattr(
                    handler, "work_control_interrupt_safe", False,
                ) is True:
                    capabilities.add(interrupt_capability(job_type))
                if self._handler_supports_steer(handler):
                    capabilities.add(steer_capability(job_type))
        self._capabilities.capabilities = capabilities

    def _configure_thought_handler_capabilities(self) -> None:
        handler = self._handlers.get(JobType.THOUGHT)
        if not bool(getattr(handler, "thought_only", False)):
            raise ValueError(
                "ThoughtJobV1 requires the strict thought-only handler"
            )
        self._capabilities.capabilities = set(
            self._capabilities.capabilities or (),
        )
        self._capabilities.capabilities.update({
            "cognition_scoped", "thought_engine:v1",
        })

    async def start(self) -> None:
        """Register with the queue and start poll + heartbeat loops."""
        if self._running or self._registered:
            raise RuntimeError("worker is already started")
        self._running = True
        registration_attempted = False
        thought_attested = False
        try:
            registration_attempted = True
            # Handler attributes may have been configured after construction;
            # register only the final, locally re-derived capability truth.
            self._rebuild_work_control_capabilities()
            await self._queue.register_worker(self._capabilities)
            self._registered = True
            if not self._running:
                return
            if JobType.THOUGHT in self._handlers:
                thought_attested = self._queue.set_thought_runtime_ready(
                    True, node_id=self.node_id,
                )
                if not thought_attested:
                    raise RuntimeError(
                        "ThoughtJobV1 handler node does not match "
                        "COLONY_THOUGHT_WORKER_NODE_ID"
                    )
            logger.info(
                "Worker %s started (caps=%s)",
                self.node_id, self._capabilities.capabilities,
            )
            await self._run_worker_loops()
        finally:
            if JobType.THOUGHT in self._handlers:
                try:
                    self._queue.set_thought_runtime_ready(
                        False,
                        node_id=self.node_id,
                        reason="thought_worker_loop_exited",
                    )
                except Exception:
                    logger.exception(
                        "Worker %s failed to clear Thought readiness",
                        self.node_id,
                    )
            self._running = False
            if self._stop_in_progress:
                await self._stop_cleanup_done.wait()
            elif self._registered or registration_attempted:
                self._registered = False
                try:
                    await self._queue.deregister_worker(self.node_id)
                except Exception:
                    logger.exception(
                        "Worker %s failed to deregister after loop exit",
                        self.node_id,
                    )

    async def _run_worker_loops(self) -> None:
        """Run both owned loops with sibling cancellation on any failure."""

        tasks = {
            asyncio.create_task(
                self._poll_loop(), name=f"{self.node_id}:poll",
            ),
            asyncio.create_task(
                self._heartbeat_loop(), name=f"{self.node_id}:heartbeat",
            ),
        }
        from colony_sidecar.task_queue.work_control import work_control_mode

        if work_control_mode() != "off":
            tasks.add(asyncio.create_task(
                self._work_control_loop(), name=f"{self.node_id}:control",
            ))
        self._loop_tasks = tasks
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            failure: Optional[BaseException] = None
            for task in done:
                if task.cancelled():
                    if self._running:
                        failure = asyncio.CancelledError()
                        break
                    continue
                exc = task.exception()
                if exc is not None:
                    failure = exc
                    break
            if failure is None and self._running:
                failure = RuntimeError("worker lifecycle loop exited unexpectedly")
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._loop_tasks is tasks:
                self._loop_tasks = set()

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """Gracefully stop: wait for in-flight jobs, then deregister."""
        if self._stop_in_progress:
            await self._stop_cleanup_done.wait()
            return
        self._stop_in_progress = True
        self._stop_cleanup_done.clear()
        try:
            self._running = False
            if JobType.THOUGHT in self._handlers:
                self._queue.set_thought_runtime_ready(
                    False,
                    node_id=self.node_id,
                    reason="thought_worker_stopped",
                )
            loop_tasks = list(self._loop_tasks)
            for task in loop_tasks:
                task.cancel()
            if loop_tasks:
                await asyncio.gather(*loop_tasks, return_exceptions=True)
            if self._running_jobs:
                logger.info("Worker %s draining %d jobs...", self.node_id, len(self._running_jobs))
                attempts = dict(self._job_attempt_ids)
                task_jobs = {
                    task: job_id for job_id, task in self._running_jobs.items()
                }
                _done, pending = await asyncio.wait(
                    list(task_jobs),
                    timeout=drain_timeout,
                )
                if pending:
                    logger.warning(
                        "Worker %s cancelling %d jobs after drain timeout",
                        self.node_id,
                        len(pending),
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in pending:
                        job_id = task_jobs[task]
                        attempt = attempts.get(job_id)
                        if not attempt:
                            continue
                        try:
                            await self._queue.fail_job(
                                job_id,
                                self.node_id,
                                "worker_shutdown_drain_timeout",
                                claim_attempt_id=attempt,
                            )
                        except Exception:
                            logger.exception(
                                "Worker %s could not close shutdown job %s",
                                self.node_id,
                                job_id,
                            )
            if self._skill_tasks:
                for task in list(self._skill_tasks):
                    task.cancel()
                await asyncio.gather(*self._skill_tasks, return_exceptions=True)
                self._skill_tasks.clear()
            if self._registered:
                self._registered = False
                await self._queue.deregister_worker(self.node_id)
            logger.info("Worker %s stopped", self.node_id)
        finally:
            self._stop_cleanup_done.set()
            self._stop_in_progress = False

    async def _poll_loop(self) -> None:
        while self._running:
            if len(self._running_jobs) < self._capabilities.max_concurrent:
                try:
                    job = await self._queue.claim_job(
                        self.node_id, self._capabilities
                    )
                    if job is not None:
                        if not self._running:
                            attempt = str(job.claim_attempt_id or "").strip()
                            if attempt:
                                try:
                                    await self._queue.release_job(
                                        job.job_id,
                                        self.node_id,
                                        claim_attempt_id=attempt,
                                    )
                                except Exception:
                                    logger.exception(
                                        "Worker %s could not release post-stop "
                                        "claim %s",
                                        self.node_id,
                                        job.job_id,
                                    )
                            else:
                                logger.error(
                                    "Worker %s received a post-stop claim with "
                                    "no exact attempt: %s",
                                    self.node_id,
                                    job.job_id,
                                )
                            break
                        task = asyncio.create_task(self._execute_job(job))
                        self._running_jobs[job.job_id] = task
                        if job.claim_attempt_id:
                            self._job_attempt_ids[job.job_id] = job.claim_attempt_id
                        task.add_done_callback(
                            lambda t, jid=job.job_id: (
                                self._running_jobs.pop(jid, None),
                                self._job_attempt_ids.pop(jid, None),
                            )
                        )
                except Exception:
                    logger.exception("Worker %s: error claiming job", self.node_id)
            await asyncio.sleep(self._poll_interval)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            attempts = dict(self._job_attempt_ids)
            running_job_ids = list(attempts)
            if running_job_ids:
                try:
                    await self._queue.send_heartbeat(
                        self.node_id,
                        running_job_ids,
                        claim_attempt_ids=attempts,
                    )
                except Exception:
                    logger.exception("Worker %s: heartbeat failed", self.node_id)
            # Update load
            load = len(self._running_jobs) / max(self._capabilities.max_concurrent, 1)
            self._capabilities.load = load
            try:
                await self._queue.update_worker_load(self.node_id, load)
            except Exception:
                logger.warning("Worker %s: failed to update load metric", self.node_id, exc_info=True)

    async def _work_control_loop(self) -> None:
        """Deliver and acknowledge controls for exact owned attempts."""

        from colony_sidecar.task_queue.work_control import work_control_mode

        while self._running:
            await asyncio.sleep(min(max(self._poll_interval / 4.0, 0.1), 1.0))
            if work_control_mode() != "live":
                reconcile = getattr(
                    self._queue,
                    "reconcile_work_control_delivery_state",
                    None,
                )
                if callable(reconcile):
                    try:
                        await reconcile()
                    except Exception:
                        logger.exception(
                            "Worker %s: inactive WorkControl reconciliation "
                            "failed",
                            self.node_id,
                        )
                continue
            pending_controls = getattr(
                self._queue, "pending_work_control_operations", None,
            )
            if not callable(pending_controls):
                continue
            try:
                controls = await pending_controls(self.node_id)
            except Exception:
                logger.exception(
                    "Worker %s: WorkControl poll failed", self.node_id,
                )
                continue
            if not isinstance(controls, list):
                continue
            for control in controls:
                try:
                    await self._apply_work_control(control)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Worker %s: WorkControl delivery failed for %s",
                        self.node_id, control.get("operation_id"),
                    )

    async def _apply_work_control(self, control: Dict[str, Any]) -> None:
        """Cooperate with one durable control, acknowledging only truth."""

        operation = str(control.get("operation") or "")
        operation_id = str(control.get("operation_id") or "")
        job_id = str(control.get("target_id") or "")
        attempt_id = str(control.get("attempt_id") or "")

        def _control_live() -> bool:
            from colony_sidecar.task_queue.work_control import (
                work_control_mode,
            )

            return work_control_mode() == "live"

        async def _retire_if_inactive() -> bool:
            if _control_live():
                return False
            reconcile = getattr(
                self._queue, "reconcile_work_control_delivery_state", None,
            )
            if callable(reconcile):
                await reconcile()
            return True

        def _deadline(value: Any) -> Optional[datetime]:
            try:
                parsed = datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        def _lease_valid(job: Optional[Job] = None) -> bool:
            now = datetime.now(timezone.utc)
            lease = _deadline(control.get("ack_deadline"))
            if lease is None or now >= lease:
                return False
            if operation == "steer" and job is not None:
                deadline = job.deadline
                if deadline is not None:
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    if now >= deadline.astimezone(timezone.utc):
                        return False
            return True

        def _exact_active(job: Optional[Job]) -> bool:
            return bool(
                job is not None
                and job.claimed_by == self.node_id
                and job.claim_attempt_id == attempt_id
                and job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            )

        async def _ack(outcome: str, details: Dict[str, Any]) -> Any:
            if await _retire_if_inactive():
                return None
            return await self._queue.acknowledge_work_control_operation(
                worker_id=self.node_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                outcome=outcome,
                details=details,
                ack_authority={
                    "authority_kind": "in_process_worker",
                    "worker_id": self.node_id,
                },
            )

        if await _retire_if_inactive():
            return

        if not _lease_valid():
            # The queue acknowledgement path independently enforces the hard
            # deadline and records expiry; this call only accelerates closure.
            await _ack("rejected", {"reason": "control_lease_elapsed"})
            return

        job = await self._queue.get_job(job_id)

        if operation == "steer":
            handler = self._handlers.get(job.job_type) if job else None
            stored_outcome = getattr(
                self._queue, "get_work_control_worker_outcome", None,
            )
            record_outcome = getattr(
                self._queue, "record_work_control_worker_outcome", None,
            )
            if not callable(stored_outcome) or not callable(record_outcome):
                await _ack("rejected", {
                    "reason": "durable_steer_outcome_ledger_unavailable",
                })
                return
            persisted = await stored_outcome(
                worker_id=self.node_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
            )
            if persisted is not None:
                # Response loss after durable outcome recording must retry the
                # acknowledgement before the handler can be invoked again.
                await _ack(
                    str(persisted["outcome"]),
                    dict(persisted.get("details") or {}),
                )
                return
            task = self._running_jobs.get(job_id)
            exact_running = bool(
                _exact_active(job)
                and _lease_valid(job)
                and task is not None
                and not task.done()
                and self._job_attempt_ids.get(job_id) == attempt_id
            )
            if (
                not exact_running
                or handler is None
                or not self._handler_supports_steer(handler)
            ):
                await _ack("rejected", {
                    "reason": "attempt_or_steer_handler_unavailable",
                })
                return
            parameters = control.get("parameters") or {}
            # Re-read after every preceding await and immediately before the
            # local apply. A delayed attempt-A delivery cannot steer B.
            current = await self._queue.get_job(job_id)
            current_task = self._running_jobs.get(job_id)
            if await _retire_if_inactive():
                return
            if not (
                _exact_active(current)
                and _lease_valid(current)
                and current_task is task
                and current_task is not None
                and not current_task.done()
                and self._job_attempt_ids.get(job_id) == attempt_id
            ):
                await _ack("rejected", {
                    "reason": "attempt_changed_before_steer_apply",
                })
                return
            try:
                report = await asyncio.wait_for(
                    handler.apply_steer(
                        job,
                        directive=str(parameters.get("directive") or ""),
                        context_refs=[
                            str(item)
                            for item in parameters.get("context_refs", [])
                        ],
                        operation_id=operation_id,
                        authority_digest=str(
                            control.get("authority_digest") or ""
                        ),
                    ),
                    timeout=5.0,
                )
            except (NotImplementedError, asyncio.TimeoutError) as exc:
                rejected = {"reason": type(exc).__name__}
                await record_outcome(
                    worker_id=self.node_id,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    outcome="rejected",
                    details=rejected,
                )
                await _ack("rejected", rejected)
                return
            if not isinstance(report, dict):
                report = {"handler_report": str(report)[:500]}
            if (
                report.get("authority_digest")
                != control.get("authority_digest")
                or report.get("authority_expanded") is not False
            ):
                rejected = {
                    "reason": "steer_authority_expansion_or_unattested",
                }
                await record_outcome(
                    worker_id=self.node_id,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    outcome="rejected",
                    details=rejected,
                )
                await _ack("rejected", rejected)
                return
            await record_outcome(
                worker_id=self.node_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                outcome="applied",
                details=report,
            )
            # The handler outcome is durable now. Re-read immediately before
            # acknowledging; a winning lifecycle transition owns the receipt.
            current = await self._queue.get_job(job_id)
            if await _retire_if_inactive():
                return
            if not (_exact_active(current) and _lease_valid(current)):
                await _ack("rejected", {
                    "reason": "attempt_changed_before_steer_ack",
                })
                return
            await _ack("applied", report)
            return

        if operation not in {"interrupt", "cancel"}:
            await _ack("rejected", {"reason": "unsupported_worker_operation"})
            return

        handler = self._handlers.get(job.job_type) if job else None
        task = self._running_jobs.get(job_id)
        if not (
            _exact_active(job)
            and _lease_valid(job)
            and handler is not None
            and getattr(
                handler, "work_control_interrupt_safe", False,
            ) is True
            and task is not None
            and not task.done()
            and self._job_attempt_ids.get(job_id) == attempt_id
        ):
            await _ack("rejected", {
                "reason": "attempt_or_interrupt_handler_unavailable",
            })
            return

        # Re-read immediately before cancel and bind the exact local Task
        # object as well as the durable attempt. There is no await between the
        # final checks and task.cancel(), so a newer task cannot be selected.
        current = await self._queue.get_job(job_id)
        current_task = self._running_jobs.get(job_id)
        if await _retire_if_inactive():
            return
        if not (
            _exact_active(current)
            and _lease_valid(current)
            and current_task is task
            and current_task is not None
            and not current_task.done()
            and self._job_attempt_ids.get(job_id) == attempt_id
        ):
            await _ack("rejected", {
                "reason": "attempt_changed_before_interrupt",
            })
            return
        task.cancel()
        _done, pending = await asyncio.wait({task}, timeout=5.0)
        if pending:
            await _ack("rejected", {"reason": "handler_did_not_stop"})
            return

        current = await self._queue.get_job(job_id)
        if await _retire_if_inactive():
            return
        if not (_exact_active(current) and _lease_valid(current)):
            await _ack("rejected", {
                "reason": "attempt_changed_before_stop_ack",
            })
            return
        await _ack("stopped", {"cooperative_stop": True})

    async def _execute_job(self, job: Job) -> None:
        handler = self._handlers.get(job.job_type)
        started_at = datetime.now(timezone.utc)
        self._job_start_times[job.job_id] = started_at

        if handler is None:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=f"No handler registered for job_type={job.job_type}",
                started_at=started_at,
                claim_attempt_id=job.claim_attempt_id,
            )
            self._job_start_times.pop(job.job_id, None)
            return

        if not await self._queue.start_job(
            job.job_id,
            self.node_id,
            claim_attempt_id=job.claim_attempt_id,
        ):
            # The claim may have expired, been released, or moved to another
            # worker between polling and execution. Never run a handler after
            # the central queue rejects the claimant/state transition.
            logger.warning(
                "Worker %s: start authority rejected for job %s",
                self.node_id,
                job.job_id,
            )
            self._job_start_times.pop(job.job_id, None)
            return
        try:
            output = await asyncio.wait_for(
                handler.execute(job),
                timeout=job.timeout_secs,
            )
            output, server_attested = _prepare_embedded_report(job, output)
            completion = await self._queue.complete_job(
                job.job_id,
                self.node_id,
                output or {},
                started_at=started_at,
                claim_attempt_id=job.claim_attempt_id,
                server_attested=server_attested,
            )
            # ThoughtJobV1 is strictly read-only.  The generic post-task
            # learner writes skill state, so it must never run for this type;
            # validated MemoryWriteProposal remains only a proposal.
            if (completion.get("transitioned") is True
                    and completion.get("job_status") == JobStatus.COMPLETED.value
                    and completion.get("governor_outcome") == "success"
                    and self._skill_learning is not None
                    and job.job_type != JobType.THOUGHT):
                _hook_task = asyncio.create_task(self._fire_skill_hook(job, output or {}))
                self._skill_tasks.add(_hook_task)
                _hook_task.add_done_callback(
                    self._skill_tasks.discard
                )
        except asyncio.TimeoutError:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=f"Timed out after {job.timeout_secs}s",
                started_at=started_at,
                claim_attempt_id=job.claim_attempt_id,
            )
        except Exception as exc:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=str(exc),
                started_at=started_at,
                claim_attempt_id=job.claim_attempt_id,
            )
        finally:
            self._job_start_times.pop(job.job_id, None)

    async def _fire_skill_hook(self, job: Job, output: Dict[str, Any]) -> None:
        """Asynchronously fire the skill learning post-task hook.

        Builds a minimal TaskSolution from the completed job and delegates
        to SkillLearningService.post_task_hook. Errors are logged and
        swallowed so they never surface to the caller.
        """
        try:
            from colony_sidecar.skills.models import TaskSolution
            solution = TaskSolution(
                task_id=job.job_id,
                task_description=str(job.payload.get("description", job.job_type.value)),
                inputs=dict(job.payload),
                output=output,
                trace=[],
                dependencies=list(job.payload.get("dependencies", [])),
                embedding=None,
                step_fingerprint=[str(k) for k in output.keys()],
                duration_secs=0.0,
                completed_at=datetime.now(timezone.utc),
            )
            await self._skill_learning.post_task_hook(solution)
        except Exception:
            logger.debug("Skill learning hook failed for job %s", job.job_id, exc_info=True)

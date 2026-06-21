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

    async def execute(self, job: Job) -> Dict[str, Any]:
        """Execute the job and return an output dict.

        Raise any exception on failure. WorkerNode catches all exceptions
        and transitions the job to FAILED.
        """
        raise NotImplementedError


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
        self._poll_interval = poll_interval_secs
        self._heartbeat_interval = heartbeat_interval_secs
        self._running_jobs: Dict[str, asyncio.Task] = {}
        self._job_start_times: Dict[str, datetime] = {}
        self._running = False
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
        self._handlers[job_type] = handler
        self._capabilities.job_types.add(job_type)

    async def start(self) -> None:
        """Register with the queue and start poll + heartbeat loops."""
        self._running = True
        await self._queue.register_worker(self._capabilities)
        logger.info(
            "Worker %s started (caps=%s)",
            self.node_id, self._capabilities.capabilities,
        )
        await asyncio.gather(
            self._poll_loop(),
            self._heartbeat_loop(),
        )

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """Gracefully stop: wait for in-flight jobs, then deregister."""
        self._running = False
        if self._running_jobs:
            logger.info("Worker %s draining %d jobs...", self.node_id, len(self._running_jobs))
            await asyncio.wait(
                list(self._running_jobs.values()),
                timeout=drain_timeout,
            )
        await self._queue.deregister_worker(self.node_id)
        logger.info("Worker %s stopped", self.node_id)

    async def _poll_loop(self) -> None:
        while self._running:
            if len(self._running_jobs) < self._capabilities.max_concurrent:
                try:
                    job = await self._queue.claim_job(
                        self.node_id, self._capabilities
                    )
                    if job is not None:
                        task = asyncio.create_task(self._execute_job(job))
                        self._running_jobs[job.job_id] = task
                        task.add_done_callback(
                            lambda t, jid=job.job_id: self._running_jobs.pop(jid, None)
                        )
                except Exception:
                    logger.exception("Worker %s: error claiming job", self.node_id)
            await asyncio.sleep(self._poll_interval)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            running_job_ids = list(self._running_jobs.keys())
            if running_job_ids:
                try:
                    await self._queue.send_heartbeat(self.node_id, running_job_ids)
                except Exception:
                    logger.exception("Worker %s: heartbeat failed", self.node_id)
            # Update load
            load = len(self._running_jobs) / max(self._capabilities.max_concurrent, 1)
            self._capabilities.load = load
            try:
                await self._queue.update_worker_load(self.node_id, load)
            except Exception:
                logger.warning("Worker %s: failed to update load metric", self.node_id, exc_info=True)

    async def _execute_job(self, job: Job) -> None:
        handler = self._handlers.get(job.job_type)
        started_at = datetime.now(timezone.utc)
        self._job_start_times[job.job_id] = started_at

        if handler is None:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=f"No handler registered for job_type={job.job_type}",
                started_at=started_at,
            )
            self._job_start_times.pop(job.job_id, None)
            return

        await self._queue.start_job(job.job_id, self.node_id)
        try:
            output = await asyncio.wait_for(
                handler.execute(job),
                timeout=job.timeout_secs,
            )
            await self._queue.complete_job(
                job.job_id, self.node_id, output or {}, started_at=started_at
            )
            if self._skill_learning is not None:
                _hook_key = f"hook-{job.job_id}"
                _hook_task = asyncio.create_task(self._fire_skill_hook(job, output or {}))
                self._running_jobs[_hook_key] = _hook_task
                _hook_task.add_done_callback(
                    lambda t, k=_hook_key: self._running_jobs.pop(k, None)
                )
        except asyncio.TimeoutError:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=f"Timed out after {job.timeout_secs}s",
                started_at=started_at,
            )
        except Exception as exc:
            await self._queue.fail_job(
                job.job_id, self.node_id,
                error=str(exc),
                started_at=started_at,
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

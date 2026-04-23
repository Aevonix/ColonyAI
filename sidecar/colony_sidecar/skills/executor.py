"""Colony Skills — skill executor with capability-gated sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import logging
import os
import pathlib
import sys
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.skills.models import SkillManifest, SkillStatus
from colony_sidecar.skills.registry import SkillRegistry
from colony_sidecar.skills.security.scanner import ASTScanner
from colony_sidecar.skills.security.guards import CapabilityGuard

logger = logging.getLogger(__name__)


_DEFAULT_SANDBOX_MODE = "subprocess" if sys.platform.startswith("linux") else "inprocess"


def _resolve_sandbox_mode() -> str:
    """Return the configured sandbox mode, falling back to the platform default."""
    override = (os.environ.get("COLONY_SKILL_SANDBOX") or "").strip().lower()
    if override in ("subprocess", "inprocess"):
        if override == "inprocess" and _DEFAULT_SANDBOX_MODE == "subprocess":
            logger.warning(
                "COLONY_SKILL_SANDBOX=inprocess overrides the default subprocess "
                "isolation — skills will run in the sidecar process."
            )
        return override
    return _DEFAULT_SANDBOX_MODE


class SecurityError(RuntimeError):
    """Raised when a skill fails integrity or security checks."""


@dataclass
class ExecutionResult:
    """Result of a skill invocation."""
    execution_id: str
    skill_id: str
    status: str          # "success" | "failed" | "violated" | "timeout"
    output: Any
    error: Optional[str]
    duration_ms: int
    peak_memory_mb: Optional[float]
    violations: List[str] = field(default_factory=list)


class SkillExecutor:
    """Executes Colony skills inside a capability-gated sandbox.

    Execution pipeline:
      1. Load manifest and verify skill status.
      2. Run capability guard checks.
      3. Verify on-disk source checksum.
      4. Execute either in a hardened subprocess (default on Linux) or
         in-process (rollback path, controlled by COLONY_SKILL_SANDBOX).
      5. Log result to execution log; quarantine on timeout.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        guard: CapabilityGuard,
        scanner: ASTScanner,
        execution_timeout_secs: float = 60.0,
        sandbox_mode: Optional[str] = None,
    ) -> None:
        self._registry = registry
        self._guard = guard
        self._scanner = scanner
        self._timeout = execution_timeout_secs
        self._sandbox_mode = (sandbox_mode or _resolve_sandbox_mode()).lower()

    async def invoke(
        self,
        skill_id: str,
        inputs: Dict[str, Any],
        caller_context: Optional[str] = None,
    ) -> ExecutionResult:
        """Invoke a skill by ID with the given inputs."""
        execution_id = f"exec-{uuid.uuid4().hex[:12]}"
        start = datetime.now(timezone.utc)

        manifest = await self._registry.get(skill_id)
        if not manifest:
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="failed",
                output=None,
                error=f"Skill '{skill_id}' not found in registry.",
                duration_ms=0,
                peak_memory_mb=None,
            )

        if manifest.status != SkillStatus.ACTIVE:
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="failed",
                output=None,
                error=f"Skill '{skill_id}' is not active (status: {manifest.status.value}).",
                duration_ms=0,
                peak_memory_mb=None,
            )

        guard_result = await self._guard.check(manifest, inputs)
        if not guard_result.allowed:
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="failed",
                output=None,
                error=f"Capability guard denied execution: {guard_result.reason}",
                duration_ms=0,
                peak_memory_mb=None,
                violations=guard_result.violations,
            )

        skill_dir_str = manifest.skill_dir or ""
        skill_dir = pathlib.Path(skill_dir_str)
        skill_path = skill_dir / "skill.py"
        if not skill_path.exists():
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="failed",
                output=None,
                error=f"Skill '{skill_id}' source file not found.",
                duration_ms=0,
                peak_memory_mb=None,
            )

        try:
            verified_source = self._read_verified_source(skill_path, manifest)
            if self._sandbox_mode == "subprocess":
                raw = await asyncio.wait_for(
                    self._run_subprocess(verified_source, inputs, manifest),
                    timeout=manifest.permissions.max_duration_secs,
                )
                end = datetime.now(timezone.utc)
                duration_ms = int((end - start).total_seconds() * 1000)
                return await self._finalize_subprocess_result(
                    skill_id=skill_id,
                    execution_id=execution_id,
                    raw=raw,
                    duration_ms=duration_ms,
                )

            # inprocess rollback path — kept verbatim so operators can flip
            # back to the pre-sandbox behaviour without redeploying.
            output = await asyncio.wait_for(
                self._run_skill_inprocess(verified_source, inputs),
                timeout=manifest.permissions.max_duration_secs,
            )
            end = datetime.now(timezone.utc)
            duration_ms = int((end - start).total_seconds() * 1000)
            await self._registry.record_execution(skill_id, execution_id, "success", duration_ms)
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="success",
                output=output,
                error=None,
                duration_ms=duration_ms,
                peak_memory_mb=None,
            )
        except asyncio.TimeoutError:
            await self._registry.quarantine(skill_id, f"Execution timeout in {execution_id}")
            end = datetime.now(timezone.utc)
            duration_ms = int((end - start).total_seconds() * 1000)
            await self._registry.record_execution(
                skill_id, execution_id, "timeout", duration_ms, ["timeout"]
            )
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="timeout",
                output=None,
                error="Execution exceeded maximum duration.",
                duration_ms=duration_ms,
                peak_memory_mb=None,
                violations=["timeout"],
            )
        except Exception as exc:
            end = datetime.now(timezone.utc)
            duration_ms = int((end - start).total_seconds() * 1000)
            await self._registry.record_execution(
                skill_id, execution_id, "failed", duration_ms
            )
            logger.debug("Skill '%s' execution error (%s)", skill_id, exc, exc_info=True)
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="failed",
                output=None,
                error=f"Skill '{skill_id}' execution failed: {type(exc).__name__}",
                duration_ms=duration_ms,
                peak_memory_mb=None,
            )

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    @staticmethod
    def _read_verified_source(
        skill_path: pathlib.Path,
        manifest: SkillManifest,
    ) -> bytes:
        """Read + checksum-verify the skill source in one shot.

        Returns the in-memory bytes on success; raises SecurityError on
        missing or mismatched checksum. Reading once and returning bytes
        eliminates the TOCTOU window between verification and execution —
        both the in-process and subprocess paths operate on the returned
        buffer, never touching the filesystem again.
        """
        if not manifest.checksum_sha256:
            raise SecurityError(
                "Skill has no checksum — integrity cannot be verified."
            )
        file_bytes = skill_path.read_bytes()
        actual_digest = hashlib.sha256(file_bytes).hexdigest()
        if not hmac.compare_digest(actual_digest, manifest.checksum_sha256):
            raise SecurityError(
                "Skill checksum mismatch — file may have been tampered with."
            )
        return file_bytes

    # ------------------------------------------------------------------
    # Subprocess path
    # ------------------------------------------------------------------

    async def _run_subprocess(
        self,
        source_bytes: bytes,
        inputs: Dict[str, Any],
        manifest: SkillManifest,
    ) -> Dict[str, Any]:
        """Run the skill in a hardened subprocess via sandbox_runner.

        Returns the parsed JSON payload from the runner's stdout, or a
        synthesized failure dict on malformed output / nonzero exit.
        """
        payload = json.dumps({
            "source": source_bytes.decode("utf-8", errors="replace"),
            "inputs": inputs,
            "allowed_imports": manifest.permissions.allowed_imports if hasattr(manifest.permissions, 'allowed_imports') else [],
            "limits": {
                "mem_mb": manifest.permissions.max_memory_mb,
                "cpu_secs": manifest.permissions.max_duration_secs,
                "fsize_mb": 8,
            },
        }).encode("utf-8")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "colony_sidecar.skills.sandbox_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await proc.communicate(input=payload)
        except asyncio.CancelledError:
            # Parent timeout — kill the child and re-raise so asyncio.wait_for
            # sees TimeoutError in the caller.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        returncode = proc.returncode or 0

        if not stdout:
            # The runner always emits exactly one JSON line on success or
            # handled failure. An empty stdout means the kernel killed
            # the process before it could report (RLIMIT_AS, RLIMIT_CPU,
            # segfault, OOM killer). Surface stderr excerpt for operators.
            return {
                "status": "failed",
                "error": f"runner_killed (rc={returncode}): {stderr[-200:]}",
                "peak_memory_kb": 0,
            }

        try:
            parsed = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            return {
                "status": "failed",
                "error": f"runner_malformed_output: {stdout[-200:]}",
                "peak_memory_kb": 0,
            }

        if not isinstance(parsed, dict) or "status" not in parsed:
            return {
                "status": "failed",
                "error": "runner returned an unexpected payload shape",
                "peak_memory_kb": 0,
            }

        # Defense in depth: runner claimed success but exited nonzero.
        if parsed.get("status") == "success" and returncode != 0:
            parsed = {
                "status": "failed",
                "error": f"runner exited nonzero ({returncode}) despite success payload",
                "peak_memory_kb": parsed.get("peak_memory_kb", 0),
            }

        return parsed

    async def _finalize_subprocess_result(
        self,
        skill_id: str,
        execution_id: str,
        raw: Dict[str, Any],
        duration_ms: int,
    ) -> ExecutionResult:
        """Map the runner's JSON payload onto ExecutionResult + persist log."""
        status = raw.get("status", "failed")
        peak_kb = int(raw.get("peak_memory_kb") or 0)
        peak_mb: Optional[float] = (peak_kb / 1024.0) if peak_kb else None

        if status == "success":
            await self._registry.record_execution(
                skill_id, execution_id, "success", duration_ms
            )
            return ExecutionResult(
                execution_id=execution_id,
                skill_id=skill_id,
                status="success",
                output=raw.get("output"),
                error=None,
                duration_ms=duration_ms,
                peak_memory_mb=peak_mb,
            )

        error = raw.get("error") or "skill failed with no error message"
        await self._registry.record_execution(
            skill_id, execution_id, "failed", duration_ms
        )
        return ExecutionResult(
            execution_id=execution_id,
            skill_id=skill_id,
            status="failed",
            output=None,
            error=str(error),
            duration_ms=duration_ms,
            peak_memory_mb=peak_mb,
        )

    # ------------------------------------------------------------------
    # In-process rollback path
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_skill_inprocess(
        source_bytes: bytes,
        inputs: Dict[str, Any],
    ) -> Any:
        """Legacy in-process execution. Used only when COLONY_SKILL_SANDBOX=inprocess."""
        code = compile(source_bytes, "<skill>", "exec")
        module = types.ModuleType("_skill_module")
        exec(code, module.__dict__)  # noqa: S102  # in-memory, checksum verified
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise AttributeError("Skill has no 'run' function.")
        # Inject colony runtime if the skill signature expects it
        import inspect
        sig = inspect.signature(run_fn)
        if "colony" in sig.parameters:
            from colony_sidecar.skills.runtime import ColonyRuntime
            colony_rt = ColonyRuntime(base_url=os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
            inputs = {"colony": colony_rt, **inputs}
        return await run_fn(**inputs)

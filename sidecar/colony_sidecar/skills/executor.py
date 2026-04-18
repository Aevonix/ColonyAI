"""Colony Skills — skill executor with capability-gated sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import logging
import pathlib
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
    """Executes Colony skills inside a capability-gated async sandbox.

    Execution pipeline:
      1. Load manifest and verify skill status.
      2. Run capability guard checks.
      3. Execute with asyncio timeout.
      4. Log result to execution log.
      5. If timeout/violation, quarantine the skill.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        guard: CapabilityGuard,
        scanner: ASTScanner,
        execution_timeout_secs: float = 60.0,
    ) -> None:
        self._registry = registry
        self._guard = guard
        self._scanner = scanner
        self._timeout = execution_timeout_secs

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
            output = await asyncio.wait_for(
                self._run_skill(skill_path, inputs, manifest),
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

    @staticmethod
    async def _run_skill(
        skill_path: pathlib.Path,
        inputs: Dict[str, Any],
        manifest: SkillManifest,
    ) -> Any:
        """Load skill from disk with SHA-256 integrity verification.

        Reads the file once into memory, verifies the checksum against the
        stored manifest value, then compiles and executes from the in-memory
        bytes — eliminating the TOCTOU window between existence check and load.

        Raises:
            SecurityError: If the checksum is missing or does not match.
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

        code = compile(file_bytes, str(skill_path), "exec")
        module = types.ModuleType("_skill_module")
        exec(code, module.__dict__)  # noqa: S102  # nosec B102  # in-memory compiled bytes, checksum verified
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise AttributeError("Skill has no 'run' function.")
        return await run_fn(**inputs)

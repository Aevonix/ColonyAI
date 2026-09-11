"""Sandbox backends: pluggable isolated code execution (Phase B item 6).

The backend -- NOT the caller -- enforces containment: no egress, no
credentials, capped CPU/memory/pids/wall-clock, read-only rootfs with a single
writable workdir. A tool can ask to run a script; it can never widen these
limits (they come from env/policy, resolved by the manager, and are passed to
the backend which applies them literally).

Reference backend is Docker; a DisabledSandbox is the default when Docker is
absent so the feature degrades to a clear "unavailable" rather than an unsafe
host-side exec.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_LANG_INTERP = {
    "python": ["python", "/work/script"],
    "py": ["python", "/work/script"],
    "bash": ["sh", "/work/script"],
    "sh": ["sh", "/work/script"],
    "node": ["node", "/work/script"],
    "javascript": ["node", "/work/script"],
}


@dataclass
class SandboxLimits:
    """Containment limits, resolved from policy/env and applied by the backend."""
    image: str = "python:3.12-slim"
    cpus: float = 1.0
    memory: str = "512m"
    timeout_secs: int = 30
    pids_limit: int = 128
    egress: str = "none"                # none | allowlist
    max_artifact_bytes: int = 1_048_576  # 1 MiB total read-back cap

    @property
    def network(self) -> str:
        # Only "none" is honored as a hard no-egress; allowlist requires an
        # egress proxy the deployment provides (documented, not default).
        return "none" if self.egress == "none" else self.egress


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    artifacts: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "stdout": self.stdout, "stderr": self.stderr,
            "exit_code": self.exit_code, "timed_out": self.timed_out,
            "artifacts": list(self.artifacts.keys()),
            "artifact_preview": {k: v[:500] for k, v in self.artifacts.items()},
            "error": self.error,
        }


class SandboxBackend(ABC):
    name = "abstract"

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def run(self, script: str, lang: str, limits: SandboxLimits) -> SandboxResult:
        ...


class DisabledSandbox(SandboxBackend):
    """Default backend when no isolation is available. Never executes."""
    name = "disabled"

    def __init__(self, reason: str = "no container runtime available") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def run(self, script: str, lang: str, limits: SandboxLimits) -> SandboxResult:
        return SandboxResult(error=f"sandbox unavailable: {self._reason}")


class DockerSandbox(SandboxBackend):
    """Ephemeral-container backend: no egress, no creds, capped resources."""
    name = "docker"

    def __init__(self, docker_bin: str = "docker") -> None:
        self._docker = docker_bin

    def available(self) -> bool:
        return shutil.which(self._docker) is not None

    def build_command(self, workdir: str, lang: str,
                      limits: SandboxLimits) -> List[str]:
        """The exact docker invocation (pure -- unit-tested for containment).

        Guarantees: --network none (no egress), no -e/env (no credentials),
        --read-only rootfs with a single writable bind workdir, dropped
        capabilities + no-new-privileges, hard CPU/memory/pids caps, and a
        wall-clock timeout inside the container.
        """
        interp = _LANG_INTERP.get((lang or "python").lower(),
                                  _LANG_INTERP["python"])
        return [
            self._docker, "run", "--rm",
            "--network", limits.network,
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "-v", f"{workdir}:/work:rw",
            "-w", "/work",
            "--cpus", str(limits.cpus),
            "--memory", str(limits.memory),
            "--pids-limit", str(limits.pids_limit),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            limits.image,
            "timeout", str(limits.timeout_secs), *interp,
        ]

    def run(self, script: str, lang: str, limits: SandboxLimits) -> SandboxResult:
        workdir = tempfile.mkdtemp(prefix="colony-sandbox-")
        try:
            script_path = os.path.join(workdir, "script")
            with open(script_path, "w") as f:
                f.write(script or "")
            cmd = self.build_command(workdir, lang, limits)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=limits.timeout_secs + 10)  # outer guard > inner
            except subprocess.TimeoutExpired:
                return SandboxResult(timed_out=True,
                                     error="wall-clock timeout")
            except Exception as exc:
                return SandboxResult(error=f"backend error: {exc}")
            result = SandboxResult(
                stdout=(proc.stdout or "")[:100_000],
                stderr=(proc.stderr or "")[:20_000],
                exit_code=proc.returncode,
                timed_out=(proc.returncode == 124))  # timeout(1) exit code
            self._read_artifacts(workdir, script_path, limits, result)
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _read_artifacts(workdir: str, script_path: str,
                        limits: SandboxLimits, result: SandboxResult) -> None:
        budget = limits.max_artifact_bytes
        for root, _dirs, files in os.walk(workdir):
            for fn in files:
                path = os.path.join(root, fn)
                if path == script_path:
                    continue
                if budget <= 0:
                    result.error = (result.error
                                    or "artifact size cap reached").strip()
                    return
                try:
                    with open(path, "rb") as fh:
                        data = fh.read(budget + 1)
                except Exception:
                    continue
                if len(data) > budget:
                    result.error = "artifact size cap reached"
                    data = data[:budget]
                budget -= len(data)
                rel = os.path.relpath(path, workdir)
                result.artifacts[rel] = data.decode("utf-8", "replace")


def select_backend(mode: str) -> SandboxBackend:
    """Pick a backend: Docker when present, else Disabled. Mode gates whether
    the manager actually executes, not which backend exists."""
    docker = DockerSandbox()
    if docker.available():
        return docker
    return DisabledSandbox()

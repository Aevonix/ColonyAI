"""Exploration sandbox: gated, isolated code execution (cognition item 6).

Safe curiosity -- run a script inside a locked-down backend (no egress, no
credentials, capped resources, read-only rootfs + one writable workdir) behind
a mode flag, a DirectiveGuard boundary check, and approval tiering. Server-side
enforcement: the caller can never widen containment. Default off.
"""

from colony_sidecar.sandbox.backend import (
    DisabledSandbox, DockerSandbox, SandboxBackend, SandboxLimits,
    SandboxResult, select_backend,
)
from colony_sidecar.sandbox.manager import (
    SandboxManager, resolve_limits, sandbox_mode,
)

__all__ = [
    "SandboxBackend", "SandboxLimits", "SandboxResult",
    "DockerSandbox", "DisabledSandbox", "select_backend",
    "SandboxManager", "sandbox_mode", "resolve_limits",
]

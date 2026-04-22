"""Shared helpers for job handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from colony_sidecar.task_queue.worker import JobHandler
from colony_sidecar.task_queue.models import Job

logger = logging.getLogger(__name__)


async def run_subprocess(
    cmd: list[str], timeout: float
) -> tuple[int, str, str]:
    """Run a subprocess, capture stdout/stderr, and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise asyncio.TimeoutError(
            f"Subprocess {cmd[0]!r} timed out after {timeout}s"
        )
    return proc.returncode, stdout_b.decode(), stderr_b.decode()


__all__ = ["JobHandler", "Job", "run_subprocess"]

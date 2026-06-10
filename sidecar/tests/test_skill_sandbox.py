"""Tests for the subprocess-isolated skill sandbox.

Two classes of tests here:

1. **Runner-direct tests** (`test_runner_*`) shell out to
   ``python -m colony_sidecar.skills.sandbox_runner`` with a crafted
   stdin payload and assert on the JSON emitted to stdout. These pin
   the real process-boundary behaviour on Linux.

2. **Executor tests** (`test_executor_*`) construct a ``SkillExecutor``
   with mocked registry/guard and a tmpdir-resident skill, then assert
   on the ``ExecutionResult`` returned from ``invoke``. These exercise
   the full async path including the parent-side timeout and quarantine.

Tests that rely on Linux-only rlimit behaviour are skipped elsewhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run_sandbox(payload: dict, *, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Execute the runner as a subprocess and return the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "colony_sidecar.skills.sandbox_runner"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )


def _parse_last_json_line(stdout: bytes) -> dict:
    text = stdout.decode("utf-8", errors="replace").strip()
    assert text, "runner emitted empty stdout"
    return json.loads(text.splitlines()[-1])


# ── Runner-direct tests ───────────────────────────────────────────────────────


def test_runner_success_returns_output():
    result = _run_sandbox({
        "source": "def run(**kw): return {'ok': True, 'got': kw}",
        "inputs": {"a": 1},
        "limits": {"mem_mb": 128, "cpu_secs": 5, "fsize_mb": 2},
    })
    assert result.returncode == 0
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "success"
    assert parsed["output"] == {"ok": True, "got": {"a": 1}}


def test_runner_async_run_is_awaited():
    src = textwrap.dedent("""
        import asyncio
        async def run(**kw):
            await asyncio.sleep(0)
            return {"awaited": True}
    """)
    # The skill imports asyncio, so it must be declared in the manifest —
    # the sandbox's import allow-list applies to test skills too.
    result = _run_sandbox({
        "source": src, "inputs": {}, "limits": {},
        "allowed_imports": ["asyncio"],
    })
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "success"
    assert parsed["output"] == {"awaited": True}


def test_runner_exception_reports_failure_cleanly():
    result = _run_sandbox({
        "source": "def run(**kw): raise ValueError('boom')",
        "inputs": {},
    })
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "failed"
    assert "ValueError" in parsed["error"]
    assert "boom" in parsed["error"]


def test_runner_missing_run_function_fails_gracefully():
    result = _run_sandbox({
        "source": "x = 1",
        "inputs": {},
    })
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "failed"
    assert "run" in parsed["error"]


def test_runner_eval_is_stripped_from_builtins():
    """The runner strips eval/exec/open — even if the scanner missed them."""
    src = textwrap.dedent("""
        def run(**kw):
            return eval("1 + 1")
    """)
    result = _run_sandbox({"source": src, "inputs": {}})
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "failed"
    assert "NameError" in parsed["error"] or "eval" in parsed["error"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_NPROC is Linux-specific")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="RLIMIT_NPROC is bypassed for processes with CAP_SYS_RESOURCE (root)",
)
def test_runner_blocks_fork():
    src = textwrap.dedent("""
        import os
        def run(**kw):
            os.fork()
            return "should not reach"
    """)
    result = _run_sandbox({
        "source": src, "inputs": {}, "limits": {"mem_mb": 128, "cpu_secs": 5},
    })
    # Either the JSON payload reports failure, or the kernel killed the
    # runner before it could emit output (either is a clean block).
    parsed = _parse_last_json_line(result.stdout) if result.stdout.strip() else {}
    assert parsed.get("status") != "success"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is Linux-specific")
def test_runner_blocks_memory_balloon():
    src = textwrap.dedent("""
        def run(**kw):
            # Allocate well over the 64 MiB cap below.
            buf = bytearray(256 * 1024 * 1024)
            return len(buf)
    """)
    result = _run_sandbox(
        {"source": src, "inputs": {}, "limits": {"mem_mb": 64, "cpu_secs": 5}},
        timeout=15.0,
    )
    # The kernel may SIGKILL the runner before it emits output, or the
    # runner may catch MemoryError and report failure cleanly. Both are
    # acceptable outcomes — what matters is that the success path never
    # fires with a buffer this large.
    if result.stdout.strip():
        parsed = _parse_last_json_line(result.stdout)
        assert parsed["status"] == "failed"
    else:
        # No stdout → kernel kill. Verify the process exited nonzero.
        assert result.returncode != 0


def test_runner_invalid_stdin_reports_failure():
    result = subprocess.run(
        [sys.executable, "-m", "colony_sidecar.skills.sandbox_runner"],
        input=b"not json at all",
        capture_output=True,
        timeout=5.0,
    )
    parsed = _parse_last_json_line(result.stdout)
    assert parsed["status"] == "failed"
    assert "invalid" in parsed["error"].lower()


# ── Executor tests (full parent-side path) ────────────────────────────────────


class _FakeRegistry:
    """Minimal registry satisfying the SkillExecutor interface."""

    def __init__(self, manifest):
        self._manifest = manifest
        self.executions: list[tuple] = []
        self.quarantined: list[tuple] = []

    async def get(self, skill_id: str):
        return self._manifest if self._manifest.skill_id == skill_id else None

    async def record_execution(
        self, skill_id, execution_id, status, duration_ms, violations=None,
    ):
        self.executions.append((skill_id, execution_id, status, duration_ms, violations))

    async def quarantine(self, skill_id: str, reason: str):
        self.quarantined.append((skill_id, reason))


class _PassGuard:
    async def check(self, manifest, inputs):
        class _R:
            allowed = True
            reason = "ok"
            violations: list[str] = []
        return _R()


def _build_skill(tmp_path: Path, source: str, *, skill_id: str = "skill-test") -> "SkillManifest":
    """Write skill.py with a matching manifest and return the manifest."""
    from colony_sidecar.skills.models import (
        SkillManifest, SkillPermissions, SkillStatus,
    )
    from datetime import datetime, timezone

    skill_dir = tmp_path / skill_id
    skill_dir.mkdir()
    skill_path = skill_dir / "skill.py"
    source_bytes = source.encode("utf-8")
    skill_path.write_bytes(source_bytes)
    checksum = hashlib.sha256(source_bytes).hexdigest()

    return SkillManifest(
        skill_id=skill_id,
        name="test",
        version="1.0",
        description="",
        author_colony_id="col-x",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status=SkillStatus.ACTIVE,
        permissions=SkillPermissions(max_duration_secs=5, max_memory_mb=128),
        checksum_sha256=checksum,
        skill_dir=str(skill_dir),
    )


def _make_executor(manifest, sandbox_mode: str = "subprocess"):
    from colony_sidecar.skills.executor import SkillExecutor
    from colony_sidecar.skills.security.scanner import ASTScanner

    registry = _FakeRegistry(manifest)
    executor = SkillExecutor(
        registry=registry,
        guard=_PassGuard(),
        scanner=ASTScanner(),
        sandbox_mode=sandbox_mode,
    )
    return executor, registry


@pytest.mark.asyncio
async def test_executor_subprocess_happy_path(tmp_path):
    manifest = _build_skill(
        tmp_path,
        "async def run(**kw): return {'echoed': kw}",
    )
    executor, registry = _make_executor(manifest)

    result = await executor.invoke(manifest.skill_id, {"hello": "world"})

    assert result.status == "success"
    assert result.output == {"echoed": {"hello": "world"}}
    assert result.error is None
    assert registry.executions[0][2] == "success"


@pytest.mark.asyncio
async def test_executor_rejects_tampered_source(tmp_path):
    manifest = _build_skill(tmp_path, "async def run(**kw): return 1")

    # Mutate the file after manifest was built.
    skill_path = Path(manifest.skill_dir) / "skill.py"
    skill_path.write_text("async def run(**kw): return 2  # tampered")

    executor, registry = _make_executor(manifest)
    result = await executor.invoke(manifest.skill_id, {})

    assert result.status == "failed"
    assert "SecurityError" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only rlimit behaviour")
async def test_executor_subprocess_timeout_quarantines(tmp_path):
    """A while-True skill must timeout and trigger quarantine."""
    manifest = _build_skill(
        tmp_path,
        textwrap.dedent("""
            def run(**kw):
                while True:
                    pass
        """),
    )
    # Lower the timeout so the test is fast.
    manifest.permissions.max_duration_secs = 1
    executor, registry = _make_executor(manifest)

    result = await executor.invoke(manifest.skill_id, {})
    assert result.status == "timeout"
    assert registry.quarantined
    assert registry.quarantined[0][0] == manifest.skill_id


@pytest.mark.asyncio
async def test_executor_malformed_runner_output_reported_cleanly(tmp_path, monkeypatch):
    """If the runner exits without emitting valid JSON, surface a failure
    rather than raising in the executor."""
    manifest = _build_skill(tmp_path, "async def run(**kw): return 1")
    executor, registry = _make_executor(manifest)

    async def _fake_subprocess(source_bytes, inputs, m):
        # Directly return the malformed-output shape produced by the
        # executor's own parser.
        return {"status": "failed", "error": "runner_malformed_output: xyz",
                "peak_memory_kb": 0}

    monkeypatch.setattr(executor, "_run_subprocess", _fake_subprocess)
    result = await executor.invoke(manifest.skill_id, {})
    assert result.status == "failed"
    assert "malformed" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_inprocess_rollback_path(tmp_path):
    """With sandbox_mode=inprocess the old in-process code path runs."""
    manifest = _build_skill(
        tmp_path,
        "async def run(**kw): return {'path': 'inprocess', **kw}",
    )
    executor, registry = _make_executor(manifest, sandbox_mode="inprocess")
    result = await executor.invoke(manifest.skill_id, {"a": 1})
    assert result.status == "success"
    assert result.output == {"path": "inprocess", "a": 1}


@pytest.mark.asyncio
async def test_executor_populates_peak_memory_from_runner(tmp_path):
    """The subprocess path should report a non-None peak_memory_mb."""
    manifest = _build_skill(
        tmp_path,
        "async def run(**kw): return 'ok'",
    )
    executor, _ = _make_executor(manifest)
    result = await executor.invoke(manifest.skill_id, {})
    # Not every platform populates ru_maxrss usefully, but when it does
    # the executor should surface it.
    assert result.status == "success"
    if result.peak_memory_mb is not None:
        assert result.peak_memory_mb > 0


def test_sandbox_mode_env_var_resolution(monkeypatch):
    """COLONY_SKILL_SANDBOX overrides the platform default."""
    from colony_sidecar.skills.executor import _resolve_sandbox_mode

    monkeypatch.setenv("COLONY_SKILL_SANDBOX", "inprocess")
    assert _resolve_sandbox_mode() == "inprocess"

    monkeypatch.setenv("COLONY_SKILL_SANDBOX", "subprocess")
    assert _resolve_sandbox_mode() == "subprocess"

    monkeypatch.delenv("COLONY_SKILL_SANDBOX", raising=False)
    mode = _resolve_sandbox_mode()
    # Default depends on platform; must always resolve to one of the two.
    assert mode in ("subprocess", "inprocess")

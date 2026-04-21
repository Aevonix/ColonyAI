"""Standalone skill-execution runner.

Invoked as ``python -m colony_sidecar.skills.sandbox_runner``. Reads a
single JSON object from stdin describing the skill source, inputs, and
resource limits; executes the skill's ``run`` function inside a process
that has been hardened with ``resource.setrlimit``; emits a single JSON
object on stdout with the result.

Parent-side contract (``skills.executor.SkillExecutor``):
    in  (stdin) : {"source": str, "inputs": dict, "limits":
                   {"mem_mb": int, "cpu_secs": int, "fsize_mb": int}}
    out (stdout): {"status": "success"|"failed",
                   "output"?: Any, "error"?: str,
                   "peak_memory_kb": int}

All errors inside this runner are captured and reported on stdout — the
runner never prints a stack trace to stderr as part of its contract, and
exits 0 on normal completion so the parent can distinguish runner
failures from kernel kills.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional


def _fallback_repr(value: Any) -> Any:
    """Best-effort conversion to something JSON can encode."""
    try:
        json.dumps(value)
        return value
    except Exception:
        return repr(value)


def _apply_limits(mem_mb: int, cpu_secs: int, fsize_mb: int) -> None:
    """Apply process-level resource caps. Best-effort per platform."""
    import resource

    # Memory. Linux has RLIMIT_AS (total virtual memory); Darwin does not
    # enforce RLIMIT_AS reliably — fall back to RLIMIT_DATA on that
    # platform so heap allocations still hit a wall.
    mem_bytes = mem_mb * 1024 * 1024
    mem_set = False
    if sys.platform.startswith("linux"):
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            mem_set = True
        except (ValueError, OSError):
            pass
    if not mem_set:
        try:
            resource.setrlimit(resource.RLIMIT_DATA, (mem_bytes, mem_bytes))
        except (ValueError, OSError, AttributeError):
            pass

    # Wall-clock timeout is enforced by the parent via asyncio.wait_for;
    # RLIMIT_CPU is a belt-and-braces backstop in case the parent's kill
    # signal is delayed or ignored. A generous cushion avoids racing the
    # parent — the parent should always win under normal operation, and
    # this rlimit only fires if the parent's kill is itself blocked.
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU, (cpu_secs + 10, cpu_secs + 15),
        )
    except (ValueError, OSError):
        pass

    # Writable output size — stops `print(huge_thing)` from filling disks.
    fsize_bytes = fsize_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    except (ValueError, OSError):
        pass

    # No fork / exec. Blocks `os.fork()`, `os.system()`, subprocess.Popen,
    # multiprocessing — cuts off the broadest class of escape.
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    except (ValueError, OSError, AttributeError):
        pass

    # Cap file descriptors so a skill can't exhaust the process table.
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ValueError, OSError):
        pass


def _peak_memory_kb() -> int:
    """Return peak resident-set size in KB for the current process."""
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports ru_maxrss in KB; macOS in bytes. Normalize to KB.
        val = int(ru.ru_maxrss)
        if sys.platform == "darwin" and val > 10 * 1024 * 1024:
            val = val // 1024
        return val
    except Exception:
        return 0


async def _invoke(source: str, inputs: Dict[str, Any]) -> Any:
    """Compile + exec the skill in a minimal namespace and call run()."""
    # Minimal builtins. The scanner already rejects the loudest offenders
    # at upload time; stripping them again at runtime is belt-and-braces
    # in case a skill reached the runner through a non-standard path.
    #
    # ``__import__`` and ``__build_class__`` must stay reachable — Python's
    # ``import`` statement and ``class`` statement compile down to calls to
    # them, so without them even well-behaved skills can't run.
    import builtins as _bi

    _banned = {
        "eval", "exec", "compile", "open", "breakpoint", "input",
    }
    _safe_dunders = {"__import__", "__build_class__", "__name__", "__doc__"}

    safe_builtins: Dict[str, Any] = {}
    for name in dir(_bi):
        if name in _banned:
            continue
        if name.startswith("_") and name not in _safe_dunders:
            continue
        safe_builtins[name] = getattr(_bi, name)

    namespace: Dict[str, Any] = {"__builtins__": safe_builtins, "__name__": "__skill__"}
    code = compile(source, "<skill>", "exec")
    exec(code, namespace)  # noqa: S102

    run_fn = namespace.get("run")
    if run_fn is None:
        raise AttributeError("skill has no 'run' function")

    result = run_fn(**inputs)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except Exception as exc:
        _emit({
            "status": "failed",
            "error": f"runner: invalid stdin payload: {type(exc).__name__}",
            "peak_memory_kb": 0,
        })
        return 0

    source: str = request.get("source", "")
    inputs: Dict[str, Any] = request.get("inputs") or {}
    limits: Dict[str, Any] = request.get("limits") or {}

    mem_mb = int(limits.get("mem_mb") or 256)
    cpu_secs = int(limits.get("cpu_secs") or 60)
    fsize_mb = int(limits.get("fsize_mb") or 8)

    _apply_limits(mem_mb=mem_mb, cpu_secs=cpu_secs, fsize_mb=fsize_mb)

    # Close any inherited fds above the standard three. Best-effort —
    # a skill shouldn't have access to parent sockets or pipes.
    try:
        os.closerange(3, 64)
    except Exception:
        pass

    try:
        output = asyncio.run(_invoke(source, inputs))
        _emit({
            "status": "success",
            "output": _fallback_repr(output),
            "peak_memory_kb": _peak_memory_kb(),
        })
    except MemoryError:
        _emit({
            "status": "failed",
            "error": "memory_limit_exceeded",
            "peak_memory_kb": _peak_memory_kb(),
        })
    except Exception as exc:
        _emit({
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "peak_memory_kb": _peak_memory_kb(),
        })
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Exploration sandbox: gated, isolated execution (Phase B item 6).

Containment is enforced by the backend, not the caller; the manager adds the
mode gate, the boundary check, and approval tiering. These tests use a mock
backend so no Docker is required.
"""

from __future__ import annotations

import os

import pytest

from colony_sidecar.directives import Verdict
from colony_sidecar.sandbox import SandboxManager, resolve_limits
from colony_sidecar.sandbox.backend import (
    DisabledSandbox, DockerSandbox, SandboxLimits, SandboxResult,
)


class _FakeDirectives:
    def __init__(self, allowed: bool, reason: str = "ok") -> None:
        self._v = Verdict(allowed=allowed, reason=reason)

    def check(self, action):  # noqa: ARG002
        return self._v


class _MockBackend:
    name = "mock"

    def __init__(self) -> None:
        self.calls = []

    def available(self) -> bool:
        return True

    def run(self, script, lang, limits):
        self.calls.append((script, lang, limits))
        return SandboxResult(stdout="hi", exit_code=0)


# -- containment: the command the backend actually issues ----------------

def test_build_command_no_egress_no_creds_capped():
    cmd = DockerSandbox().build_command(
        "/wd", "python",
        SandboxLimits(image="img:1", cpus=2.0, memory="256m",
                      timeout_secs=15, pids_limit=64))
    # no egress
    assert cmd[cmd.index("--network") + 1] == "none"
    # no credentials injected
    assert "-e" not in cmd and "--env" not in cmd
    # hard caps passed through literally
    assert cmd[cmd.index("--cpus") + 1] == "2.0"
    assert cmd[cmd.index("--memory") + 1] == "256m"
    assert cmd[cmd.index("--pids-limit") + 1] == "64"
    assert "15" in cmd  # inner timeout
    assert "--read-only" in cmd and "--cap-drop" in cmd
    assert "img:1" in cmd


def test_allowlist_egress_is_not_silently_none():
    lim = SandboxLimits(egress="allowlist")
    assert lim.network == "allowlist"  # honored (requires a proxy, documented)


# -- mode gate ------------------------------------------------------------

def test_off_never_runs(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "off")
    mgr = SandboxManager()
    backend = _MockBackend()
    mgr._backend = backend
    out = mgr.run("print(1)", purpose="test", owner_directed=True)
    assert out["ran"] is False and out["reason"] == "sandbox_off"
    assert backend.calls == []


def test_dry_run_executes_nothing(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "dry_run")
    mgr = SandboxManager()
    backend = _MockBackend()
    mgr._backend = backend
    out = mgr.run("print(1)", purpose="test", owner_directed=True)
    assert out["ran"] is False and out["dry_run"] is True
    assert out["command"] and backend.calls == []


def test_live_runs_backend_and_records(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "live")

    class _SM:
        def __init__(self):
            self.recorded = []
            self.journal = None

        def record(self, domain, outcome, **kw):
            self.recorded.append((domain, outcome))

    sm = _SM()
    mgr = SandboxManager(self_model=sm)
    backend = _MockBackend()
    mgr._backend = backend
    out = mgr.run("print(1)", purpose="test", owner_directed=True)
    assert out["ran"] is True and out["outcome"] == "success"
    assert backend.calls and sm.recorded == [("sandbox", "success")]


# -- approval tiering -----------------------------------------------------

def test_owner_directed_is_auto(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "dry_run")
    mgr = SandboxManager()
    out = mgr.run("print(1)", purpose="p", owner_directed=True)
    assert out.get("tier") == "auto" and out["dry_run"] is True


def test_non_owner_directed_is_flagged_and_held(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "dry_run")
    mgr = SandboxManager()
    backend = _MockBackend()
    mgr._backend = backend
    out = mgr.run("print(1)", purpose="p", owner_directed=False)
    assert out["ran"] is False and out["reason"] == "approval_required"
    assert out["tier"] == "flagged" and backend.calls == []


def test_flagged_but_approved_proceeds(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "dry_run")
    mgr = SandboxManager()
    out = mgr.run("print(1)", purpose="p", owner_directed=False, approved=True)
    assert out.get("dry_run") is True


# -- boundary gate --------------------------------------------------------

def test_boundary_blocks_run(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "live")
    mgr = SandboxManager(directive_manager=_FakeDirectives(False, "leave prod alone"))
    backend = _MockBackend()
    mgr._backend = backend
    out = mgr.run("import prod", purpose="poke prod", owner_directed=True)
    assert out["ran"] is False and out["reason"] == "boundary_blocked"
    assert backend.calls == []


# -- server-side limits (caller cannot widen) ----------------------------

def test_limits_come_from_env_not_caller(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_CPUS", "0.5")
    monkeypatch.setenv("COLONY_SANDBOX_MEMORY", "128m")
    monkeypatch.setenv("COLONY_SANDBOX_TIMEOUT", "5")
    lim = resolve_limits()
    assert lim.cpus == 0.5 and lim.memory == "128m" and lim.timeout_secs == 5
    # run() takes no limits argument at all -> the caller has no lever.
    import inspect
    assert "limits" not in inspect.signature(SandboxManager.run).parameters


# -- artifact size cap ----------------------------------------------------

def test_artifact_size_cap(tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    script = workdir / "script"
    script.write_text("noop")
    big = workdir / "out.bin"
    big.write_bytes(b"x" * 5000)
    result = SandboxResult()
    DockerSandbox._read_artifacts(
        str(workdir), str(script),
        SandboxLimits(max_artifact_bytes=1000), result)
    total = sum(len(v) for v in result.artifacts.values())
    assert total <= 1000
    assert "cap" in result.error


# -- disabled backend -----------------------------------------------------

def test_disabled_backend_reports_unavailable(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "live")
    mgr = SandboxManager()
    mgr._backend = DisabledSandbox("no docker")
    out = mgr.run("print(1)", purpose="p", owner_directed=True)
    assert out["ran"] is False and out["reason"] == "backend_unavailable"


def test_status_shape(monkeypatch):
    monkeypatch.setenv("COLONY_SANDBOX_MODE", "dry_run")
    st = SandboxManager().status()
    assert st["mode"] == "dry_run" and "backend" in st and "limits" in st

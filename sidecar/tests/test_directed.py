"""Tests for directed action (option A): intake, gates, dry-run dispatch, audit."""

from __future__ import annotations

import asyncio

from colony_sidecar.directed import (
    ScopedTask, ScopedTaskStore, ScopeLimits, scope_from_directive,
    audit_via_report, audit_completion, DirectedActionService,
)
from colony_sidecar.directives import DirectiveManager, DirectiveStore


_KNOWN = [{"kind": "repo", "name": "widget-api", "aliases": "the widget repo"},
          {"kind": "repo", "name": "billing-svc", "aliases": ""}]


# ---------------------------------------------------------------------------
# Intake (deterministic scoping)
# ---------------------------------------------------------------------------

def test_intake_read_only_scope():
    t = scope_from_directive("look at the widget repo and summarize recent changes", _KNOWN)
    assert t.targets and t.targets[0]["name"] == "widget-api"   # alias resolved
    assert set(t.allowed_ops) == {"analyze", "read", "search"}
    assert t.mutating is False


def test_intake_mutating_scope_with_commit_cap():
    t = scope_from_directive("fix the retry bug in billing-svc, at most 2 commits", _KNOWN)
    assert t.targets[0]["name"] == "billing-svc"
    assert t.mutating is True
    assert "modify_files" in t.allowed_ops and "push_branch" in t.allowed_ops
    assert t.limits.max_commits == 2                             # parsed cap
    assert t.limits.force_push is False


def test_intake_unknown_target_resolves_empty():
    t = scope_from_directive("review the mystery-repo code", _KNOWN)
    assert t.targets == []          # never fuzzy-matches an unknown repo


def test_intake_is_deterministic():
    a = scope_from_directive("audit widget-api", _KNOWN)
    b = scope_from_directive("audit widget-api", _KNOWN)
    assert a.allowed_ops == b.allowed_ops and a.targets == b.targets


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def _service(dm=None, feedback=None, deliver=None, mirrors=None):
    return DirectedActionService(
        store=ScopedTaskStore(db_path=None), directive_manager=dm,
        mirrors=mirrors, feedback_store=feedback, delivery_router=deliver)


def test_gate_boundary_refuses_with_citation():
    dm = DirectiveManager(DirectiveStore(db_path=None))
    dm.capture_from_message("don't touch the billing-svc repo")
    svc = _service(dm=dm)
    async def run():
        t = await svc.intake("fix the retry bug in billing-svc")
        assert t.status == "refused"
        assert "billing-svc" in t.refusal_reason      # citation included
    asyncio.run(run())


def test_gate_read_only_auto_approves():
    svc = _service()
    async def run():
        t = await svc.intake("summarize recent changes")
        assert t.status == "approved"
        assert t.approval.get("required") is False
    asyncio.run(run())


def test_gate_mutating_requires_approval_then_approve():
    svc = _service()
    async def run():
        t = await svc.intake("refactor the parser module")
        assert t.status == "awaiting_approval"
        assert t.approval.get("required") is True
        got = svc.approve(t.id, approved_by="owner")
        assert got.status == "approved"
        assert got.approval.get("granted_by") == "owner"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Dispatch (dry-run)
# ---------------------------------------------------------------------------

def test_dispatch_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    svc = _service()
    async def run():
        t = await svc.intake("analyze the code")
        out = await svc.dispatch(t.id)
        assert out.get("dry_run") is True
        assert out.get("dispatched") is False
        assert out["payload"]["type"] == "directed_task"
        assert out["payload"]["task"]["id"] == t.id
        assert svc.store.get(t.id).status == "dispatched_dry"
    asyncio.run(run())


def test_dispatch_refuses_unapproved(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    svc = _service()
    async def run():
        t = await svc.intake("refactor everything")   # mutating -> awaiting
        out = await svc.dispatch(t.id)
        assert out["dispatched"] is False
        assert "not_approved" in out["reason"]
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Audit (post-action verification)
# ---------------------------------------------------------------------------

def _mtask(**kw):
    return ScopedTask(
        directive_text="fix bug", objective="fix bug",
        allowed_ops=["analyze", "read", "modify_files", "commit", "push_branch"],
        limits=ScopeLimits(branch_prefix="colony/", max_commits=3,
                           path_globs=["src/**"]),
        **kw)


def test_audit_clean_report():
    t = _mtask()
    a = audit_via_report(t, {
        "summary": "fixed", "operations": ["read", "modify_files", "commit"],
        "files_touched": ["src/x.py"], "commits": 2, "branch": "colony/fix-bug"})
    assert a["ok"] is True and a["findings"] == []


def test_audit_flags_out_of_scope():
    t = _mtask()
    a = audit_via_report(t, {
        "summary": "did stuff", "operations": ["modify_files", "open_pr"],   # open_pr not granted
        "files_touched": ["src/x.py", "infra/deploy.sh"],                    # outside globs
        "commits": 7,                                                        # over cap
        "branch": "main",                                                    # wrong branch
        "force_push": True})
    assert a["ok"] is False
    joined = " | ".join(a["findings"])
    assert "open_pr" in joined and "deploy.sh" in joined
    assert "exceeds cap" in joined and "prefix" in joined and "force push" in joined


def test_audit_mutation_on_readonly_scope():
    t = ScopedTask(directive_text="just look", allowed_ops=["analyze", "read"])
    a = audit_via_report(t, {"summary": "oops", "operations": ["commit"],
                             "commits": 1, "branch": "colony/x",
                             "files_touched": []})
    assert a["ok"] is False
    assert any("read-only scope" in f for f in a["findings"])


def test_complete_records_outcome_and_notifies():
    calls = {}
    class FB:
        def record(self, itype, outcome): calls["fb"] = (itype, outcome)
    delivered = []
    async def deliver(payload):
        delivered.append(payload); return False   # shadow-held
    svc = _service(feedback=FB(), deliver=deliver)
    async def run():
        t = await svc.intake("summarize the module")
        t.status = "dispatched"; svc.store.save(t)   # strict reports (H7.2)
        out = await svc.complete(t.id, {
            "summary": "3 modules reviewed", "operations": ["read"],
            "files_touched": [], "commits": 0, "branch": ""})
        assert out["verdict"] == "clean"
        assert svc.store.get(t.id).status == "completed"
        assert calls["fb"] == ("directed_action", "actioned")
        assert delivered and delivered[0]["type"] == "proposal"
    asyncio.run(run())


def test_complete_violation_flags_loud():
    class FB:
        def __init__(self): self.rec = None
        def record(self, itype, outcome): self.rec = (itype, outcome)
    fb = FB()
    svc = _service(feedback=fb)
    async def run():
        t = await svc.intake("summarize the module")   # read-only scope
        t.status = "dispatched"; svc.store.save(t)     # strict reports (H7.2)
        out = await svc.complete(t.id, {
            "summary": "i changed things", "operations": ["modify_files", "commit"],
            "files_touched": ["a.py"], "commits": 2, "branch": "main"})
        assert out["verdict"] == "violation"
        assert svc.store.get(t.id).status == "violated"
        assert fb.rec == ("directed_action", "dismissed")
    asyncio.run(run())

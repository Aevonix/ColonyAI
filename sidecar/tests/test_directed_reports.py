"""Report-back hardening (H7.2): strict status gating on directed reports."""

from __future__ import annotations

import asyncio

from colony_sidecar.directed import DirectedActionService, ScopedTaskStore
from colony_sidecar.directed.service import report_token_for
from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine,
)

_READ_REPORT = {"summary": "reviewed", "operations": ["analyze", "read"],
                "files_touched": [], "commits": 0, "branch": ""}


def _self_model():
    store = CompetenceStore()
    journal = ActionJournal()
    return SelfModel(store, trust=TrustEngine(store, journal=journal),
                     journal=journal)


def _service(sm=None):
    return DirectedActionService(store=ScopedTaskStore(db_path=None),
                                 self_model=sm)


def _run(coro):
    return asyncio.run(coro)


def test_strict_rejects_report_before_dispatch():
    """The hole this unit closes: a report on a never-dispatched task."""
    sm = _self_model()
    svc = _service(sm)

    async def run():
        t = await svc.intake("summarize the module")   # approved, NOT dispatched
        out = await svc.complete(t.id, dict(_READ_REPORT))
        assert out["ok"] is False
        assert out["reason"] == "status_mismatch"
        # nothing recorded, nothing mutated
        assert svc.store.get(t.id).status == "approved"
        assert sm.store.events("directed:read", include_shadow=False) == []
        # the rejection is journaled loudly
        entries = sm.journal.recent(domain="directed:read")
        assert any(e["decision"] == "blocked" for e in entries)
    _run(run())


def test_strict_accepts_dispatched_and_rejects_double_report():
    sm = _self_model()
    svc = _service(sm)

    async def run():
        t = await svc.intake("summarize the module")
        t.status = "dispatched"; svc.store.save(t)
        first = await svc.complete(t.id, dict(_READ_REPORT))
        assert first["ok"] is True and first["verdict"] == "clean"
        assert sm.store.events("directed:read", include_shadow=False)
        second = await svc.complete(t.id, dict(_READ_REPORT))
        assert second["ok"] is False
        assert second["reason"] == "double_report"
        # the double report added no second trust event
        assert len(sm.store.events("directed:read", include_shadow=False)) == 1
    _run(run())


def test_dry_echo_records_no_trust_outcome(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    sm = _self_model()
    svc = _service(sm)

    async def run():
        t = await svc.intake("summarize the module")
        await svc.dispatch(t.id)                       # -> dispatched_dry
        out = await svc.complete(t.id, dict(_READ_REPORT))
        assert out["ok"] is True and out.get("dry_echo") is True
        assert sm.store.events("directed:read", include_shadow=False) == []
    _run(run())


def test_strict_verifies_report_token_when_key_set(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_HMAC_KEY", "test-secret")
    svc = _service()

    async def run():
        t = await svc.intake("summarize the module")
        t.status = "dispatched"; svc.store.save(t)
        bad = await svc.complete(t.id, dict(_READ_REPORT))
        assert bad["ok"] is False and bad["reason"] == "bad_report_token"
        good = await svc.complete(
            t.id, {**_READ_REPORT, "report_token": report_token_for(t.id)})
        assert good["ok"] is True
    _run(run())


def test_legacy_flag_off_accepts_any_status(monkeypatch):
    """Flag-off regression lock: =0 restores accept-in-any-status."""
    monkeypatch.setenv("COLONY_DIRECTED_STRICT_REPORTS", "0")
    sm = _self_model()
    svc = _service(sm)

    async def run():
        t = await svc.intake("summarize the module")   # approved only
        out = await svc.complete(t.id, dict(_READ_REPORT))
        assert out["ok"] is True and out["verdict"] == "clean"
        assert svc.store.get(t.id).status == "completed"
        assert sm.store.events("directed:read", include_shadow=False)
    _run(run())

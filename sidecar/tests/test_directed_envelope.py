"""Directed dispatch envelope: HMAC signing + daily dispatch cap (H7.1)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

from colony_sidecar.directed import DirectedActionService, ScopedTaskStore
from colony_sidecar.directed.service import report_token_for


def _service():
    return DirectedActionService(store=ScopedTaskStore(db_path=None))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def test_envelope_unsigned_by_default(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    monkeypatch.delenv("COLONY_DIRECTED_HMAC_KEY", raising=False)
    svc = _service()

    async def run():
        t = await svc.intake("analyze the code")
        out = await svc.dispatch(t.id)
        env = out["payload"]["envelope"]
        assert env["task_id"] == t.id
        assert env["nonce"] and env["issued_at"]
        # no key -> no signature, no report token (legacy contract)
        assert "signature" not in env
        assert "report_token" not in env
        assert report_token_for(t.id) == ""
    _run(run())


def test_envelope_signed_when_key_set(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    monkeypatch.setenv("COLONY_DIRECTED_HMAC_KEY", "test-secret")
    svc = _service()

    async def run():
        t = await svc.intake("analyze the code")
        out = await svc.dispatch(t.id)
        env = out["payload"]["envelope"]
        assert env["algo"] == "hmac-sha256"
        expected = hmac.new(
            b"test-secret",
            f"{t.id}.{env['issued_at']}.{env['nonce']}".encode(),
            hashlib.sha256).hexdigest()
        assert env["signature"] == expected
        assert env["report_token"] == report_token_for(t.id)
        assert env["report_token"]           # non-empty when a key is set
    _run(run())


# ---------------------------------------------------------------------------
# Daily dispatch cap
# ---------------------------------------------------------------------------

def test_dispatch_cap_binds(monkeypatch):
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    monkeypatch.setenv("COLONY_DIRECTED_MAX_DISPATCH_PER_DAY", "2")
    svc = _service()

    async def run():
        ids = []
        for _ in range(3):
            t = await svc.intake("analyze the code")
            ids.append(t.id)
        assert (await svc.dispatch(ids[0])).get("dry_run") is True
        assert (await svc.dispatch(ids[1])).get("dry_run") is True
        out = await svc.dispatch(ids[2])
        assert out["dispatched"] is False
        assert out["reason"] == "daily_dispatch_cap"
        # the capped task stays approved (retryable), never failed
        assert svc.store.get(ids[2]).status == "approved"
    _run(run())


def test_dispatch_cap_zero_disables(monkeypatch):
    """Flag-off regression lock: cap<=0 restores the uncapped legacy path."""
    monkeypatch.setenv("COLONY_DIRECTED_MODE", "dry_run")
    monkeypatch.setenv("COLONY_DIRECTED_MAX_DISPATCH_PER_DAY", "0")
    svc = _service()

    async def run():
        for _ in range(12):
            t = await svc.intake("analyze the code")
            out = await svc.dispatch(t.id)
            assert out.get("dry_run") is True
    _run(run())

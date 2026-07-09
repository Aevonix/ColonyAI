"""U22 — boundary (DirectiveGuard) checks fail CLOSED on error.

An owner boundary is a prohibition; if the check itself raises we must not
assume the action is permitted. COLONY_BOUNDARY_FAIL_CLOSED defaults to
true: an exception inside the boundary check refuses the delivery /
directed intake with reason boundary_check_error, logs a WARNING, and
increments a counter. Setting the flag to false restores the legacy
allow-on-error behavior. The ResponseGuard delivery call (a content-quality
gate, not an owner boundary) intentionally stays fail-open and is NOT
covered here.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.directed import DirectedActionService, ScopedTaskStore
from colony_sidecar.directives.guard import boundary_fail_closed


class _ExplodingDirectives:
    """A directive manager whose boundary check always raises."""

    def check(self, action):
        raise RuntimeError("boundary store unavailable")


class _FakeDelivery:
    _rate_limiter = None

    def __init__(self):
        self.pushed = []

    def preview_initiative(self, payload):
        return {"person_id": "cid-owner-xyz", "urgency": 0.7,
                "channel_hint": "dm",
                "target": {"user_chat": "whatsapp:home-chat-1"}}

    async def push_initiative(self, payload):
        self.pushed.append(payload)
        return True


def _payload():
    return {
        "id": "prop-9", "type": "proposal", "priority": 0.7,
        "title": "A useful finding", "description": "A useful finding.",
        "rationale": "", "suggested_action": "", "entity_id": None,
        "entity_type": "proposal", "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }


def _loop(directives):
    cfg = AutonomyConfig()
    cfg.proactive_delivery_enabled = True
    cfg.delivery_shadow_mode = False
    return AutonomyLoop(registry=SimpleNamespace(directives=directives), config=cfg)


def _service(dm):
    return DirectedActionService(
        store=ScopedTaskStore(db_path=None), directive_manager=dm)


# ---------------------------------------------------------------------------
# Flag semantics
# ---------------------------------------------------------------------------

def test_flag_defaults_to_fail_closed(monkeypatch):
    monkeypatch.delenv("COLONY_BOUNDARY_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)
    assert boundary_fail_closed() is True


def test_flag_false_restores_legacy(monkeypatch):
    monkeypatch.setenv("COLONY_BOUNDARY_FAIL_CLOSED", "false")
    assert boundary_fail_closed() is False


# ---------------------------------------------------------------------------
# Delivery path (autonomy/loop.py _route_reachout_delivery)
# ---------------------------------------------------------------------------

def test_delivery_boundary_error_refuses_by_default(monkeypatch, caplog):
    monkeypatch.delenv("COLONY_BOUNDARY_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")

    loop = _loop(_ExplodingDirectives())
    delivery = _FakeDelivery()
    with caplog.at_level(logging.WARNING, logger="colony_sidecar.autonomy.loop"):
        ok = asyncio.run(loop._route_reachout_delivery(_payload(), delivery))

    assert ok is False
    assert delivery.pushed == []                       # nothing left the machine
    assert loop.stats.boundary_check_errors == 1
    assert loop.stats.as_dict()["boundary_check_errors"] == 1
    assert any("boundary_check_error" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)


def test_delivery_boundary_error_allows_when_flag_false(monkeypatch):
    monkeypatch.setenv("COLONY_BOUNDARY_FAIL_CLOSED", "false")
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")

    loop = _loop(_ExplodingDirectives())
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(_payload(), delivery))

    assert ok is True                                  # legacy allow-on-error
    assert len(delivery.pushed) == 1
    assert loop.stats.boundary_check_errors == 1       # still counted


def test_delivery_healthy_boundary_path_unchanged(monkeypatch):
    """Regression lock: no directives wired -> delivery proceeds as before."""
    monkeypatch.delenv("COLONY_BOUNDARY_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")

    loop = _loop(None)
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(_payload(), delivery))

    assert ok is True
    assert loop.stats.boundary_check_errors == 0


# ---------------------------------------------------------------------------
# Directed intake (directed/service.py intake gate 1)
# ---------------------------------------------------------------------------

def test_directed_boundary_error_refuses_by_default(monkeypatch, caplog):
    monkeypatch.delenv("COLONY_BOUNDARY_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)

    svc = _service(_ExplodingDirectives())
    with caplog.at_level(logging.WARNING, logger="colony_sidecar.directed.service"):
        task = asyncio.run(svc.intake("summarize recent changes"))

    assert task.status == "refused"
    assert task.refusal_reason == "boundary_check_error"
    assert svc.boundary_check_errors == 1
    # Refused task is persisted with the refusal.
    assert svc.store.get(task.id).status == "refused"
    assert any("boundary_check_error" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)


def test_directed_boundary_error_allows_when_flag_false(monkeypatch):
    monkeypatch.setenv("COLONY_BOUNDARY_FAIL_CLOSED", "false")

    svc = _service(_ExplodingDirectives())
    task = asyncio.run(svc.intake("summarize recent changes"))

    # Legacy allow-on-error: intake proceeds to gate 2 (read-only auto-approve).
    assert task.status == "approved"
    assert svc.boundary_check_errors == 1              # still counted


def test_directed_healthy_boundary_path_unchanged(monkeypatch):
    """Regression lock: no directive manager -> intake proceeds as before."""
    monkeypatch.delenv("COLONY_BOUNDARY_FAIL_CLOSED", raising=False)

    svc = _service(None)
    task = asyncio.run(svc.intake("summarize recent changes"))

    assert task.status == "approved"
    assert svc.boundary_check_errors == 0

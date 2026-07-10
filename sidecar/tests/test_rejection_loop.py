"""RejectionFeedbackLoop (H6.2): ResponseGuard-native regenerate-on-block.

Error contract under test:
  * a guard BLOCK is never overridden by a loop-internal error (closed on
    the block side);
  * only a revision the guard itself clears may ship.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.gate.rejection import (
    FeedbackLoopResult, GateRejectionEvent, RejectionFeedbackLoop,
    RejectionStore,
)
from colony_sidecar.gate.response_guard import GuardFinding, GuardResult


def _blocked(reason="secret_leak", excerpt="sk-123"):
    return GuardResult(decision="revise", mode="enforce", findings=[
        GuardFinding(check=reason, severity="block", reason=reason,
                     excerpt=excerpt)])


def _allowed():
    return GuardResult(decision="allow", mode="enforce", findings=[])


class FakeGuard:
    """Blocks any text containing SECRET; allows everything else."""

    def __init__(self, raise_on_reeval=False):
        self.calls = 0
        self._raise = raise_on_reeval

    async def evaluate(self, *, response_text="", **kw):
        self.calls += 1
        if self._raise:
            raise RuntimeError("guard backend down")
        return _blocked() if "SECRET" in response_text else _allowed()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Loop unit behavior
# ---------------------------------------------------------------------------

def test_allow_passes_untouched():
    loop = RejectionFeedbackLoop(FakeGuard(), store=RejectionStore())

    async def run():
        res = await loop.run("hello", initial_result=_allowed())
        assert res.passed is True and res.payload == "hello"
    _run(run())


def test_regenerates_once_and_passes():
    store = RejectionStore()

    async def regen(prompt_fragment, blocked_text):
        assert "secret_leak" in prompt_fragment
        return "a clean revision"

    loop = RejectionFeedbackLoop(FakeGuard(), store=store, regenerate=regen)

    async def run():
        res = await loop.run("here is a SECRET", initial_result=_blocked(),
                             turn_id="t1", target_contact_id="c1")
        assert res.passed is True
        assert res.payload == "a clean revision"
        rows = store.recent()
        assert rows and rows[0]["eventually_succeeded"] == 1
        assert rows[0]["block_reason"] == "secret_leak"
    _run(run())


def test_no_regenerator_block_stands():
    store = RejectionStore()
    loop = RejectionFeedbackLoop(FakeGuard(), store=store)

    async def run():
        res = await loop.run("here is a SECRET", initial_result=_blocked())
        assert res.passed is False and res.payload is None
        assert store.recent()[0]["eventually_succeeded"] == 0
    _run(run())


def test_regenerator_error_never_overrides_block():
    async def regen(prompt_fragment, blocked_text):
        raise RuntimeError("llm down")

    loop = RejectionFeedbackLoop(FakeGuard(), regenerate=regen)

    async def run():
        res = await loop.run("here is a SECRET", initial_result=_blocked())
        assert res.passed is False and res.payload is None
    _run(run())


def test_reeval_error_block_stands():
    async def regen(prompt_fragment, blocked_text):
        return "a clean revision"

    loop = RejectionFeedbackLoop(FakeGuard(raise_on_reeval=True),
                                 regenerate=regen)

    async def run():
        res = await loop.run("here is a SECRET", initial_result=_blocked())
        assert res.passed is False and res.payload is None
    _run(run())


# ---------------------------------------------------------------------------
# Wiring: proactive enforce branch of _route_reachout_delivery
# ---------------------------------------------------------------------------

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


def _payload(text):
    return {
        "id": "prop-1", "type": "proposal", "priority": 0.7,
        "title": "Finding", "description": text,
        "rationale": "", "suggested_action": "", "entity_id": None,
        "entity_type": "proposal", "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }


def _loop(llm=None):
    cfg = AutonomyConfig()
    cfg.proactive_delivery_enabled = True
    cfg.delivery_shadow_mode = False
    return AutonomyLoop(
        registry=SimpleNamespace(directives=None, llm_router=llm), config=cfg)


def _wire_guard(monkeypatch, tmp_path, guard):
    import colony_sidecar.api.routers.host as host
    monkeypatch.setattr(host, "_response_guard", guard)
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)


def test_delivery_enforce_block_revised_and_sent(monkeypatch, tmp_path):
    class LLM:
        async def complete(self, messages, **kw):
            return SimpleNamespace(content="a clean revision")

    _wire_guard(monkeypatch, tmp_path, FakeGuard())
    loop = _loop(llm=LLM())
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("contains a SECRET"), delivery))
    assert ok is True
    assert delivery.pushed[0]["description"] == "a clean revision"


def test_delivery_block_stands_when_regeneration_fails(monkeypatch, tmp_path):
    class LLM:
        async def complete(self, messages, **kw):
            raise RuntimeError("llm down")

    _wire_guard(monkeypatch, tmp_path, FakeGuard())
    loop = _loop(llm=LLM())
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("contains a SECRET"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_delivery_block_stands_without_llm(monkeypatch, tmp_path):
    _wire_guard(monkeypatch, tmp_path, FakeGuard())
    loop = _loop(llm=None)
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("contains a SECRET"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_delivery_clean_message_unchanged(monkeypatch, tmp_path):
    """Regression lock: an allowed message ships exactly as before."""
    _wire_guard(monkeypatch, tmp_path, FakeGuard())
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a perfectly clean update"), delivery))
    assert ok is True
    assert delivery.pushed[0]["description"] == "a perfectly clean update"

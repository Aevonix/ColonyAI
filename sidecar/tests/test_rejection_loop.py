"""RejectionFeedbackLoop (H6.2): ResponseGuard-native regenerate-on-block.

Error contract under test:
  * a guard BLOCK is never overridden by a loop-internal error (closed on
    the block side);
  * only a revision the guard itself clears may ship.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.gate.rejection import (
    FeedbackLoopResult, GateRejectionEvent, RejectionFeedbackLoop,
    RejectionStore,
)
from colony_sidecar.gate.response_guard import (
    GuardFinding,
    GuardMode,
    GuardResult,
    response_text_digest,
)
from colony_sidecar.gate.surface_policy import POLICY_DIGEST, POLICY_ID


def _blocked(reason="secret_leak", excerpt="sk-123", response_text=""):
    return GuardResult(
        decision="revise",
        mode="enforce",
        findings=[GuardFinding(
            check=reason, severity="block", reason=reason, excerpt=excerpt,
        )],
        surface="proactive_text",
        surface_family="text",
        applicability="guarded",
        guard_status="evaluated",
        policy_id=POLICY_ID,
        policy_digest=POLICY_DIGEST,
        candidate_digest=response_text_digest(response_text),
    )


def _allowed(response_text=""):
    return GuardResult(
        decision="allow",
        mode="enforce",
        findings=[],
        surface="proactive_text",
        surface_family="text",
        applicability="guarded",
        guard_status="evaluated",
        policy_id=POLICY_ID,
        policy_digest=POLICY_DIGEST,
        candidate_digest=response_text_digest(response_text),
    )


class FakeGuard:
    """Blocks any text containing SECRET; allows everything else."""

    configured_mode = GuardMode.ENFORCE

    def __init__(self, raise_on_reeval=False):
        self.calls = 0
        self._raise = raise_on_reeval

    async def evaluate(self, *, response_text="", **kw):
        self.calls += 1
        if self._raise:
            raise RuntimeError("guard backend down")
        return (
            _blocked(response_text=response_text)
            if "SECRET" in response_text
            else _allowed(response_text)
        )


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


def test_delivery_enforce_rejects_unbound_feedback_revision(monkeypatch, tmp_path):
    class LLM:
        async def complete(self, messages, **kw):
            return SimpleNamespace(content="a clean revision")

    class UnboundRevisionGuard:
        configured_mode = GuardMode.ENFORCE

        def __init__(self):
            self.calls = 0

        async def evaluate(self, *, response_text="", **_kwargs):
            self.calls += 1
            if "SECRET" in response_text:
                return _blocked(response_text=response_text)
            result = _allowed(response_text)
            result.candidate_digest = "0" * 64
            return result

    guard = UnboundRevisionGuard()
    _wire_guard(monkeypatch, tmp_path, guard)
    loop = _loop(llm=LLM())
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("contains a SECRET"), delivery))
    assert ok is False
    assert delivery.pushed == []
    assert guard.calls >= 3


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
    """A canonical enforce ALLOW bound to the exact candidate may ship."""
    _wire_guard(monkeypatch, tmp_path, FakeGuard())
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a perfectly clean update"), delivery))
    assert ok is True
    assert delivery.pushed[0]["description"] == "a perfectly clean update"


def test_delivery_enforce_snapshots_stable_allow_fields_once(
    monkeypatch, tmp_path,
):
    """A stable exact ALLOW ships, with no raw-verdict TOCTOU rereads."""
    authority_fields = {
        "decision", "mode", "surface", "surface_family", "applicability",
        "guard_status", "policy_id", "policy_digest", "candidate_digest",
        "blocked", "findings",
    }

    class CountingAllow:
        def __init__(self, response_text):
            object.__setattr__(self, "reads", {name: 0 for name in authority_fields})
            object.__setattr__(self, "values", {
                "decision": "allow",
                "mode": "enforce",
                "surface": "proactive_text",
                "surface_family": "text",
                "applicability": "guarded",
                "guard_status": "evaluated",
                "policy_id": POLICY_ID,
                "policy_digest": POLICY_DIGEST,
                "candidate_digest": response_text_digest(response_text),
                "blocked": False,
                "findings": [],
            })

        def __getattribute__(self, name):
            if name in authority_fields:
                reads = object.__getattribute__(self, "reads")
                reads[name] += 1
                return object.__getattribute__(self, "values")[name]
            return object.__getattribute__(self, name)

    class CountingGuard:
        configured_mode = GuardMode.ENFORCE

        def __init__(self):
            self.result = None

        async def evaluate(self, *, response_text="", **_kwargs):
            self.result = CountingAllow(response_text)
            return self.result

    guard = CountingGuard()
    _wire_guard(monkeypatch, tmp_path, guard)
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a stable candidate"), delivery))
    assert ok is True
    assert len(delivery.pushed) == 1
    assert guard.result.reads == {name: 1 for name in authority_fields}


def test_delivery_enforce_rejects_stateful_initial_decision(
    monkeypatch, tmp_path,
):
    """A REVISE that flips to ALLOW after validation cannot ship."""
    class StatefulInitialVerdict:
        mode = "enforce"
        surface = "proactive_text"
        surface_family = "text"
        applicability = "guarded"
        guard_status = "evaluated"
        policy_id = POLICY_ID
        policy_digest = POLICY_DIGEST
        findings = []
        blocked = True

        def __init__(self, response_text):
            self.candidate_digest = response_text_digest(response_text)
            self.decision_reads = 0

        @property
        def decision(self):
            self.decision_reads += 1
            return "revise" if self.decision_reads == 1 else "allow"

    class StatefulInitialGuard:
        configured_mode = GuardMode.ENFORCE

        def __init__(self):
            self.result = None

        async def evaluate(self, *, response_text="", **_kwargs):
            self.result = StatefulInitialVerdict(response_text)
            return self.result

    guard = StatefulInitialGuard()
    _wire_guard(monkeypatch, tmp_path, guard)
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("must not escape"), delivery))
    assert ok is False
    assert delivery.pushed == []
    assert guard.result.decision_reads == 1


def test_delivery_enforce_rejects_stateful_revised_decision(
    monkeypatch, tmp_path,
):
    """The final revision authority cannot flip after validation."""
    class LLM:
        async def complete(self, messages, **kw):
            return SimpleNamespace(content="a clean revision")

    class StatefulRevisedVerdict:
        mode = "enforce"
        surface = "proactive_text"
        surface_family = "text"
        applicability = "guarded"
        guard_status = "evaluated"
        policy_id = POLICY_ID
        policy_digest = POLICY_DIGEST
        findings = []
        blocked = True

        def __init__(self, response_text):
            self.candidate_digest = response_text_digest(response_text)
            self.decision_reads = 0

        @property
        def decision(self):
            self.decision_reads += 1
            return "revise" if self.decision_reads == 1 else "allow"

    class StatefulRevisedGuard:
        configured_mode = GuardMode.ENFORCE

        def __init__(self):
            self.calls = 0
            self.final_result = None

        async def evaluate(self, *, response_text="", **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _blocked(response_text=response_text)
            if self.calls == 2:
                return _allowed(response_text)
            self.final_result = StatefulRevisedVerdict(response_text)
            return self.final_result

    guard = StatefulRevisedGuard()
    _wire_guard(monkeypatch, tmp_path, guard)
    loop = _loop(llm=LLM())
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("contains a SECRET"), delivery))
    assert ok is False
    assert delivery.pushed == []
    assert guard.final_result is not None
    assert guard.final_result.decision_reads == 1


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("decision", "nonsense"),
        ("policy_id", "wrong-policy"),
        ("policy_digest", "0" * 64),
        ("candidate_digest", "f" * 64),
        ("mode", "shadow"),
        ("surface_family", "artifact"),
        ("applicability", "excluded"),
        ("surface", "text_chat"),
        ("guard_status", "bypassed"),
    ],
)
def test_delivery_enforce_rejects_malformed_or_unbound_verdicts(
    monkeypatch, tmp_path, field_name, bad_value,
):
    class MalformedGuard:
        configured_mode = GuardMode.ENFORCE

        async def evaluate(self, *, response_text="", **_kwargs):
            result = _allowed(response_text)
            setattr(result, field_name, bad_value)
            return result

    _wire_guard(monkeypatch, tmp_path, MalformedGuard())
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_delivery_enforce_rejects_verdict_with_missing_bindings(
    monkeypatch, tmp_path,
):
    class IncompleteGuard:
        configured_mode = GuardMode.ENFORCE

        async def evaluate(self, **_kwargs):
            return SimpleNamespace(
                decision="allow",
                mode="enforce",
                surface="proactive_text",
                guard_status="evaluated",
                blocked=False,
            )

    _wire_guard(monkeypatch, tmp_path, IncompleteGuard())
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_delivery_enforce_blocks_when_guard_is_unavailable(monkeypatch, tmp_path):
    import colony_sidecar.api.routers.host as host

    _wire_guard(monkeypatch, tmp_path, None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is False
    assert delivery.pushed == []
    assert host._response_guard is None


def test_delivery_enforce_blocks_when_guard_raises(monkeypatch, tmp_path):
    from colony_sidecar.gate.response_guard import GuardMode

    class BrokenGuard:
        configured_mode = GuardMode.ENFORCE

        async def evaluate(self, **_kwargs):
            raise RuntimeError("guard unavailable")

    _wire_guard(monkeypatch, tmp_path, BrokenGuard())
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_environment_enforce_cannot_be_weakened_by_stale_shadow_guard(
    monkeypatch, tmp_path,
):
    from colony_sidecar.gate.response_guard import GuardMode

    class StaleShadowGuard:
        configured_mode = GuardMode.SHADOW

        async def evaluate(self, **_kwargs):
            return GuardResult(
                decision="allow", mode="shadow", surface="proactive_text",
                findings=[],
            )

    _wire_guard(monkeypatch, tmp_path, StaleShadowGuard())
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_delivery_shadow_guard_outage_remains_fail_open(monkeypatch, tmp_path):
    _wire_guard(monkeypatch, tmp_path, None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "shadow")
    loop = _loop()
    delivery = _FakeDelivery()
    ok = asyncio.run(loop._route_reachout_delivery(
        _payload("a candidate update"), delivery))
    assert ok is True
    assert delivery.pushed[0]["description"] == "a candidate update"

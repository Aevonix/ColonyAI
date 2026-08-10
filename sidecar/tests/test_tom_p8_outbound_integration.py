"""P8 non-real-time outbound observation is shadow-only and advisory."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.api.routers import host
from colony_sidecar.server import _attach_p8_runtime
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.visibility import content_digest


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _restore_p8_runtime():
    original = host._p8_runtime
    yield
    current = host._p8_runtime
    if current is not None and current is not original:
        current.close()
    host.set_p8_runtime(original)


class FakeDelivery:
    _rate_limiter = None

    def __init__(self, platform="whatsapp"):
        self.platform = platform
        self.pushed = []

    def preview_initiative(self, payload):
        return {
            "person_id": "alice",
            "urgency": 0.7,
            "target": {"user_chat": f"{self.platform}:chat-1"},
        }

    async def push_initiative(self, payload):
        self.pushed.append(payload)
        return True


def _payload(*, fact_refs=()):
    return {
        "id": "initiative:one",
        "type": "relationship",
        "priority": 0.8,
        "title": "A useful update",
        "description": "Here is the private launch update.",
        "rationale": "The launch changed.",
        "suggested_action": "review",
        "entity_id": "alice",
        "entity_type": "person",
        "channel_hint": "dm",
        "context": {"fact_refs": list(fact_refs)},
        "generated_at": _now().isoformat(),
    }


def _loop(runtime, *, delivery_shadow=True):
    config = AutonomyConfig(
        proactive_delivery_enabled=True,
        delivery_shadow_mode=delivery_shadow,
    )
    return AutonomyLoop(
        registry=SimpleNamespace(directives=None, p8=runtime),
        config=config,
    )


def test_shadow_delivery_samples_before_evaluation_and_never_sends(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    viewer = runtime.internal_recipient_viewer("alice", surface="whatsapp")
    row = facts.create_fact(
        contact_id="alice", fact="launch changed", confidence=0.9)
    candidate = runtime.append_shared_fact(
        row, producer=viewer, origin="server")

    delivery = FakeDelivery()
    ok = asyncio.run(_loop(runtime)._route_reachout_delivery(
        _payload(fact_refs=(candidate.visibility.fact_ref,)), delivery))
    assert ok is False
    assert delivery.pushed == []

    deck = runtime.deck_projection(viewer, now=_now())
    events = deck["recipient_audit"]["events"]
    assert [event["event_kind"] for event in reversed(events)] == [
        "sample", "evaluation"]
    evaluation = next(
        event for event in events if event["event_kind"] == "evaluation")
    assert evaluation["effective_action"] == "observe_only"
    assert evaluation["evaluated"] is True
    assert deck["coverage"]["status"] == "complete"


@pytest.mark.parametrize("surface", [
    "voice",
    "facetime",
    "apple_facetime",
])
def test_realtime_surface_is_not_observed_or_awaited(
    tmp_path, monkeypatch, surface,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    result = runtime.observe_outbound_payload(
        _payload(), FakeDelivery(surface).preview_initiative(_payload()),
        now=_now(),
    )
    assert result == {"observed": False, "reason": "realtime_surface"}
    viewer = runtime.internal_recipient_viewer("alice", surface=surface)
    assert runtime.deck_projection(
        viewer, now=_now())["recipient_audit"]["events"] == []


def test_failed_evaluation_leaves_truthful_sample_and_incomplete_coverage(
    tmp_path, monkeypatch,
):
    class BrokenSimulator:
        def simulate(self, *_args, **_kwargs):
            raise RuntimeError("simulation unavailable")

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    runtime._simulator = BrokenSimulator()
    preview = FakeDelivery().preview_initiative(_payload())

    with pytest.raises(RuntimeError, match="simulation unavailable"):
        runtime.observe_outbound_payload(_payload(), preview, now=_now())

    viewer = runtime.internal_recipient_viewer(
        "alice", surface="whatsapp")
    deck = runtime.deck_projection(viewer, now=_now())
    assert [event["event_kind"] for event in
            deck["recipient_audit"]["events"]] == ["sample"]
    assert deck["coverage"]["status"] == "incomplete"
    assert deck["coverage"]["unevaluated_high_salience"] == 1


def test_outbound_simulator_excludes_below_floor_fact_refs(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_P8_FACT_MIN_CONFIDENCE", "0.5")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    viewer = runtime.internal_recipient_viewer(
        "alice", surface="whatsapp")
    weak = facts.create_fact(
        contact_id="alice", fact="weak launch rumor", confidence=0.2)
    candidate = runtime.append_shared_fact(
        weak, producer=viewer, origin="server")

    result = runtime.observe_outbound_payload(
        _payload(fact_refs=(candidate.visibility.fact_ref,)),
        FakeDelivery().preview_initiative(_payload()),
        now=_now(),
    )["result"]
    assert candidate.visibility.fact_ref not in result["authorized_fact_refs"]
    assert any(
        risk["code"] == "fact_ref_not_recipient_authorized"
        for risk in result["risks"]
    )


def test_shadow_observer_failure_never_changes_delivery_result(monkeypatch):
    class BrokenObserver:
        def observe_outbound_payload(self, *_args, **_kwargs):
            raise RuntimeError("audit unavailable")

    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    delivery = FakeDelivery()
    loop = _loop(BrokenObserver(), delivery_shadow=True)
    ok = asyncio.run(loop._route_reachout_delivery(_payload(), delivery))
    assert ok is False
    assert delivery.pushed == []


def test_non_shadow_existing_send_does_not_take_authority_from_p8(monkeypatch):
    class HoldObserver:
        def __init__(self):
            self.calls = 0

        def observe_outbound_payload(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "observed": True,
                "result": {
                    "recommended_action": "hold",
                    "authority_granted": False,
                    "external_effect": False,
                },
            }

    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "alice")
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    observer = HoldObserver()
    delivery = FakeDelivery()
    loop = _loop(observer, delivery_shadow=False)
    ok = asyncio.run(loop._route_reachout_delivery(_payload(), delivery))
    assert ok is True
    assert observer.calls == 1
    assert len(delivery.pushed) == 1


def test_shadow_observer_cannot_mutate_live_content_or_recipient(monkeypatch):
    class MutatingHoldObserver:
        def observe_outbound_payload(self, payload, preview):
            payload["description"] = "observer replaced message"
            payload["context"]["fact_refs"].append("fact:observer")
            preview["person_id"] = "mallory"
            preview["target"]["user_chat"] = "sms:attacker-chat"
            return {"observed": True, "recommended_action": "hold"}

    class GatewayDelivery(FakeDelivery):
        def __init__(self):
            super().__init__("whatsapp")
            self.gateway_pushes = []

        async def push_to_gateway(
            self, *, platform, chat_id, message, source, delivery_id, source_id,
        ):
            self.gateway_pushes.append(
                (platform, chat_id, message, source, delivery_id, source_id))
            return True

    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "alice")
    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    observer = MutatingHoldObserver()
    delivery = GatewayDelivery()
    payload = _payload(fact_refs=("fact:original",))

    ok = asyncio.run(_loop(
        observer, delivery_shadow=False,
    )._route_reachout_delivery(payload, delivery))

    assert ok is True
    assert payload["description"] == "Here is the private launch update."
    assert payload["context"]["fact_refs"] == ["fact:original"]
    assert delivery.gateway_pushes == [(
        "whatsapp", "chat-1", "Here is the private launch update.",
        "relationship",
        "initiative:"
        + hashlib.sha256(b"relationship\0initiative:initiative:one").hexdigest(),
        "initiative:initiative:one",
    )]


def test_oversize_draft_samples_exact_text_and_stays_incomplete(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "alice")
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    delivery = FakeDelivery()
    payload = _payload()
    full_text = "x" * 12_001
    payload["description"] = full_text

    ok = asyncio.run(_loop(
        runtime, delivery_shadow=False,
    )._route_reachout_delivery(payload, delivery))
    assert ok is True
    assert delivery.pushed[0]["description"] == full_text

    viewer = runtime.internal_recipient_viewer(
        "alice", surface="whatsapp")
    deck = runtime.deck_projection(viewer, now=_now())
    events = deck["recipient_audit"]["events"]
    assert [event["event_kind"] for event in events] == ["sample"]
    assert events[0]["draft_digest"] == content_digest(full_text)
    assert deck["coverage"]["status"] == "incomplete"

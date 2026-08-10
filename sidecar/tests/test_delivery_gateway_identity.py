from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from colony_sidecar.briefings.delivery import WhatsAppBriefingGateway
from colony_sidecar.briefings.models import Briefing
from colony_sidecar.delivery.bridge import ProactiveDeliveryBridge, _GatewayOutcomeStore
from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter


class _Response:
    def __init__(self, *, status=200, body="ok", headers=None):
        self.status = status
        self._body = body
        self.headers = dict(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self._body


class _Session:
    def __init__(self, captured, response=None):
        self.captured = captured
        self.response = response or _Response()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, *, json, headers, timeout):
        self.captured.append((url, dict(json), dict(headers), timeout.total))
        return self.response


class _SessionFactory:
    def __init__(self, captured, responses):
        self.captured = captured
        self.responses = list(responses)

    def __call__(self):
        if not self.responses:
            raise AssertionError("unexpected gateway HTTP request")
        return _Session(self.captured, self.responses.pop(0))


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _boundary_document(delivery_id, state, *, intent_id, provider_delivered):
    return {
        "schema": "GatewayBoundaryOutcomeV1",
        "version": 1,
        "delivery_id": delivery_id,
        "state": state,
        "intent_id": intent_id,
        "provider_delivered": provider_delivered,
    }


def test_gateway_flat_contract_adds_optional_ids_without_breaking_legacy(monkeypatch):
    captured = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _Session(captured))
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="test-secret",
    )
    assert asyncio.run(
        bridge.push_to_gateway(
            platform="whatsapp",
            chat_id="12125550199@s.whatsapp.net",
            message="hello",
            source="relationship",
            delivery_id="initiative:" + "a" * 64,
            source_id="initiative-source-0001",
        )
    )
    assert captured[-1][1] == {
        "platform": "whatsapp",
        "chat_id": "12125550199@s.whatsapp.net",
        "message": "hello",
        "source": "relationship",
        "delivery_id": "initiative:" + "a" * 64,
        "source_id": "initiative-source-0001",
    }
    assert asyncio.run(
        bridge.push_to_gateway(
            platform="rcs",
            chat_id="legacy-thread",
            message="legacy",
            source="briefing",
        )
    )
    assert captured[-1][1] == {
        "platform": "rcs",
        "chat_id": "legacy-thread",
        "message": "legacy",
        "source": "briefing",
    }


def test_governed_gateway_requires_exact_non_delivery_admission(monkeypatch, tmp_path):
    delivery_id = "initiative:" + "b" * 64
    document = _boundary_document(
        delivery_id,
        "awaiting_approval",
        intent_id="colony-intent:" + "c" * 64,
        provider_delivered=False,
    )
    captured = []
    response = _Response(
        body=json.dumps(document),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda: _Session(captured, response)
    )
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="g" * 40,
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
    )
    outcome = asyncio.run(
        bridge.push_to_gateway(
            platform="whatsapp",
            chat_id="12125550199@s.whatsapp.net",
            message="hello",
            source="relationship",
            delivery_id=delivery_id,
            source_id="initiative-source-0002",
        )
    )
    assert bool(outcome) is True
    assert outcome.provider_delivered is False
    assert outcome.admission_state == "awaiting_approval"
    assert outcome.terminal is False
    assert outcome.observation_new is True
    assert captured[0][2]["Authorization"] == "Bearer " + "g" * 40


def test_governed_contract_is_whatsapp_specific_and_preserves_legacy_rcs(
    monkeypatch, tmp_path,
):
    captured = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _Session(captured))
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="transport-specific-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
    )
    assert bridge.governed_gateway_admission_enabled() is True
    assert bridge.governed_gateway_admission_enabled("whatsapp") is True
    assert bridge.governed_gateway_admission_enabled("rcs") is False
    assert bridge.governed_gateway_admission_enabled("sms") is False

    outcome = asyncio.run(bridge.push_to_gateway(
        platform="rcs",
        chat_id="legacy-rcs-thread",
        message="legacy path",
        source="relationship",
    ))
    assert bool(outcome) is True
    assert outcome.provider_delivered is True
    assert outcome.contract == "legacy_delivery"
    assert captured == [(
        "http://127.0.0.1:18802/internal/deliver",
        {
            "platform": "rcs",
            "chat_id": "legacy-rcs-thread",
            "message": "legacy path",
            "source": "relationship",
        },
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer transport-specific-secret",
        },
        5.0,
    )]


def test_governed_gateway_rejects_inconsistent_exact_outcome(monkeypatch, tmp_path):
    delivery_id = "initiative:" + "d" * 64
    document = _boundary_document(
        delivery_id,
        "awaiting_approval",
        intent_id="colony-intent:" + "e" * 64,
        provider_delivered=True,
    )
    captured = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _SessionFactory(
            captured,
            [
                _Response(
                    body=json.dumps(document),
                    headers={"Content-Type": "application/json"},
                )
            ],
        ),
    )
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="g" * 40,
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
    )
    rejected = asyncio.run(
        bridge.push_to_gateway(
            platform="whatsapp",
            chat_id="12125550199@s.whatsapp.net",
            message="hello",
            source="relationship",
            delivery_id=delivery_id,
            source_id="initiative-source-invalid",
        )
    )
    assert bool(rejected) is False
    assert rejected.provider_delivered is False


def test_governed_gateway_lifecycle_is_durable_and_does_not_resubmit(
    monkeypatch, tmp_path,
):
    delivery_id = "initiative:" + "f" * 64
    intent_id = "colony-intent:" + "1" * 64
    clock = _Clock()
    captured = []
    factory = _SessionFactory(
        captured,
        [
            _Response(
                body=json.dumps(
                    _boundary_document(
                        delivery_id,
                        "awaiting_approval",
                        intent_id=intent_id,
                        provider_delivered=False,
                    )
                ),
                headers={"Content-Type": "application/json"},
            ),
            _Response(
                body=json.dumps(
                    _boundary_document(
                        delivery_id,
                        "delivered",
                        intent_id=intent_id,
                        provider_delivered=True,
                    )
                ),
                headers={"Content-Type": "application/json"},
            ),
        ],
    )
    monkeypatch.setattr(aiohttp, "ClientSession", factory)
    db_path = tmp_path / "outcomes.db"
    kwargs = dict(
        platform="whatsapp",
        chat_id="12125550199@s.whatsapp.net",
        message="hello",
        source="relationship",
        delivery_id=delivery_id,
        source_id="initiative-source-lifecycle",
    )
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="lifecycle-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(db_path),
        gateway_poll_seconds=5,
        clock=clock,
    )

    admitted = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert bool(admitted) is True
    assert admitted.provider_delivered is False
    assert admitted.observation_new is True
    assert len(captured) == 1
    assert bridge.governed_pending_delivery_ids() == (delivery_id,)

    cached_pending = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert cached_pending.admission_state == "awaiting_approval"
    assert cached_pending.provider_delivered is False
    assert cached_pending.observation_new is False
    assert len(captured) == 1

    clock.advance(5)
    delivered = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert bool(delivered) is True
    assert delivered.provider_delivered is True
    assert delivered.admission_state == "delivered"
    assert delivered.terminal is True
    assert delivered.observation_new is True
    assert len(captured) == 2
    assert bridge.governed_pending_delivery_ids() == ()

    cached_delivered = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert cached_delivered.provider_delivered is True
    assert cached_delivered.observation_new is False
    assert len(captured) == 2

    # A process restart reuses the durable terminal result and performs no HTTP.
    restarted = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="lifecycle-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(db_path),
        gateway_poll_seconds=5,
        clock=clock,
    )
    after_restart = asyncio.run(restarted.push_to_gateway(**kwargs))
    assert after_restart.provider_delivered is True
    assert after_restart.observation_new is False
    assert len(captured) == 2


def test_governed_gateway_terminal_failure_converges_once(monkeypatch, tmp_path):
    delivery_id = "initiative:" + "2" * 64
    intent_id = "colony-intent:" + "3" * 64
    clock = _Clock()
    captured = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _SessionFactory(
            captured,
            [
                _Response(
                    body=json.dumps(
                        _boundary_document(
                            delivery_id,
                            "accepted",
                            intent_id=intent_id,
                            provider_delivered=False,
                        )
                    ),
                    headers={"Content-Type": "application/json"},
                ),
                _Response(
                    body=json.dumps(
                        _boundary_document(
                            delivery_id,
                            "failed",
                            intent_id=intent_id,
                            provider_delivered=False,
                        )
                    ),
                    headers={"Content-Type": "application/json"},
                ),
            ],
        ),
    )
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="failure-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
        gateway_poll_seconds=5,
        clock=clock,
    )
    kwargs = dict(
        platform="whatsapp",
        chat_id="12125550199@s.whatsapp.net",
        message="hello",
        source="relationship",
        delivery_id=delivery_id,
        source_id="initiative-source-failure",
    )
    assert asyncio.run(bridge.push_to_gateway(**kwargs)).admission_state == "accepted"
    clock.advance(5)
    failed = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert bool(failed) is False
    assert failed.admission_state == "failed"
    assert failed.terminal is True
    assert failed.observation_new is True
    assert len(captured) == 2
    cached = asyncio.run(bridge.push_to_gateway(**kwargs))
    assert cached.admission_state == "failed"
    assert cached.observation_new is False
    assert len(captured) == 2


def test_governed_gateway_rejects_stable_id_reuse_for_different_bytes(
    monkeypatch, tmp_path,
):
    delivery_id = "initiative:" + "4" * 64
    intent_id = "colony-intent:" + "5" * 64
    captured = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _SessionFactory(
            captured,
            [
                _Response(
                    body=json.dumps(
                        _boundary_document(
                            delivery_id,
                            "awaiting_approval",
                            intent_id=intent_id,
                            provider_delivered=False,
                        )
                    ),
                    headers={"Content-Type": "application/json"},
                )
            ],
        ),
    )
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="conflict-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
    )
    base = dict(
        platform="whatsapp",
        chat_id="12125550199@s.whatsapp.net",
        source="relationship",
        delivery_id=delivery_id,
        source_id="initiative-source-conflict",
    )
    assert asyncio.run(bridge.push_to_gateway(message="first", **base))
    conflict = asyncio.run(bridge.push_to_gateway(message="changed", **base))
    assert bool(conflict) is False
    assert len(captured) == 1


def test_gateway_outcome_store_rejects_intent_change_and_state_regression(tmp_path):
    clock = _Clock()
    store = _GatewayOutcomeStore(
        tmp_path / "outcomes.db", clock=clock, poll_seconds=5
    )
    delivery_id = "initiative:" + "6" * 64
    intent_id = "colony-intent:" + "7" * 64
    store.reserve(delivery_id, "8" * 64)
    observed, changed = store.observe(
        delivery_id,
        state="awaiting_approval",
        intent_id=intent_id,
        provider_delivered=False,
    )
    assert observed["state"] == "awaiting_approval"
    assert changed is True

    with pytest.raises(ValueError, match="intent identity changed"):
        store.observe(
            delivery_id,
            state="accepted",
            intent_id="colony-intent:" + "9" * 64,
            provider_delivered=False,
        )
    store.observe(
        delivery_id,
        state="accepted",
        intent_id=intent_id,
        provider_delivered=False,
    )
    with pytest.raises(ValueError, match="transition regressed"):
        store.observe(
            delivery_id,
            state="awaiting_approval",
            intent_id=intent_id,
            provider_delivered=False,
        )

    store.observe(
        delivery_id,
        state="failed",
        intent_id=intent_id,
        provider_delivered=False,
    )
    same, changed = store.observe(
        delivery_id,
        state="failed",
        intent_id=intent_id,
        provider_delivered=False,
    )
    assert same["state"] == "failed"
    assert changed is False
    with pytest.raises(ValueError, match="transition regressed|terminal outcome"):
        store.observe(
            delivery_id,
            state="delivered",
            intent_id=intent_id,
            provider_delivered=True,
        )


def test_gateway_outcome_store_requires_intent_for_non_delivery_outcome(tmp_path):
    store = _GatewayOutcomeStore(
        tmp_path / "outcomes.db", clock=_Clock(), poll_seconds=5
    )
    delivery_id = "initiative:" + "a" * 63 + "0"
    store.reserve(delivery_id, "b" * 64)
    with pytest.raises(ValueError, match="intent identity is missing"):
        store.observe(
            delivery_id,
            state="ambiguous",
            intent_id="",
            provider_delivered=False,
        )

def test_whatsapp_briefing_uses_retry_stable_briefing_identity():
    captured = []

    class Bridge:
        async def push_to_gateway(self, **kwargs):
            captured.append(kwargs)
            return True

    briefing = Briefing(briefing_id="briefing-source-0001")
    result = WhatsAppBriefingGateway(
        delivery_bridge=Bridge(), chat_id="12125550199@s.whatsapp.net"
    ).send(briefing, "Daily briefing")
    assert result.success is True
    assert captured == [
        {
            "platform": "whatsapp",
            "chat_id": "12125550199@s.whatsapp.net",
            "message": "Daily briefing",
            "source": "briefing",
            "delivery_id": "briefing:briefing-source-0001",
            "source_id": "briefing:briefing-source-0001",
        }
    ]

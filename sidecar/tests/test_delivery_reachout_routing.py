"""Tests for reach-out delivery routing, shadow preview, and rate-gate wiring.

Covers:
- initiative-type classification (reach-out vs internal) + env override
- ProactiveDeliveryBridge.preview_initiative resolves recipient/target/payload
  WITHOUT sending, sharing prep with push_initiative
- the internal executor's default types exclude reach-out types
"""

from __future__ import annotations

from types import SimpleNamespace

from colony_sidecar.delivery.classification import is_reachout, reachout_types
from colony_sidecar.delivery.bridge import GatewayPushResult, ProactiveDeliveryBridge
from colony_sidecar.delivery.channels import Channel
from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter


class StubRegistry:
    """Minimal ChannelRegistry stand-in returning fixed channels."""

    def __init__(self, home=None, dm=None):
        self._home = home
        self._dm = dm

    def resolve(self, person_id, channel_type="home"):
        return self._dm if channel_type == "dm" else self._home


def _bridge():
    home = Channel(platform="whatsapp", chat_id="home@lid", channel_type="home")
    dm = Channel(platform="whatsapp", chat_id="dm@lid", channel_type="dm")
    # In-memory rate limiter (db_path=None) so no disk state.
    return ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        channel_registry=StubRegistry(home=home, dm=dm),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_reachout_defaults():
    assert is_reachout("follow_up") is True
    assert is_reachout("relationship") is True
    assert is_reachout("system") is False
    assert is_reachout("capability_gap") is False
    assert is_reachout("") is False


def test_reachout_env_override(monkeypatch):
    monkeypatch.setenv("COLONY_REACHOUT_TYPES", "introduction, scheduling")
    assert reachout_types() == frozenset({"introduction", "scheduling"})
    assert is_reachout("follow_up") is False   # no longer in the set
    assert is_reachout("introduction") is True


# ---------------------------------------------------------------------------
# preview_initiative (read-only)
# ---------------------------------------------------------------------------

def test_preview_follow_up_resolves_owner_and_target(monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")
    bridge = _bridge()
    initiative = {
        "id": "init-42",
        "type": "follow_up",
        "priority": 0.7,
        "title": "Follow up with the owner about the deploy",
        "description": "Follow up with the owner about the deploy",
        "rationale": "No reply in 3 days",
        "suggested_action": "review_and_decide",
        "entity_id": "goal-123",          # a goal id, NOT a person
        "entity_type": "follow_up",
    }
    preview = bridge.preview_initiative(initiative)

    # follow_up targets the OWNER bucket (not the goal entity_id)
    assert preview["person_id"] == "cid-owner-xyz"
    assert preview["urgency"] == 0.7
    assert preview["channel_hint"] == "dm"
    assert preview["target"]["home_chat"] == "whatsapp:home@lid"
    assert preview["target"]["user_chat"] == "whatsapp:dm@lid"
    assert preview["webhook_payload"]["payload"]["initiative_type"] == "follow_up"
    assert preview["initiative_type"] == "follow_up"


def test_preview_relationship_targets_the_person(monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")
    bridge = _bridge()
    initiative = {
        "id": "init-r",
        "type": "relationship",
        "priority": 0.5,
        "title": "Reconnect with Alice",
        "entity_id": "cid-alice",         # relationship: entity_id IS the person
    }
    preview = bridge.preview_initiative(initiative)
    # relationship routes to the entity person, not the owner
    assert preview["person_id"] == "cid-alice"


def test_preview_does_not_consume_rate_budget(monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")
    bridge = _bridge()
    initiative = {"id": "i", "type": "follow_up", "priority": 0.5, "entity_id": "g"}
    assert bridge._rate_limiter.daily_count("cid-owner-xyz") == 0
    for _ in range(5):
        bridge.preview_initiative(initiative)
    # Preview never records a delivery, so the budget is untouched
    # (time-of-day independent: this is the invariant that matters).
    assert bridge._rate_limiter.daily_count("cid-owner-xyz") == 0


def test_preview_matches_push_prep(monkeypatch):
    """preview and push share _prepare_initiative_dispatch (same routing)."""
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")
    bridge = _bridge()
    initiative = {"id": "i", "type": "follow_up", "priority": 0.9, "entity_id": "g"}
    prep = bridge._prepare_initiative_dispatch(initiative)
    preview = bridge.preview_initiative(initiative)
    assert prep["person_id"] == preview["person_id"]
    assert prep["target"] == preview["target"]
    assert prep["channel_hint"] == preview["channel_hint"]
    # priority 0.9 (<=1.0 float) scales to 90 on the wire
    assert prep["payload"]["payload"]["priority"] == 90


def test_governed_preview_resolves_new_contact_from_real_async_store(
    monkeypatch, tmp_path,
):
    """A V3-authorized contact does not need a channels.json side write."""
    import asyncio
    from colony_sidecar.contacts.config import ContactsConfig
    from colony_sidecar.contacts.store import SQLiteContactStore
    from colony_sidecar.delivery.channels import ChannelRegistry

    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    monkeypatch.setenv("WHATSAPP_HOME_CHANNEL", "owner-group@g.us")

    async def scenario():
        store = SQLiteContactStore(
            config=ContactsConfig(sqlite_path=str(tmp_path / "contacts.db")),
        )
        await store.connect()
        try:
            contact = await store.create(
                display_name="Newly authorized contact",
                interaction_allowed=True,
            )
            await store.add_handle(
                contact.contact_id,
                gateway="whatsapp",
                address="12125550199@s.whatsapp.net",
                is_primary=True,
                verified=True,
                source="owner",
            )
            registry = ChannelRegistry.load(
                json_path=str(tmp_path / "missing-channels.json"),
                contacts_store=store,
            )
            # The historical sync path cannot await the production store.
            assert registry.resolve(contact.contact_id, "dm") is None
            bridge = ProactiveDeliveryBridge(
                rate_limiter=DeliveryRateLimiter(db_path=None),
                gateway_url="http://127.0.0.1:18802",
                gateway_api_key="test-governed-secret",
                gateway_contract="governed_admission_v1",
                gateway_outcome_db=str(tmp_path / "outcomes.db"),
                channel_registry=registry,
            )
            return await bridge.preview_initiative_async({
                "id": "new-contact-outreach",
                "type": "relationship",
                "priority": 0.7,
                "title": "Check in",
                "description": "A bounded check-in.",
                "entity_id": contact.contact_id,
                "entity_type": "person",
            })
        finally:
            await store.close()

    preview = asyncio.run(scenario())
    assert preview["target"] == {
        "user_chat": "whatsapp:12125550199@s.whatsapp.net",
    }
    assert preview["webhook_payload"]["delivery_context"] == preview["target"]


def test_governed_non_owner_missing_verified_dm_never_falls_back_home(
    monkeypatch, tmp_path,
):
    class RegistryWithHomeOnly:
        def resolve(self, _person_id, channel_type="home"):
            if channel_type == "home":
                return Channel(
                    platform="whatsapp",
                    chat_id="owner-group@g.us",
                    channel_type="home",
                )
            return None

        async def resolve_exact_verified_dm(self, _person_id, *, platform):
            assert platform == "whatsapp"
            return None

    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    bridge = ProactiveDeliveryBridge(
        rate_limiter=DeliveryRateLimiter(db_path=None),
        gateway_url="http://127.0.0.1:18802",
        gateway_api_key="test-governed-secret",
        gateway_contract="governed_admission_v1",
        gateway_outcome_db=str(tmp_path / "outcomes.db"),
        channel_registry=RegistryWithHomeOnly(),
    )
    import asyncio
    preview = asyncio.run(bridge.preview_initiative_async({
        "id": "missing-exact-dm",
        "type": "relationship",
        "priority": 0.7,
        "title": "Check in",
        "entity_id": "cid-contact",
        "entity_type": "person",
    }))
    assert preview["target"] == {}
    assert "delivery_context" not in preview["webhook_payload"]


# ---------------------------------------------------------------------------
# Executor default types exclude reach-out
# ---------------------------------------------------------------------------

def test_executor_defaults_exclude_reachout():
    from colony_sidecar.services.initiative_executor import (
        _DEFAULT_TYPES, _EXECUTABLE_TYPES,
    )
    for t in ("follow_up", "relationship", "introduction",
              "scheduling", "commitment", "calendar"):
        assert t not in _DEFAULT_TYPES, f"{t} should not be executor-claimed"
    # Internal types are still handled.
    for t in ("system", "capability_gap", "data_quality", "research"):
        assert t in _DEFAULT_TYPES
    # And the exclusion is exactly the reach-out set.
    assert set(_EXECUTABLE_TYPES) - set(_DEFAULT_TYPES) == set(reachout_types())


# ---------------------------------------------------------------------------
# Delivery transport selection (env-driven): gateway vs hermes_webhook
# ---------------------------------------------------------------------------

def test_gateway_transport_uses_push_to_gateway(monkeypatch):
    """COLONY_DELIVERY_TRANSPORT=gateway routes the sanitised text through
    push_to_gateway (flat contract) instead of the structured webhook."""
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-xyz")

    cfg = AutonomyConfig()
    cfg.proactive_delivery_enabled = True
    cfg.delivery_shadow_mode = False
    loop = AutonomyLoop(registry=SimpleNamespace(directives=None), config=cfg)

    calls = {}

    class FakeDelivery:
        _rate_limiter = None

        def preview_initiative(self, payload):
            return {"person_id": "cid-owner-xyz", "urgency": 0.7,
                    "channel_hint": "dm",
                    "target": {"user_chat": "whatsapp:home-chat-1"}}

        async def push_to_gateway(
            self, *, platform, chat_id, message, source, delivery_id, source_id,
        ):
            calls["gateway"] = (
                platform, chat_id, message, source, delivery_id, source_id,
            )
            return True

        async def push_initiative(self, payload):
            calls["webhook"] = payload
            return True

    payload = {
        "id": "prop-1", "type": "proposal", "priority": 0.7,
        "title": "A useful finding", "description": "A useful finding.\nDetails here.",
        "rationale": "", "suggested_action": "", "entity_id": None,
        "entity_type": "proposal", "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    ok = asyncio.run(loop._route_reachout_delivery(payload, FakeDelivery()))
    assert ok is True
    assert "webhook" not in calls          # structured path NOT used
    platform, chat_id, message, source, delivery_id, source_id = calls["gateway"]
    assert platform == "whatsapp" and chat_id == "home-chat-1"
    assert "A useful finding" in message and source == "proposal"
    assert source_id == "initiative:prop-1"
    assert delivery_id.startswith("initiative:") and len(delivery_id) == 75


def test_default_transport_still_uses_webhook(monkeypatch):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig

    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    cfg = AutonomyConfig()
    cfg.proactive_delivery_enabled = True
    cfg.delivery_shadow_mode = False
    loop = AutonomyLoop(registry=SimpleNamespace(directives=None), config=cfg)

    calls = {}

    class FakeDelivery:
        _rate_limiter = None

        def preview_initiative(self, payload):
            return {"person_id": "p", "urgency": 0.7, "channel_hint": "dm",
                    "target": {"user_chat": "whatsapp:home-chat-1"}}

        async def push_to_gateway(self, **kw):
            calls["gateway"] = kw
            return True

        async def push_initiative(self, payload):
            calls["webhook"] = payload
            return True

    payload = {
        "id": "prop-2", "type": "proposal", "priority": 0.7,
        "title": "T", "description": "D", "rationale": "",
        "suggested_action": "", "entity_id": None, "entity_type": "proposal",
        "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    ok = asyncio.run(loop._route_reachout_delivery(payload, FakeDelivery()))
    assert ok is True
    assert "webhook" in calls and "gateway" not in calls


def test_gateway_default_keeps_legacy_non_owner_standing_gate(monkeypatch):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    from colony_sidecar.initiatives import standing_approvals

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.setattr(standing_approvals, "is_approved", lambda _name: False)
    cfg = AutonomyConfig(
        proactive_delivery_enabled=True, delivery_shadow_mode=False,
    )
    loop = AutonomyLoop(registry=SimpleNamespace(directives=None), config=cfg)

    async def non_owner(_person_id, _payload):
        return False

    loop._recipient_is_owner = non_owner

    class LegacyGateway:
        _rate_limiter = None
        calls = 0

        def preview_initiative(self, _payload):
            return {
                "person_id": "cid-guest",
                "urgency": 0.7,
                "target": {"user_chat": "whatsapp:12125550199@s.whatsapp.net"},
            }

        def governed_gateway_admission_enabled(self, _platform=None):
            return False

        async def push_to_gateway(self, **_kwargs):
            self.calls += 1
            return True

    gateway = LegacyGateway()
    payload = {
        "id": "rel-legacy-gate", "type": "relationship", "priority": 0.7,
        "title": "Check in", "description": "A bounded check-in.",
        "entity_id": "cid-guest", "entity_type": "person", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    assert asyncio.run(loop._route_reachout_delivery(payload, gateway)) is False
    assert gateway.calls == 0


def test_governed_whatsapp_route_never_exempts_rcs_or_sms_non_owner(
    monkeypatch,
):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    from colony_sidecar.initiatives import standing_approvals

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.setattr(standing_approvals, "is_approved", lambda _name: False)
    loop = AutonomyLoop(
        registry=SimpleNamespace(directives=None),
        config=AutonomyConfig(
            proactive_delivery_enabled=True, delivery_shadow_mode=False,
        ),
    )

    async def non_owner(_person_id, _payload):
        return False

    loop._recipient_is_owner = non_owner

    for platform in ("rcs", "sms"):
        class GovernedWhatsAppGateway:
            _rate_limiter = None

            def __init__(self):
                self.calls = 0
                self.platform_queries = []

            def preview_initiative(self, _payload):
                return {
                    "person_id": "cid-guest",
                    "urgency": 0.7,
                    "target": {"user_chat": f"{platform}:12125550199"},
                }

            def governed_gateway_admission_enabled(self, selected=None):
                self.platform_queries.append(selected)
                return selected is None or selected == "whatsapp"

            async def push_to_gateway(self, **_kwargs):
                self.calls += 1
                return True

        gateway = GovernedWhatsAppGateway()
        payload = {
            "id": f"rel-{platform}-must-gate",
            "type": "relationship",
            "priority": 0.7,
            "title": "Check in",
            "description": "A bounded check-in.",
            "entity_id": "cid-guest",
            "entity_type": "person",
            "context": {},
            "generated_at": "2099-01-01T00:00:00+00:00",
        }
        assert asyncio.run(
            loop._route_reachout_delivery(payload, gateway)
        ) is False
        assert gateway.platform_queries == [platform]
        assert gateway.calls == 0


def test_governed_gateway_admission_bypasses_only_legacy_boolean_and_is_not_delivery(
    monkeypatch,
):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    from colony_sidecar.initiatives import standing_approvals
    from colony_sidecar.api.routers import host

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.delenv("COLONY_GUARD_MODE", raising=False)
    monkeypatch.setattr(standing_approvals, "is_approved", lambda _name: False)
    monkeypatch.setattr(host, "_response_guard", None)

    class RateLimiter:
        def __init__(self):
            self.recorded = []

        def can_deliver(self, _person_id, urgency=0.5):
            return True, "ok"

        def record_delivery(self, person_id):
            self.recorded.append(person_id)

    class SelfModel:
        def __init__(self):
            self.records = []

        def record(self, *values):
            self.records.append(values)

    rate = RateLimiter()
    model = SelfModel()
    cfg = AutonomyConfig(
        proactive_delivery_enabled=True, delivery_shadow_mode=False,
    )
    loop = AutonomyLoop(
        registry=SimpleNamespace(directives=None, self_model=model), config=cfg,
    )

    async def non_owner(_person_id, _payload):
        return False

    loop._recipient_is_owner = non_owner

    class GovernedGateway:
        _rate_limiter = rate
        calls = 0
        outcomes = (
            (True, False, "awaiting_approval", False, True),
            (True, True, "delivered", True, True),
            (True, True, "delivered", True, False),
        )

        def preview_initiative(self, _payload):
            raise AssertionError("governed gateway must use the async exact preview")

        async def preview_initiative_async(self, _payload):
            return {
                "person_id": "cid-guest",
                "urgency": 0.7,
                "target": {"user_chat": "whatsapp:12125550199@s.whatsapp.net"},
            }

        def governed_gateway_admission_enabled(self, platform=None):
            return platform is None or platform == "whatsapp"

        async def push_to_gateway(self, **kwargs):
            accepted, delivered, state, terminal, observation_new = self.outcomes[
                self.calls
            ]
            self.calls += 1
            return GatewayPushResult(
                accepted=accepted,
                provider_delivered=delivered,
                contract="governed_admission_v1",
                admission_state=state,
                delivery_id=kwargs["delivery_id"],
                terminal=terminal,
                observation_new=observation_new,
            )

    gateway = GovernedGateway()
    payload = {
        "id": "rel-governed-gate", "type": "relationship", "priority": 0.7,
        "title": "Check in", "description": "A bounded check-in.",
        "entity_id": "cid-guest", "entity_type": "person", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    # The downstream accepted queue work, but no provider delivery occurred.
    assert asyncio.run(loop._route_reachout_delivery(payload, gateway)) is False
    assert gateway.calls == 1
    assert rate.recorded == []
    assert model.records == []
    assert loop.stats.actions_executed == 0
    assert len(loop._governed_delivery_replays) == 1
    loop._registry.delivery = gateway

    # A later lifecycle poll converges to real provider delivery and accounts
    # for it exactly once. Re-reading the cached terminal result stays true but
    # cannot consume a second rate/stat/self-model event.
    asyncio.run(loop._phase_governed_delivery_reconciliation())
    assert rate.recorded == ["cid-guest"]
    assert model.records == [("delivery", "success")]
    assert loop.stats.actions_executed == 1
    assert loop._governed_delivery_replays == {}
    assert asyncio.run(loop._route_reachout_delivery(payload, gateway)) is True
    assert gateway.calls == 3
    assert rate.recorded == ["cid-guest"]
    assert model.records == [("delivery", "success")]
    assert loop.stats.actions_executed == 1


def test_governed_reconciler_runs_in_reactive_mode_until_stopped(monkeypatch):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
    from colony_sidecar.identity import resolver as identity_resolver

    class Resolver:
        async def owner_identities(self):
            return ("owner",)

    monkeypatch.setattr(identity_resolver, "get_identity_resolver", lambda: Resolver())

    class Delivery:
        def governed_gateway_admission_enabled(self, platform=None):
            return platform is None or platform == "whatsapp"

        def governed_gateway_poll_seconds(self):
            return 0.01

    delivery = Delivery()
    loop = AutonomyLoop(
        registry=SimpleNamespace(delivery=delivery),
        config=AutonomyConfig(mode=AutonomyMode.REACTIVE),
    )
    calls = []

    async def reconcile():
        calls.append("poll")

    loop._phase_governed_delivery_reconciliation = reconcile

    async def scenario():
        await loop.start()
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.01)
        await loop.stop()

    asyncio.run(scenario())
    assert calls
    assert loop._governed_reconcile_task is None


def test_reactive_restart_rebuilds_only_durable_admitted_initiative(monkeypatch):
    import asyncio
    import hashlib
    from datetime import datetime, timezone
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
    from colony_sidecar.api.routers import host
    from colony_sidecar.identity import resolver as identity_resolver

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.delenv("COLONY_GUARD_MODE", raising=False)
    monkeypatch.setattr(host, "_response_guard", None)

    class Resolver:
        async def owner_identities(self):
            return ("owner",)

    monkeypatch.setattr(identity_resolver, "get_identity_resolver", lambda: Resolver())

    initiative_id = "restart-pending-initiative"
    source_id = "initiative:" + initiative_id
    delivery_id = "initiative:" + hashlib.sha256(
        ("relationship\0" + source_id).encode("utf-8")
    ).hexdigest()
    initiative = SimpleNamespace(
        id=initiative_id,
        type="relationship",
        priority=0.7,
        description="A durable bounded check-in.",
        rationale="Owner-authorized contact follow-up",
        action_hint="review_and_decide",
        entity_id="cid-guest",
        context={},
        created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )

    class InitiativeStore:
        calls = []

        def list(self, **kwargs):
            self.calls.append(kwargs)
            return [initiative]

    class SelfModel:
        def __init__(self):
            self.records = []

        def record(self, *values):
            self.records.append(values)

    class Gateway:
        _rate_limiter = None

        def __init__(self):
            self.calls = []

        def governed_gateway_admission_enabled(self, platform=None):
            return platform is None or platform == "whatsapp"

        def governed_gateway_poll_seconds(self):
            return 0.01

        def governed_pending_delivery_ids(self, *, limit=100):
            assert limit == 100
            return () if self.calls else (delivery_id,)

        def preview_initiative(self, _payload):
            return {
                "person_id": "cid-guest",
                "urgency": 0.7,
                "target": {"user_chat": "whatsapp:12125550199@s.whatsapp.net"},
            }

        async def push_to_gateway(self, **kwargs):
            self.calls.append(kwargs)
            return GatewayPushResult(
                accepted=True,
                provider_delivered=True,
                contract="governed_admission_v1",
                admission_state="delivered",
                delivery_id=kwargs["delivery_id"],
                terminal=True,
                observation_new=True,
            )

    gateway = Gateway()
    store = InitiativeStore()
    model = SelfModel()
    loop = AutonomyLoop(
        registry=SimpleNamespace(
            delivery=gateway,
            initiative_store=store,
            directives=None,
            self_model=model,
        ),
        config=AutonomyConfig(
            mode=AutonomyMode.REACTIVE,
            proactive_delivery_enabled=True,
            delivery_shadow_mode=False,
        ),
    )

    async def non_owner(_person_id, _payload):
        return False

    loop._recipient_is_owner = non_owner

    async def scenario():
        await loop.start()
        for _ in range(30):
            if gateway.calls:
                break
            await asyncio.sleep(0.01)
        await loop.stop()

    asyncio.run(scenario())
    assert store.calls == [{"status": ["pending"], "limit": 1000}]
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["delivery_id"] == delivery_id
    assert gateway.calls[0]["source_id"] == source_id
    assert loop._governed_delivery_replays == {}
    assert model.records == [("delivery", "success")]
    assert loop.stats.actions_executed == 1


def test_governed_gateway_terminal_failure_is_accounted_once(monkeypatch):
    import asyncio
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    from colony_sidecar.initiatives import standing_approvals
    from colony_sidecar.api.routers import host

    monkeypatch.setenv("COLONY_DELIVERY_TRANSPORT", "gateway")
    monkeypatch.delenv("COLONY_GUARD_MODE", raising=False)
    monkeypatch.setattr(standing_approvals, "is_approved", lambda _name: False)
    monkeypatch.setattr(host, "_response_guard", None)

    class SelfModel:
        def __init__(self):
            self.records = []

        def record(self, *values):
            self.records.append(values)

    model = SelfModel()
    loop = AutonomyLoop(
        registry=SimpleNamespace(directives=None, self_model=model),
        config=AutonomyConfig(
            proactive_delivery_enabled=True, delivery_shadow_mode=False,
        ),
    )

    async def non_owner(_person_id, _payload):
        return False

    loop._recipient_is_owner = non_owner

    class GovernedGateway:
        _rate_limiter = None
        calls = 0

        def preview_initiative(self, _payload):
            return {
                "person_id": "cid-guest",
                "urgency": 0.7,
                "target": {"user_chat": "whatsapp:12125550199@s.whatsapp.net"},
            }

        def governed_gateway_admission_enabled(self, platform=None):
            return platform is None or platform == "whatsapp"

        async def push_to_gateway(self, **kwargs):
            self.calls += 1
            return GatewayPushResult(
                accepted=False,
                provider_delivered=False,
                contract="governed_admission_v1",
                admission_state="failed",
                delivery_id=kwargs["delivery_id"],
                terminal=True,
                observation_new=self.calls == 1,
            )

    gateway = GovernedGateway()
    payload = {
        "id": "rel-governed-failed", "type": "relationship", "priority": 0.7,
        "title": "Check in", "description": "A bounded check-in.",
        "entity_id": "cid-guest", "entity_type": "person", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    assert asyncio.run(loop._route_reachout_delivery(payload, gateway)) is False
    assert model.records == [("delivery", "failure")]
    assert asyncio.run(loop._route_reachout_delivery(payload, gateway)) is False
    assert model.records == [("delivery", "failure")]
    assert loop.stats.actions_executed == 0

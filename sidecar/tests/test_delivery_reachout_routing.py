"""Tests for reach-out delivery routing, shadow preview, and rate-gate wiring.

Covers:
- initiative-type classification (reach-out vs internal) + env override
- ProactiveDeliveryBridge.preview_initiative resolves recipient/target/payload
  WITHOUT sending, sharing prep with push_initiative
- the internal executor's default types exclude reach-out types
"""

from __future__ import annotations

from colony_sidecar.delivery.classification import is_reachout, reachout_types
from colony_sidecar.delivery.bridge import ProactiveDeliveryBridge
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

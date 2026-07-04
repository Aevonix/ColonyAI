"""Relationship provenance + signal floor, honest why_it_helps, and the
outbound third-party delivery gate (relationship-perspective fix)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from colony_sidecar.intelligence.relationships import signal_floor as sf


# ---------------------------------------------------------------------------
# Item 1 + 2: provenance (direct interlocutors only) + signal floor
# ---------------------------------------------------------------------------

def test_identical_score_batch_yields_zero(monkeypatch):
    """Regression: a batch of contacts sharing one identical score and
    identical staleness (an ingestion artifact: one event + uniform default
    decay), even when all are direct interlocutors by count, must be dropped
    whole. Killed at the floor, not by a hardcode."""
    monkeypatch.delenv("COLONY_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    monkeypatch.delenv("COLONY_RELATIONSHIP_MAX_IDENTICAL", raising=False)
    batch = [
        {"entity_id": f"p{i}", "name": f"Person {i}", "interaction_count": 9,
         "relationship_score": 0.226}
        for i in range(6)
    ]
    assert sf.filter_relationship_candidates(batch) == []


def test_passively_observed_third_party_dropped(monkeypatch):
    """No direct-exchange evidence => passively observed => out of scope."""
    monkeypatch.delenv("COLONY_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    cands = [
        {"entity_id": "obs", "name": "Observed", "relationship_score": 0.5},  # no count
        {"entity_id": "few", "name": "Barely", "interaction_count": 1,
         "relationship_score": 0.6},  # below floor of 3
    ]
    assert sf.filter_relationship_candidates(cands) == []


def test_genuine_direct_interlocutor_survives(monkeypatch):
    monkeypatch.delenv("COLONY_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    monkeypatch.delenv("COLONY_RELATIONSHIP_MAX_IDENTICAL", raising=False)
    cands = [
        {"entity_id": "real", "name": "Real Friend", "interaction_count": 12,
         "relationship_score": 0.71},
        {"entity_id": "real2", "name": "Other", "interaction_count": 5,
         "relationship_score": 0.33},
    ]
    survivors = sf.filter_relationship_candidates(cands)
    assert {c["entity_id"] for c in survivors} == {"real", "real2"}


def test_explicit_score_history_floor():
    cands = [
        {"entity_id": "a", "interaction_count": 8, "relationship_score": 0.4,
         "score_events": 1},   # only one score event -> no variance -> drop
        {"entity_id": "b", "interaction_count": 8, "relationship_score": 0.5,
         "score_events": 4},
    ]
    survivors = sf.filter_relationship_candidates(cands)
    assert {c["entity_id"] for c in survivors} == {"b"}


def test_enrich_pulls_interaction_count_from_contact_store():
    class FakeStore:
        async def get(self, cid):
            if cid == "known":
                return SimpleNamespace(interaction_count=7, score=0.4)
            return None
        async def find_by_person_node_id(self, cid):
            return None
    cands = [{"entity_id": "known"}, {"entity_id": "unknown"}]
    asyncio.run(sf.enrich_interaction_counts(cands, FakeStore()))
    assert cands[0]["interaction_count"] == 7
    assert "interaction_count" not in cands[1]  # unresolved -> stays observed


# ---------------------------------------------------------------------------
# Item 4: honest why_it_helps (grounded or does not ship)
# ---------------------------------------------------------------------------

def test_ungrounded_thought_does_not_ship():
    from colony_sidecar.proposals.engine import build_from_thinker
    for rationale in ("", "I think this work is worth doing now.",
                      "moves a piece of your work forward"):
        init = SimpleNamespace(description="Do a thing", rationale=rationale,
                               type="task", priority=0.6)
        assert build_from_thinker(init) is None


def test_grounded_thought_ships_with_evidence_based_why():
    from colony_sidecar.proposals.engine import build_from_thinker
    init = SimpleNamespace(
        description="Draft migration plan",
        rationale="The auth service still uses the deprecated v1 token format, "
                  "which breaks next month. Migrating now avoids an outage.",
        type="task", priority=0.7)
    prop = build_from_thinker(init)
    assert prop is not None
    assert "moves a piece of your work forward" not in prop.why_it_helps
    assert prop.why_it_helps  # grounded, from the rationale
    assert "deprecated v1 token format" in prop.finding


def test_research_without_goal_or_finding_does_not_ship():
    from colony_sidecar.proposals.engine import build_from_research
    assert build_from_research("", "some finding", []) is None
    assert build_from_research("a goal", "", []) is None


def test_research_with_evidence_ships_grounded():
    from colony_sidecar.proposals.engine import build_from_research
    prop = build_from_research("best vector DB for us", "Qdrant fits.",
                               [{"title": "bench", "url": "http://x"}])
    assert prop is not None
    assert "best vector DB for us" in prop.why_it_helps


# ---------------------------------------------------------------------------
# Item 3: outbound third-party delivery gate
# ---------------------------------------------------------------------------

def _make_loop():
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.autonomy.config import AutonomyConfig
    cfg = AutonomyConfig()
    cfg.proactive_delivery_enabled = True
    cfg.delivery_shadow_mode = False
    return AutonomyLoop(registry=SimpleNamespace(directives=None), config=cfg)


class _Delivery:
    _rate_limiter = None

    def __init__(self, person_id, target):
        self._pid = person_id
        self._target = target
        self.sent = []

    def preview_initiative(self, payload):
        return {"person_id": self._pid, "urgency": 0.7,
                "channel_hint": "dm", "target": self._target}

    async def push_initiative(self, payload):
        self.sent.append(payload)
        return True

    async def push_to_gateway(self, **kw):
        self.sent.append(kw)
        return True


def test_non_owner_delivery_blocked_without_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    from colony_sidecar.identity.resolver import reset_identity_resolver
    reset_identity_resolver()

    loop = _make_loop()
    delivery = _Delivery("cid-third-party", {"user_chat": "whatsapp:tp-chat"})
    payload = {
        "id": "rel-1", "type": "relationship", "priority": 0.7,
        "title": "Check in with X", "description": "Check in with X.",
        "entity_id": "cid-third-party", "entity_type": "person",
        "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    ok = asyncio.run(loop._route_reachout_delivery(payload, delivery))
    assert ok is False and delivery.sent == []
    reset_identity_resolver()


def test_owner_directed_proposal_not_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    from colony_sidecar.identity.resolver import reset_identity_resolver
    reset_identity_resolver()

    loop = _make_loop()
    delivery = _Delivery("cid-owner", {"user_chat": "whatsapp:owner-chat"})
    payload = {
        "id": "prop-1", "type": "proposal", "priority": 0.7,
        "title": "A finding", "description": "A finding.",
        "entity_id": None, "entity_type": "proposal",
        "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    ok = asyncio.run(loop._route_reachout_delivery(payload, delivery))
    assert ok is True and len(delivery.sent) == 1
    reset_identity_resolver()


def test_non_owner_delivery_allowed_with_standing_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLONY_DELIVERY_TRANSPORT", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    from colony_sidecar.identity.resolver import reset_identity_resolver
    reset_identity_resolver()
    from colony_sidecar.initiatives import standing_approvals
    standing_approvals.grant("outbound_third_party_delivery")

    loop = _make_loop()
    delivery = _Delivery("cid-third-party", {"user_chat": "whatsapp:tp-chat"})
    payload = {
        "id": "rel-2", "type": "relationship", "priority": 0.7,
        "title": "Check in with X", "description": "Check in with X.",
        "entity_id": "cid-third-party", "entity_type": "person",
        "channel_hint": "dm", "context": {},
        "generated_at": "2099-01-01T00:00:00+00:00",
    }
    ok = asyncio.run(loop._route_reachout_delivery(payload, delivery))
    assert ok is True and len(delivery.sent) == 1
    reset_identity_resolver()

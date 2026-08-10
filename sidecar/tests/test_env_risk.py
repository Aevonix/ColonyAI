"""L1.2 — environment-risk classifier: R0..R3, monotone, fail-closed.

Every missing signal (no gateway class, no census, no owner identity, weak
resolution, broken store) grades R3. Lower classes are granted only on
positive, verified evidence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.channels.presence import ConversationPresenceStore
from colony_sidecar.gate.env_risk import (
    R0, R1, R2, R3, classify, env_risk_window_hours, gateway_class)

OWNER = "cid-owner"


class FakeContacts:
    def __init__(self, tiers):
        self._tiers = dict(tiers)

    async def get(self, contact_id):
        tier = self._tiers.get(contact_id)
        if tier is None:
            return None
        return SimpleNamespace(contact_id=contact_id, trust_tier=tier)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    monkeypatch.setenv("COLONY_ENV_RISK_GATEWAY_CLASS",
                       "dm:private,kiosk:embodied,web:public")
    monkeypatch.delenv("COLONY_ENV_RISK_WINDOW_HOURS", raising=False)


def _stores(*sightings, tiers=None):
    """sightings: (conversation, contact, method[, group])"""
    p = ConversationPresenceStore()
    for s in sightings:
        conv, cid, method = s[0], s[1], s[2]
        group = s[3] if len(s) > 3 else ""
        p.record(conv, cid, method=method, group_id=group)
    c = FakeContacts(tiers or {})
    return p, c


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------

def test_gateway_class_default_unclassified(monkeypatch):
    monkeypatch.delenv("COLONY_ENV_RISK_GATEWAY_CLASS", raising=False)
    assert gateway_class("dm") == ""


def test_gateway_class_parsing(monkeypatch):
    monkeypatch.setenv("COLONY_ENV_RISK_GATEWAY_CLASS",
                       "dm:private, WEB:Public ,junk,bad:sorta")
    assert gateway_class("dm") == "private"
    assert gateway_class("web") == "public"
    assert gateway_class("bad") == ""      # unknown class => unclassified
    assert gateway_class("other") == ""


def test_window_hours_default_and_malformed(monkeypatch):
    monkeypatch.delenv("COLONY_ENV_RISK_WINDOW_HOURS", raising=False)
    assert env_risk_window_hours() == 48.0
    monkeypatch.setenv("COLONY_ENV_RISK_WINDOW_HOURS", "banana")
    assert env_risk_window_hours() == 48.0
    monkeypatch.setenv("COLONY_ENV_RISK_WINDOW_HOURS", "-1")
    assert env_risk_window_hours() == 48.0


# ---------------------------------------------------------------------------
# The classes, from positive evidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r0_owner_private(env):
    p, c = _stores(("dm:owner", OWNER, "handle"))
    r = await classify("dm:owner", OWNER, presence_store=p, contacts_store=c)
    assert r.level == R0


@pytest.mark.asyncio
async def test_r1_trusted_private_dm(env):
    p, c = _stores(("dm:alice", OWNER, "handle"),
                   ("dm:alice", "cid-alice", "handle"),
                   tiers={"cid-alice": "trusted"})
    r = await classify("dm:alice", "cid-alice",
                       presence_store=p, contacts_store=c)
    assert r.level == R1


@pytest.mark.asyncio
async def test_r2_known_social_dm(env):
    p, c = _stores(("dm:bob", OWNER, "handle"),
                   ("dm:bob", "cid-bob", "handle"),
                   tiers={"cid-bob": "regular"})
    r = await classify("dm:bob", "cid-bob",
                       presence_store=p, contacts_store=c)
    assert r.level == R2


@pytest.mark.asyncio
async def test_r2_all_trusted_group(env):
    p, c = _stores(("dm:grp", OWNER, "handle", "g1"),
                   ("dm:grp", "cid-a", "handle", "g1"),
                   ("dm:grp", "cid-b", "handle", "g1"),
                   tiers={"cid-a": "trusted", "cid-b": "inner_circle"})
    r = await classify("dm:grp", "cid-a", presence_store=p, contacts_store=c)
    assert r.level == R2


# ---------------------------------------------------------------------------
# R3: every hostile / missing signal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r3_default_without_gateway_classification(env, monkeypatch):
    monkeypatch.delenv("COLONY_ENV_RISK_GATEWAY_CLASS", raising=False)
    p, c = _stores(("dm:owner", OWNER, "handle"))
    r = await classify("dm:owner", OWNER, presence_store=p, contacts_store=c)
    assert r.level == R3
    assert any("gateway-class" in x for x in r.reasons)


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway", ["web:page", "kiosk:lobby"])
async def test_r3_public_and_embodied_gateways(env, gateway):
    p, c = _stores((gateway, OWNER, "handle"))
    r = await classify(gateway, OWNER, presence_store=p, contacts_store=c)
    assert r.level == R3


@pytest.mark.asyncio
async def test_r3_owner_identity_unset(env, monkeypatch):
    monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
    monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
    p, c = _stores(("dm:x", "cid-a", "handle"), tiers={"cid-a": "trusted"})
    r = await classify("dm:x", "cid-a", presence_store=p, contacts_store=c)
    assert r.level == R3
    assert "owner-identity-unset" in r.reasons


@pytest.mark.asyncio
async def test_r3_reader_system_or_empty(env):
    p, c = _stores(("dm:x", OWNER, "handle"))
    for reader in ("system", "", None):
        r = await classify("dm:x", reader, presence_store=p, contacts_store=c)
        assert r.level == R3


@pytest.mark.asyncio
async def test_r3_reader_not_in_census(env):
    p, c = _stores(("dm:x", OWNER, "handle"), tiers={"cid-a": "trusted"})
    r = await classify("dm:x", "cid-a", presence_store=p, contacts_store=c)
    assert r.level == R3
    assert "reader-not-in-census" in r.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["shadow", "client", "scoped_name", ""])
async def test_r3_weak_reader_resolution(env, method):
    p, c = _stores(("dm:x", OWNER, "handle"),
                   ("dm:x", "cid-a", method),
                   tiers={"cid-a": "trusted"})
    r = await classify("dm:x", "cid-a", presence_store=p, contacts_store=c)
    assert r.level == R3


@pytest.mark.asyncio
async def test_r3_shadow_group_member_raises_risk(env):
    """Monotone ratchet: the same R1 DM becomes R3 the moment a shadow
    participant is sighted."""
    p, c = _stores(("dm:alice", OWNER, "handle"),
                   ("dm:alice", "cid-alice", "handle"),
                   tiers={"cid-alice": "trusted", "cid-shadow": "unknown"})
    before = await classify("dm:alice", "cid-alice",
                            presence_store=p, contacts_store=c)
    assert before.level == R1
    p.record("dm:alice", "cid-shadow", method="shadow")
    after = await classify("dm:alice", "cid-alice",
                           presence_store=p, contacts_store=c)
    assert after.level == R3


@pytest.mark.asyncio
async def test_r3_group_member_below_trusted(env):
    p, c = _stores(("dm:grp", OWNER, "handle", "g1"),
                   ("dm:grp", "cid-a", "handle", "g1"),
                   ("dm:grp", "cid-b", "handle", "g1"),
                   tiers={"cid-a": "trusted", "cid-b": "regular"})
    r = await classify("dm:grp", "cid-a", presence_store=p, contacts_store=c)
    assert r.level == R3
    assert any("below-trusted" in x for x in r.reasons)


@pytest.mark.asyncio
async def test_r3_reader_tier_below_regular_dm(env):
    p, c = _stores(("dm:x", OWNER, "handle"),
                   ("dm:x", "cid-p", "handle"),
                   tiers={"cid-p": "peripheral"})
    r = await classify("dm:x", "cid-p", presence_store=p, contacts_store=c)
    assert r.level == R3


@pytest.mark.asyncio
async def test_r3_unresolvable_contact(env):
    p, c = _stores(("dm:x", OWNER, "handle"),
                   ("dm:x", "cid-ghost", "handle"))   # no contact row
    r = await classify("dm:x", "cid-ghost", presence_store=p, contacts_store=c)
    assert r.level == R3


@pytest.mark.asyncio
async def test_r3_on_store_error(env):
    class Broken:
        def census(self, *a, **k):
            raise RuntimeError("io error")

    _, c = _stores(tiers={"cid-a": "trusted"})
    r = await classify("dm:x", "cid-a", presence_store=Broken(),
                       contacts_store=c)
    assert r.level == R3
    assert any(x.startswith("classifier-error") for x in r.reasons)


@pytest.mark.asyncio
async def test_r3_missing_stores(env):
    r = await classify("dm:x", "cid-a", presence_store=None,
                       contacts_store=None)
    assert r.level == R3


# ---------------------------------------------------------------------------
# Owner endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_risk_endpoint(env, monkeypatch):
    p, c = _stores(("dm:alice", OWNER, "handle"),
                   ("dm:alice", "cid-alice", "handle"),
                   tiers={"cid-alice": "trusted"})
    monkeypatch.setattr(host_mod, "_presence_store", p)
    monkeypatch.setattr(host_mod, "_contacts_store", c)
    out = await host_mod.env_risk(conversation_key="dm:alice",
                                  contact_id="cid-alice")
    assert out["label"] == "R1"
    assert out["level"] == R1
    assert {r["contact_id"] for r in out["census"]} == {OWNER, "cid-alice"}
    # identity/topology only — no content-bearing keys in census rows
    assert all(set(r) <= {"contact_id", "method", "group_id", "last_seen_at"}
               for r in out["census"])


@pytest.mark.asyncio
async def test_env_risk_endpoint_fails_closed_without_stores(env, monkeypatch):
    monkeypatch.setattr(host_mod, "_presence_store", None)
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    out = await host_mod.env_risk(conversation_key="dm:alice",
                                  contact_id="cid-alice")
    assert out["label"] == "R3"
    assert out["census"] == []

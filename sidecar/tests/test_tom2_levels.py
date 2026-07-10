"""L1.3 — effective-level resolver: the min-chain of independent brakes.

Any single brake decaying (config, ceiling, environment risk, enforce
evidence, cross-context gate) silently drops the level for the turn; any
error resolves 0. Shipped defaults resolve 0 everywhere.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar import doctor
from colony_sidecar.channels.presence import ConversationPresenceStore
from colony_sidecar.tom import levels
from colony_sidecar.tom.levels import (
    DEFAULT_RISK_CAPS, clear_level_cache, configured_level,
    configured_max_level, parse_risk_caps, resolve_effective_level,
    set_evidence_probe)

OWNER = "cid-owner"


class FakeContacts:
    def __init__(self, tiers):
        self._tiers = dict(tiers)

    async def get(self, contact_id):
        tier = self._tiers.get(contact_id)
        return None if tier is None else SimpleNamespace(
            contact_id=contact_id, trust_tier=tier)


@pytest.fixture(autouse=True)
def _reset():
    clear_level_cache()
    set_evidence_probe(None)
    yield
    clear_level_cache()
    set_evidence_probe(None)


@pytest.fixture()
def r1_world(monkeypatch):
    """An R1 environment: private gateway, owner + trusted strong reader."""
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    monkeypatch.setenv("COLONY_ENV_RISK_GATEWAY_CLASS", "dm:private")
    p = ConversationPresenceStore()
    p.record("dm:alice", OWNER, method="handle")
    p.record("dm:alice", "cid-alice", method="handle")
    c = FakeContacts({"cid-alice": "trusted"})
    return p, c


async def _resolve(p, c, conv="dm:alice", reader="cid-alice", **kw):
    return await resolve_effective_level(
        conv, reader, presence_store=p, contacts_store=c, **kw)


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------

def test_level_defaults(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_LEVEL", raising=False)
    monkeypatch.delenv("COLONY_TOM2_MAX_LEVEL", raising=False)
    assert configured_level() == 0
    assert configured_max_level() == 1


def test_malformed_level_fails_closed(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "two")
    assert configured_level() == 0
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "!!")
    assert configured_max_level() == 0
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "9")
    assert configured_level() == 2          # clamped, not trusted


def test_risk_caps_default(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_RISK_CAPS", raising=False)
    assert parse_risk_caps() == {0: 2, 1: 2, 2: 1, 3: 0}


@pytest.mark.parametrize("raw", [
    "garbage", "0:2,1:2,2:1", "0:2,1:2,2:1,3:9", "0:2,1:2,2:1,4:0",
    "0:2,0:1,2:1,3:0", "0:2,1:2,2:one,3:0",
])
def test_malformed_risk_caps_all_zero(monkeypatch, raw):
    monkeypatch.setenv("COLONY_TOM2_RISK_CAPS", raw)
    assert parse_risk_caps() == {0: 0, 1: 0, 2: 0, 3: 0}


def test_doctor_warns_on_malformed_caps(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_RISK_CAPS", "garbage")
    r = doctor.check_tom2_risk_caps()
    assert r.status == doctor.WARN
    assert "all-0" in r.detail
    monkeypatch.setenv("COLONY_TOM2_RISK_CAPS", DEFAULT_RISK_CAPS)
    assert doctor.check_tom2_risk_caps().status == doctor.PASS
    monkeypatch.delenv("COLONY_TOM2_RISK_CAPS", raising=False)
    assert doctor.check_tom2_risk_caps().status == doctor.PASS


# ---------------------------------------------------------------------------
# The min-chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shipped_defaults_resolve_zero(r1_world, monkeypatch):
    """Default-inert lock: even in a perfect R1 room with live evidence,
    shipped defaults resolve level 0."""
    for var in ("COLONY_TOM2_LEVEL", "COLONY_TOM2_MAX_LEVEL",
                "COLONY_TOM2_RISK_CAPS", "COLONY_TOM2_CROSS_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    set_evidence_probe(lambda gw: True)
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == 0
    assert res.terms["configured"] == 0


@pytest.mark.asyncio
async def test_full_chain_reaches_level_2(r1_world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(lambda gw: gw == "dm")
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == 2
    assert res.env_risk == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("brake, expected", [
    ({"COLONY_TOM2_LEVEL": "0"}, 0),
    ({"COLONY_TOM2_MAX_LEVEL": "1"}, 1),
    ({"COLONY_TOM2_CROSS_CONTEXT": "0"}, 1),
    ({"COLONY_TOM2_RISK_CAPS": "0:2,1:0,2:0,3:0"}, 0),
])
async def test_each_brake_drops_alone(r1_world, monkeypatch, brake, expected):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(lambda gw: True)
    for k, v in brake.items():
        monkeypatch.setenv(k, v)
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == expected


@pytest.mark.asyncio
async def test_no_evidence_probe_caps_at_1(r1_world, monkeypatch):
    """The KEY prerequisite: until the egress net wires a live probe, the
    system cannot exceed level 1 no matter what is configured."""
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == 1
    assert res.terms["enforce_evidence"] == 1


@pytest.mark.asyncio
async def test_probe_error_means_no_evidence(r1_world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")

    def boom(gw):
        raise RuntimeError("probe down")

    set_evidence_probe(boom)
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == 1


@pytest.mark.asyncio
async def test_hostile_environment_resolves_zero(monkeypatch):
    """R3 (default grade: no gateway classes, no census) => cap 0."""
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", OWNER)
    monkeypatch.delenv("COLONY_ENV_RISK_GATEWAY_CLASS", raising=False)
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(lambda gw: True)
    res = await _resolve(ConversationPresenceStore(), FakeContacts({}))
    assert res.level == 0
    assert res.env_risk == 3


@pytest.mark.asyncio
async def test_resolver_error_resolves_zero(r1_world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")

    def boom():
        raise RuntimeError("caps store on fire")

    monkeypatch.setattr(levels, "parse_risk_caps", boom)
    p, c = r1_world
    res = await _resolve(p, c)
    assert res.level == 0
    assert any(r.startswith("resolver-error") for r in res.reasons)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_serves_within_ttl_and_clears(r1_world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    set_evidence_probe(lambda gw: True)
    p, c = r1_world
    first = await _resolve(p, c)
    assert first.level == 2
    # brake decays; cached grade may persist up to the TTL...
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "0")
    assert (await _resolve(p, c)).level == 2
    # ...but never past an explicit clear, and never with use_cache=False
    assert (await _resolve(p, c, use_cache=False)).level == 0
    clear_level_cache()
    assert (await _resolve(p, c)).level == 0

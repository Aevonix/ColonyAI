"""Per-check enforce allowlist + circuit breaker (H6.3).

Enforce ramps one check at a time (COLONY_GUARD_ENFORCE_CHECKS, default
secret_leak); a rolling-24h block counter trips a breaker that suspends
suppression (fails open — it can only ever weaken enforcement, never latch
INTO enforce).
"""

from __future__ import annotations

import pytest

from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

SECRET = "the ssn is 123-45-6789"               # trips secret_leak (PII scan)
DISCLOSURE = "his home address is on file"      # trips disclosure_tier at group_guest


def _guard():
    return ResponseGuard(default_mode=GuardMode.ENFORCE)


@pytest.mark.asyncio
async def test_default_allowlist_blocks_only_secret_leak(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    guard = _guard()
    r1 = await guard.evaluate(response_text=SECRET,
                              trust_tier=TrustTier.GROUP_GUEST,
                              target_gateway="rcs")
    assert r1.decision == "revise"
    assert any(f.check == "secret_leak" for f in r1.findings)
    # a non-allowlisted check keeps shadow semantics inside enforce:
    # observed and audited, never suppressing
    r2 = await guard.evaluate(response_text=DISCLOSURE,
                              trust_tier=TrustTier.GROUP_GUEST,
                              target_gateway="rcs")
    assert r2.decision == "allow"
    assert any(f.check == "disclosure_tier" for f in r2.findings)


@pytest.mark.asyncio
async def test_allowlist_all_restores_full_enforcement(monkeypatch):
    """Flag-off regression lock: =all is the legacy every-check enforce."""
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    guard = _guard()
    r = await guard.evaluate(response_text=DISCLOSURE,
                             trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs")
    assert r.decision == "revise"


@pytest.mark.asyncio
async def test_shadow_never_blocks_regardless_of_allowlist(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    guard = ResponseGuard(default_mode=GuardMode.SHADOW)
    r = await guard.evaluate(response_text=SECRET,
                             trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs")
    assert r.decision == "allow"
    assert any(f.check == "secret_leak" for f in r.findings)


@pytest.mark.asyncio
async def test_breaker_trips_after_n_blocks(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    monkeypatch.setenv("COLONY_GUARD_TRIP_BLOCKS", "3")
    guard = _guard()
    for _ in range(3):
        r = await guard.evaluate(response_text=SECRET, target_gateway="rcs")
        assert r.decision == "revise"
    # 4th block would exceed the trip threshold: breaker opens, suppression
    # suspends, but the finding is still observed (audit keeps measuring)
    r4 = await guard.evaluate(response_text=SECRET, target_gateway="rcs")
    assert r4.decision == "allow"
    assert any(f.check == "secret_leak" for f in r4.findings)
    status = guard.breaker_status()
    assert status["tripped"] is True
    assert status["blocks_24h"] == 3
    assert status["enforce_checks"] == ["secret_leak"]


@pytest.mark.asyncio
async def test_breaker_off_flag_keeps_enforcing(monkeypatch):
    """Flag-off regression lock: BREAKER=off never suspends suppression."""
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    monkeypatch.setenv("COLONY_GUARD_BREAKER", "off")
    monkeypatch.setenv("COLONY_GUARD_TRIP_BLOCKS", "1")
    guard = _guard()
    for _ in range(3):
        r = await guard.evaluate(response_text=SECRET, target_gateway="rcs")
        assert r.decision == "revise"
    assert guard.breaker_status()["tripped"] is False


@pytest.mark.asyncio
async def test_trip_blocks_zero_disables_tripping(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    monkeypatch.setenv("COLONY_GUARD_TRIP_BLOCKS", "0")
    guard = _guard()
    for _ in range(5):
        r = await guard.evaluate(response_text=SECRET, target_gateway="rcs")
        assert r.decision == "revise"

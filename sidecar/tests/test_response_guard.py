"""ResponseGuard — the rebuilt messaging response gate: shadow vs enforce, hard voice
exclusion, fail-open on any internal fault, and pluggable provenance cross-context check.
"""

import pytest

from colony_sidecar.gate.response_guard import (
    CrossContextGuard,
    GuardFinding,
    GuardMode,
    ResponseGuard,
    to_gate_tier,
)
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

LEAK = "his home address is on file"   # trips L4 private-detail at group_guest/peripheral


@pytest.mark.asyncio
async def test_excluded_gateway_is_never_gated():
    # a deployment supplies the gateways to skip (e.g. its voice path); Colony hardcodes none
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, excluded_gateways={"voice"})
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="voice")
    assert r.decision == "allow" and not r.findings
    # the same leak on a non-excluded gateway IS caught, proving exclusion is what skipped it
    r2 = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                              target_gateway="rcs")
    assert r2.decision == "revise"


@pytest.mark.asyncio
async def test_shadow_reports_but_never_blocks():
    guard = ResponseGuard(default_mode=GuardMode.SHADOW)
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs")
    assert r.decision == "allow"                                   # shadow never blocks
    assert any(f.check == "disclosure_tier" for f in r.findings)   # but it observed the leak


@pytest.mark.asyncio
async def test_enforce_revises_on_blocking_finding():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs")
    assert r.decision == "revise" and r.blocked is True


@pytest.mark.asyncio
async def test_clean_text_allows():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r = await guard.evaluate(response_text="sure, see you at 6",
                             trust_tier=TrustTier.GROUP_GUEST, target_gateway="rcs")
    assert r.decision == "allow" and not r.findings


@pytest.mark.asyncio
async def test_fail_open_when_a_check_raises():
    class Boom(CrossContextGuard):
        async def check(self, **kw):
            raise RuntimeError("provenance store down")
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, cross_context=Boom())
    r = await guard.evaluate(response_text="hi", trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs")
    assert r.decision == "allow"                                   # broken check skipped, not fatal


@pytest.mark.asyncio
async def test_cross_context_findings_flow_through():
    class Leaky(CrossContextGuard):
        async def check(self, **kw):
            return [GuardFinding("cross_context", "block", "entity 'Robin' from another chat")]
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, cross_context=Leaky())
    r = await guard.evaluate(response_text="Robin said hi", trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs")
    assert r.decision == "revise"
    assert any(f.check == "cross_context" for f in r.findings)


def test_tier_coercion():
    assert to_gate_tier("group_guest") is TrustTier.GROUP_GUEST
    assert to_gate_tier("acquaintance") is TrustTier.PERIPHERAL
    assert to_gate_tier("unknown") is TrustTier.PERIPHERAL
    assert to_gate_tier(TrustTier.TRUSTED) is TrustTier.TRUSTED
    assert to_gate_tier("garbage") is TrustTier.REGULAR

"""ResponseGuard exact-surface, monotonic-mode and failure contracts.
"""

import hashlib

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


@pytest.fixture(autouse=True)
def _full_enforce(monkeypatch):
    """These tests predate the per-check enforce allowlist (H6.3): pin the
    legacy all-checks enforcement they were written against. The allowlist
    default (secret_leak only) is covered in test_guard_enforce_policy.py."""
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")


@pytest.mark.asyncio
async def test_gateway_label_cannot_bypass_guard():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, excluded_gateways={"voice"})
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="voice", surface="text_chat")
    assert r.decision == "revise"


@pytest.mark.asyncio
async def test_realtime_voice_surface_bypasses_without_evaluation():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r = await guard.evaluate(
        response_text=LEAK,
        trust_tier=TrustTier.GROUP_GUEST,
        target_gateway="rcs",
        surface="realtime_voice",
    )
    assert r.decision == "allow" and not r.findings
    assert r.mode == "excluded"
    assert r.applicability == "excluded"
    assert r.guard_status == "bypassed"


@pytest.mark.asyncio
async def test_shadow_reports_but_never_blocks():
    guard = ResponseGuard(default_mode=GuardMode.SHADOW)
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "allow"                                   # shadow never blocks
    assert any(f.check == "disclosure_tier" for f in r.findings)   # but it observed the leak


@pytest.mark.asyncio
async def test_enforce_revises_on_blocking_finding():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r = await guard.evaluate(response_text=LEAK, trust_tier=TrustTier.GROUP_GUEST,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "revise" and r.blocked is True


@pytest.mark.asyncio
async def test_clean_text_allows():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r = await guard.evaluate(response_text="sure, see you at 6",
                             trust_tier=TrustTier.GROUP_GUEST, target_gateway="rcs",
                             surface="text_chat")
    assert r.decision == "allow" and not r.findings
    assert r.candidate_digest == hashlib.sha256(
        "sure, see you at 6".encode("utf-8")
    ).hexdigest()
    assert r.to_dict()["candidate_digest"] == r.candidate_digest


@pytest.mark.asyncio
async def test_enforce_fails_closed_when_configured_check_raises():
    class Boom(CrossContextGuard):
        async def check(self, **kw):
            raise RuntimeError("provenance store down")
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, cross_context=Boom())
    r = await guard.evaluate(response_text="hi", trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "block"
    assert r.guard_status == "degraded"
    assert any(f.check == "guard_unavailable" and f.severity == "block"
               for f in r.findings)


@pytest.mark.asyncio
async def test_shadow_fails_open_when_configured_check_raises():
    class Boom(CrossContextGuard):
        async def check(self, **kw):
            raise RuntimeError("provenance store down")
    guard = ResponseGuard(default_mode=GuardMode.SHADOW, cross_context=Boom())
    r = await guard.evaluate(response_text="hi", trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "allow"
    assert r.guard_status == "degraded"
    assert any(f.check == "guard_unavailable" and f.severity == "warn"
               for f in r.findings)


@pytest.mark.asyncio
async def test_enforce_fails_closed_when_injection_detector_absent():
    """The injection detector is configured in __init__; None means its
    ruleset failed to load. Enforce must mark it unavailable and fail
    closed, not silently skip the check (which previously allowed)."""
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    guard._injection = None    # simulate ruleset load failure
    r = await guard.evaluate(response_text="sure, see you at 6",
                             trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "block"
    assert r.guard_status == "degraded"
    assert any(f.check == "guard_unavailable" and "injection" in f.reason
               for f in r.findings)


@pytest.mark.asyncio
async def test_request_cannot_weaken_enforce_but_can_strengthen_shadow():
    enforce = ResponseGuard(default_mode=GuardMode.ENFORCE)
    r1 = await enforce.evaluate(
        surface="text_chat",
        response_text=LEAK,
        trust_tier=TrustTier.GROUP_GUEST,
        mode=GuardMode.SHADOW,
    )
    assert r1.mode == "enforce" and r1.decision == "revise"

    shadow = ResponseGuard(default_mode=GuardMode.SHADOW)
    r2 = await shadow.evaluate(
        surface="text_chat",
        response_text=LEAK,
        trust_tier=TrustTier.GROUP_GUEST,
        mode=GuardMode.ENFORCE,
    )
    assert r2.mode == "enforce" and r2.decision == "revise"


@pytest.mark.asyncio
async def test_cross_context_findings_flow_through():
    class Leaky(CrossContextGuard):
        async def check(self, **kw):
            return [GuardFinding("cross_context", "block", "entity 'Robin' from another chat")]
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, cross_context=Leaky())
    r = await guard.evaluate(response_text="Robin said hi", trust_tier=TrustTier.REGULAR,
                             target_gateway="rcs", surface="text_chat")
    assert r.decision == "revise"
    assert any(f.check == "cross_context" for f in r.findings)


def test_tier_coercion():
    assert to_gate_tier("group_guest") is TrustTier.GROUP_GUEST
    assert to_gate_tier("acquaintance") is TrustTier.PERIPHERAL
    assert to_gate_tier("unknown") is TrustTier.PERIPHERAL
    assert to_gate_tier(TrustTier.TRUSTED) is TrustTier.TRUSTED
    assert to_gate_tier("garbage") is TrustTier.REGULAR


@pytest.mark.asyncio
async def test_authorized_cross_context_is_exempt_and_audited():
    from colony_sidecar.gate.guard_audit import GuardAuditStore
    from colony_sidecar.gate.response_guard import CrossContextGuard, GuardFinding

    class Leaky(CrossContextGuard):
        async def check(self, **kw):
            return [GuardFinding("cross_context", "block", "entity X from another chat", "[x]")]

    audit = GuardAuditStore(":memory:")
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, cross_context=Leaky(), audit_store=audit)

    # unauthorized cross-context -> revise (blocked) in enforce
    r1 = await guard.evaluate(surface="text_chat", response_text="re X", target_gateway="rcs",
                              conversation_key="rcs:B", mentioned_entities=["X"], authorized=False)
    assert r1.decision == "revise"

    # owner-directed (authorized) -> exempt: allowed, finding downgraded to info
    r2 = await guard.evaluate(surface="text_chat", response_text="re X", target_gateway="rcs",
                              conversation_key="rcs:B", mentioned_entities=["X"], authorized=True)
    assert r2.decision == "allow"
    assert any(f.check == "cross_context" and f.severity == "info" for f in r2.findings)

    # both events tracked, split by authorized
    s = audit.summary()
    assert s["total"] == 2 and s["authorized_transfers"] == 1 and s["unauthorized_flags"] == 1

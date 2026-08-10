"""L3.2 — tom2_epistemic egress net: block-severity, inert without taints.

Three block triggers while a level-2 injection taint is live: (a) naming a
tainted subject inside an epistemic-claim pattern, (b) self-referential
modeling claims, (c) tainted fact TEXT surfacing toward a different
conversation. With no active taint the check answers [] from a clock
comparison — zero cost, zero false positives — which is why it ships on
the DEFAULT enforce allowlist. Also covered: candidate-verdict audit rows
never masquerade as applied egress enforcement evidence (the L2 prerequisite
remains deliberately unmet).
"""

from __future__ import annotations

import time

import pytest

from colony_sidecar.gate.guard_audit import GuardAuditStore
from colony_sidecar.gate.layers.tom2_epistemic import Tom2EpistemicGuard
from colony_sidecar.gate.response_guard import (
    GuardMode, ResponseGuard, enforce_allowlist)
from colony_sidecar.gate.taint import TaintRegistry
from colony_sidecar.tom.facts import SharedFactsStore

CONV = "dm:cid-alice"
OTHER_CONV = "dm:cid-carol"


@pytest.fixture()
def world(tmp_path):
    facts = SharedFactsStore(db_path=str(tmp_path / "facts.db"))
    f = facts.create_fact(contact_id="cid-alice",
                          fact="the launch moved to friday",
                          confidence=0.9)
    taints = TaintRegistry()
    return taints, facts, f


def _taint(taints, f, conv=CONV):
    taints.register(conv, "cid-bob", subject_names=["Bob Smith", "bobby"],
                    fact_ref=f["id"], kind="unaware_of")


async def _check(taints, facts, text, conv=CONV):
    return await Tom2EpistemicGuard(taints, facts_store=facts).check(
        response_text=text, conversation_key=conv)


# ---------------------------------------------------------------------------
# Inertness (the reason it can sit on the default enforce allowlist)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inert_without_active_taint(world):
    taints, facts, _ = world
    for text in ("Bob doesn't know about the launch",
                 "I keep track of what everyone knows",
                 "the launch moved to friday"):
        assert await _check(taints, facts, text) == []


@pytest.mark.asyncio
async def test_inert_after_taint_expiry(world):
    taints, facts, f = world
    taints.register(CONV, "cid-bob", subject_names=["Bob"],
                    fact_ref=f["id"], kind="unaware_of", ttl_seconds=0.05)
    time.sleep(0.08)
    out = await _check(taints, facts, "Bob hasn't heard about it")
    assert out == []


@pytest.mark.asyncio
async def test_internal_error_with_live_taint_is_not_clean(world):
    """While a taint is live, an internal fault must NOT answer [] ("clean"):
    it propagates so ResponseGuard records the check as unavailable. With no
    taint (provably inert) an error still answers []."""
    class _BrokenRegistry:
        def __init__(self, active: bool) -> None:
            self._active = active

        def any_active(self):
            return self._active

        def all_active(self):
            raise RuntimeError("taint db corrupt")

    _, facts, _ = world
    guard = Tom2EpistemicGuard(_BrokenRegistry(True), facts_store=facts)
    with pytest.raises(RuntimeError):
        await guard.check(response_text="Bob doesn't know about the launch",
                          conversation_key=CONV)
    inert = Tom2EpistemicGuard(_BrokenRegistry(False), facts_store=facts)
    assert await inert.check(response_text="anything",
                             conversation_key=CONV) == []


def test_default_allowlist_includes_tom2_epistemic(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    assert enforce_allowlist() == frozenset({"secret_leak",
                                             "tom2_epistemic"})


# ---------------------------------------------------------------------------
# (a) tainted subject + epistemic-claim pattern
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "well, Bob Smith doesn't know about that yet",
    "bobby hasn't heard the news",
    "Bob Smith is unaware of the change",
    "please don't tell Bob Smith",
    "let's keep it from bobby for now",
    "Bob Smith hasn't been told",
    "bobby has no idea",
    "Bob Smith already knows, actually",       # 'knows' is also a claim
])
async def test_subject_plus_epistemic_pattern_blocks(world, text):
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts, text)
    assert any(x.check == "tom2_epistemic" and x.severity == "block"
               for x in out), text


@pytest.mark.asyncio
async def test_subject_name_without_epistemic_claim_passes(world):
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts,
                       "I had lunch with Bob Smith on tuesday")
    assert out == []


@pytest.mark.asyncio
async def test_untainted_name_with_epistemic_claim_passes(world):
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts, "Carol doesn't know about the picnic")
    assert out == []


@pytest.mark.asyncio
async def test_claim_blocks_even_in_a_different_conversation(world):
    """The prior is hot GLOBALLY for its TTL: voicing it anywhere leaks."""
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts, "bobby hasn't heard yet",
                       conv=OTHER_CONV)
    assert any(x.check == "tom2_epistemic" for x in out)


# ---------------------------------------------------------------------------
# (b) self-referential modeling claims
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "I keep track of what everyone knows",
    "I model who knows what around here",
    "my model of who knows this is pretty good",
    "that's in my epistemic prior",
    "I was given a silent prior about this",
])
async def test_self_modeling_claim_blocks_while_taint_live(world, text):
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts, text)
    assert any(x.check == "tom2_epistemic" and x.severity == "block"
               for x in out), text


# ---------------------------------------------------------------------------
# (c) tainted fact text toward a DIFFERENT conversation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fact_text_spill_to_other_conversation_blocks(world):
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts,
                       "by the way, the launch moved to friday",
                       conv=OTHER_CONV)
    assert any("different conversation" in x.reason for x in out)
    # the finding never carries the fact text itself
    assert all("friday" not in (x.excerpt or "") for x in out)


@pytest.mark.asyncio
async def test_fact_text_in_its_own_conversation_passes(world):
    """The reader OWNS this fact in the tainted conversation (H3.5
    guarantees that) — repeating it there is not a spill."""
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts,
                       "right, the launch moved to friday", conv=CONV)
    assert out == []


# ---------------------------------------------------------------------------
# Known miss (documented limitation, asserted honestly)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_known_miss_paraphrase_escapes_the_lexical_net(world):
    """DOCUMENTED LIMITATION: the (a)/(b) patterns are lexical. A paraphrase
    of the epistemic claim — no pattern word, no fact text — escapes this
    net. The structural guarantee lives upstream (the renderer can only
    inject fact text the reader already owns), so the escape leaks phrasing
    pressure about a known-to-the-reader subject, never new content. If
    this test ever FAILS, the net got smarter — update this documentation.
    """
    taints, facts, f = world
    _taint(taints, f)
    out = await _check(taints, facts,
                       "Bob Smith is still in the dark about it")
    assert out == []            # the miss, asserted


# ---------------------------------------------------------------------------
# ResponseGuard integration: block-severity under the default allowlist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_enforces_tom2_epistemic_by_default(world, monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    taints, facts, f = world
    _taint(taints, f)
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          tom2_epistemic=Tom2EpistemicGuard(
                              taints, facts_store=facts))
    r = await guard.evaluate(surface="text_chat", response_text="bobby hasn't heard the news",
                             target_gateway="dm", conversation_key=CONV)
    assert r.decision == "revise"
    assert any(x.check == "tom2_epistemic" for x in r.findings)
    # clean traffic (no taint hit) is untouched
    r2 = await guard.evaluate(surface="text_chat", response_text="see you at the picnic",
                              target_gateway="dm", conversation_key=CONV)
    assert r2.decision == "allow" and r2.findings == []


@pytest.mark.asyncio
async def test_registry_fault_marks_check_unavailable(world):
    """A faulted registry cannot prove inertness, so the check surfaces as
    unavailable — enforce fails closed instead of silently allowing."""
    class _Boom:
        def any_active(self):
            raise RuntimeError("registry down")

    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          tom2_epistemic=Tom2EpistemicGuard(_Boom()))
    r = await guard.evaluate(surface="text_chat", response_text="hello", target_gateway="dm")
    assert r.decision == "block"
    assert any(f.check == "guard_unavailable" and "tom2_epistemic" in f.reason
               for f in r.findings)


# ---------------------------------------------------------------------------
# Candidate verdicts never satisfy the applied-enforcement prerequisite
# ---------------------------------------------------------------------------

def _seed_enforce_rows(audit, gateway="dm", n=3):
    from colony_sidecar.gate.surface_policy import POLICY_DIGEST, POLICY_ID

    for _ in range(n):
        audit.record(conversation_key=f"{gateway}:x", mode="enforce",
                     decision="revise", authorized=False,
                     checks=["secret_leak"], entities=[], gateway=gateway,
                     surface="text_chat", policy_id=POLICY_ID,
                     policy_digest=POLICY_DIGEST)


def test_probe_false_even_with_current_policy_verdict_rows(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    audit = GuardAuditStore()
    _seed_enforce_rows(audit)
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, audit_store=audit)
    probe = guard.evidence_probe()
    assert probe("dm") is False
    assert probe("other-gateway") is False       # silence proves nothing


def test_probe_false_when_check_not_allowlisted(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "secret_leak")
    audit = GuardAuditStore()
    _seed_enforce_rows(audit)
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, audit_store=audit)
    assert guard.evidence_probe()("dm") is False


def test_probe_false_when_breaker_open(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    monkeypatch.setenv("COLONY_GUARD_TRIP_BLOCKS", "1")
    audit = GuardAuditStore()
    _seed_enforce_rows(audit)
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE, audit_store=audit)
    guard._block_times.append(time.time())       # trip it
    assert guard.evidence_probe()("dm") is False


def test_probe_false_without_audit_store(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_ENFORCE_CHECKS", raising=False)
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    assert guard.evidence_probe()("dm") is False

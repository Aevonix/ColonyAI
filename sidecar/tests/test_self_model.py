"""Self-model / trust engine (item 4 + Amendment 1)."""

from __future__ import annotations

import time

from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine, floor_class,
    self_brief,
)


def _fill(store, domain, wins=0, losses=0, timeouts=0, shadow=False):
    for _ in range(wins):
        store.record(domain, "success", shadow=shadow)
    for _ in range(losses):
        store.record(domain, "failure", shadow=shadow)
    for _ in range(timeouts):
        store.record(domain, "timeout", shadow=shadow)


# ---------------------------------------------------------------------------
# CompetenceStore
# ---------------------------------------------------------------------------

def test_record_math_and_rates():
    s = CompetenceStore()
    _fill(s, "research", wins=4, losses=1)
    d = s.get("research")
    assert d["n"] == 5
    assert d["success_rate"] == 0.8
    assert d["timeout_rate"] == 0.0


def test_ewma_latency():
    s = CompetenceStore()
    s.record("x", "success", latency_secs=10.0)
    assert s.get("x")["ewma_latency_secs"] == 10.0
    s.record("x", "success", latency_secs=20.0)
    # 0.3 * 20 + 0.7 * 10 = 13
    assert abs(s.get("x")["ewma_latency_secs"] - 13.0) < 0.01


def test_events_windowing_and_shadow_flag():
    s = CompetenceStore()
    s.record("d", "success", shadow=True)
    s.record("d", "failure")
    assert len(s.events("d")) == 2
    assert len(s.events("d", include_shadow=False)) == 1
    assert s.events("d", since=time.time() + 10) == []


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def test_brief_thresholds():
    s = CompetenceStore()
    _fill(s, "research", wins=8, losses=1)          # reliable
    _fill(s, "scheduling", wins=1, losses=3)        # weak
    _fill(s, "coding", wins=2, timeouts=2)          # timeout-prone
    text = self_brief(s.snapshot(), {"total": 2, "active_initiatives": 1,
                                     "active_projects": 1, "queued_jobs": 0})
    assert "research" in text and "reliably" in text
    assert "scheduling" in text and "fail" in text
    assert "coding" in text and "Timeout-prone" in text
    assert "2 in flight" in text


def test_brief_empty_without_evidence():
    assert self_brief([], {"total": 0}) == ""


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def test_journal_roundtrip_and_today():
    j = ActionJournal()
    jid = j.record("directed:read", "audited a repo",
                   reasoning="read-only auto", confidence=0.7,
                   decision="acted", ref="stask-1")
    assert jid > 0
    j.set_outcome(jid, "clean")
    entries = j.today()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "clean"
    assert j.recent(domain="directed:read")[0]["ref"] == "stask-1"
    assert j.recent(domain="other") == []


# ---------------------------------------------------------------------------
# TrustEngine: confidence, gate, graduation, breaker, floor
# ---------------------------------------------------------------------------

def test_confidence_laplace_and_violation_penalty():
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    assert abs(t.confidence("new") - 0.5) < 0.01     # no evidence -> 0.5 prior
    _fill(store, "good", wins=8)
    assert t.confidence("good") > 0.85
    store.record("bad", "failure", violation=True)
    assert t.confidence("bad") < 0.35


def test_gate_floor_always_asks_even_with_perfect_record():
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    _fill(store, "ops", wins=20)
    t.set_stage("ops", "act_first", notify=False)
    out = t.gate("ops", "wire money to the vendor for $500",
                 default_stage="act_first")
    assert out["decision"] == "ask"
    assert out["floor"] == "money_movement"


def test_floor_classes():
    assert floor_class("rm -rf the build directory") == "irreversible_deletion"
    assert floor_class("rotate the api key for the service") == "credential_change"
    assert floor_class("send a bulk message to all contacts") == "bulk_third_party_messaging"
    assert floor_class("summarize recent commits") is None


def test_gate_stages():
    store = CompetenceStore()
    j = ActionJournal()
    t = TrustEngine(store, journal=j)
    # unset domain uses the caller default
    assert t.gate("a", "analyze data", default_stage="shadow")["decision"] == "hold"
    assert t.gate("a", "analyze data", default_stage="ask_first")["decision"] == "ask"
    # act_first with strong record acts; weak record asks
    _fill(store, "strong", wins=10)
    t.set_stage("strong", "act_first", notify=False)
    assert t.gate("strong", "analyze data")["decision"] == "act"
    t.set_stage("weak", "act_first", notify=False)
    _fill(store, "weak", wins=1, losses=4)
    assert t.gate("weak", "analyze data")["decision"] == "ask"
    # every gate call journaled
    assert len(j.recent(limit=50)) >= 4


def test_autograduation_shadow_to_ask_to_act(monkeypatch):
    monkeypatch.setenv("COLONY_TRUST_AUTOGRADUATE", "true")
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=t)
    # calibration: 3 clean shadow runs graduate shadow -> ask_first
    for _ in range(3):
        sm.record("projects", "success", shadow=True)
    assert t.stage("projects") == "ask_first"
    assert any(not n["demotion"] for n in t.pending_notices)
    # real track record graduates ask_first -> act_first
    for _ in range(6):
        sm.record("projects", "success")
    assert t.stage("projects") == "act_first"


def test_circuit_breaker_demotes_on_violation():
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=t)
    t.set_stage("directed:x", "act_first", notify=False)
    sm.record("directed:x", "failure", violation=True)
    assert t.stage("directed:x") == "ask_first"
    assert any(n["demotion"] for n in t.pending_notices)


def test_circuit_breaker_demotes_on_clustered_failures():
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=t)
    t.set_stage("flaky", "act_first", notify=False)
    for _ in range(3):
        sm.record("flaky", "failure")
    assert t.stage("flaky") == "ask_first"


def test_shadow_events_do_not_earn_act_first(monkeypatch):
    monkeypatch.setenv("COLONY_TRUST_AUTOGRADUATE", "true")
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=t)
    for _ in range(10):
        sm.record("simulated", "success", shadow=True)
    # calibration promotes to ask_first only; act_first needs REAL outcomes
    assert t.stage("simulated") == "ask_first"


def test_autograduate_disable(monkeypatch):
    monkeypatch.setenv("COLONY_TRUST_AUTOGRADUATE", "false")
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=t)
    for _ in range(5):
        sm.record("held", "success", shadow=True)
    assert t.stage("held") == "shadow"


def test_trust_notices_durable_across_instances(tmp_path):
    db = str(tmp_path / "trust.db")
    store = CompetenceStore()
    t = TrustEngine(store, db_path=db, journal=ActionJournal())
    t.set_stage("research", "ask_first", reason="calibrated")
    # a fresh engine (post-restart) still sees the undelivered notice
    t2 = TrustEngine(CompetenceStore(), db_path=db, journal=ActionJournal())
    notices = t2.undelivered_notices()
    assert len(notices) == 1 and notices[0]["domain"] == "research"
    t2.mark_notice_delivered(notices[0]["id"])
    assert t2.undelivered_notices() == []


# ---------------------------------------------------------------------------
# Adaptive delivery cap
# ---------------------------------------------------------------------------

def test_delivery_cap_earned_and_bounded(monkeypatch):
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    assert t.delivery_cap(3) == 3          # no track record yet
    for _ in range(30):
        store.record("delivery", "success")
    cap = t.delivery_cap(3)
    assert cap > 3
    monkeypatch.setenv("COLONY_TRUST_DELIVERY_CAP_MAX", "4")
    assert t.delivery_cap(3) == 4          # bounded by the max


def test_rate_limiter_uses_cap_provider():
    from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter
    rl = DeliveryRateLimiter(max_per_day=1, cooldown_hours=0,
                             quiet_start_hour=0, quiet_end_hour=0,
                             cap_provider=lambda base: base + 1)
    rl.record_delivery("p1")
    allowed, reason = rl.can_deliver("p1")
    assert allowed  # base cap of 1 is raised to 2 by the provider
    rl.record_delivery("p1")
    allowed, reason = rl.can_deliver("p1")
    assert not allowed and "daily_limit" in reason


# ---------------------------------------------------------------------------
# Load + status
# ---------------------------------------------------------------------------

class _FakeInitStore:
    def count(self, status=None):
        return 2


class _FakeReg:
    initiative_store = _FakeInitStore()
    project_engine = None
    task_queue = None


def test_load_counts_live_reads():
    sm = SelfModel(CompetenceStore(), registry=_FakeReg())
    load = sm.load()
    assert load["active_initiatives"] == 2
    assert load["total"] == 2


def test_status_includes_trust():
    store = CompetenceStore()
    t = TrustEngine(store, journal=ActionJournal())
    t.set_stage("x", "ask_first", notify=False)
    sm = SelfModel(store, trust=t)
    st = sm.status()
    assert any(r["domain"] == "x" for r in st["trust"])


# ---------------------------------------------------------------------------
# Calibration feeds confidence (stated-vs-realized is consumed, not decorative)
# ---------------------------------------------------------------------------

def test_overconfidence_discounts_trust_confidence():
    honest = CompetenceStore()
    braggart = CompetenceStore()
    # Identical realized track records (3 wins / 3 losses)...
    for s in (honest, braggart):
        for i in range(3):
            s.record("d", "success", stated_confidence=0.5 if s is honest else 0.95)
            s.record("d", "failure", stated_confidence=0.5 if s is honest else 0.95)
    t_honest = TrustEngine(honest)
    t_braggart = TrustEngine(braggart)
    # ...but the domain that STATED 0.95 while realizing 0.5 is overconfident
    # and earns less trust than the well-calibrated one.
    assert t_braggart.confidence("d") < t_honest.confidence("d")


def test_underconfidence_never_penalized():
    humble = CompetenceStore()
    plain = CompetenceStore()
    for i in range(6):
        humble.record("d", "success", stated_confidence=0.2)  # understates
        plain.record("d", "success")
    assert TrustEngine(humble).confidence("d") == TrustEngine(plain).confidence("d")


def test_calibration_needs_five_events():
    s = CompetenceStore()
    for i in range(3):
        s.record("d", "failure", stated_confidence=0.99)
    base = CompetenceStore()
    for i in range(3):
        base.record("d", "failure")
    # Below the n>=5 evidence bar the penalty must not engage.
    assert TrustEngine(s).confidence("d") == TrustEngine(base).confidence("d")

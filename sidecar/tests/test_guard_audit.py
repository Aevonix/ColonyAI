"""GuardAuditStore: records every finding-bearing evaluation (any check), keeps a
daily total-evaluations counter, and summarizes per-check counts + would_block_rate
over 24h/7d/14d so a false-positive budget can be judged in shadow (H6.1)."""
import pytest

from colony_sidecar.gate.guard_audit import GuardAuditStore


def test_records_and_summarizes():
    a = GuardAuditStore(":memory:")
    a.record(conversation_key="rcs:B", mode="shadow", decision="allow", authorized=False,
             checks=["cross_context"], entities=["[falcon]"], response_text="re Falcon",
             would_block=True)
    a.record(conversation_key="rcs:C", mode="shadow", decision="allow", authorized=True,
             checks=["cross_context"], entities=["[falcon]"], response_text="sharing as asked")
    s = a.summary()
    assert s["total"] == 2
    assert s["authorized_transfers"] == 1
    assert s["unauthorized_flags"] == 1
    assert len(a.recent(authorized=True)) == 1
    assert a.recent(authorized=True)[0]["conversation_key"] == "rcs:C"


def test_eval_counter_and_windowed_rates():
    a = GuardAuditStore(":memory:")
    for _ in range(10):
        a.count_evaluation()
    a.record(conversation_key=None, mode="shadow", decision="allow", authorized=False,
             checks=["secret_leak"], entities=[], would_block=True)
    a.record(conversation_key=None, mode="shadow", decision="allow", authorized=False,
             checks=["injection"], entities=[], would_block=False)
    s = a.summary()
    for w in ("24h", "7d", "14d"):
        win = s["windows"][w]
        assert win["evaluations"] == 10
        assert win["flagged_events"] == 2
        assert win["would_block"] == 1
        assert win["would_block_rate"] == pytest.approx(0.1)
        assert win["by_check"] == {"secret_leak": 1, "injection": 1}


def test_would_block_rate_none_without_evaluations():
    a = GuardAuditStore(":memory:")
    assert a.summary()["windows"]["24h"]["would_block_rate"] is None


def test_recent_check_filter():
    a = GuardAuditStore(":memory:")
    a.record(conversation_key="k1", mode="shadow", decision="allow", authorized=False,
             checks=["secret_leak", "injection"], entities=[])
    a.record(conversation_key="k2", mode="shadow", decision="allow", authorized=False,
             checks=["cross_context"], entities=[])
    assert [e["conversation_key"] for e in a.recent(check="secret_leak")] == ["k1"]
    assert [e["conversation_key"] for e in a.recent(check="cross_context")] == ["k2"]
    # token match, not substring: "leak" alone matches nothing
    assert a.recent(check="leak") == []


@pytest.mark.asyncio
async def test_guard_records_any_finding_not_just_cross_context():
    """H6.1: a secret_leak finding produces an audit row; a clean evaluation
    produces none but still bumps the evaluation counter."""
    from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard

    a = GuardAuditStore(":memory:")
    guard = ResponseGuard(default_mode=GuardMode.SHADOW, audit_store=a)

    # clean reply: counted, not recorded
    r = await guard.evaluate(response_text="see you tomorrow")
    assert r.decision == "allow"
    assert a.summary()["total"] == 0

    # PII finding (SSN): counted AND recorded with would_block
    r = await guard.evaluate(response_text="her ssn is 123-45-6789")
    assert any(f.check == "secret_leak" for f in r.findings)
    s = a.summary()
    assert s["total"] == 1
    assert s["windows"]["24h"]["evaluations"] == 2
    assert s["windows"]["24h"]["by_check"].get("secret_leak") == 1
    assert s["windows"]["24h"]["would_block"] == 1

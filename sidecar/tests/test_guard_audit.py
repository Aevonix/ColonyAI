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


# ---------------------------------------------------------------------------
# L1.4 — per-gateway enforce evidence (the tom2 level resolver's proof input)
# ---------------------------------------------------------------------------

def _enforce_rows(a, n, gateway="rcs"):
    for _ in range(n):
        a.record(conversation_key="k", mode="enforce", decision="revise",
                 authorized=False, checks=["secret_leak"], entities=[],
                 gateway=gateway)


def test_gateway_column_migration_is_additive(tmp_path):
    """A pre-existing DB without the gateway column opens cleanly; its old
    rows stay NULL-gateway and can never satisfy an evidence query."""
    import sqlite3

    db = str(tmp_path / "audit.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE guard_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
               conversation_key TEXT, mode TEXT, decision TEXT,
               authorized INTEGER NOT NULL DEFAULT 0, checks TEXT,
               entities TEXT, response_excerpt TEXT);
           CREATE TABLE guard_eval_days (
               day TEXT PRIMARY KEY, evaluations INTEGER NOT NULL DEFAULT 0);
           INSERT INTO guard_events (ts, conversation_key, mode, decision,
               authorized, checks, entities, response_excerpt)
           VALUES ('2999-01-01T00:00:00+00:00', 'k', 'enforce', 'revise',
                   0, 'secret_leak', '', '');""")
    conn.commit()
    conn.close()

    a = GuardAuditStore(db)
    assert a.summary()["total"] == 1                 # old rows intact
    assert a.enforce_evidence("rcs") is False        # NULL gateway ≠ proof
    _enforce_rows(a, 3)
    assert a.enforce_evidence("rcs") is True


def test_enforce_evidence_thresholds(monkeypatch):
    monkeypatch.delenv("COLONY_GUARD_EVIDENCE_MIN", raising=False)
    a = GuardAuditStore(":memory:")
    assert a.enforce_evidence("rcs") is False
    _enforce_rows(a, 2)
    assert a.enforce_evidence("rcs") is False        # 2 < default 3
    _enforce_rows(a, 1)
    assert a.enforce_evidence("rcs") is True
    assert a.enforce_evidence("RCS") is True         # normalized
    assert a.enforce_evidence("sms") is False        # other gateway ≠ proof
    assert a.enforce_evidence("") is False
    assert a.enforce_evidence(None) is False


def test_enforce_evidence_ignores_shadow_and_stale_rows():
    a = GuardAuditStore(":memory:")
    for _ in range(5):
        a.record(conversation_key="k", mode="shadow", decision="allow",
                 authorized=False, checks=["secret_leak"], entities=[],
                 gateway="rcs")
    assert a.enforce_evidence("rcs") is False        # shadow proves nothing
    _enforce_rows(a, 3)
    a._conn.execute("UPDATE guard_events SET ts='2000-01-01T00:00:00+00:00' "
                    "WHERE mode='enforce'")
    a._conn.commit()
    assert a.enforce_evidence("rcs") is False        # outside the window


def test_enforce_evidence_min_env(monkeypatch):
    from colony_sidecar.gate.guard_audit import evidence_min

    monkeypatch.setenv("COLONY_GUARD_EVIDENCE_MIN", "5")
    assert evidence_min() == 5
    monkeypatch.setenv("COLONY_GUARD_EVIDENCE_MIN", "banana")
    assert evidence_min() == 3                       # malformed => default
    monkeypatch.setenv("COLONY_GUARD_EVIDENCE_MIN", "0")
    assert evidence_min() == 1                       # zero-proof is not a config
    monkeypatch.delenv("COLONY_GUARD_EVIDENCE_MIN", raising=False)
    a = GuardAuditStore(":memory:")
    _enforce_rows(a, 1, gateway="sms")
    monkeypatch.setenv("COLONY_GUARD_EVIDENCE_MIN", "1")
    assert a.enforce_evidence("sms") is True


def test_enforce_evidence_fails_closed_on_store_error():
    a = GuardAuditStore(":memory:")
    _enforce_rows(a, 3)
    a._conn.close()
    assert a.enforce_evidence("rcs") is False        # error => no proof


@pytest.mark.asyncio
async def test_response_guard_threads_gateway_into_audit():
    from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard

    a = GuardAuditStore(":memory:")
    guard = ResponseGuard(default_mode=GuardMode.SHADOW, audit_store=a)
    await guard.evaluate(response_text="her ssn is 123-45-6789",
                         target_gateway="RCS")
    await guard.evaluate(response_text="her ssn is 123-45-6789")
    rows = a.recent()
    assert [r["gateway"] for r in rows] == [None, "rcs"]

"""L4.3 — doctor level-coherence check + /tom2/status leveled posture.

The min-chain silently (and safely) degrades an incoherent posture; the
doctor's job is to stop the owner BELIEVING a level is live when it can
never render: LEVEL=2 without MAX_LEVEL=2 / CROSS_CONTEXT=1 / an
allowlisted+evidenced tom2_epistemic check, or malformed risk caps.
/tom2/status exposes {configured, max, risk_caps, sample_decision}.
"""

from __future__ import annotations

import pytest

from colony_sidecar import doctor
from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.gate.guard_audit import GuardAuditStore
from colony_sidecar.tom.levels import clear_level_cache, set_evidence_probe


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("COLONY_TOM2_LEVEL", "COLONY_TOM2_MAX_LEVEL",
                "COLONY_TOM2_CROSS_CONTEXT", "COLONY_TOM2_RISK_CAPS",
                "COLONY_GUARD_ENFORCE_CHECKS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    clear_level_cache()
    set_evidence_probe(None)
    yield
    clear_level_cache()
    set_evidence_probe(None)


def _arm_l2_flags(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")


def _seed_evidence(tmp_path, gateway="dm", n=3):
    audit = GuardAuditStore(db_path=str(tmp_path / "colony-guard-audit.db"))
    for _ in range(n):
        audit.record(conversation_key=f"{gateway}:x", mode="enforce",
                     decision="allow", authorized=False,
                     checks=["secret_leak"], entities=[], gateway=gateway)
    audit.close()


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def test_pass_at_default_level_zero():
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.PASS
    assert "kill switch" in r.detail


def test_warn_level2_without_max_level(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "COLONY_TOM2_MAX_LEVEL" in r.detail
    assert "unreachable" in r.detail


def test_warn_level2_without_cross_context(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "2")
    monkeypatch.setenv("COLONY_TOM2_MAX_LEVEL", "2")
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "COLONY_TOM2_CROSS_CONTEXT" in r.detail


def test_warn_level2_without_allowlisted_check(monkeypatch):
    _arm_l2_flags(monkeypatch)
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "secret_leak")
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "tom2_epistemic" in r.detail


def test_warn_level2_without_enforce_evidence(monkeypatch):
    """Flags coherent, allowlist fine — but no audit trail means no proof
    of enforcement, so level 2 will cap at 1 every turn."""
    _arm_l2_flags(monkeypatch)
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "evidence" in r.detail


def test_pass_level2_with_full_posture(monkeypatch, tmp_path):
    _arm_l2_flags(monkeypatch)
    _seed_evidence(tmp_path)
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.PASS
    assert "coherent" in r.detail


def test_stale_evidence_warns(monkeypatch, tmp_path):
    import sqlite3
    _arm_l2_flags(monkeypatch)
    _seed_evidence(tmp_path)
    db = tmp_path / "colony-guard-audit.db"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE guard_events SET ts = '2000-01-01T00:00:00'")
    conn.commit()
    conn.close()
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "24h" in r.detail


def test_warn_on_malformed_risk_caps_when_raised(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "1")
    monkeypatch.setenv("COLONY_TOM2_RISK_CAPS", "garbage")
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.WARN
    assert "COLONY_TOM2_RISK_CAPS" in r.detail


def test_level1_with_valid_caps_passes(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "1")
    r = doctor.check_tom2_level_coherence()
    assert r.status == doctor.PASS


# ---------------------------------------------------------------------------
# /tom2/status posture surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_reports_leveled_posture(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_LEVEL", "1")
    out = await host_mod.tom2_status()
    assert out["configured"] == 1
    assert out["max"] == 1                            # default ceiling
    assert out["risk_caps"] == {
        "valid": True, "caps": {"0": 2, "1": 2, "2": 1, "3": 0}}
    sample = out["sample_decision"]
    assert sample is not None
    # the placeholder environment is hostile by construction: the sample
    # shows the resolver failing closed, with every brake term visible
    assert sample["level"] == 0
    assert set(sample["terms"]) >= {"configured", "max", "risk_cap",
                                    "enforce_evidence", "cross_context"}


@pytest.mark.asyncio
async def test_status_flags_malformed_caps(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_RISK_CAPS", "garbage")
    out = await host_mod.tom2_status()
    assert out["risk_caps"]["valid"] is False
    assert out["risk_caps"]["caps"] == {"0": 0, "1": 0, "2": 0, "3": 0}

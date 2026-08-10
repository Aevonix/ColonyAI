"""L3.1 — injection-taint registry: TTL'd, refs-not-content, restart-safe.

A taint marks "a level-2 epistemic line about SUBJECT is live in some
conversation's context window"; the egress net keys off it. Rows carry
opaque ids + normalized subject display names only — fact text is refused
at the boundary (privacy pin, raw-DB regression lock below).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from colony_sidecar.gate.taint import (
    DEFAULT_TTL_SECS, TaintRegistry, taint_ttl_secs)


@pytest.fixture()
def reg():
    return TaintRegistry()


def _register(reg, **kw):
    args = dict(conversation_key="dm:cid-alice", subject_contact_id="cid-bob",
                subject_names=["Bob Smith", "bobby"], fact_ref="fact-1",
                kind="unaware_of")
    args.update(kw)
    return reg.register(**args)


# ---------------------------------------------------------------------------
# TTL config
# ---------------------------------------------------------------------------

def test_ttl_default_and_malformed(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_TAINT_TTL_SECS", raising=False)
    assert taint_ttl_secs() == DEFAULT_TTL_SECS
    monkeypatch.setenv("COLONY_TOM2_TAINT_TTL_SECS", "banana")
    assert taint_ttl_secs() == DEFAULT_TTL_SECS       # protection: default
    monkeypatch.setenv("COLONY_TOM2_TAINT_TTL_SECS", "-5")
    assert taint_ttl_secs() == DEFAULT_TTL_SECS       # never zero-length
    monkeypatch.setenv("COLONY_TOM2_TAINT_TTL_SECS", "120")
    assert taint_ttl_secs() == 120.0


# ---------------------------------------------------------------------------
# Register / read
# ---------------------------------------------------------------------------

def test_register_and_active_for(reg):
    row = _register(reg)
    assert row["expires_at"] > time.time()
    active = reg.active_for("dm:cid-alice")
    assert len(active) == 1
    t = active[0]
    assert t["subject_contact_id"] == "cid-bob"
    assert t["fact_ref"] == "fact-1"
    assert t["kind"] == "unaware_of"
    # names normalized (NFKC + lower + strip)
    assert t["subject_names"] == ["bob smith", "bobby"]
    # scoped: another conversation sees nothing FOR it...
    assert reg.active_for("dm:cid-carol") == []
    # ...but the cross-conversation read sees every live taint
    assert len(reg.all_active()) == 1


def test_any_active_is_false_when_empty_and_true_when_live(reg):
    assert reg.any_active() is False
    _register(reg)
    assert reg.any_active() is True


def test_expiry(reg):
    _register(reg, ttl_seconds=0.05)
    assert reg.any_active() is True
    time.sleep(0.08)
    assert reg.any_active() is False
    assert reg.active_for("dm:cid-alice") == []
    assert reg.all_active() == []
    assert reg.purge_expired() == 1
    assert reg.counts() == {"rows": 0, "active": 0}


def test_nonpositive_ttl_falls_back_to_default(reg):
    row = _register(reg, ttl_seconds=0)
    assert row["expires_at"] - row["created_at"] == pytest.approx(
        DEFAULT_TTL_SECS, abs=1.0)


# ---------------------------------------------------------------------------
# Privacy pins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("conversation_key", "she told me the launch slipped"),
    ("subject_contact_id", "bob who works at acme"),
    ("fact_ref", "the launch moved to friday"),
])
def test_prose_refused_in_opaque_fields(reg, field, value):
    with pytest.raises(ValueError):
        _register(reg, **{field: value})


def test_unknown_kind_refused(reg):
    with pytest.raises(ValueError):
        _register(reg, kind="believes")


def test_names_are_capped_and_deduped(reg):
    row = _register(reg, subject_names=["Bob", "BOB", "x" * 500] +
                    [f"alias-{i}" for i in range(20)])
    names = row["subject_names"]
    assert len(names) <= 8
    assert names.count("bob") == 1
    assert all(len(n) <= 80 for n in names)


def test_raw_db_carries_no_fact_text(tmp_path):
    """Regression lock: the taint table stores ids/names/refs, never the
    fact sentence the inference is about."""
    db = tmp_path / "taint.db"
    reg = TaintRegistry(db_path=str(db))
    _register(reg, fact_ref="fact-abc123")
    reg.close()
    conn = sqlite3.connect(str(db))
    blob = " ".join(
        str(v) for row in conn.execute("SELECT * FROM tom2_taints")
        for v in row)
    conn.close()
    assert "fact-abc123" in blob            # the ref IS there
    assert "launch" not in blob.lower()     # prose is not


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------

def test_watermark_survives_restart(tmp_path):
    db = tmp_path / "taint.db"
    reg = TaintRegistry(db_path=str(db))
    _register(reg, ttl_seconds=300)
    reg.close()
    reg2 = TaintRegistry(db_path=str(db))
    assert reg2.any_active() is True
    assert len(reg2.active_for("dm:cid-alice")) == 1
    reg2.close()

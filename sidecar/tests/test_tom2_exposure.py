"""L2.3 — exposure ledger: refs-not-content pin + budgets + owner endpoint.

Includes the raw-DB regex regression proof: dump every column of every raw
row and show no fact text can appear — only ids.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.tom.exposure import (
    Tom2ExposureStore, budget_global_day, budget_pair_day,
    budget_reader_day)
from colony_sidecar.tom.facts import SharedFactsStore


def _expose(s, reader="cid-alice", subject="cid-bob", fact_ref="fact-1",
            conv="dm:alice"):
    return s.record_exposure(reader_contact_id=reader,
                             subject_contact_id=subject,
                             fact_ref=fact_ref, conversation_key=conv)


# ---------------------------------------------------------------------------
# Budgets: defaults, malformed, fail-closed
# ---------------------------------------------------------------------------

def test_budget_defaults(monkeypatch):
    for var in ("COLONY_TOM2_BUDGET_PAIR_DAY",
                "COLONY_TOM2_BUDGET_READER_DAY",
                "COLONY_TOM2_BUDGET_GLOBAL_DAY"):
        monkeypatch.delenv(var, raising=False)
    assert budget_pair_day() == 1
    assert budget_reader_day() == 3
    assert budget_global_day() == 10


def test_malformed_budget_is_zero(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_BUDGET_PAIR_DAY", "many")
    assert budget_pair_day() == 0
    monkeypatch.setenv("COLONY_TOM2_BUDGET_PAIR_DAY", "-4")
    assert budget_pair_day() == 0


def test_pair_budget_binds(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_BUDGET_PAIR_DAY", raising=False)
    s = Tom2ExposureStore()
    assert s.budget_ok("cid-alice", "cid-bob") is True
    _expose(s)                                     # pair budget (1) spent
    assert s.budget_ok("cid-alice", "cid-bob") is False
    assert s.budget_ok("cid-alice", "cid-carol") is True   # other pair fine


def test_reader_budget_binds(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_BUDGET_READER_DAY", "2")
    s = Tom2ExposureStore()
    _expose(s, subject="cid-b1")
    _expose(s, subject="cid-b2")
    assert s.budget_ok("cid-alice", "cid-b3") is False     # reader exhausted
    assert s.budget_ok("cid-dave", "cid-b3") is True       # other reader fine


def test_global_budget_binds(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_BUDGET_GLOBAL_DAY", "2")
    s = Tom2ExposureStore()
    _expose(s, reader="cid-r1", subject="cid-s1")
    _expose(s, reader="cid-r2", subject="cid-s2")
    assert s.budget_ok("cid-r3", "cid-s3") is False


def test_old_exposures_do_not_bind():
    s = Tom2ExposureStore()
    _expose(s)
    s._conn.execute("UPDATE tom2_exposures "
                    "SET created_at='2000-01-01T00:00:00+00:00'")
    s._conn.commit()
    assert s.budget_ok("cid-alice", "cid-bob") is True


def test_budget_fails_closed():
    s = Tom2ExposureStore()
    assert s.budget_ok("", "cid-b") is False
    assert s.budget_ok("cid-a", "") is False
    s._conn.close()
    assert s.budget_ok("cid-a", "cid-b") is False


# ---------------------------------------------------------------------------
# The refs-not-content pin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("fact_ref", "the owner is moving to Berlin"),
    ("fact_ref", ""),
    ("conversation_key", "we talked about the deal"),
    ("reader_contact_id", "alice said hi"),
    ("subject_contact_id", "bob the builder from work"),
])
def test_prose_is_refused_everywhere(field, value):
    s = Tom2ExposureStore()
    kw = dict(reader_contact_id="cid-alice", subject_contact_id="cid-bob",
              fact_ref="fact-1", conversation_key="dm:alice")
    kw[{"reader_contact_id": "reader_contact_id",
        "subject_contact_id": "subject_contact_id",
        "fact_ref": "fact_ref",
        "conversation_key": "conversation_key"}[field]] = value
    with pytest.raises(ValueError):
        s.record_exposure(**kw)
    assert s.counts()["total"] == 0


def test_privacy_no_fact_text_in_raw_rows(tmp_path):
    """THE row-level privacy proof for the ledger: exposures derived from
    real facts leave no fact text in any column of any raw row."""
    facts = SharedFactsStore(":memory:")
    f1 = facts.create_fact(contact_id="cid-alice",
                           fact="SECRETLAUNCH moved to friday",
                           confidence=0.9)
    f2 = facts.create_fact(contact_id="cid-alice",
                           fact="SECRETBUDGET was approved",
                           confidence=0.9)
    db = str(tmp_path / "exposure.db")
    s = Tom2ExposureStore(db_path=db)
    for f in (f1, f2):
        s.record_exposure(reader_contact_id="cid-alice",
                          subject_contact_id="cid-bob",
                          fact_ref=f["id"], conversation_key="dm:alice")
    s.close()

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM tom2_exposures").fetchall()
    conn.close()
    assert rows
    blob = " | ".join(str(v) for row in rows for v in row)
    for token in ("SECRETLAUNCH", "SECRETBUDGET", "moved to friday",
                  "was approved"):
        assert not re.search(re.escape(token), blob, re.IGNORECASE), token
    # topology IS present — by id only
    assert f1["id"] in blob and f2["id"] in blob


# ---------------------------------------------------------------------------
# Owner reads + endpoint
# ---------------------------------------------------------------------------

def test_recent_filters_and_counts():
    s = Tom2ExposureStore()
    _expose(s, reader="cid-r1", subject="cid-s1")
    _expose(s, reader="cid-r1", subject="cid-s2")
    _expose(s, reader="cid-r2", subject="cid-s1")
    assert len(s.recent()) == 3
    assert len(s.recent(reader_contact_id="cid-r1")) == 2
    assert len(s.recent(subject_contact_id="cid-s1")) == 2
    assert len(s.recent(reader_contact_id="cid-r1",
                        subject_contact_id="cid-s2")) == 1
    c = s.counts()
    assert c["total"] == 3 and c["readers"] == 2 and c["pairs"] == 3
    assert c["last_24h"] == 3


@pytest.mark.asyncio
async def test_exposure_endpoint(monkeypatch):
    s = Tom2ExposureStore()
    _expose(s)
    monkeypatch.setattr(host_mod, "_tom2_exposure", s)
    out = await host_mod.tom2_exposure()
    assert out["available"] is True
    assert out["summary"]["total"] == 1
    assert out["budgets"] == {"pair_day": 1, "reader_day": 3,
                              "global_day": 10}
    assert out["events"][0]["fact_ref"] == "fact-1"
    filtered = await host_mod.tom2_exposure(reader="cid-nobody")
    assert filtered["events"] == []


@pytest.mark.asyncio
async def test_exposure_endpoint_without_store(monkeypatch):
    monkeypatch.setattr(host_mod, "_tom2_exposure", None)
    out = await host_mod.tom2_exposure()
    assert out["available"] is False
    assert out["events"] == []

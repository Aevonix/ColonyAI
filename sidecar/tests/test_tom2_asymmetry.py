"""Asymmetry engine (H3.2): modes, counts-only shadow, and THE row-level
privacy proof — a regex over the raw tom2 DB shows no foreign fact text
ever lands in an inference row."""

from __future__ import annotations

import re
import sqlite3

from colony_sidecar.tom.asymmetry import AsymmetryEngine, tom2_mode
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.tom2 import Tom2Store

_FACT_A = "Alice prefers morning meetings SECRETALPHA"
_FACT_B = "Bob is moving to Lisbon SECRETBETA"
_FACT_B_LOW = "Bob might adopt a dog SECRETLOWCONF"


def _seeded_facts():
    facts = SharedFactsStore(":memory:")
    fa = facts.create_fact(contact_id="cid-a", fact=_FACT_A, confidence=0.9)
    fb = facts.create_fact(contact_id="cid-b", fact=_FACT_B, confidence=0.9)
    fl = facts.create_fact(contact_id="cid-b", fact=_FACT_B_LOW,
                           confidence=0.3)
    return facts, fa, fb, fl


def test_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)
    assert tom2_mode() == "off"


def test_off_is_inert(monkeypatch):
    """Flag-off regression lock: nothing computed, nothing written."""
    monkeypatch.delenv("COLONY_TOM2", raising=False)
    monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)
    facts, *_ = _seeded_facts()
    tom2 = Tom2Store()
    report = AsymmetryEngine(facts, tom2).run()
    assert report["mode"] == "off"
    assert report["knows"] == 0 and report["unaware_of"] == 0
    assert tom2.counts()["total"] == 0


def test_shadow_counts_only(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2", "shadow")
    facts, *_ = _seeded_facts()
    tom2 = Tom2Store()
    report = AsymmetryEngine(facts, tom2).run()
    assert report["mode"] == "shadow"
    assert report["contacts"] == 2
    assert report["knows"] == 3                    # each contact's own rows
    # A lacks B's confident fact; B lacks A's. The low-confidence fact
    # never generates an asymmetry inference.
    assert report["unaware_of"] == 2
    assert report["written"] == 0
    assert tom2.counts()["total"] == 0             # shadow writes NOTHING


def test_live_writes_refs_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2", "live")
    facts, fa, fb, fl = _seeded_facts()
    tom2 = Tom2Store()
    engine = AsymmetryEngine(facts, tom2)
    engine.run()
    a_rows = tom2.list_inferences(contact_id="cid-a", kind="unaware_of")
    assert [r["fact_ref"] for r in a_rows] == [fb["id"]]
    assert fl["id"] not in {r["fact_ref"]
                            for r in tom2.list_inferences(kind="unaware_of")}
    knows = tom2.list_inferences(contact_id="cid-b", kind="knows")
    assert {r["fact_ref"] for r in knows} == {fb["id"], fl["id"]}
    total = tom2.counts()["total"]
    engine.run()                                   # daily re-run: upserts
    assert tom2.counts()["total"] == total


def test_per_contact_row_cap(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2", "live")
    facts = SharedFactsStore(":memory:")
    facts.create_fact(contact_id="cid-a", fact="a1", confidence=0.9)
    for i in range(5):
        facts.create_fact(contact_id="cid-b", fact=f"b{i}", confidence=0.9)
    tom2 = Tom2Store()
    AsymmetryEngine(facts, tom2, max_rows_per_contact=2).run()
    assert len(tom2.list_inferences(contact_id="cid-a",
                                    kind="unaware_of")) == 2


def test_privacy_no_foreign_fact_text_in_raw_rows(monkeypatch, tmp_path):
    """THE row-level privacy proof: dump every column of every raw row of
    the tom2 DB and regex for fact text — none may appear; only ids do."""
    monkeypatch.setenv("COLONY_TOM2", "live")
    facts, fa, fb, fl = _seeded_facts()
    db_path = str(tmp_path / "tom2.db")
    tom2 = Tom2Store(db_path=db_path)
    AsymmetryEngine(facts, tom2).run()
    tom2.close()

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM tom2_inferences").fetchall()
    conn.close()
    assert rows                                     # something was written
    blob = " | ".join(str(v) for row in rows for v in row)

    # no fact text, in any casing, in any column of any row
    for token in ("SECRETALPHA", "SECRETBETA", "SECRETLOWCONF",
                  "morning meetings", "moving to Lisbon", "adopt a dog"):
        assert not re.search(re.escape(token), blob, re.IGNORECASE), token
    # cross-contact topology IS present — by id only
    assert fb["id"] in blob and fa["id"] in blob
    # and every row is owner-only
    conn = sqlite3.connect(db_path)
    vis = {r[0] for r in conn.execute(
        "SELECT DISTINCT visibility FROM tom2_inferences").fetchall()}
    conn.close()
    assert vis == {"owner"}

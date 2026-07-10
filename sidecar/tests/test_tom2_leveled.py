"""L2.2 — leveled renderers: level 1 self-reflexive, level 2 silent-prior.

Level 1 renders only the reader's OWN rows (knows = their own fact text,
unaware = one content-free caution). Level 2 consumes only L2.1-eligible
rows and delegates every line to the unmodified H3.5 gate.
"""

from __future__ import annotations

import inspect

import pytest

from colony_sidecar.tom import leveled
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.leveled import (
    LEVEL2_HEADER, UNAWARE_CAUTION, render_level1, render_level2)
from colony_sidecar.tom.tom2 import Tom2Store, render_inference_for_contact

READER = "cid-alice"


@pytest.fixture()
def stores():
    facts = SharedFactsStore(":memory:")
    tom2 = Tom2Store()
    own = facts.create_fact(contact_id=READER,
                            fact="the launch moved to friday",
                            confidence=0.9)
    foreign = facts.create_fact(contact_id="cid-carol",
                                fact="carol's private detail",
                                confidence=0.9)
    return facts, tom2, own, foreign


# ---------------------------------------------------------------------------
# Level 1 — self-reflexive
# ---------------------------------------------------------------------------

def test_level1_knows_renders_readers_own_fact(stores, monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CROSS_CONTEXT", raising=False)
    facts, tom2, own, _ = stores
    tom2.record_inference(contact_id=READER, kind="knows",
                          fact_ref=own["id"], confidence=0.8)
    out = render_level1(tom2, facts, READER)
    assert out is not None
    assert "the launch moved to friday" in out
    # self-reflexive: works with the cross-context flag OFF — no third
    # party is involved.


def test_level1_unaware_is_one_content_free_caution(stores):
    facts, tom2, own, _ = stores
    more = [facts.create_fact(contact_id=READER, fact=f"own fact {i}",
                              confidence=0.9) for i in range(3)]
    for f in [own] + more:
        tom2.record_inference(contact_id=READER, kind="unaware_of",
                              fact_ref=f["id"], confidence=0.4)
    out = render_level1(tom2, facts, READER)
    assert out == f"- {UNAWARE_CAUTION}"          # ONE line, no counts
    assert "launch" not in out and "own fact" not in out


def test_level1_foreign_fact_ref_fails_closed(stores):
    facts, tom2, _, foreign = stores
    tom2.record_inference(contact_id=READER, kind="knows",
                          fact_ref=foreign["id"], confidence=0.8)
    assert render_level1(tom2, facts, READER) is None


def test_level1_partial_evidence_visibility_fails_closed(stores):
    facts, tom2, own, foreign = stores
    tom2.record_inference(contact_id=READER, kind="knows",
                          fact_ref=own["id"], evidence_refs=[foreign["id"]],
                          confidence=0.8)
    assert render_level1(tom2, facts, READER) is None


def test_level1_never_renders_third_parties(stores):
    facts, tom2, own, _ = stores
    tom2.record_inference(contact_id="cid-bob", kind="unaware_of",
                          fact_ref=own["id"], confidence=0.4)
    tom2.record_inference(contact_id="cid-bob", kind="knows",
                          fact_ref=own["id"], confidence=0.4)
    assert render_level1(tom2, facts, READER) is None


def test_level1_fail_closed_edges(stores):
    facts, tom2, own, _ = stores
    assert render_level1(None, facts, READER) is None
    assert render_level1(tom2, None, READER) is None
    assert render_level1(tom2, facts, "") is None

    class Broken:
        def list_inferences(self, **kw):
            raise RuntimeError("db down")

    assert render_level1(Broken(), facts, READER) is None


def test_level1_limit(stores):
    facts, tom2, _, _ = stores
    for i in range(6):
        f = facts.create_fact(contact_id=READER, fact=f"known thing {i}",
                              confidence=0.9)
        tom2.record_inference(contact_id=READER, kind="knows",
                              fact_ref=f["id"], confidence=0.8)
    out = render_level1(tom2, facts, READER, limit=2)
    assert out is not None and out.count("already knows") == 2


# ---------------------------------------------------------------------------
# Level 2 — silent-prior over eligible rows, via the unmodified H3.5 gate
# ---------------------------------------------------------------------------

def _bob_unaware(tom2, fact):
    tom2.record_inference(contact_id="cid-bob", kind="unaware_of",
                          fact_ref=fact["id"], confidence=0.4)
    return tom2.list_inferences(contact_id="cid-bob")[0]


def test_level2_renders_silent_prior(stores, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, own, _ = stores
    row = _bob_unaware(tom2, own)
    out = render_level2([row], facts, READER)
    assert out is not None
    assert out.startswith(LEVEL2_HEADER)
    assert "cid-bob has not heard: the launch moved to friday" in out
    # each line is EXACTLY what the H3.5 gate renders — pure delegation
    assert render_inference_for_contact(row, facts, READER) in out


def test_level2_refuses_when_master_flag_off(stores, monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_CROSS_CONTEXT", raising=False)
    facts, tom2, own, _ = stores
    row = _bob_unaware(tom2, own)
    assert render_level2([row], facts, READER) is None


def test_level2_unvetted_foreign_row_cannot_render(stores, monkeypatch):
    """Even a caller that skips the eligibility pipeline cannot leak: the
    H3.5 gate inside refuses rows resting on foreign facts."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, _, foreign = stores
    row = _bob_unaware(tom2, foreign)
    out = render_level2([row], facts, READER)
    assert out is None
    assert True                                    # no partial header either


def test_level2_limit_and_empty(stores, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    facts, tom2, _, _ = stores
    rows = []
    for i in range(4):
        f = facts.create_fact(contact_id=READER, fact=f"reader fact {i}",
                              confidence=0.9)
        tom2.record_inference(contact_id=f"cid-s{i}", kind="unaware_of",
                              fact_ref=f["id"], confidence=0.4)
        rows.append(tom2.list_inferences(contact_id=f"cid-s{i}")[0])
    out = render_level2(rows, facts, READER, limit=2)
    assert out is not None and out.count("has not heard") == 2
    assert render_level2([], facts, READER) is None
    assert render_level2(rows, None, READER) is None
    assert render_level2(rows, facts, "") is None


def test_level2_owns_no_visibility_logic():
    """Source lock: level 2 delegates to the H3.5 renderer and never
    reimplements ref-visibility."""
    src = inspect.getsource(leveled.render_level2)
    assert "render_inference_for_contact" in src
    assert "_ref_visible_to" not in src

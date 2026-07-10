"""Tom2Store (H3.1): refs-not-content, owner-only visibility."""

from __future__ import annotations

import pytest

from colony_sidecar.tom.tom2 import Tom2Store


def test_record_and_upsert_by_triple():
    s = Tom2Store()
    a = s.record_inference(contact_id="cid-a", kind="unaware_of",
                           fact_ref="fact-123", confidence=0.4)
    assert a["visibility"] == "owner"
    b = s.record_inference(contact_id="cid-a", kind="unaware_of",
                           fact_ref="fact-123", confidence=0.6,
                           evidence_refs=["fact-999"])
    assert b["id"] == a["id"]                      # upsert, not duplicate
    assert b["confidence"] == 0.6
    assert b["evidence_refs"] == ["fact-999"]
    assert s.counts() == {"total": 1, "by_kind": {"unaware_of": 1},
                          "contacts": 1}


def test_visibility_owner_is_the_only_writable_value():
    s = Tom2Store()
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="knows",
                           fact_ref="fact-1", visibility="contact")
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="knows",
                           fact_ref="fact-1", visibility="public")
    assert s.counts()["total"] == 0


def test_refs_that_look_like_text_are_refused():
    """The refs-not-content pin: prose cannot be smuggled through refs."""
    s = Tom2Store()
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="knows",
                           fact_ref="the owner is moving to Berlin")
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="knows",
                           fact_ref="fact-1",
                           evidence_refs=["they said the deal closed"])
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="knows", fact_ref="")
    assert s.counts()["total"] == 0


def test_unknown_kind_refused():
    s = Tom2Store()
    with pytest.raises(ValueError):
        s.record_inference(contact_id="cid-a", kind="suspects",
                           fact_ref="fact-1")


def test_non_owner_reader_scope_gets_nothing():
    s = Tom2Store()
    s.record_inference(contact_id="cid-a", kind="knows", fact_ref="fact-1")
    assert s.list_inferences(contact_id="cid-a") != []
    assert s.list_inferences(contact_id="cid-a",
                             reader_scope="contact") == []
    assert s.list_inferences(reader_scope="") == []


def test_delete_for_fact_cascades():
    s = Tom2Store()
    s.record_inference(contact_id="cid-a", kind="unaware_of", fact_ref="f-1")
    s.record_inference(contact_id="cid-b", kind="unaware_of", fact_ref="f-1")
    s.record_inference(contact_id="cid-a", kind="knows", fact_ref="f-2")
    assert s.delete_for_fact("f-1") == 2
    assert s.counts()["total"] == 1

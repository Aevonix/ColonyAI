"""Promotion keeps evidence useful without treating model confidence as truth."""
import json
from types import SimpleNamespace

import pytest

from colony_sidecar.beliefs.source_claims import validated_claims
from colony_sidecar.util.model_output import final_text
from colony_sidecar.tom.extractor import _parse_fact_array
from test_source_claim_projection import claim
from test_tom_extractor import fact_item


def test_quality_and_exact_evidence_are_both_required():
    text = "My spare key is in the blue tin."
    useful = claim(text, "blue tin")
    result = validated_claims(json.dumps([useful]), message=text, prior=[], observed_at=None)
    assert result[0]["memory_quality"]["basis"] == "model_judgment_unverified"
    for bad in (dict(useful, memory_kind="source_only"), dict(useful, recall_reason=""),
                dict(useful, memory_kind=[]), dict(useful, memory_kind={}),
                dict(useful, evidence="My spare key is in the red tin."),
                {k: v for k, v in useful.items() if k != "memory_kind"}):
        assert validated_claims(json.dumps([bad]), message=text, prior=[], observed_at=None) == []


def test_contact_knowledge_saves_quote_and_deduplicates_model_repetition():
    text = "I prefer quiet rooms."
    item = fact_item(text, memory_kind="preference", fact="The contact is shy and afraid of crowds.")
    rows = _parse_fact_array(json.dumps([item, item]), conversation_text="user: " + text)
    assert len(rows) == 1 and rows[0]["fact"] == text
    assert "shy" not in json.dumps(rows)
    assert _parse_fact_array(json.dumps([item]), conversation_text="unrelated") == []
    assert _parse_fact_array(json.dumps([dict(item, memory_kind="source_only")]), conversation_text=text) == []


@pytest.mark.parametrize("disclaimer", ["not information about me", "not a factual claim about me", "not a true statement about us"])
def test_explicit_personal_disavowal_cannot_be_clipped_into_a_preference(disclaimer):
    quote = "my favorite drink is ink"
    message = f"An example, {disclaimer}: {quote}."
    raw = claim(quote, "ink", predicate="favorite drink", memory_kind="preference")
    assert validated_claims(json.dumps([raw]), message=message, prior=[], observed_at=None) == []


def test_ordinary_negative_preference_is_still_an_assertion():
    text = "I do not drink coffee."
    rows = validated_claims(json.dumps([claim(text, "do not drink coffee", memory_kind="preference")]),
                            message=text, prior=[], observed_at=None)
    assert rows[0]["value"] == "do not drink coffee"


@pytest.mark.parametrize("content,finish", [(None, "stop"), ("unfinished final", "length"), (None, "length")])
def test_persisted_output_never_uses_reasoning_or_partial_answer(content, finish):
    raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=content, reasoning_content="Maybe the image shows a person."))])
    with pytest.raises(ValueError):
        final_text(SimpleNamespace(raw=raw, content="Maybe the image shows a person."))


def test_provider_final_answer_wins_over_router_compatibility_content():
    raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content="A blue circle."))])
    assert final_text(SimpleNamespace(raw=raw, content="internal scratch text")) == "A blue circle."

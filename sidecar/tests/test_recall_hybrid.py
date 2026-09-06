"""Closed retrieval paths: fresh lexical evidence and calibrated empty recall."""
from unittest.mock import AsyncMock

import pytest

from colony_sidecar.intelligence.graph.recall import (
    calibration_fingerprint, lexical_query, render_memory_context,
)
from test_recall_ranking import RecallFixture, _Hit, _node
from test_recall_rerank import _RecordingReranker


def calibrated(monkeypatch, fixture, reranker, threshold=.8):
    metadata = {"provider": "fixture", "model": "neutral-reranker", "format": "v1",
                "weights_revision": "unverified"}
    fixture.graph.set_rerank_fn(reranker.rerank, calibration_metadata=lambda: metadata)
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_RERANK_MIN_SCORE", str(threshold))
    monkeypatch.setenv("COLONY_RECALL_RERANK_CALIBRATION", calibration_fingerprint(metadata))
    return metadata


@pytest.mark.asyncio
async def test_hybrid_finds_new_source_before_vector_index_catches_up(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_HYBRID", "on")
    fixture = RecallFixture([_Hit("old", .9)], [_node("old", state="superseded")])
    fresh = _node("fresh", confidence=.95, relevance=12.0, source_uri="synthetic:correction")
    fixture.graph._recall_lexical = AsyncMock(return_value=[fresh])
    rows = await fixture.recall("corrected preference", limit=4, person_id="recipient",
                                exclude_source_uris=["excluded:source"])
    assert [r["id"] for r in rows] == ["fresh"]
    args = fixture.graph._recall_lexical.call_args.args
    assert args[4] == "recipient"
    assert "excluded:source" in args[5]
    assert rows[0]["source_uri"] == "synthetic:correction"


@pytest.mark.asyncio
async def test_calibrated_reranker_can_return_empty_when_candidates_fit_limit(monkeypatch):
    fixture = RecallFixture([_Hit("unrelated", .92)], [_node("unrelated", confidence=.9)])
    reranker = _RecordingReranker(scores={0: .02})
    calibrated(monkeypatch, fixture, reranker)
    assert await fixture.recall("unknown fact", limit=5) == []
    assert len(reranker.calls) == 1


@pytest.mark.asyncio
async def test_one_relevant_source_does_not_require_padding_the_context(monkeypatch):
    fixture = RecallFixture([_Hit("a", .9), _Hit("b", .8)],
                            [_node("a", confidence=.9), _node("b", confidence=.9)])
    calibrated(monkeypatch, fixture, _RecordingReranker(scores={0: .98, 1: .03}))
    rows = await fixture.recall("a relevant fact", limit=5)
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["rerank_score"] == .98
    assert rows[0]["rerank_calibration"] == "configuration_verified_weights_unverified"


@pytest.mark.asyncio
async def test_model_change_invalidates_old_abstention_calibration(monkeypatch, caplog):
    fixture = RecallFixture([_Hit("a", .9)], [_node("a", confidence=.9)])
    reranker = _RecordingReranker(scores={0: .01})
    metadata = calibrated(monkeypatch, fixture, reranker)
    metadata["model"] = "different-reranker"
    rows = await fixture.recall("q", limit=5)
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["rerank_calibration"] == "mismatch"
    assert "threshold disabled" in caplog.text


@pytest.mark.asyncio
async def test_threshold_without_calibration_is_not_assumed_portable(monkeypatch):
    fixture = RecallFixture([_Hit("a", .9)], [_node("a", confidence=.9)])
    fixture.graph.set_rerank_fn(_RecordingReranker(scores={0: .01}).rerank)
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_RERANK_MIN_SCORE", ".8")
    monkeypatch.delenv("COLONY_RECALL_RERANK_CALIBRATION", raising=False)
    rows = await fixture.recall("q", limit=5)
    assert rows[0]["rerank_calibration"] == "unverified"


@pytest.mark.asyncio
async def test_supersession_pointer_excludes_old_fact_even_before_state_catches_up(monkeypatch):
    fixture = RecallFixture([_Hit("old", .99)], [_node("old", superseded_by="new")])
    assert await fixture.recall("current fact", limit=4) == []


def test_source_handles_survive_context_rendering():
    rendered = render_memory_context([dict(id="memory-1", content="Two rooms are reported.",
        source_uri="voice:synthetic:4#t=3,7", epistemic_state="observed", contradiction_count=1)])
    assert '"id": "memory-1"' in rendered
    assert '"source": "voice:synthetic:4#t=3,7"' in rendered
    assert '"contradictions": 1' in rendered
    assert "Two rooms are reported." in rendered


def test_search_syntax_cannot_become_a_lucene_operator():
    query = lexical_query('ZXQ-417 OR content:* ") secret:"coffee')
    assert query == '"zxq-417" OR "content" OR "secret" OR "coffee"'

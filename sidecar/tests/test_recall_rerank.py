"""Reranker wire-in for recall (U12): off/shadow/on, inline hard-cap,
fail-open to ANN order.

Locks: default off never calls the reranker and the vector path stays
byte-identical; shadow measures but returns ANN order; on reorders by
rerank_score * effective_confidence; timeout/exception fail open with a
warn-once (~5 min) throttle; candidates <= limit skips the rerank.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pytest

from colony_sidecar.intelligence.graph import client as client_mod
from test_recall_ranking import RecallFixture, _Hit, _node


@dataclass
class _RR:
    index: int
    score: float
    text: str = ""


class _RecordingReranker:
    def __init__(self, scores=None, delay=0.0, exc=None):
        self.calls = []
        self._scores = scores or {}
        self._delay = delay
        self._exc = exc

    async def rerank(self, query, documents, top_k=10):
        self.calls.append({"query": query, "documents": list(documents),
                           "top_k": top_k})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return [_RR(index=i, score=self._scores.get(i, 0.0))
                for i in range(len(documents))]


def _fixture_three():
    """Three candidates, ANN order c1 > c2 > c3 by relevance."""
    fx = RecallFixture(
        hits=[_Hit("c1", 0.9), _Hit("c2", 0.8), _Hit("c3", 0.7)],
        node_props=[
            _node("c1", strength=1.0, confidence=0.9),
            _node("c2", strength=1.0, confidence=0.9),
            _node("c3", strength=1.0, confidence=0.9),
        ],
    )
    return fx


@pytest.mark.asyncio
async def test_default_off_never_calls_reranker(monkeypatch):
    """Regression lock: flag unset -> ANN ordering, reranker untouched."""
    monkeypatch.delenv("COLONY_RECALL_RERANK", raising=False)
    fx = _fixture_three()
    rr = _RecordingReranker(scores={2: 9.0, 1: 5.0, 0: 1.0})
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=2)
    assert rr.calls == []
    assert [m["id"] for m in out] == ["c1", "c2"]
    assert out[0]["relevance"] == pytest.approx(0.9 * 0.9)


@pytest.mark.asyncio
async def test_skipped_when_candidates_fit_limit(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    fx = _fixture_three()
    rr = _RecordingReranker(scores={2: 9.0})
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=10)  # 3 candidates <= limit
    assert rr.calls == []
    assert [m["id"] for m in out] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_on_reorders_by_rerank_times_confidence(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    monkeypatch.delenv("COLONY_RECALL_STRENGTH_RANKING", raising=False)
    fx = _fixture_three()
    # reranker prefers the ANN loser
    rr = _RecordingReranker(scores={0: 0.1, 1: 0.5, 2: 0.9})
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=2)
    assert len(rr.calls) == 1
    assert rr.calls[0]["top_k"] == 3
    assert [m["id"] for m in out] == ["c3", "c2"]
    assert out[0]["relevance"] == pytest.approx(0.9 * 0.9)  # score * confidence


@pytest.mark.asyncio
async def test_on_blends_strength_when_strength_ranking_on(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    monkeypatch.setenv("COLONY_RECALL_STRENGTH_RANKING", "on")
    fx = RecallFixture(
        hits=[_Hit("a", 0.9), _Hit("b", 0.8), _Hit("c", 0.7)],
        node_props=[
            _node("a", strength=0.4, confidence=0.9),
            _node("b", strength=1.0, confidence=0.9),
            _node("c", strength=1.0, confidence=0.9),
        ],
    )
    rr = _RecordingReranker(scores={0: 0.9, 1: 0.5, 2: 0.1})
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=2)
    by_id = {m["id"]: m for m in out}
    assert by_id["a"]["relevance"] == pytest.approx(0.9 * 0.9 * (0.5 + 0.5 * 0.4))
    assert by_id["b"]["relevance"] == pytest.approx(0.5 * 0.9 * 1.0)


@pytest.mark.asyncio
async def test_shadow_logs_but_returns_ann_order(monkeypatch, caplog):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "shadow")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    fx = _fixture_three()
    rr = _RecordingReranker(scores={0: 0.1, 1: 0.5, 2: 0.9})
    fx.graph.set_rerank_fn(rr.rerank)
    with caplog.at_level(logging.INFO, logger=client_mod.__name__):
        out = await fx.recall("q", limit=2)
    assert len(rr.calls) == 1  # measured
    assert [m["id"] for m in out] == ["c1", "c2"]  # ANN order preserved
    assert out[0]["relevance"] == pytest.approx(0.9 * 0.9)  # untouched
    assert any("rerank shadow" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_timeout_fails_open_to_ann_order(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    monkeypatch.setenv("COLONY_RECALL_RERANK_TIMEOUT_MS", "30")
    fx = _fixture_three()
    rr = _RecordingReranker(scores={2: 9.0}, delay=5.0)
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=2)
    assert [m["id"] for m in out] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_exception_fails_open_to_ann_order(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    fx = _fixture_three()
    rr = _RecordingReranker(exc=RuntimeError("reranker down"))
    fx.graph.set_rerank_fn(rr.rerank)
    out = await fx.recall("q", limit=2)
    assert [m["id"] for m in out] == ["c1", "c2"]
    assert out[0]["relevance"] == pytest.approx(0.9 * 0.9)


@pytest.mark.asyncio
async def test_failure_warns_once_per_five_minutes(monkeypatch, caplog):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_OVERSAMPLE", "3")
    fx = _fixture_three()
    rr = _RecordingReranker(exc=RuntimeError("reranker down"))
    fx.graph.set_rerank_fn(rr.rerank)
    with caplog.at_level(logging.WARNING, logger=client_mod.__name__):
        await fx.recall("q", limit=2)
        await fx.recall("q", limit=2)
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "rerank failed" in r.message]
    assert len(warns) == 1  # second failure inside the window is debug-only


def test_set_rerank_fn_mirrors_set_embed_fn():
    fx = _fixture_three()
    assert getattr(fx.graph, "_rerank_fn", None) is None

    async def fn(query, documents, top_k=10):
        return []

    fx.graph.set_rerank_fn(fn)
    assert fx.graph._rerank_fn is fn

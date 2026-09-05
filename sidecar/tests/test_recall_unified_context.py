"""Beliefs and source quotations share authorization, selection and budget."""
import asyncio
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.intelligence.graph.recall import (
    calibration_fingerprint, pack_memory_context, provider_calibration_metadata,
    source_candidates,
)
from colony_sidecar.intelligence.graph.selection import RecallSelector
from test_recall_ranking import RecallFixture, _Hit, _node
from test_turn_source_evidence import source_app, envelope, recalled


class Reranker:
    def __init__(self, score=.99, fail=False):
        self.score, self.fail, self.calls = score, fail, []

    def calibration_metadata(self):
        return {"model": "neutral-fixture", "format_version": "v1"}

    async def rerank(self, query, documents, top_k):
        self.calls.append(list(documents))
        if self.fail:
            raise RuntimeError("fixture unavailable")
        return [{"index": i, "score": self.score} for i in range(len(documents))]


def calibrate(monkeypatch, reranker):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on")
    monkeypatch.setenv("COLONY_RECALL_RERANK_MIN_SCORE", ".8")
    monkeypatch.setenv("COLONY_RECALL_RERANK_CALIBRATION", calibration_fingerprint(
        provider_calibration_metadata(reranker)))


class Graph:
    def __init__(self, rows):
        self.rows, self.calls, self.used = rows, [], []

    async def recall_candidates(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows

    async def recall(self, **kwargs):
        raise AssertionError("mixed context must not rerank graph separately")

    def record_recall_use(self, rows):
        self.used.extend(row["id"] for row in rows if row["kind"] == "belief")


def belief(content="A hydrofoil departure was reported."):
    return {"id": "belief-a", "content": content, "source_uri": "turn:earlier",
            "epistemic_state": "inferred", "effective_confidence": .9, "relevance": .8}


@pytest.mark.asyncio
async def test_irrelevant_quotations_cannot_bypass_combined_abstention(source_app, monkeypatch):
    reranker = Reranker(score=.02)
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, "_reranker", reranker)
    graph = Graph([belief()])
    monkeypatch.setattr(host, "_graph", graph)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        assert (await client.put("/v2/host/turns/quote", json=envelope("quote"))).status_code == 201
        assert await recalled(client, query="hydrofoil hull insurance") == ""
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0]) == 2
    assert graph.calls[0]["person_id"] == "contact-a"
    assert graph.used == []


@pytest.mark.asyncio
async def test_one_context_section_preserves_both_kinds_and_one_budget(source_app, monkeypatch):
    reranker = Reranker()
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, "_reranker", reranker)
    monkeypatch.setenv("COLONY_RECALL_CONTEXT_MAX_CHARS", "1400")
    graph = Graph([belief()])
    monkeypatch.setattr(host, "_graph", graph)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        body = envelope("quote")
        body["user_message"]["content"] = "The hydrofoil departure is Friday at nine."
        await client.put("/v2/host/turns/quote", json=body)
        response = await client.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "test-host"},
            "context": {"contact_id": "contact-a", "session_id": "session-b"},
            "incoming_message": {"role": "user", "content": "hydrofoil"},
        })
    sections = response.json()["sections"]
    assert not any(section["id"] == "colony-conversation-evidence" for section in sections)
    memory = [section for section in sections if section["id"] == "colony-memory"]
    assert len(memory) == 1
    text = memory[0]["body"]
    assert len(text) <= 1400
    assert '"kind": "belief"' in text and '"kind": "source_quote"' in text
    assert '"source": "turn:quote"' in text and '"role": "user"' in text
    assert graph.used == ["belief-a"]
    assert len(reranker.calls) == 1


@pytest.mark.asyncio
async def test_graph_unavailable_does_not_bypass_source_abstention(source_app, monkeypatch):
    reranker = Reranker(score=.01)
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, "_reranker", reranker)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await client.put("/v2/host/turns/quote", json=envelope("quote"))
        assert await recalled(client) == ""
    assert len(reranker.calls) == 1


@pytest.mark.asyncio
async def test_visibility_filter_runs_before_combined_model_call(source_app, monkeypatch):
    reranker = Reranker()
    calibrate(monkeypatch, reranker)
    monkeypatch.setattr(host, "_reranker", reranker)
    private = {**belief("Private unrelated fact"), "id": "private"}
    graph = Graph([private, belief()])
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setattr(host, "_p8_filter_graph_recall", lambda rows: [row for row in rows if row["id"] != "private"])
    graph._filter_erased_source_memories = AsyncMock(side_effect=lambda rows: rows)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        assert "hydrofoil" in await recalled(client)
    assert reranker.calls == [["A hydrofoil departure was reported."]]
    graph._filter_erased_source_memories.assert_awaited_once()


@pytest.mark.asyncio
async def test_candidate_retrieval_does_not_reinforce_or_rerank(monkeypatch):
    fixture = RecallFixture([_Hit("a", .9)], [_node("a")])
    fixture.graph._maybe_rerank = AsyncMock(side_effect=AssertionError("premature ranking"))
    fixture.graph._touch_memory_safe = AsyncMock()
    rows = await fixture.graph.recall_candidates(query="q", limit=25)
    assert [row["id"] for row in rows] == ["a"]
    fixture.graph._touch_memory_safe.assert_not_awaited()
    fixture.graph.record_recall_use([{**rows[0], "kind": "belief"}, {"id": "source", "kind": "source_quote"}])
    await asyncio.gather(*fixture.graph._bg_tasks)
    fixture.graph._touch_memory_safe.assert_awaited_once_with("a")


def quotes():
    return source_candidates([{"turn_id": "turn-1", "role": "assistant", "content": "Quoted claim.",
                               "occurred_at": "2026-01-02", "ingested_at": "2026-01-03"}])


@pytest.mark.asyncio
async def test_reranker_failure_has_one_bounded_annotated_fallback(monkeypatch):
    reranker = Reranker(fail=True)
    calibrate(monkeypatch, reranker)
    selector = RecallSelector(reranker.rerank, calibration_metadata=lambda: provider_calibration_metadata(reranker))
    rows, text = await selector.select_context("q", [belief()], quotes(), max_chars=1200)
    assert len(rows) == 2 and len(text) <= 1200
    assert all(row["rerank_status"] == "unavailable" for row in rows)
    assert len(reranker.calls) == 1


def test_shared_budget_marks_truncation_without_mutating_original_evidence():
    original = {**quotes()[0], "content": 'A long "quoted" passage.\n' * 400}
    rows, text = pack_memory_context([original, belief()], max_chars=700)
    assert len(text) <= 700 and rows[0]["excerpt_truncated"] is True
    assert rows[0]["source_uri"] == "turn:turn-1"
    assert rows[0]["role"] == "assistant"
    assert original["content"] == 'A long "quoted" passage.\n' * 400
    assert "excerpt_truncated" not in original


@pytest.mark.asyncio
async def test_one_result_limit_covers_sources_and_beliefs(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    beliefs = [{**belief(), "id": f"b-{i}"} for i in range(10)]
    sources = [{**quotes()[0], "id": f"s-{i}"} for i in range(10)]
    rows, text = await RecallSelector().select_context("q", beliefs, sources, limit=5)
    assert len(rows) == 5 and len(text) <= 6000
    assert {row["kind"] for row in rows} == {"belief", "source_quote"}

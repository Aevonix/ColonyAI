"""Tests for ResearchOrchestrator WEB and API source wiring."""

from __future__ import annotations

import pytest

from colony_sidecar.intelligence.components.research_orchestrator import (
    ResearchOrchestrator,
    ResearchResult,
    ResearchSource,
    SourceType,
)
from colony_sidecar.research.search.base import SearchResult


class _StubSearchOrchestrator:
    def __init__(self, results):
        self._results = results

    @property
    def has_providers(self) -> bool:
        return True

    async def search(self, query: str, max_results: int = 5):
        return self._results[:max_results]


@pytest.mark.asyncio
async def test_web_source_uses_search_orchestrator():
    results = [
        SearchResult(
            title="Colony AI",
            url="https://example.com/colony",
            snippet="A cognitive layer for agents.",
            source="duckduckgo",
            rank=1,
        ),
        SearchResult(
            title="Swarm intelligence",
            url="https://example.com/swarm",
            snippet="Emergent behavior in ant colonies.",
            source="duckduckgo",
            rank=2,
        ),
    ]
    orch = ResearchOrchestrator(
        graph_client=None,
        event_bus=None,
        search_orchestrator=_StubSearchOrchestrator(results),
    )
    orch.register_source(ResearchSource(type=SourceType.WEB, name="web", priority=0.9))
    report = await orch.research("colony ai", max_sources=1)
    assert len(report.results) == 1
    r = report.results[0]
    assert "Colony AI" in r.content
    assert r.citations == [
        "https://example.com/colony",
        "https://example.com/swarm",
    ]


@pytest.mark.asyncio
async def test_web_source_without_orchestrator_returns_empty():
    orch = ResearchOrchestrator(search_orchestrator=None)
    orch.register_source(ResearchSource(type=SourceType.WEB, name="web", priority=0.9))
    report = await orch.research("x")
    assert report.results == []


@pytest.mark.asyncio
async def test_api_source_calls_registered_handler():
    orch = ResearchOrchestrator()
    orch.register_source(ResearchSource(type=SourceType.API, name="weather", priority=0.9))

    async def _weather_handler(query: str):
        return ResearchResult(source="weather", content=f"forecast for {query}", confidence=0.7)

    orch.register_api_handler("weather", _weather_handler)
    report = await orch.research("tokyo")
    assert len(report.results) == 1
    assert report.results[0].content == "forecast for tokyo"


@pytest.mark.asyncio
async def test_api_source_without_handler_skips():
    orch = ResearchOrchestrator()
    orch.register_source(ResearchSource(type=SourceType.API, name="unregistered", priority=0.9))
    report = await orch.research("x")
    assert report.results == []

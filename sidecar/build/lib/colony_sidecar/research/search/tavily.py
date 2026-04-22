"""Tavily search provider — designed for AI agents, returns clean content."""

from __future__ import annotations

import logging
from typing import Optional

from colony_sidecar.research.search.base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class TavilyProvider(SearchProvider):
    """Tavily search API — optimized for AI agent use cases."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._base_url = "https://api.tavily.com/search"

    @property
    def name(self) -> str:
        return "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not HAS_HTTPX:
            logger.warning("httpx not installed — cannot use Tavily")
            return []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self._base_url, json={
                    "api_key": self._api_key,
                    "query": query,
                    "max_results": max_results,
                    "include_raw_content": True,
                    "search_depth": "advanced",
                })
                resp.raise_for_status()
                data = resp.json()
                return [
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        snippet=r.get("content", "")[:500],
                        content=r.get("raw_content"),
                        source="tavily",
                        rank=i,
                    )
                    for i, r in enumerate(data.get("results", []))
                ]
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return []

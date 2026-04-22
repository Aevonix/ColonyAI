"""SerpAPI search provider."""

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


class SerpAPIProvider(SearchProvider):
    """Google search via SerpAPI."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._base_url = "https://serpapi.com/search"

    @property
    def name(self) -> str:
        return "serpapi"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not HAS_HTTPX:
            logger.warning("httpx not installed — cannot use SerpAPI")
            return []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self._base_url, params={
                    "q": query,
                    "api_key": self._api_key,
                    "engine": "google",
                    "num": max_results,
                    "output": "json",
                })
                resp.raise_for_status()
                data = resp.json()
                results = []
                for i, r in enumerate(data.get("organic_results", [])[:max_results]):
                    results.append(SearchResult(
                        title=r.get("title", ""),
                        url=r.get("link", ""),
                        snippet=r.get("snippet", ""),
                        content=r.get("snippet"),  # SerpAPI doesn't return full content
                        source="serpapi",
                        rank=i,
                    ))
                return results
        except Exception as e:
            logger.warning("SerpAPI search failed: %s", e)
            return []

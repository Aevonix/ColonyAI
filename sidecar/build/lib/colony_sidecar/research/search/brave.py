"""Brave Search API provider."""

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


class BraveSearchProvider(SearchProvider):
    """Brave Search API — privacy-focused search."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._base_url = "https://api.search.brave.com/res/v1/web/search"

    @property
    def name(self) -> str:
        return "brave"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not HAS_HTTPX:
            logger.warning("httpx not installed — cannot use Brave Search")
            return []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self._base_url, params={
                    "q": query,
                    "count": max_results,
                }, headers={
                    "X-Subscription-Token": self._api_key,
                    "Accept": "application/json",
                })
                resp.raise_for_status()
                data = resp.json()
                results = []
                for i, r in enumerate(data.get("web", {}).get("results", [])[:max_results]):
                    results.append(SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        snippet=r.get("description", ""),
                        content=None,
                        source="brave",
                        rank=i,
                    ))
                return results
        except Exception as e:
            logger.warning("Brave Search failed: %s", e)
            return []

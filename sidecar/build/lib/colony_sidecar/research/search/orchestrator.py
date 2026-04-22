"""Search orchestrator — routes queries, handles rate limits, caches results."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from colony_sidecar.research.search.base import SearchProvider, SearchResult
from colony_sidecar.research.search.cache import SearchCache

logger = logging.getLogger(__name__)


class SearchOrchestrator:
    """Routes search queries to providers, handles rate limits and caching.

    If no providers are configured, returns empty results (graceful degradation).
    """

    def __init__(
        self,
        providers: Optional[List[SearchProvider]] = None,
        cache_ttl: int = 3600,
        max_cache_entries: int = 500,
    ):
        self._providers: Dict[str, SearchProvider] = {}
        if providers:
            for p in providers:
                self._providers[p.name] = p
        self._cache = SearchCache(ttl_seconds=cache_ttl, max_entries=max_cache_entries)
        self._rate_tracker: Dict[str, List[float]] = {name: [] for name in self._providers}
        self._default_provider = next(iter(self._providers), None)

    @property
    def has_providers(self) -> bool:
        return len(self._providers) > 0

    def add_provider(self, provider: SearchProvider) -> None:
        """Add a search provider."""
        self._providers[provider.name] = provider
        self._rate_tracker[provider.name] = []
        if not self._default_provider:
            self._default_provider = provider

    async def search(
        self,
        query: str,
        max_results: int = 5,
        provider: str = "",
    ) -> List[SearchResult]:
        """Execute a search query.

        Args:
            query: Search query string
            max_results: Maximum number of results
            provider: Preferred provider name (empty = auto-select)

        Returns:
            List of search results, empty if no providers available.
        """
        if not self._providers:
            return []

        # Check cache
        cached = self._cache.get(query)
        if cached is not None:
            logger.debug("Search cache hit for '%s'", query[:50])
            return cached[:max_results]

        # Select provider
        prov = self._select_provider(provider)
        if not prov:
            return []

        # Rate limit check
        if not self._check_rate_limit(prov.name):
            prov = self._fallback_provider(prov.name)
            if not prov:
                logger.warning("All search providers rate-limited")
                return []

        # Execute search
        try:
            results = await prov.search(query, max_results)
            self._cache.put(query, results)
            logger.info("Search via %s: '%s' → %d results", prov.name, query[:50], len(results))
            return results
        except Exception as e:
            logger.warning("Search via %s failed: %s", prov.name, e)
            return []

    def _select_provider(self, preferred: str) -> Optional[SearchProvider]:
        if preferred and preferred in self._providers:
            return self._providers[preferred]
        return self._default_provider

    def _fallback_provider(self, exclude: str) -> Optional[SearchProvider]:
        for name, prov in self._providers.items():
            if name != exclude and self._check_rate_limit(name):
                return prov
        return None

    def _check_rate_limit(self, name: str) -> bool:
        if name not in self._rate_tracker:
            return False
        now = time.time()
        # Clean up entries older than 60 seconds
        self._rate_tracker[name] = [t for t in self._rate_tracker[name] if now - t < 60]
        prov = self._providers.get(name)
        if not prov:
            return False
        return len(self._rate_tracker[name]) < prov.rate_limit_per_minute

    def clear_cache(self) -> None:
        self._cache.clear()

    def list_providers(self) -> List[str]:
        return list(self._providers.keys())

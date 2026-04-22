"""Search result cache to avoid duplicate API calls."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, List, Optional

from colony_sidecar.research.search.base import SearchResult

logger = logging.getLogger(__name__)


class SearchCache:
    """In-memory search result cache with TTL."""

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 500):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: Dict[str, tuple[float, List[SearchResult]]] = {}

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.lower().strip().encode()).hexdigest()

    def get(self, query: str) -> Optional[List[SearchResult]]:
        key = self._key(query)
        entry = self._cache.get(key)
        if entry is None:
            return None
        timestamp, results = entry
        if time.time() - timestamp > self._ttl:
            del self._cache[key]
            return None
        return results

    def put(self, query: str, results: List[SearchResult]) -> None:
        # Evict oldest entries if at capacity
        if len(self._cache) >= self._max_entries:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest_key]

        key = self._key(query)
        self._cache[key] = (time.time(), results)

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)

"""Base classes for search providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SearchResult:
    """A single search result."""

    title: str
    url: str
    snippet: str
    content: Optional[str] = None  # Full page content if available
    source: str = ""
    rank: int = 0
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SearchProvider(ABC):
    """Base class for search API providers."""

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Execute a search query and return results."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'tavily', 'serpapi')."""
        ...

    @property
    def rate_limit_per_minute(self) -> int:
        """Maximum requests per minute."""
        return 30

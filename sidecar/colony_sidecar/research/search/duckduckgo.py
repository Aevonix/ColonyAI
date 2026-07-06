"""DuckDuckGo HTML search provider — zero-config fallback.

Uses the lite HTML endpoint (https://html.duckduckgo.com/html/) because
it requires no API key and no JS rendering. This is best-effort — DDG
can change their markup at any time — so any failure degrades to an
empty result set rather than raising.
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
from typing import List

import httpx

from colony_sidecar.research.search.base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)


_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a" href="(?P<url>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>"
    r'.*?<a class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _html.unescape(_TAG_RE.sub("", s)).strip()


class DuckDuckGoProvider(SearchProvider):
    """HTML-endpoint DuckDuckGo provider.

    No API key required. Fragile against markup changes — callers
    MUST tolerate empty results silently.
    """

    _ENDPOINT = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout_secs: float = 8.0) -> None:
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "duckduckgo"

    @property
    def rate_limit_per_minute(self) -> int:
        # DuckDuckGo has no documented limit; stay polite.
        return 20

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        params = {"q": query}
        try:
            # UA is deployment-overridable (COLONY_SEARCH_USER_AGENT); the
            # default is a neutral project identifier with no deployment URL.
            _ua = os.environ.get(
                "COLONY_SEARCH_USER_AGENT",
                "Mozilla/5.0 (compatible; ColonyAI research crawler)")
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": _ua,
                    "Accept": "text/html,application/xhtml+xml",
                },
                follow_redirects=True,
            ) as client:
                resp = await client.post(self._ENDPOINT, data=params)
                resp.raise_for_status()
                body = resp.text
        except Exception as exc:
            logger.warning("DuckDuckGo request failed: %s", exc)
            return []

        results: List[SearchResult] = []
        for i, m in enumerate(_RESULT_RE.finditer(body)):
            if i >= max_results:
                break
            url = m.group("url")
            # DDG wraps external URLs in a redirect; strip it when present.
            if url.startswith("//duckduckgo.com/l/?uddg="):
                from urllib.parse import parse_qs, unquote, urlparse
                qs = parse_qs(urlparse("https:" + url).query)
                uddg = qs.get("uddg", [""])[0]
                if uddg:
                    url = unquote(uddg)
            results.append(
                SearchResult(
                    title=_strip_tags(m.group("title")),
                    url=url,
                    snippet=_strip_tags(m.group("snippet")),
                    source=self.name,
                    rank=i + 1,
                )
            )
        return results

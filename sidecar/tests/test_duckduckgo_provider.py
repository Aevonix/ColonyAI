"""Unit tests for DuckDuckGoProvider — parser + resilience."""

from __future__ import annotations

import httpx
import pytest

from colony_sidecar.research.search.duckduckgo import DuckDuckGoProvider


_SAMPLE_HTML = """\
<html><body>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://example.com/a">Example A</a>
  <a class="result__snippet" href="/x">First snippet</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb&amp;rut=x">Example B</a>
  <a class="result__snippet" href="/y">Second snippet</a>
</div>
</body></html>
"""


@pytest.mark.asyncio
async def test_duckduckgo_parses_results(monkeypatch):
    async def _fake_post(self, url, data):
        req = httpx.Request("POST", url)
        return httpx.Response(200, text=_SAMPLE_HTML, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    provider = DuckDuckGoProvider()
    results = await provider.search("colony ai", max_results=5)
    assert len(results) == 2
    assert results[0].title == "Example A"
    assert results[0].url == "https://example.com/a"
    assert results[0].snippet == "First snippet"
    # Redirect unwrapping
    assert results[1].url == "https://example.org/b"


@pytest.mark.asyncio
async def test_duckduckgo_empty_on_network_error(monkeypatch):
    async def _raising_post(self, url, data):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx.AsyncClient, "post", _raising_post)
    provider = DuckDuckGoProvider()
    assert await provider.search("anything") == []


@pytest.mark.asyncio
async def test_duckduckgo_respects_max_results(monkeypatch):
    async def _fake_post(self, url, data):
        req = httpx.Request("POST", url)
        return httpx.Response(200, text=_SAMPLE_HTML, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    provider = DuckDuckGoProvider()
    assert len(await provider.search("q", max_results=1)) == 1

"""Unit harness for the colony-memory Hermes provider (plugins/colony-memory).

The provider previously had no test coverage at all; this loads it straight
from the plugin directory (it is a standalone module, no Hermes install
needed) and exercises the prefetch-cache and per-turn-contact logic with a
stubbed httpx transport.

Regression locks:
  * COLONY_PREFETCH_QUERY_CHECK unset/0 -> prefetch() consumes whatever the
    background prefetch cached, regardless of query (legacy behavior).
  * COLONY_PREFETCH_TURN_CONTACT unset/0 -> /context/assemble and the
    temporal brief are requested with the provider-wide self._contact_id.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import types

import pytest

_PROVIDER_PATH = (pathlib.Path(__file__).resolve().parents[2]
                  / "plugins" / "colony-memory" / "provider.py")


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "colony_memory_provider_under_test", _PROVIDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def provider_mod():
    return _load_provider_module()


# --- stubbed httpx -----------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpx:
    """Drop-in for the provider module's `httpx` attribute. Records every
    request and answers from a route table {(method, path_suffix): payload}."""

    class HTTPError(Exception):
        pass

    class HTTPStatusError(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests = []
        fake = self

        class _Client:
            def __init__(self, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def _handle(self, method, url, **kwargs):
                fake.requests.append(
                    {"method": method, "url": url,
                     "params": kwargs.get("params"),
                     "json": kwargs.get("json")})
                for (m, suffix), payload in fake.routes.items():
                    if m == method and url.endswith(suffix):
                        return _FakeResponse(payload=payload)
                return _FakeResponse(payload={})

            def get(self, url, **kwargs):
                return self._handle("GET", url, **kwargs)

            def post(self, url, **kwargs):
                return self._handle("POST", url, **kwargs)

        self.Client = _Client


def _make_provider(provider_mod, fake_httpx, monkeypatch):
    monkeypatch.setattr(provider_mod, "httpx", fake_httpx)
    p = provider_mod.ColonyMemoryProvider(config={
        "url": "http://sidecar.test", "api_key": "k", "contact_id": "cid-base"})
    return p


_ASSEMBLE = ("POST", "/v1/host/context/assemble")
_TEMPORAL = ("GET", "/v1/host/context/temporal")
_RESOLVE = ("GET", "/v1/host/contacts/resolve")


def _assemble_calls(fake):
    return [r for r in fake.requests if r["url"].endswith(_ASSEMBLE[1])]


# --- U14: prefetch cached-query match ---------------------------------------

def test_prefetch_default_consumes_cache_regardless_of_query(
        provider_mod, monkeypatch):
    """Regression lock: flag off (default) keeps the legacy consume-any path."""
    monkeypatch.delenv("COLONY_PREFETCH_QUERY_CHECK", raising=False)
    fake = _FakeHttpx(routes={
        _ASSEMBLE: {"sections": [{"title": "M", "body": "cached-A", "priority": 90}]},
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    })
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.queue_prefetch("query A", session_id="s1")
    p._prefetch_thread.join(timeout=5)
    out = p.prefetch("query B", session_id="s1")   # DIFFERENT query
    assert "cached-A" in out                        # legacy: consumed anyway
    assert len(_assemble_calls(fake)) == 1          # no fresh fetch
    assert p._stale_cache_misses == 0


def test_prefetch_query_check_rejects_stale_cache(provider_mod, monkeypatch):
    monkeypatch.setenv("COLONY_PREFETCH_QUERY_CHECK", "1")
    fake = _FakeHttpx(routes={
        _ASSEMBLE: {"sections": [{"title": "M", "body": "fresh", "priority": 90}]},
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    })
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.queue_prefetch("query A", session_id="s1")
    p._prefetch_thread.join(timeout=5)
    p.prefetch("query B", session_id="s1")          # mismatch -> fresh fetch
    assert len(_assemble_calls(fake)) == 2          # queued + fresh
    assert p._stale_cache_misses == 1
    # And the stale cache was dropped, not left to poison a later turn.
    assert p._cached_context == ""


def test_prefetch_query_check_consumes_matching_cache(provider_mod, monkeypatch):
    monkeypatch.setenv("COLONY_PREFETCH_QUERY_CHECK", "1")
    fake = _FakeHttpx(routes={
        _ASSEMBLE: {"sections": [{"title": "M", "body": "cached-A", "priority": 90}]},
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    })
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.queue_prefetch("query A", session_id="s1")
    p._prefetch_thread.join(timeout=5)
    out = p.prefetch("query A", session_id="s1")    # SAME query+session
    assert "cached-A" in out
    assert len(_assemble_calls(fake)) == 1          # cache hit, no re-fetch
    assert p._stale_cache_misses == 0


# --- U15: per-turn contact in prefetch ---------------------------------------

def test_prefetch_sync_default_uses_provider_contact(provider_mod, monkeypatch):
    """Regression lock: flag off (default) assembles with self._contact_id."""
    monkeypatch.delenv("COLONY_PREFETCH_TURN_CONTACT", raising=False)
    fake = _FakeHttpx(routes={_ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    monkeypatch.setattr(p, "_turn_contact", lambda: "cid-turn")
    p._prefetch_sync("hello", session_id="s1")
    calls = _assemble_calls(fake)
    assert len(calls) == 1
    assert calls[0]["json"]["context"]["contact_id"] == "cid-base"


def test_prefetch_sync_turn_contact_flag_uses_turn_contact(
        provider_mod, monkeypatch):
    monkeypatch.setenv("COLONY_PREFETCH_TURN_CONTACT", "1")
    fake = _FakeHttpx(routes={_ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    monkeypatch.setattr(p, "_turn_contact", lambda: "cid-turn")
    p._prefetch_sync("hello", session_id="s1")
    assert _assemble_calls(fake)[0]["json"]["context"]["contact_id"] == "cid-turn"


def test_prefetch_sync_turn_contact_fails_open(provider_mod, monkeypatch):
    monkeypatch.setenv("COLONY_PREFETCH_TURN_CONTACT", "1")
    fake = _FakeHttpx(routes={_ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)

    def _boom():
        raise RuntimeError("resolver down")

    monkeypatch.setattr(p, "_turn_contact", _boom)
    p._prefetch_sync("hello", session_id="s1")
    assert _assemble_calls(fake)[0]["json"]["context"]["contact_id"] == "cid-base"


def test_temporal_block_turn_contact_flag(provider_mod, monkeypatch):
    monkeypatch.setenv("COLONY_PREFETCH_TURN_CONTACT", "1")
    fake = _FakeHttpx(routes={_TEMPORAL: {"title": "Current Time", "body": "now"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    monkeypatch.setattr(p, "_turn_contact", lambda: "cid-turn")
    block = p._fresh_temporal_block_sync()
    assert "now" in block
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal[0]["params"]["contact_id"] == "cid-turn"
    # The 15s cache must not serve cid-turn's brief to a different contact.
    monkeypatch.setattr(p, "_turn_contact", lambda: "cid-other")
    p._fresh_temporal_block_sync()
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal[-1]["params"]["contact_id"] == "cid-other"


def test_temporal_block_default_uses_provider_contact(provider_mod, monkeypatch):
    monkeypatch.delenv("COLONY_PREFETCH_TURN_CONTACT", raising=False)
    fake = _FakeHttpx(routes={_TEMPORAL: {"title": "Current Time", "body": "now"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    monkeypatch.setattr(p, "_turn_contact", lambda: "cid-turn")
    p._fresh_temporal_block_sync()
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal[0]["params"]["contact_id"] == "cid-base"


def test_resolve_handle_ttl_cache(provider_mod, monkeypatch):
    """_resolve_handle results are TTL-cached so per-turn resolution does not
    hammer /contacts/resolve on every prefetch."""
    fake = _FakeHttpx(routes={_RESOLVE: {"contact_id": "cid-r"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    resolves = [r for r in fake.requests if r["url"].endswith(_RESOLVE[1])]
    assert len(resolves) == 1                       # second call served by TTL cache
    # Expired entry refetches.
    key = "sms:+15550001"
    ts, cid = p._handle_cache[key]
    p._handle_cache[key] = (ts - 3600.0, cid)
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    resolves = [r for r in fake.requests if r["url"].endswith(_RESOLVE[1])]
    assert len(resolves) == 2

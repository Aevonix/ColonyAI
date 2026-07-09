"""World-context query mode (U18): COLONY_WORLD_CONTEXT_QUERY=message/entities.

The Related Entities context section used to FTS the WHOLE message against the
world model — sentence noise in, noise entities out. `entities` mode extracts
proper-noun candidates first and ORs precise per-name lookups. Regression
lock: default (`message`) is exactly one whole-message find_entities call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host as host_mod


class _FakeWorldStore:
    def __init__(self, by_query=None):
        self.by_query = by_query or {}
        self.calls = []

    async def find_entities(self, query, limit=20, **kwargs):
        self.calls.append({"query": query, "limit": limit})
        return list(self.by_query.get(query, []))[:limit]


def _ent(eid, name):
    return SimpleNamespace(id=eid, name=name, entity_type="person")


_MSG = "did Robin Sanchez ever reply about the Falcon Initiative rollout?"


@pytest.mark.asyncio
async def test_default_mode_is_single_whole_message_call(monkeypatch):
    """Regression lock: message mode (default) = legacy single FTS call."""
    monkeypatch.delenv("COLONY_WORLD_CONTEXT_QUERY", raising=False)
    store = _FakeWorldStore(by_query={_MSG: [_ent("e1", "Robin Sanchez")]})
    monkeypatch.setattr(host_mod, "_world_store", store)
    out = await host_mod._world_context_entities(_MSG, limit=5)
    assert [e.id for e in out] == ["e1"]
    assert store.calls == [{"query": _MSG, "limit": 5}]


@pytest.mark.asyncio
async def test_entities_mode_ors_per_candidate_lookups(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_CONTEXT_QUERY", "entities")
    store = _FakeWorldStore(by_query={
        "Robin Sanchez": [_ent("e1", "Robin Sanchez")],
        "Falcon Initiative": [_ent("e2", "Falcon Initiative")],
    })
    monkeypatch.setattr(host_mod, "_world_store", store)
    out = await host_mod._world_context_entities(_MSG, limit=5)
    ids = {e.id for e in out}
    assert "e1" in ids
    # per-candidate lookups, not the whole sentence
    assert all(c["query"] != _MSG for c in store.calls)
    assert all(c["limit"] == 2 for c in store.calls)
    assert len(store.calls) <= 5


@pytest.mark.asyncio
async def test_entities_mode_dedupes_and_caps(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_CONTEXT_QUERY", "entities")
    dup = _ent("e1", "Robin Sanchez")
    store = _FakeWorldStore(by_query={
        "Robin Sanchez": [dup, dup],
        "Falcon Initiative": [dup, _ent("e2", "Falcon Initiative")],
    })
    monkeypatch.setattr(host_mod, "_world_store", store)
    out = await host_mod._world_context_entities(_MSG, limit=5)
    assert len([e for e in out if e.id == "e1"]) == 1     # deduped by id
    assert len(out) <= 5


@pytest.mark.asyncio
async def test_entities_mode_falls_back_on_empty_extraction(monkeypatch):
    """No proper nouns in the message -> the legacy whole-message call runs."""
    monkeypatch.setenv("COLONY_WORLD_CONTEXT_QUERY", "entities")
    msg = "hey, any update on that thing from earlier?"
    store = _FakeWorldStore(by_query={msg: [_ent("e9", "fallback")]})
    monkeypatch.setattr(host_mod, "_world_store", store)
    out = await host_mod._world_context_entities(msg, limit=5)
    assert [e.id for e in out] == ["e9"]
    assert store.calls[-1]["query"] == msg


@pytest.mark.asyncio
async def test_entities_mode_falls_back_on_extractor_error(monkeypatch):
    monkeypatch.setenv("COLONY_WORLD_CONTEXT_QUERY", "entities")
    store = _FakeWorldStore(by_query={_MSG: [_ent("e9", "fallback")]})
    monkeypatch.setattr(host_mod, "_world_store", store)

    class _Boom:
        async def extract(self, *a, **k):
            raise RuntimeError("extractor down")

    monkeypatch.setattr(host_mod, "_get_conversation_extractor", lambda: _Boom())
    out = await host_mod._world_context_entities(_MSG, limit=5)
    assert [e.id for e in out] == ["e9"]

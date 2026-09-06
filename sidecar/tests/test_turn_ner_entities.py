"""Canonical history keeps entity provenance without redundant graph memory."""

from __future__ import annotations

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import (
    HostIdentity, HostMessage, HostTurnContext, TurnSyncRequest)
from colony_sidecar.gate.context_provenance import ContextProvenanceStore


class _FakeGraph:
    def __init__(self):
        self.calls = []

    async def record_turn(self, **kwargs):
        self.calls.append(kwargs)
        return "mem-1"


def _body(entities=(), user_text="have you heard from Robin Sanchez lately?"):
    return TurnSyncRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id="c1",
                                channel_id="rcs:conv-1"),
        entities=list(entities),
        summary="User: x\nAssistant: y",
        user_message=HostMessage(role="user", content=user_text),
    )


@pytest.fixture
def wired(monkeypatch):
    graph = _FakeGraph()
    store = ContextProvenanceStore(":memory:")
    monkeypatch.setattr(host_mod, "_graph", graph)
    monkeypatch.setattr(host_mod, "_context_provenance", store)
    return graph, store


@pytest.mark.asyncio
async def test_canonical_turn_keeps_provenance_without_graph_summary(wired):
    graph, store = wired
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    assert graph.calls == []
    # ...while provenance still records host + NER entities (legacy behavior).
    assert store.contexts_for("Robin Sanchez")
    assert store.contexts_for("Host Entity")


@pytest.mark.asyncio
async def test_legacy_summary_only_still_records_host_entities(wired):
    graph, store = wired
    body = _body(entities=["Host Entity"])
    body.user_message = None
    response = await host_mod.turns_sync(body)
    ents = graph.calls[0]["entities"]
    assert "Host Entity" in ents            # host entities always kept, first
    assert response.accepted and response.continuity_updated
    assert not response.source_recorded


@pytest.mark.asyncio
async def test_graph_failure_cannot_break_canonical_ingress_but_legacy_reports_it(wired, monkeypatch):
    async def fail(**kwargs):
        raise OSError("controlled graph unavailable")
    monkeypatch.setattr(wired[0], "record_turn", fail)
    response = await host_mod.turns_sync(_body())
    assert response.accepted and response.source_recorded and response.continuity_updated
    assert response.skipped_reason is None
    legacy = _body(); legacy.user_message = None
    response = await host_mod.turns_sync(legacy)
    assert not response.accepted and response.skipped_reason == "graph_record_failed"


@pytest.mark.asyncio
async def test_ner_failure_keeps_host_provenance(wired, monkeypatch):
    graph, store = wired

    class _Boom:
        async def extract(self, *a, **k):
            raise RuntimeError("extractor down")

    monkeypatch.setattr(host_mod, "_get_conversation_extractor", lambda: _Boom())
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    assert graph.calls == []
    assert store.contexts_for("Host Entity")


@pytest.mark.asyncio
async def test_single_extraction_shared_with_provenance(wired, monkeypatch):
    """The extractor runs ONCE per turn (was: once for provenance, and U20
    would have added a second for record_turn)."""
    calls = []

    class _Counting:
        async def extract(self, text, src):
            calls.append(text)

            class _R:
                entities = []
            return _R()

    monkeypatch.setattr(host_mod, "_get_conversation_extractor", lambda: _Counting())
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    assert len(calls) == 1

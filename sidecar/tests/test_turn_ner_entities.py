"""NER entities into record_turn (U20): COLONY_TURN_NER_ENTITIES.

record_turn used to see only body.entities — usually [] from real hosts — so
turn memories had no :MENTIONS edges and salience scored every exchange as
entity-free. With the flag on, the rule-based extraction (already run once
per turn for provenance) is merged into the record_turn entities (cap 12).
Regression lock: flag off keeps record_turn's entities == body.entities
byte-identical, while provenance still gets host + extracted entities.
"""

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
async def test_default_record_turn_gets_only_host_entities(wired, monkeypatch):
    """Regression lock: flag off -> record_turn sees exactly body.entities."""
    monkeypatch.delenv("COLONY_TURN_NER_ENTITIES", raising=False)
    graph, store = wired
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    assert graph.calls[0]["entities"] == ["Host Entity"]
    # ...while provenance still records host + NER entities (legacy behavior).
    assert store.contexts_for("Robin Sanchez")
    assert store.contexts_for("Host Entity")


@pytest.mark.asyncio
async def test_flag_merges_ner_into_record_turn(wired, monkeypatch):
    monkeypatch.setenv("COLONY_TURN_NER_ENTITIES", "1")
    graph, store = wired
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    ents = graph.calls[0]["entities"]
    assert "Host Entity" in ents            # host entities always kept, first
    assert "Robin Sanchez" in ents          # NER merged in
    assert store.contexts_for("Robin Sanchez")   # provenance unchanged


@pytest.mark.asyncio
async def test_flag_dedupes_and_caps_at_12(wired, monkeypatch):
    monkeypatch.setenv("COLONY_TURN_NER_ENTITIES", "1")
    graph, _ = wired
    text = ("Robin Sanchez met " + ", ".join(
        f"Alice Number{i} Smith" for i in range(1, 15)) + " yesterday.")
    await host_mod.turns_sync(_body(entities=["Robin Sanchez"], user_text=text))
    ents = graph.calls[0]["entities"]
    assert len(ents) <= 12
    assert len({e.lower() for e in ents}) == len(ents)   # deduped
    assert ents[0] == "Robin Sanchez"                    # host-first ordering


@pytest.mark.asyncio
async def test_flag_fails_open_to_host_entities(wired, monkeypatch):
    monkeypatch.setenv("COLONY_TURN_NER_ENTITIES", "1")
    graph, _ = wired

    class _Boom:
        async def extract(self, *a, **k):
            raise RuntimeError("extractor down")

    monkeypatch.setattr(host_mod, "_get_conversation_extractor", lambda: _Boom())
    await host_mod.turns_sync(_body(entities=["Host Entity"]))
    assert graph.calls[0]["entities"] == ["Host Entity"]


@pytest.mark.asyncio
async def test_single_extraction_shared_with_provenance(wired, monkeypatch):
    """The extractor runs ONCE per turn (was: once for provenance, and U20
    would have added a second for record_turn)."""
    monkeypatch.setenv("COLONY_TURN_NER_ENTITIES", "1")
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

"""turn-sync populates the context-provenance store: a turn's entities are recorded under
its conversation context (channel_id), so cross-context leak detection has live data."""

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import (
    HostIdentity, HostMessage, HostTurnContext, TurnSyncRequest)
from colony_sidecar.gate.context_provenance import ContextProvenanceStore


@pytest.mark.asyncio
async def test_turn_sync_records_entities_under_conversation(monkeypatch):
    store = ContextProvenanceStore(":memory:")
    monkeypatch.setattr(host_mod, "_context_provenance", store)
    monkeypatch.setattr(host_mod, "_graph", None)   # isolate: no graph side effects

    body = TurnSyncRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id="c1", channel_id="rcs:conv-9"),
        entities=["Project Falcon"],
        user_message=HostMessage(role="user", content="any update on Project Falcon?"),
    )
    await host_mod.turns_sync(body)

    contexts = store.contexts_for("Project Falcon")
    assert any(c["conversation_key"] == "rcs:conv-9" for c in contexts)


@pytest.mark.asyncio
async def test_turn_sync_without_channel_is_safe(monkeypatch):
    store = ContextProvenanceStore(":memory:")
    monkeypatch.setattr(host_mod, "_context_provenance", store)
    monkeypatch.setattr(host_mod, "_graph", None)
    body = TurnSyncRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id="c1"),  # no channel_id
        entities=["Project Falcon"],
    )
    await host_mod.turns_sync(body)   # must not raise
    assert store.contexts_for("Project Falcon") == []


@pytest.mark.asyncio
async def test_turn_sync_ner_populates_without_host_entities(monkeypatch):
    store = ContextProvenanceStore(":memory:")
    monkeypatch.setattr(host_mod, "_context_provenance", store)
    monkeypatch.setattr(host_mod, "_graph", None)
    body = TurnSyncRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id="c1", channel_id="rcs:conv-9"),
        user_message=HostMessage(role="user", content="have you heard from Robin Sanchez lately?"),
    )  # NB: no body.entities — relies on Colony NER
    await host_mod.turns_sync(body)
    assert store.contexts_for("Robin Sanchez")   # NER pulled it from the message text

"""Selective erasure across source, projection and disconnected-host boundaries."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response, Request
import pytest
from colony_sidecar.api.routers import host
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.idempotency import SourceErased
from test_hermes_turn_outbox import _load_client, _create_database, _CURRENT_SCHEMA, _PENDING_INDEX, _APPLICATION_ID

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    return TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")

def source(ledger, turn_id="turn-a", *, contact="contact-a", session="session-a", messages=None):
    messages = messages or [{"role": "user", "content": "The hydrofoil password is pâss-unique."}]
    ledger.record_source(turn_id, contact_id=contact, session_id=session, messages=messages)
    return messages

def queued(turn_id, messages, *, contact="contact-a"):
    return {"turn_id": turn_id, "contact_id": contact, "session_id": "session-a", "checkpoint_messages": messages}

def test_selective_redaction_survives_restart(ledger):
    messages = source(ledger)
    survivor = {"role": "user", "content": "The bicycle is blue."}
    source(ledger, "checkpoint-a", messages=messages + [survivor])
    source(ledger, "other-contact", contact="contact-b", messages=messages)
    source(ledger, "other-session", session="session-b", messages=messages)
    result = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    assert set(result["affected_source_ids"]) == {"turn-a", "checkpoint-a"}
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert reopened.is_source_erased("turn-a", "contact-a")
    assert not reopened.is_source_erased("turn-a", "contact-b")
    assert reopened.is_projection_erased("checkpoint-a")
    assert reopened.search_sources("bicycle", contact_id="contact-a", session_id="session-a")
    assert all(row["turn_id"] == "other-session" for row in reopened.search_sources("password", contact_id="contact-a", session_id="session-a"))
    assert reopened.search_sources("password", contact_id="contact-b", session_id="session-a")
    with sqlite3.connect(ledger.db_path) as conn:
        checkpoint = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='checkpoint-a'").fetchone()[0])
        tombstones = str(conn.execute("SELECT * FROM source_erasures").fetchall())
    assert checkpoint == [survivor] and "pâss-unique" not in tombstones
    with pytest.raises(SourceErased):
        source(reopened)
    source(reopened, "checkpoint-rewrapped", messages=messages + [survivor])
    assert reopened.search_sources("password", contact_id="contact-a", session_id="session-a")[0]["turn_id"] == "other-session"

def test_ambiguity_and_cross_contact_selection_are_non_mutating(ledger):
    message = source(ledger)
    source(ledger, "checkpoint-a", messages=message)
    with pytest.raises(ValueError, match="ambiguous_source"):
        ledger.erase_sources(contact_id="contact-a", old_text=message[0]["content"], session_id="session-a")
    with pytest.raises(ValueError, match="source_not_found"):
        ledger.erase_sources(contact_id="contact-b", turn_ids=["turn-a"])
    assert ledger.erasure_feed("contact-a")["head"] == 0

def test_feed_pages_and_detects_restore_behind_host(ledger):
    source(ledger, "a")
    source(ledger, "b", session="session-b")
    ledger.erase_sources(contact_id="contact-a", turn_ids=["a", "b"])
    first = ledger.erasure_feed("contact-a", limit=1)
    assert first["complete"] is False
    second = ledger.erasure_feed("contact-a", after=first["through"])
    assert second["complete"] is True and second["head"] == 2
    with pytest.raises(ValueError, match="cursor"):
        ledger.erasure_feed("contact-a", after=3)

def test_outbox_v1_migration_and_redaction(ledger, tmp_path):
    module = _load_client("source_erasure_outbox")
    path = tmp_path / "host.sqlite3"
    _create_database(path, [_CURRENT_SCHEMA, _PENDING_INDEX], application_id=_APPLICATION_ID, user_version=1)
    outbox = module.TurnOutbox(path)
    assert outbox.prepare()["user_version"] == 2
    messages = source(ledger)
    survivor = {"role": "user", "content": "The bicycle is blue."}
    outbox.enqueue("turn-a", queued("turn-a", messages))
    outbox.enqueue("checkpoint-a", queued("checkpoint-a", messages + [survivor]))
    outbox.enqueue("other-contact", queued("other-contact", messages, contact="contact-b"))
    ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    outbox.apply_erasure_page("contact-a", ledger.erasure_feed("contact-a"))
    reopened = module.TurnOutbox(path)
    rows = reopened.snapshot()
    assert len(rows) == 2 and rows[0]["turn_id"] == "other-contact"
    assert rows[1]["payload"]["checkpoint_messages"] == [survivor]
    assert reopened.enqueue("turn-a", queued("turn-a", messages))["state"] == "erased"
    assert len(reopened.snapshot()) == 2 and reopened.erasure_watermark("contact-a") == 1
    assert module.source_message_hash("session-a", messages[0]) == ledger.erasure_feed("contact-a")["events"][0]["message_hashes"][0]

def test_disconnected_replay_holds_then_reconciles_before_put(ledger, tmp_path, monkeypatch):
    module = _load_client("source_erasure_replay")
    outbox = module.TurnOutbox(tmp_path / "host.sqlite3")
    messages = source(ledger)
    outbox.enqueue("turn-a", queued("turn-a", messages))
    client = module.ColonyClient()
    writes = []
    monkeypatch.setattr(client, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(client, "put", lambda *a, **k: writes.append(k))
    deliver = lambda payload, timeout_seconds: client.sync_turn(**payload, outbox=outbox, timeout_seconds=timeout_seconds)
    assert outbox.drain(deliver, timeout_seconds=1) == 0
    assert outbox.snapshot()[0]["state"] == "pending" and not writes
    ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    monkeypatch.setattr(client, "get", lambda *a, **k: Response(200, json=ledger.erasure_feed("contact-a", k["params"]["after"]), request=Request("GET", "http://test")))
    assert outbox.drain(deliver, timeout_seconds=1) == 0
    assert not writes and outbox.snapshot() == []

def test_delivered_payload_is_purged_without_replaying_survivors(ledger, tmp_path):
    module = _load_client("source_erasure_delivered")
    outbox = module.TurnOutbox(tmp_path / "host.sqlite3")
    messages = source(ledger)
    outbox.enqueue("checkpoint-a", queued("checkpoint-a", messages + [{"role": "user", "content": "Unrelated."}]))
    assert outbox.drain(lambda *a, **k: True, timeout_seconds=1) == 1
    ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    outbox.apply_erasure_page("contact-a", ledger.erasure_feed("contact-a"))
    assert outbox.snapshot() == []

@pytest.mark.asyncio
async def test_api_erases_before_graph_cleanup_and_blocks_replay(ledger, monkeypatch):
    messages = source(ledger)
    monkeypatch.setattr(host, "_graph", SimpleNamespace(delete_source_memories=AsyncMock(side_effect=OSError("graph down"))))
    effects = AsyncMock(side_effect=AssertionError("erased turn ran effects"))
    monkeypatch.setattr(host, "_process_turn_sync", effects)
    app = FastAPI()
    app.include_router(host.router)
    app.include_router(host.v2_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        erased = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-a", "source_ids": ["turn-a"]})
        assert erased.status_code == 200 and erased.json()["graph_cleanup"] == "pending"
        body = {"identity": {"host_id": "test"}, "context": {"session_id": "session-a", "contact_id": "contact-a", "turn_id": "turn-a"}, "checkpoint_messages": messages}
        replay = await client.put("/v2/host/turns/turn-a", json=body)
        assert replay.json()["skipped_reason"] == "source_erased"
        assert replay.json()["source_recorded"] is False and effects.await_count == 0
        wrong = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-b", "source_ids": ["turn-a"]})
        assert wrong.status_code == 422

@pytest.mark.asyncio
async def test_graph_lineage_and_late_projection_guard(ledger):
    from colony_sidecar.intelligence.graph.client import ColonyGraph
    graph = object.__new__(ColonyGraph)
    graph.store_memory = AsyncMock(return_value="memory-a")
    source(ledger)
    await graph.record_turn("session-a", "contact-a", [], [], [], "A meaningful hydrofoil summary.", turn_id="turn-a")
    stored = graph.store_memory.call_args.kwargs
    assert stored["source_uri"] == "turn:turn-a" and stored["metadata"]["source_turn_id"] == "turn-a"
    ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    await graph.record_turn("session-a", "contact-a", [], [], [], "A late summary.", turn_id="turn-a")
    assert graph.store_memory.await_count == 1
    assert await graph._filter_erased_source_memories([{"source_uri": "turn:turn-a"}, {"source_uri": "file:unrelated"}]) == [{"source_uri": "file:unrelated"}]
    assert await ColonyGraph.store_memory(graph, "late", "episodic", [], source_uri="turn:turn-a") == ""

@pytest.mark.asyncio
async def test_authenticated_contact_cannot_select_another_person(ledger, monkeypatch):
    from colony_sidecar.api.authority import RequestAuthority
    source(ledger, contact="contact-b")
    app = FastAPI()
    @app.middleware("http")
    async def principal(request, call_next):
        request.state.colony_authority = RequestAuthority(
            principal_id="host-a", credential_id="key-a", scopes=frozenset({"memory:write", "turns:write"}),
            viewer_person_id="contact-a", person_ids=frozenset({"contact-a"}),
            audiences=frozenset({"viewer"}), authenticated=True,
        )
        return await call_next(request)
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        erased = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-b", "source_ids": ["turn-a"]})
        feed = await client.get("/v1/host/memory/sources/erasures", params={"contact_id": "contact-b"})
        assert erased.status_code == 403 and feed.status_code == 403
    assert ledger.search_sources("hydrofoil", contact_id="contact-b", session_id="session-a")


def test_repeat_erase_retains_derived_cleanup_targets(ledger):
    messages = source(ledger)
    source(ledger, "checkpoint-a", messages=messages + [{"role": "user", "content": "Unrelated."}])
    first = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    again = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    assert set(again["affected_source_ids"]) == set(first["affected_source_ids"])
    assert again["watermark"] == first["watermark"]

@pytest.mark.asyncio
async def test_mcp_forget_tool_reaches_the_real_erasure_api(ledger, monkeypatch):
    pytest.importorskip("mcp")
    from colony_sidecar.mcp.server import create_server
    import httpx
    source(ledger)
    monkeypatch.setattr(host, "_graph", None)
    monkeypatch.setenv("COLONY_MCP_SOURCE", "test-host")
    app = FastAPI()
    app.include_router(host.router)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=ASGITransport(app=app), base_url="http://test", **kw))
    server = create_server()
    result = await server._tool_manager._tools["colony_forget_sources"].fn(source_ids=["turn-a"], contact_id="contact-a")
    assert result["source_erased"] is True
    assert ledger.is_source_erased("turn-a", "contact-a")

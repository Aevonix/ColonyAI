"""Source bytes survive ingestion and are consumed by scoped context recall."""

import hashlib
import json
import sqlite3

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.api.schemas.host import TurnSyncRequest
from colony_sidecar.turns import TurnIdempotencyLedger, canonical_turn_digest


@pytest.fixture
def source_app(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    for name in ("_graph", "_contacts_store", "_presence_store", "_context_provenance", "_telemetry", "_p8_runtime", "_reranker", "_context_recall_selector"):
        monkeypatch.setattr(host, name, None)
    app = FastAPI()
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


def envelope(turn_id, *, checkpoint=False):
    content = "ordinary context " * 180 + "The hydrofoil departure is Friday at nine."
    body = {
        "identity": {"host_id": "test-host"},
        "context": {"session_id": "session-a", "contact_id": "contact-a", "channel_id": "test:thread-a", "turn_id": turn_id},
    }
    if checkpoint:
        body["checkpoint_messages"] = [{"role": "user", "content": content}]
    else:
        body["user_message"] = {"role": "user", "content": content}
        body["assistant_message"] = {"role": "assistant", "content": "Understood."}
    return body


async def recalled(client, *, contact="contact-a", session="session-a", query="hydrofoil"):
    response = await client.post("/v1/host/context/assemble", json={
        "identity": {"host_id": "test-host"},
        "context": {"contact_id": contact, "session_id": session},
        "incoming_message": {"role": "user", "content": query},
    })
    assert response.status_code == 200, response.text
    return "\n".join(
        section["body"] for section in response.json()["sections"]
        if section["id"] == "colony-memory"
    )


@pytest.mark.asyncio
async def test_complete_turn_is_recalled_across_sessions_after_reopen(source_app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        body = envelope("turn-source")
        body["context"]["metadata"] = {"occurred_at": "2026-01-02T03:04:05+00:00"}
        response = await client.put("/v2/host/turns/turn-source", json=body)
        assert response.status_code == 201
        assert response.json()["source_recorded"] is True
        assert response.json()["continuity_updated"] is True
        assert response.json()["skipped_reason"] is None
        assert "Friday at nine" in await recalled(client, session="session-b")
        assert await recalled(client, contact="contact-b") == ""
    reopened = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    assert "Friday at nine" in reopened.search_sources(
        "hydrofoil", contact_id="contact-a", session_id="later"
    )[0]["content"]
    hit = reopened.search_sources("hydrofoil", contact_id="contact-a", session_id="later")[0]
    assert hit["occurred_at"] == "2026-01-02T03:04:05+00:00"
    assert hit["ingested_at"] != hit["occurred_at"]
    with sqlite3.connect(tmp_path / "turn-idempotency.db") as conn:
        messages = json.loads(conn.execute("SELECT messages_json FROM turn_sources").fetchone()[0])
        assert messages[0]["content"] == body["user_message"]["content"]


@pytest.mark.asyncio
async def test_checkpoint_replay_has_no_ordinary_effect_and_is_session_scoped(source_app, monkeypatch, tmp_path):
    async def forbidden(*args, **kwargs):
        raise AssertionError("checkpoint ran ordinary conversation effects")
    monkeypatch.setattr(host, "_process_turn_sync", forbidden)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        body = envelope("checkpoint-source", checkpoint=True)
        first = await client.put("/v2/host/turns/checkpoint-source", json=body)
        again = await client.put("/v2/host/turns/checkpoint-source", json=body)
        assert first.status_code == 201 and again.status_code == 200
        assert first.json()["source_recorded"] is True
        assert "Friday at nine" in await recalled(client)
        assert await recalled(client, session="session-b") == ""
        assert await recalled(client, contact="contact-b") == ""
        body["checkpoint_messages"][0]["content"] += " Changed."
        changed = await client.put("/v2/host/turns/checkpoint-source", json=body)
        assert changed.status_code == 409
    with sqlite3.connect(tmp_path / "turn-idempotency.db") as conn:
        assert conn.execute("SELECT count(*) FROM turn_sources").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM turn_ingestion").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_media_references_retained_but_not_lexically_indexed(source_app, tmp_path):
    body = envelope("media-source", checkpoint=True)
    blocks = [
        {"type": "text", "text": "The hydrofoil is pictured here."},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/mediaonlymarker.png"}},
    ]
    body["checkpoint_messages"][0]["content"] = blocks
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        assert (await client.put("/v2/host/turns/media-source", json=body)).status_code == 201
        assert "pictured here" in await recalled(client)
        assert await recalled(client, query="mediaonlymarker") == ""
        body["checkpoint_messages"][0]["api_content"] = "injected recall"
        assert (await client.put("/v2/host/turns/invalid-source", json=body)).status_code == 422
    with sqlite3.connect(tmp_path / "turn-idempotency.db") as conn:
        stored = json.loads(conn.execute("SELECT messages_json FROM turn_sources").fetchone()[0])[0]
        assert stored["content"][0] == blocks[0]
        assert stored["content"][1]["type"] == "image_unretained"
        from colony_sidecar.turns.idempotency import source_message_hash
        assert source_message_hash("session-a", stored) == source_message_hash("session-a", {"role": "user", "content": blocks})


def test_additive_schema_preserves_existing_idempotency_digest():
    payload = TurnSyncRequest.model_validate(envelope("existing-turn"))
    legacy = payload.model_dump(mode="json", exclude_none=False)
    legacy.pop("checkpoint_messages")
    expected = hashlib.sha256(json.dumps(
        legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    assert canonical_turn_digest(payload) == expected


@pytest.mark.asyncio
async def test_empty_checkpoint_is_rejected_without_storing_garbage(source_app, tmp_path):
    body = envelope("empty-source", checkpoint=True)
    body["checkpoint_messages"] = []
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        assert (await client.put("/v2/host/turns/empty-source", json=body)).status_code == 422
    assert not (tmp_path / "turn-idempotency.db").exists()

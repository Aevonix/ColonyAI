"""Phase 1 regression tests for stable turn-ingestion idempotency.

Audit reproduction: the legacy handler accepted the same successful turn twice
and ran ``record_turn`` twice. A stable turn ID must instead produce one set of
downstream effects, an identical replay, or an explicit content conflict.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host


class _CountingGraph:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_turn(self, **kwargs):
        self.calls.append(kwargs)
        return f"memory-{len(self.calls)}"


class _BlockingGraph(_CountingGraph):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record_turn(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        await self.release.wait()
        return "memory-blocked"


def _payload(turn_id: str = "turn-001", *, topic: str = "alpha") -> dict:
    return {
        "identity": {"host_id": "test-host", "instance_id": "instance-a"},
        "context": {
            "session_id": "session-1",
            "contact_id": "contact-1",
            "channel_id": "test:thread-1",
            "turn_id": turn_id,
        },
        "topics": [topic],
        "user_message": {"role": "user", "content": "Remember alpha"},
        "assistant_message": {"role": "assistant", "content": "I will."},
    }


@pytest.fixture
def graph(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    value = _CountingGraph()
    monkeypatch.setattr(host, "_graph", value)
    # Keep this contract test focused on synchronous ingestion effects.
    monkeypatch.setattr(host, "_presence_store", None)
    monkeypatch.setattr(host, "_contacts_store", None)
    monkeypatch.setattr(host, "_context_provenance", None)
    monkeypatch.setattr(host, "_telemetry", None)
    return value


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


@pytest.mark.asyncio
async def test_v1_identical_turn_retry_has_one_downstream_effect(app, graph):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/host/turns/sync", json=_payload())
        retry = await client.post("/v1/host/turns/sync", json=_payload())

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.headers["Idempotency-Status"] == "replayed"
    assert retry.json() == first.json()
    assert graph.calls == []
    assert first.json()["source_recorded"] and first.json()["continuity_updated"]


@pytest.mark.asyncio
async def test_v1_same_turn_id_with_different_content_conflicts(app, graph):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/host/turns/sync", json=_payload())
        conflict = await client.post(
            "/v1/host/turns/sync", json=_payload(topic="different")
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "turn_id_content_conflict"
    assert graph.calls == []


@pytest.mark.asyncio
async def test_v2_put_returns_created_replayed_and_conflict(app, graph):
    body = _payload()
    body["context"].pop("turn_id")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.put("/v2/host/turns/turn-002", json=body)
        retry = await client.put("/v2/host/turns/turn-002", json=body)
        body["assistant_message"]["content"] = "Conflicting response"
        conflict = await client.put("/v2/host/turns/turn-002", json=body)

    assert (first.status_code, first.headers["Idempotency-Status"]) == (201, "created")
    assert (retry.status_code, retry.headers["Idempotency-Status"]) == (200, "replayed")
    assert conflict.status_code == 409
    assert graph.calls == []


@pytest.mark.asyncio
async def test_v2_path_and_body_turn_ids_must_match(app, graph):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v2/host/turns/path-turn", json=_payload(turn_id="body-turn")
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "turn_id_mismatch"
    assert graph.calls == []


@pytest.mark.asyncio
async def test_v2_accepts_url_escaped_host_turn_ids(app, graph):
    body = _payload(turn_id="host/turn 7")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v2/host/turns/host%2Fturn%207", json=body
        )

    assert response.status_code == 201
    assert graph.calls == []


@pytest.mark.asyncio
async def test_v2_concurrent_retry_is_truthful_pending_then_completed_replay(
        app, graph, monkeypatch):
    blocking = _BlockingGraph()
    monkeypatch.setattr(host, "_graph", blocking)
    body = _payload(turn_id="turn-concurrent")
    # Legacy summary-only callers still use the graph. Block that real
    # downstream effect to exercise concurrent ingestion and durable replay.
    body.pop("user_message")
    body.pop("assistant_message")
    body["summary"] = "Legacy summary-only turn."

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_task = asyncio.create_task(
            client.put("/v2/host/turns/turn-concurrent", json=body)
        )
        await asyncio.wait_for(blocking.entered.wait(), timeout=2)

        pending = await client.put(
            "/v2/host/turns/turn-concurrent", json=body
        )
        assert pending.status_code == 202
        assert pending.headers["Idempotency-Status"] == "in_progress"
        assert pending.headers["Retry-After"] == "1"
        assert pending.json()["accepted"] is False
        assert len(blocking.calls) == 1

        blocking.release.set()
        first = await asyncio.wait_for(first_task, timeout=2)
        replay = await client.put(
            "/v2/host/turns/turn-concurrent", json=body
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Status"] == "replayed"
    assert replay.json() == first.json()
    assert len(blocking.calls) == 1


@pytest.mark.asyncio
async def test_interrupted_creator_becomes_ambiguous_not_success(
        app, graph, monkeypatch):
    async def _crash(_body):
        raise RuntimeError("simulated creator crash")

    monkeypatch.setattr(host, "_process_turn_sync", _crash)
    body = _payload(turn_id="turn-crashed")
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        first = await client.put("/v2/host/turns/turn-crashed", json=body)
        retry = await client.put("/v2/host/turns/turn-crashed", json=body)

    assert first.status_code == 500
    assert retry.status_code == 503
    assert retry.json()["detail"]["code"] == "turn_ingestion_ambiguous"

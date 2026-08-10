"""Shared-facts -> memory-graph backfill (U7): explicit admin endpoint that
mirrors facts stored before the create-time mirror existed.

Locks: 501 when stores are unwired, dry_run counts without writing, live run
mirrors serially and counts per-fact failures without aborting, and
single-flight (409 while a run is in progress)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.tom.facts import SharedFactsStore


class _RecordingGraph:
    """Fake ColonyGraph capturing store_memory calls."""

    def __init__(self, fail_on: set | None = None, gate: asyncio.Event | None = None):
        self.calls = []
        self._fail_on = fail_on or set()
        self._gate = gate

    async def store_memory(self, **kwargs):
        if self._gate is not None:
            await self._gate.wait()
        self.calls.append(kwargs)
        if kwargs["content"] in self._fail_on:
            raise RuntimeError("embedding unavailable")
        return f"mem-{len(self.calls)}"


@asynccontextmanager
async def _app(graph, facts_store):
    from fastapi import FastAPI
    prev_graph, prev_facts = host_mod._graph, host_mod._facts_store
    prev_state = dict(host_mod._facts_backfill_state)
    host_mod._graph = graph
    host_mod._facts_store = facts_store
    host_mod._facts_backfill_state.clear()
    host_mod._facts_backfill_state["running"] = False
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client
    finally:
        host_mod._graph = prev_graph
        host_mod._facts_store = prev_facts
        host_mod._facts_backfill_state.clear()
        host_mod._facts_backfill_state.update(prev_state)


@pytest.fixture
def facts_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = SharedFactsStore(path)
    yield s
    s.close()
    os.unlink(path)


async def _wait_done(timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if not host_mod._facts_backfill_state.get("running"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("backfill did not finish")


@pytest.mark.asyncio
async def test_501_when_facts_store_missing():
    async with _app(_RecordingGraph(), None) as client:
        resp = await client.post("/v1/host/mind/facts/backfill", json={})
        assert resp.status_code == 501


@pytest.mark.asyncio
async def test_501_when_graph_missing(facts_store):
    async with _app(None, facts_store) as client:
        resp = await client.post("/v1/host/mind/facts/backfill", json={})
        assert resp.status_code == 501


@pytest.mark.asyncio
async def test_dry_run_default_counts_without_writing(facts_store):
    for i in range(3):
        facts_store.create_fact(contact_id="c1", fact=f"fact {i}", confidence=0.9)
    graph = _RecordingGraph()
    async with _app(graph, facts_store) as client:
        resp = await client.post("/v1/host/mind/facts/backfill", json={})
        assert resp.status_code == 200
        body = resp.json()
        # dry_run is the DEFAULT — an empty request never mutates the graph
        assert body == {"dry_run": True, "started": False, "total": 3}
        assert graph.calls == []


@pytest.mark.asyncio
async def test_min_confidence_and_limit_filter(facts_store):
    facts_store.create_fact(contact_id="c1", fact="low", confidence=0.2)
    facts_store.create_fact(contact_id="c1", fact="high-1", confidence=0.9)
    facts_store.create_fact(contact_id="c1", fact="high-2", confidence=0.9)
    async with _app(_RecordingGraph(), facts_store) as client:
        resp = await client.post("/v1/host/mind/facts/backfill",
                                 json={"min_confidence": 0.5})
        assert resp.json()["total"] == 2
        resp = await client.post("/v1/host/mind/facts/backfill",
                                 json={"min_confidence": 0.5, "limit": 1})
        assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_live_run_mirrors_each_fact(facts_store):
    for i in range(4):
        facts_store.create_fact(contact_id=f"c{i}", fact=f"fact {i}",
                                source="explicit", confidence=0.8)
    graph = _RecordingGraph()
    async with _app(graph, facts_store) as client:
        resp = await client.post("/v1/host/mind/facts/backfill",
                                 json={"dry_run": False, "sleep_ms": 0})
        assert resp.status_code == 200
        assert resp.json() == {"dry_run": False, "started": True, "total": 4}
        await _wait_done()
        status = (await client.get("/v1/host/mind/facts/backfill")).json()
    assert len(graph.calls) == 4
    assert status["processed"] == 4
    assert status["mirrored"] == 4
    assert status["failed"] == 0
    assert status["finished_at"]
    # The mirror path is _mirror_fact_to_graph: fact memories, tom source uri
    for call in graph.calls:
        assert call["memory_type"] == "fact"
        assert call["source_uri"] == "tom:shared_fact"
        assert call["metadata"]["shared_fact"] is True


@pytest.mark.asyncio
async def test_per_fact_failure_counted_and_run_continues(facts_store):
    for name in ("good-1", "bad", "good-2"):
        facts_store.create_fact(contact_id="c1", fact=name, confidence=0.8)
    graph = _RecordingGraph(fail_on={"bad"})
    async with _app(graph, facts_store) as client:
        await client.post("/v1/host/mind/facts/backfill",
                          json={"dry_run": False, "sleep_ms": 0})
        await _wait_done()
        status = (await client.get("/v1/host/mind/facts/backfill")).json()
    assert status["processed"] == 3
    assert status["mirrored"] == 2
    assert status["failed"] == 1
    assert status["running"] is False


@pytest.mark.asyncio
async def test_second_invocation_409_while_running(facts_store):
    facts_store.create_fact(contact_id="c1", fact="slow fact", confidence=0.8)
    gate = asyncio.Event()
    graph = _RecordingGraph(gate=gate)
    async with _app(graph, facts_store) as client:
        first = await client.post("/v1/host/mind/facts/backfill",
                                  json={"dry_run": False, "sleep_ms": 0})
        assert first.status_code == 200
        second = await client.post("/v1/host/mind/facts/backfill",
                                   json={"dry_run": False, "sleep_ms": 0})
        assert second.status_code == 409
        gate.set()
        await _wait_done()
        third = await client.post("/v1/host/mind/facts/backfill", json={})
        assert third.status_code == 200  # lock released after completion

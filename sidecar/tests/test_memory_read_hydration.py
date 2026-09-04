"""Regression test for the 2026-09-04 recall probe.

Observed by RUNNING the sidecar against a real Neo4j: POST /memory/read
always answered ``{"entries": []}`` while /memory/search returned rows,
because ColonyGraph.read_memories hydrates ``created_at`` as a
``neo4j.time.DateTime`` and the router handed it to MemoryEntry (a string
field) unconverted. The pydantic error was swallowed into the empty
"no data" reply, so the endpoint looked healthy and returned nothing.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host


class _Neo4jDateTime:
    """Stand-in for neo4j.time.DateTime: not a str, stringifies like one."""

    def __str__(self) -> str:
        return "2026-09-04T10:41:03.459000000+00:00"


class _HydratingGraph:
    class _Driver:
        async def verify_connectivity(self):
            return None

    def __init__(self) -> None:
        self.driver = self._Driver()
        self._embed_fn = None
        self._vector_store = None

    async def read_memories(self, **kwargs):
        return [{
            "id": "mem-1",
            "content": "the user prefers oat milk",
            "type": "preference",
            "strength": 0.83,
            "created_at": _Neo4jDateTime(),
            "entities": ["oat milk"],
            "person_id": kwargs.get("person_id"),
        }]


@pytest.fixture
def hydrating_graph(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    graph = _HydratingGraph()
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setattr(host, "_presence_store", None)
    monkeypatch.setattr(host, "_contacts_store", None)
    monkeypatch.setattr(host, "_context_provenance", None)
    monkeypatch.setattr(host, "_telemetry", None)
    return graph


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(host.router)
    return app


@pytest.mark.asyncio
async def test_memory_read_serialises_graph_datetimes(app, hydrating_graph):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/v1/host/memory/read", json={
            "identity": {"host_id": "t"},
        })
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1, "a hydrated row must not collapse into 'no data'"
    entry = entries[0]
    assert entry["id"] == "mem-1"
    assert entry["created_at"] == "2026-09-04T10:41:03.459000000+00:00"
    assert entry["strength"] == pytest.approx(0.83)
    assert entry["entities"] == ["oat milk"]

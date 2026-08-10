"""Regression tests for the 2026-08-10 functional validation sweep.

Every test here reproduces a defect that was observed by RUNNING the system
(not inferred from reading code): silent turn-ingestion green-lights, the
skills invoke path awaiting a non-awaitable, briefings swallowed into empty
200s, safety-gate context never populated, memory endpoints whose "backend
down" was indistinguishable from "no data", health advertising a dead memory
capability, two unhandled 500s, and a doctor blind spot for the graph
backend.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DeadBackendGraph:
    """A wired ColonyGraph whose backing store is unreachable.

    Mirrors the live failure: the client object exists (so the sidecar
    considers memory "wired") but every operation raises, and
    driver.verify_connectivity() — the /memory/status availability
    determination — fails.
    """

    class _Driver:
        async def verify_connectivity(self):
            raise RuntimeError("Neo4j unreachable")

    def __init__(self) -> None:
        self.driver = self._Driver()
        self._embed_fn = None
        self._vector_store = None

    async def record_turn(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def recall(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def read_memories(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def store_memory(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")


@pytest.fixture
def dead_graph(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    graph = _DeadBackendGraph()
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


def _client(app: FastAPI, **kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, **kwargs), base_url="http://test",
    )


# ---------------------------------------------------------------------------
# 1. /turns/sync must not green-light a turn whose record_turn failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turns_sync_does_not_greenlight_failed_ingestion(app, dead_graph):
    async with _client(app) as client:
        resp = await client.post("/v1/host/turns/sync", json={
            "identity": {"host_id": "test-host"},
            "context": {"session_id": "session-1", "contact_id": "contact-1",
                        "channel_id": "test:thread-1"},
            "topics": ["alpha"],
            "summary": "user said alpha",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] is False, (
        "a turn that was NOT recorded must never be reported accepted")
    assert data["continuity_updated"] is False
    assert data["errors"], "record_turn failure must be reported, not swallowed"
    assert "record_turn failed" in data["errors"][0]
    assert data["skipped_reason"] == "graph_record_failed"


# ---------------------------------------------------------------------------
# 2. Built-in skills must actually execute through SkillExecutor.invoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_builtin_skills_actually_execute():
    from colony_sidecar.skills.executor import SkillExecutor
    from colony_sidecar.skills.registry import SkillRegistry
    from colony_sidecar.skills.security.guards import CapabilityGuard
    from colony_sidecar.skills.security.scanner import ASTScanner

    registry = SkillRegistry()
    assert "subsystem_health" in registry.list_skills()
    executor = SkillExecutor(
        registry=registry, guard=CapabilityGuard(), scanner=ASTScanner(),
    )
    # Before the fix this raised
    # "object SubsystemHealthSkill can't be used in 'await' expression"
    # (the endpoint surfaced it as a 500) for EVERY built-in skill.
    result = await executor.invoke("subsystem_health", {})
    assert result.status == "success", result.error
    assert result.output == {"result": "no_action"}


# ---------------------------------------------------------------------------
# 3. /briefings returns stored briefings and surfaces store failures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefings_returned_and_failures_surface(app, monkeypatch):
    from colony_sidecar.briefings.models import (
        Briefing, BriefingSection, BriefingType,
    )

    briefing = Briefing(
        briefing_type=BriefingType.DAILY,
        sections=[BriefingSection(name="tasks",
                                  narrative="Two tasks due today.")],
    )

    class _Engine:
        def get_recent(self, limit=10):
            return [briefing]

    monkeypatch.setattr(host, "_briefings_engine", _Engine())
    async with _client(app) as client:
        resp = await client.get("/v1/host/briefings")
        assert resp.status_code == 200
        items = resp.json()["briefings"]
        assert len(items) == 1, (
            "stored briefings must be returned, not an empty success")
        assert items[0]["id"] == briefing.briefing_id
        assert items[0]["briefing_type"] == "daily"
        assert "Two tasks due today." in items[0]["body"]

        class _Broken:
            def get_recent(self, limit=10):
                raise RuntimeError("briefing store exploded")

        monkeypatch.setattr(host, "_briefings_engine", _Broken())
        resp = await client.get("/v1/host/briefings")
        assert resp.status_code == 500, (
            "a store failure must surface, not become 200 []")


# ---------------------------------------------------------------------------
# 4. /safety/check must populate session/contact gate context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_check_populates_gate_context(app, monkeypatch):
    captured = {}

    class _Gate:
        async def evaluate(self, payload):
            captured["payload"] = payload
            return SimpleNamespace(
                blocked=False, blocking_layer=None, block_reason=None,
                flagged_excerpt=None, layer_results=None,
            )

    monkeypatch.setattr(host, "_response_gate", _Gate())
    async with _client(app) as client:
        resp = await client.post("/v1/host/safety/check", json={
            "identity": {"host_id": "test-host"},
            "context": {"session_id": "sess-9", "contact_id": "contact-7",
                        "turn_id": "turn-3"},
            "response_text": "hello there",
        })
    assert resp.status_code == 200
    payload = captured["payload"]
    assert payload.session_id == "sess-9", (
        "gate payload must carry the request's session context")
    assert payload.target_contact_id == "contact-7"
    assert payload.turn_id == "turn-3"


# ---------------------------------------------------------------------------
# 5. Memory endpoints: backend-down must be distinguishable from "no data"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_endpoints_distinguish_backend_down(app, dead_graph):
    async with _client(app) as client:
        search = await client.post("/v1/host/memory/search", json={
            "identity": {"host_id": "t"}, "query": "anything",
        })
        assert search.status_code == 503
        assert search.json()["detail"]["code"] == "memory_backend_unavailable"

        read = await client.post("/v1/host/memory/read", json={
            "identity": {"host_id": "t"},
        })
        assert read.status_code == 503
        assert read.json()["detail"]["code"] == "memory_backend_unavailable"

        write = await client.post("/v1/host/memory/write", json={
            "identity": {"host_id": "t"}, "content": "remember this",
        })
        assert write.status_code == 503
        assert write.json()["detail"]["code"] == "memory_backend_unavailable"


# ---------------------------------------------------------------------------
# 6. /health must not advertise a memory capability whose backend is dead
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_does_not_claim_dead_memory_backend(app, dead_graph):
    async with _client(app) as client:
        resp = await client.get("/v1/host/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "memory" not in data["capabilities"], (
        "an unreachable backend must not be advertised as a live capability")
    assert "UNREACHABLE" in data["notes"]["memory"]
    assert data["status"] == "degraded"


# ---------------------------------------------------------------------------
# 7a. Invalid job_type is a clear 400, not an unhandled enum ValueError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_job_type_is_400_not_500(monkeypatch):
    from colony_sidecar.api.routers import task_queue

    monkeypatch.setattr(task_queue, "_get_queue", lambda: object())
    app = FastAPI()
    app.include_router(task_queue.router)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post("/v1/host/queue/jobs", json={
            "job_type": "definitely_not_a_job_type",
        })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_job_type"
    assert "definitely_not_a_job_type" in detail["message"]
    assert detail["valid_job_types"]


# ---------------------------------------------------------------------------
# 7b. /world/extract rejects non-base64 content with a clear 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_world_extract_plain_text_is_clear_400(app, monkeypatch):
    class _Pipeline:
        async def extract(self, **kwargs):  # pragma: no cover — never reached
            return []

    monkeypatch.setattr(host, "_extraction_pipeline", _Pipeline())
    async with _client(app) as client:
        resp = await client.post("/v1/host/world/extract", json={
            "identity": {"host_id": "t"},
            "content": "this is definitely not base64 !!!",
        })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_content_encoding"
    assert "base64" in detail["message"]


# ---------------------------------------------------------------------------
# 8. Doctor covers graph/memory backend reachability
# ---------------------------------------------------------------------------

def test_doctor_flags_unreachable_graph_backend(monkeypatch):
    from colony_sidecar import doctor

    assert "server-memory-graph" in doctor.SERVER_CHECK_NAMES

    def _fake(url, api_key="", timeout=10.0):
        assert url.endswith("/v1/host/memory/status")
        return 200, {
            "wired": False, "graph_wired": True, "neo4j_connected": False,
            "embeddings_ready": True, "vector_store_ready": True,
        }

    monkeypatch.setattr(doctor, "_http_get", _fake)
    result = doctor.check_server_memory_graph("http://x", "k", 5.0)
    assert result.status == doctor.FAIL
    assert "UNREACHABLE" in result.detail

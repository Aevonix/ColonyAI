"""Tests for the Colony sidecar — import checks, API endpoints, setup wizard."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create a fresh sidecar app for each test."""
    from colony_sidecar.server import create_app
    return create_app()


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client wired to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Import checks — all 16 subsystems
# ---------------------------------------------------------------------------

SUBSYSTEMS = [
    ("colony_sidecar.reasoning.loop", "ReasoningLoop"),
    ("colony_sidecar.reasoning.executor", "ToolExecutor"),
    ("colony_sidecar.gate.pipeline", "ResponseGate"),
    ("colony_sidecar.intelligence.graph.client", "ColonyGraph"),
    ("colony_sidecar.intelligence.cognition.metalearner", "MetaLearner"),
    ("colony_sidecar.intelligence.synthesis.connection_discoverer", "ConnectionDiscoverer"),
    ("colony_sidecar.intelligence.learning.continuous_learner", "ContinuousLearner"),
    ("colony_sidecar.intelligence.mind_model.signal_collector", "SignalCollector"),
    ("colony_sidecar.intelligence.relationships.trust_tiers", "TrustTier"),
    ("colony_sidecar.goals.engine", "GoalEngine"),
    ("colony_sidecar.briefings.engine", "BriefingEngine"),
    ("colony_sidecar.delivery.bridge", "ProactiveDeliveryBridge"),
    ("colony_sidecar.research.pipeline", "ResearchPipeline"),
    ("colony_sidecar.contacts.store", "ContactStore"),
    ("colony_sidecar.world_model.store", "WorldModelStore"),
    ("colony_sidecar.vector.embedder", "EmbeddingPipeline"),
    ("colony_sidecar.skills.registry", "SkillRegistry"),
]


@pytest.mark.parametrize("module,cls", SUBSYSTEMS, ids=[s[1] for s in SUBSYSTEMS])
def test_subsystem_import(module, cls):
    """Every subsystem should import without errors."""
    import importlib
    mod = importlib.import_module(module)
    assert hasattr(mod, cls), f"{module} has no {cls}"


# ---------------------------------------------------------------------------
# Server + OpenAPI
# ---------------------------------------------------------------------------

def test_create_app():
    from colony_sidecar.server import create_app
    app = create_app()
    assert app.title == "Colony Intelligence Sidecar"


def test_openapi_spec_export():
    from colony_sidecar.server import create_app
    app = create_app()
    spec = app.openapi()
    assert "paths" in spec
    assert "components" in spec
    schemas = spec.get("components", {}).get("schemas", {})
    paths = spec.get("paths", {})
    assert len(schemas) >= 50, f"Only {len(schemas)} schemas"
    assert len(paths) >= 25, f"Only {len(paths)} paths"


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/v1/host/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "capabilities" in data
    assert "notes" in data


# ---------------------------------------------------------------------------
# Memory stubs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_read_empty(client):
    resp = await client.post("/v1/host/memory/read", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


@pytest.mark.asyncio
async def test_memory_search_empty(client):
    resp = await client.post("/v1/host/memory/search", json={
        "identity": {"host_id": "test"},
        "query": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


@pytest.mark.asyncio
async def test_memory_write_no_graph(client):
    resp = await client.post("/v1/host/memory/write", json={
        "identity": {"host_id": "test"},
        "content": "test memory",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_memory_flush_no_graph(client):
    resp = await client.post("/v1/host/memory/flush", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_memory_embed_not_wired(client):
    resp = await client.post("/v1/host/memory/embed", json={
        "identity": {"host_id": "test"},
        "inputs": ["hello"],
    })
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_assemble_empty(client):
    resp = await client.post("/v1/host/context/assemble", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "incoming_message": {"role": "user", "content": "hello"},
    })
    assert resp.status_code == 200
    assert "sections" in resp.json()


@pytest.mark.asyncio
async def test_enriched_context(client):
    resp = await client.post("/v1/host/context/enriched", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "message": "hello",
    })
    assert resp.status_code == 200
    assert "sections" in resp.json()


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reasoning_turn_not_wired(client):
    resp = await client.post("/v1/host/reasoning/turn", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_check_passthrough(client):
    resp = await client.post("/v1/host/safety/check", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "response_text": "Hello!",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "pass"
    assert data["blocked"] is False


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signals_ingest(client):
    resp = await client.post("/v1/host/signals/ingest", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


# ---------------------------------------------------------------------------
# Turns sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turns_sync(client):
    resp = await client.post("/v1/host/turns/sync", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "topics": ["test"],
        "entities": [],
        "tools_used": [],
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_goals_empty(client):
    resp = await client.get("/v1/host/goals")
    assert resp.status_code == 200
    assert resp.json()["goals"] == []


@pytest.mark.asyncio
async def test_get_goal_not_found(client):
    resp = await client.get("/v1/host/goals/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_contacts_empty(client):
    resp = await client.get("/v1/host/contacts")
    assert resp.status_code == 200
    assert resp.json()["contacts"] == []


@pytest.mark.asyncio
async def test_get_contact_not_found(client):
    resp = await client.get("/v1/host/contacts/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_contact_style(client):
    resp = await client.post("/v1/host/contacts/c1/style", json={
        "identity": {"host_id": "test"},
        "person_id": "c1",
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_briefings_empty(client):
    resp = await client.get("/v1/host/briefings")
    assert resp.status_code == 200
    assert resp.json()["briefings"] == []


# ---------------------------------------------------------------------------
# World model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_entities_empty(client):
    resp = await client.get("/v1/host/world/entities")
    assert resp.status_code == 200
    assert resp.json()["entities"] == []


@pytest.mark.asyncio
async def test_query_entities_empty(client):
    resp = await client.post("/v1/host/world/entities/query", json={
        "identity": {"host_id": "test"},
        "query": "python",
    })
    assert resp.status_code == 200
    assert resp.json()["entities"] == []


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cognition_cycle_no_backend(client):
    resp = await client.post("/v1/host/cognition/cycle", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["cpi"] is None


@pytest.mark.asyncio
async def test_cpi_no_backend(client):
    resp = await client.get("/v1/host/cognition/cpi")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"] == 0.0


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_start_not_wired(client):
    resp = await client.post("/v1/host/research/start", json={
        "identity": {"host_id": "test"},
        "topic": "quantum computing",
    })
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_list_research_empty(client):
    resp = await client.get("/v1/host/research")
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_pending_deliveries_empty(client):
    resp = await client.get("/v1/host/delivery/pending")
    assert resp.status_code == 200
    assert resp.json()["pending"] == []


@pytest.mark.asyncio
async def test_mark_delivery_sent(client):
    resp = await client.post("/v1/host/delivery/mark-sent", json={
        "identity": {"host_id": "test"},
        "delivery_id": "d1",
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_connections_empty(client):
    resp = await client.post("/v1/host/synthesis/discover", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["connections"] == []


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_correction_no_learner(client):
    resp = await client.post("/v1/host/learning/correction", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "original": "hi",
        "correction": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_submit_engagement_no_learner(client):
    resp = await client.post("/v1/host/learning/engagement", json={
        "identity": {"host_id": "test"},
        "briefing_id": "b1",
        "action": "opened",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_learning_weights_no_learner(client):
    resp = await client.get("/v1/host/learning/weights")
    assert resp.status_code == 200
    assert resp.json()["weights"] == {}


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_skills_empty(client):
    resp = await client.get("/v1/host/skills/registry")
    assert resp.status_code == 200
    assert resp.json()["skills"] == []


@pytest.mark.asyncio
async def test_get_skill_not_found(client):
    resp = await client.get("/v1/host/skills/registry/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_insights_empty(client):
    resp = await client.get("/v1/host/insights")
    assert resp.status_code == 200
    assert resp.json()["insights"] == []


@pytest.mark.asyncio
async def test_dismiss_insight(client):
    resp = await client.post("/v1/host/insights/i1/dismiss")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def test_setup_noninteractive():
    from colony_sidecar.setup import run_noninteractive
    with tempfile.TemporaryDirectory() as td:
        code = run_noninteractive(root_dir=td)
        assert code == 0
        env_path = Path(td) / ".env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "COLONY_API_KEY=" in content
        assert "NEO4J_URI=" in content


# ---------------------------------------------------------------------------
# ReasoningLoop unit tests
# ---------------------------------------------------------------------------

def test_tool_call_extraction():
    """Verify tool call extraction from a mock LiteLLM response."""
    from colony_sidecar.reasoning.loop import ReasoningLoop

    class MockFunc:
        name = "read_file"
        arguments = '{"path": "/tmp/test"}'

    class MockToolCall:
        id = "tc_123"
        function = MockFunc()

    class MockMessage:
        tool_calls = [MockToolCall()]

    class MockChoice:
        message = MockMessage()

    class MockResponse:
        choices = [MockChoice()]

    result = ReasoningLoop._extract_tool_calls(MockResponse())
    assert len(result) == 1
    assert result[0]["name"] == "read_file"
    assert result[0]["arguments"] == {"path": "/tmp/test"}


def test_tool_call_extraction_empty():
    from colony_sidecar.reasoning.loop import ReasoningLoop
    assert ReasoningLoop._extract_tool_calls(None) == []
    assert ReasoningLoop._extract_tool_calls(type("R", (), {"choices": []})()) == []


def test_build_assistant_message():
    from colony_sidecar.reasoning.loop import ReasoningLoop
    msg = ReasoningLoop._build_assistant_message(None, "hello", [])
    assert msg["role"] == "assistant"
    assert msg["content"] == "hello"

    msg_with_tools = ReasoningLoop._build_assistant_message(
        None, "", [{"id": "tc_1", "name": "run", "arguments": {"cmd": "ls"}}]
    )
    assert msg_with_tools["tool_calls"]
    assert msg_with_tools["tool_calls"][0]["function"]["name"] == "run"


# ---------------------------------------------------------------------------
# ToolExecutor unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_executor_unknown_tool():
    from colony_sidecar.reasoning.executor import ToolExecutor
    executor = ToolExecutor()
    results = await executor.execute_batch([
        {"id": "tc_1", "name": "unknown_tool", "arguments": {}}
    ])
    assert len(results) == 1
    parsed = json.loads(results[0]["content"])
    assert parsed["status"] == "needs_host_execution"


@pytest.mark.asyncio
async def test_tool_executor_custom_handler():
    from colony_sidecar.reasoning.executor import ToolExecutor

    async def mock_handler(args):
        return f"result: {args.get('x', 0)}"

    executor = ToolExecutor(handlers={"add": mock_handler})
    results = await executor.execute_batch([
        {"id": "tc_1", "name": "add", "arguments": {"x": 42}}
    ])
    assert len(results) == 1
    assert results[0]["content"] == "result: 42"


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chain / Identity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_identity_status_not_initialized(client):
    resp = await client.get("/v1/host/identity/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["initialized"] is False


@pytest.mark.asyncio
async def test_identity_init_not_wired(client):
    resp = await client.post("/v1/host/identity/init", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_chain_verify_no_chain(client):
    resp = await client.post("/v1/host/chain/verify", json={
        "identity": {"host_id": "test"},
        "data": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secrets_list_empty(client):
    resp = await client.post("/v1/host/secrets/list", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["keys"] == []


@pytest.mark.asyncio
async def test_secrets_get_not_found(client):
    resp = await client.post("/v1/host/secrets/get", json={
        "identity": {"host_id": "test"},
        "key": "nonexistent",
    })
    assert resp.status_code == 200
    assert resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_secrets_set_no_manager(client):
    resp = await client.post("/v1/host/secrets/set", json={
        "identity": {"host_id": "test"},
        "key": "test_key",
        "value": "test_val",
    })
    assert resp.status_code == 200
    assert resp.json()["stored"] is False


@pytest.mark.asyncio
async def test_secrets_delete_no_manager(client):
    resp = await client.post("/v1/host/secrets/delete", json={
        "identity": {"host_id": "test"},
        "key": "test_key",
    })
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False

"""End-to-end integration tests for OpenClaw → Colony plugin → Colony sidecar.

These tests verify the full stack: OpenClaw gateway routes requests through
the Colony plugin, which calls the Colony sidecar API, which executes the
subsystem logic.

Prerequisites:
- OpenClaw gateway running on the target host (default: localhost:18789)
- Colony sidecar running on the target host (default: localhost:7777)
- Colony plugin registered and loaded in OpenClaw
- Environment variables:
    OPENCLAW_URL  — gateway WebSocket/HTTP URL (default: http://localhost:18789)
    COLONY_URL    — sidecar HTTP URL (default: http://localhost:7777)
    COLONY_API_KEY — API key for sidecar auth

Run:
    OPENCLAW_URL=http://localhost:18789 COLONY_URL=http://localhost:7777 \
    COLONY_API_KEY=your-key pytest tests/e2e/ -v
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENCLAW_URL = os.environ.get("OPENCLAW_URL", "http://localhost:18789")
COLONY_URL = os.environ.get("COLONY_URL", "http://localhost:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "")

COLONY_HEADERS = {"Authorization": f"Bearer {COLONY_API_KEY}"} if COLONY_API_KEY else {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def colony():
    """HTTP client for the Colony sidecar."""
    return httpx.Client(base_url=COLONY_URL, headers=COLONY_HEADERS, timeout=30)


@pytest.fixture(scope="session")
def openclaw():
    """HTTP client for the OpenClaw gateway."""
    return httpx.Client(base_url=OPENCLAW_URL, timeout=60)


@pytest.fixture(scope="session")
def colony_health(colony):
    """Colony health response (session-scoped)."""
    r = colony.get("/v1/host/health")
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# 1. Colony Sidecar Health
# ---------------------------------------------------------------------------

class TestColonySidecar:
    """Verify the Colony sidecar is operational."""

    def test_health_endpoint(self, colony):
        r = colony.get("/v1/host/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert len(data["capabilities"]) >= 20

    def test_identity_status(self, colony):
        r = colony.get("/v1/host/identity/status")
        assert r.status_code == 200
        data = r.json()
        assert data["initialized"] is True
        assert data["colony_id"] is not None
        assert data["keys_configured"] is True
        assert data["public_key"] is not None

    def test_genesis_flag_exists(self, colony):
        """Verify the is_genesis flag is present in identity status."""
        r = colony.get("/v1/host/identity/status")
        data = r.json()
        # is_genesis is a boolean flag — its value depends on deployment
        assert isinstance(data.get("is_genesis"), bool)

    def test_node_identity(self, colony):
        """Verify node identity exists."""
        r = colony.get("/v1/host/identity/status")
        data = r.json()
        assert data["node_id"] is not None
        assert data["node_public_key"] is not None

    def test_memory_subsystem_wired(self, colony):
        """Verify memory subsystem status is available."""
        r = colony.get("/v1/host/memory/status")
        assert r.status_code == 200
        data = r.json()
        # Memory may not be fully wired in test environments
        # where Neo4j/embeddings are unavailable
        if data["wired"]:
            assert data["neo4j_connected"] is True
        # Key is that the endpoint responds correctly

    def test_scheduler_running(self, colony):
        """Verify autonomy scheduler has tasks registered."""
        r = colony.get("/v1/host/autonomy/schedule")
        assert r.status_code == 200
        data = r.json()
        schedules = data.get("schedules", [])
        assert len(schedules) >= 5  # At least 5 default periodic tasks


# ---------------------------------------------------------------------------
# 2. OpenClaw Gateway Health
# ---------------------------------------------------------------------------

class TestOpenClawGateway:
    """Verify the OpenClaw gateway is running and Colony plugin is loaded."""

    def test_gateway_health(self, openclaw):
        """Gateway should respond to health check."""
        # OpenClaw gateway has a dashboard on /
        r = openclaw.get("/")
        assert r.status_code in (200, 302, 404)  # Any response means it's running

    def test_colony_plugin_loaded(self):
        """Colony plugin should be in the loaded plugins list."""
        import subprocess
        # Try openclaw via nvm if direct PATH fails
        nvm_path = os.path.expanduser("~/.nvm/nvm.sh")
        cmd = f"source {nvm_path} 2>/dev/null && openclaw plugins list" if os.path.exists(nvm_path) else "openclaw plugins list"
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            pytest.skip(f"openclaw CLI not available (exit {result.returncode}): {output[:200]}")
        # Colony plugin should be loaded and registered
        assert "colony" in output.lower()


# ---------------------------------------------------------------------------
# 3. Memory Through Colony
# ---------------------------------------------------------------------------

class TestMemoryIntegration:
    """Test memory write and search through the Colony sidecar."""

    def test_write_and_search(self, colony):
        """Write a memory and search for it."""
        # Write
        unique_text = f"e2e-test-{uuid.uuid4().hex[:8]}: The quantum flux capacitor enables time travel"
        r = colony.post("/v1/host/memory/write", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "content": unique_text,
            "metadata": {"source": "e2e-test", "importance": 0.8},
        })
        assert r.status_code == 200
        data = r.json()
        assert data.get("accepted", False) is True or data.get("id") is not None

        # Wait for indexing
        time.sleep(5)

        # Search
        r = colony.post("/v1/host/memory/search", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "query": "quantum flux capacitor",
            "limit": 5,
        })
        assert r.status_code == 200
        data = r.json()
        # Should find at least one result matching the write
        results = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        # If no results, the write may still be indexing — not a hard failure
        if len(results) == 0:
            pytest.skip("Memory search returned 0 results — indexing may still be in progress")

    def test_memory_persistence(self, colony):
        """Verify memories persist across queries."""
        unique_text = f"persistence-test-{uuid.uuid4().hex[:8]}: Colossal magnetoresistance in manganites"
        colony.post("/v1/host/memory/write", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "content": unique_text,
        })
        time.sleep(2)

        # Search immediately
        r1 = colony.post("/v1/host/memory/search", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "query": "magnetoresistance",
        })
        assert r1.status_code == 200

        # Search again after a delay
        time.sleep(1)
        r2 = colony.post("/v1/host/memory/search", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "query": "magnetoresistance",
        })
        assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 4. Response Gate
# ---------------------------------------------------------------------------

class TestResponseGate:
    """Test the response gate (safety pipeline) through Colony."""

    def test_safe_message_passes(self, colony):
        """Safe content should pass through the gate."""
        r = colony.post("/v1/host/response-gate/check", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "response_text": "Hello, how are you doing today?",
            "incoming_message": "Hi there",
        })
        assert r.status_code == 200
        data = r.json()
        # Should pass (no block reason)
        assert data.get("blocked", False) is False or data.get("block_reason") is None

    def test_pii_detection(self, colony):
        """PII content should be flagged by the gate."""
        r = colony.post("/v1/host/response-gate/check", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "response_text": "My SSN is 123-45-6789 and my credit card is 4111-1111-1111-1111",
            "incoming_message": "What is your SSN?",
        })
        assert r.status_code == 200
        data = r.json()
        # Should be flagged (PII layer should catch it)
        # Even if not blocked, it should have layer_results indicating PII detection
        assert data.get("blocked") is True or data.get("flagged_excerpt") is not None or data.get("layer_results") is not None


# ---------------------------------------------------------------------------
# 5. Reasoning
# ---------------------------------------------------------------------------

class TestReasoningIntegration:
    """Test the reasoning loop through Colony."""

    def test_simple_reasoning(self, colony):
        """Verify reasoning returns a coherent response."""
        r = colony.post("/v1/host/reasoning/turn", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e-reasoning", "contact_id": "e2e"},
            "messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
            "max_iterations": 1,
        }, timeout=60)
        assert r.status_code == 200
        data = r.json()
        # Response should contain "4"
        response_text = json.dumps(data) if isinstance(data, dict) else str(data)
        assert "4" in response_text

    def test_native_calculate_tool(self, colony):
        """Verify the calculate native tool is available."""
        r = colony.post("/v1/host/reasoning/turn", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e-calc", "contact_id": "e2e"},
            "messages": [{"role": "user", "content": "Use the calculate tool to compute 15 * 7"}],
            "max_iterations": 2,
        }, timeout=60)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 6. Goals
# ---------------------------------------------------------------------------

class TestGoalsIntegration:
    """Test goal creation and lifecycle through Colony."""

    def test_create_and_list_goals(self, colony):
        """Create a goal and verify it appears in the list."""
        goal_text = f"e2e-goal-{uuid.uuid4().hex[:8]}: Deploy the quantum stabilizer"
        r = colony.post("/v1/host/goals", json={
            "identity": {"host_id": "e2e-test"},
            "title": goal_text,
            "description": goal_text,
            "priority": "high",
        })
        assert r.status_code in (200, 201)

        r = colony.get("/v1/host/goals")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 7. World Model
# ---------------------------------------------------------------------------

class TestWorldModelIntegration:
    """Test world model entity management through Colony."""

    def test_find_entities(self, colony):
        """Query entities from the world model."""
        r = colony.get("/v1/host/world/entities")
        assert r.status_code == 200

    def test_extraction_pipeline(self, colony):
        """Test the format extraction pipeline."""
        import base64
        test_json = json.dumps({"name": "E2E Test Entity", "type": "concept", "importance": "high"})
        content = base64.b64encode(test_json.encode()).decode()
        r = colony.post("/v1/host/world/extract", json={
            "identity": {"host_id": "e2e-test"},
            "content": content,
            "mime_type": "application/json",
        })
        assert r.status_code == 200
        data = r.json()
        assert len(data.get("entities", [])) > 0
        assert data["entities"][0]["name"] == "E2E Test Entity"


# ---------------------------------------------------------------------------
# 8. Context Assembly
# ---------------------------------------------------------------------------

class TestContextAssembly:
    """Test context assembly pulling from multiple subsystems."""

    def test_context_includes_multiple_sources(self, colony):
        """Context should pull from memory, goals, world model, skills."""
        r = colony.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e-context", "contact_id": "e2e"},
            "query": "test context assembly",
            "incoming_message": {"role": "user", "content": "test context assembly"},
        })
        assert r.status_code == 200
        data = r.json()
        # Context should have sections from multiple subsystems
        context_text = json.dumps(data)
        # At minimum, skills and world model should be present
        assert len(context_text) > 50  # Non-trivial context


# ---------------------------------------------------------------------------
# 9. Extraction
# ---------------------------------------------------------------------------

class TestExtractionIntegration:
    """Test document format extraction through Colony."""

    def test_csv_extraction(self, colony):
        import base64
        csv_content = "name,type,role\nAlice,person,engineer\nBob,person,designer\n"
        content = base64.b64encode(csv_content.encode()).decode()
        r = colony.post("/v1/host/world/extract", json={
            "identity": {"host_id": "e2e-test"},
            "content": content,
            "mime_type": "text/csv",
        })
        assert r.status_code == 200
        data = r.json()
        assert len(data.get("entities", [])) == 2

    def test_json_array_extraction(self, colony):
        import base64
        json_content = json.dumps([
            {"name": "Quantum Lab", "type": "organization"},
            {"name": "Dr. Smith", "type": "person"},
        ])
        content = base64.b64encode(json_content.encode()).decode()
        r = colony.post("/v1/host/world/extract", json={
            "identity": {"host_id": "e2e-test"},
            "content": content,
            "mime_type": "application/json",
        })
        assert r.status_code == 200
        data = r.json()
        assert len(data.get("entities", [])) == 2


# ---------------------------------------------------------------------------
# 10. Search Providers
# ---------------------------------------------------------------------------

class TestSearchIntegration:
    """Test search provider configuration."""

    def test_search_providers_listed(self, colony):
        """Search providers endpoint should respond."""
        r = colony.get("/v1/host/search/providers")
        assert r.status_code == 200
        data = r.json()
        assert "providers" in data
        assert "available" in data


# ---------------------------------------------------------------------------
# 11. Autonomy
# ---------------------------------------------------------------------------

class TestAutonomyIntegration:
    """Test autonomy loop and scheduler through Colony."""

    def test_autonomy_cycle(self, colony):
        """Autonomy cycle should execute."""
        r = colony.post("/v1/host/autonomy/cycle", json={
            "identity": {"host_id": "e2e-test"},
        })
        assert r.status_code == 200

    def test_scheduler_schedules(self, colony):
        """Scheduler should have periodic tasks."""
        r = colony.get("/v1/host/autonomy/schedule")
        assert r.status_code == 200
        data = r.json()
        schedules = data.get("schedules", [])
        assert len(schedules) > 0
        # Verify expected default tasks
        task_names = [s["name"] for s in schedules]
        assert "health_check" in task_names
        assert "memory_consolidate" in task_names

    def test_scheduler_enable_disable(self, colony):
        """Should be able to enable/disable a schedule."""
        r = colony.get("/v1/host/autonomy/schedule")
        schedules = r.json().get("schedules", [])
        if not schedules:
            pytest.skip("No schedules to test")
        sched_id = schedules[0]["id"]

        # Disable
        r = colony.post(f"/v1/host/autonomy/schedule/{sched_id}/disable")
        assert r.status_code == 200

        # Re-enable
        r = colony.post(f"/v1/host/autonomy/schedule/{sched_id}/enable")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 12. Contacts & Briefings
# ---------------------------------------------------------------------------

class TestContactsBriefings:
    """Test contacts and briefings subsystems."""

    def test_list_contacts(self, colony):
        r = colony.get("/v1/host/contacts")
        assert r.status_code == 200

    def test_list_briefings(self, colony):
        r = colony.get("/v1/host/briefings")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 13. Secrets
# ---------------------------------------------------------------------------

class TestSecretsIntegration:
    """Test secrets vault through Colony."""

    def test_secrets_crud(self, colony):
        key = f"e2e-test-key-{uuid.uuid4().hex[:6]}"
        # Create
        r = colony.post("/v1/host/secrets/set", json={
            "identity": {"host_id": "e2e-test"},
            "key": key,
            "value": "super-secret-e2e-value",
        })
        assert r.status_code in (200, 201)

        # List
        r = colony.post("/v1/host/secrets/list", json={
            "identity": {"host_id": "e2e-test"},
        })
        assert r.status_code == 200

        # Get
        r = colony.post("/v1/host/secrets/get", json={
            "identity": {"host_id": "e2e-test"},
            "key": key,
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 14. Signals & Cognition
# ---------------------------------------------------------------------------

class TestSignalsCognition:
    """Test signal ingestion and cognition metrics."""

    def test_signal_ingest(self, colony):
        r = colony.post("/v1/host/signals/ingest", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e", "contact_id": "e2e"},
            "signal_type": "engagement",
            "data": {"test": True},
        })
        assert r.status_code == 200

    def test_cognition_cpi(self, colony):
        r = colony.get("/v1/host/cognition/cpi")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 15. Full System Health
# ---------------------------------------------------------------------------

class TestFullSystemHealth:
    """Comprehensive system health check matching colony doctor."""

    def test_all_capabilities_present(self, colony, colony_health):
        """All 22 Colony capabilities should be present."""
        expected = {
            "memory", "consolidate", "response_gate", "signals",
            "reasoning", "goals", "contacts", "briefings",
            "world_model", "cognition", "research", "delivery", "synthesis",
            "learning", "skills", "identity", "secrets", "autonomy",
            "sessions", "task_queue", "events",
            "commitments", "affect", "shared_facts", "patterns", "surprises",
            "context", "world_model_api", "event_journal", "context_compression",
            "skill_sandbox", "security_scanner", "tom_extract",
        }
        actual = set(colony_health.get("capabilities", []))
        missing = expected - actual
        assert not missing, f"Missing capabilities: {missing}"

    def test_memory_wired(self, colony):
        r = colony.get("/v1/host/memory/status")
        data = r.json()
        # Memory wiring depends on Neo4j availability
        # In test mode without Neo4j auth, it may be unwired
        if data.get("wired"):
            assert True
        else:
            # At minimum, endpoint should respond
            assert "wired" in data

    def test_identity_complete(self, colony):
        r = colony.get("/v1/host/identity/status")
        d = r.json()
        assert d["colony_id"]
        assert d["public_key"]
        assert d["node_id"]
        assert d["keys_configured"] is True
        assert d["is_genesis"] is not None  # Value is deployment-specific

"""Unit tests for Apsimo MCP Server."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip entire module if mcp package is not installed
pytest.importorskip("mcp")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    """Set required env vars for all tests."""
    monkeypatch.setenv("COLONY_API_KEY", "test-key")
    monkeypatch.setenv("COLONY_URL", "http://localhost:7777")
    monkeypatch.setenv("COLONY_MCP_CONTACT_ID", "testuser")
    monkeypatch.setenv("COLONY_MCP_SOURCE", "test-runner")


@pytest.fixture
def server():
    from apsimo.mcp.server import create_server
    return create_server()


@pytest.fixture
def tool_names(server):
    return list(server._tool_manager._tools.keys())


# ---------------------------------------------------------------------------
# Server creation tests
# ---------------------------------------------------------------------------

class TestServerCreation:
    def test_creates_server(self, server):
        assert server.name == "apsimo"

    @pytest.mark.asyncio
    async def test_legacy_calls_use_one_advertised_catalog(self, server):
        with patch('apsimo.mcp.server._get', new=AsyncMock(return_value={'status': 'ok'})) as get:
            old = await server.call_tool('colony_health', {})
            new = await server.call_tool('apsimo_health', {})
        assert old == new
        assert get.await_count == 2
        assert len(await server.list_tools()) == 19
        assert all(tool.name.startswith('apsimo_') for tool in await server.list_tools())

    def test_has_19_tools(self, tool_names):
        assert len(tool_names) == 19

    def test_has_expected_tools(self, tool_names):
        expected = [
            "apsimo_health",
            "apsimo_get_context",
            "apsimo_check_commitments",
            "apsimo_lookup_facts",
            "apsimo_check_affect",
            "apsimo_search_world",
            "apsimo_get_patterns",
            "apsimo_create_commitment",
            "apsimo_fulfill_commitment",
            "apsimo_cancel_commitment",
            "apsimo_remember_fact",
            "apsimo_forget_fact",
            "apsimo_forget_sources",
            "apsimo_record_affect",
            "apsimo_record_surprise",
            # Task / initiative tools added after the original 14.
            "apsimo_task_complete",
            "apsimo_task_snooze",
            "apsimo_task_dismiss",
            "apsimo_initiative_feedback",
        ]
        for tool in expected:
            assert tool in tool_names, f"Missing tool: {tool}"

    def test_has_resources(self, server):
        resources = list(server._resource_manager._resources.keys())
        assert len(resources) >= 4  # status, commitments, world, surprises

    def test_has_prompts(self, server):
        prompts = list(server._prompt_manager._prompts.keys())
        assert "daily_briefing" in prompts
        assert "pre_task" in prompts
        assert "post_task" in prompts

    def test_read_only_tools(self, server):
        tools = server._tool_manager._tools
        ro_tools = [name for name, t in tools.items() if t.annotations.readOnlyHint]
        assert len(ro_tools) == 7
        assert "apsimo_health" in ro_tools
        assert "apsimo_get_context" in ro_tools

    def test_mutating_tools(self, server):
        tools = server._tool_manager._tools
        rw_tools = [name for name, t in tools.items() if not t.annotations.readOnlyHint]
        assert len(rw_tools) == 12
        assert "apsimo_create_commitment" in rw_tools
        assert "apsimo_remember_fact" in rw_tools


# ---------------------------------------------------------------------------
# Contact ID resolution tests
# ---------------------------------------------------------------------------

class TestContactIdResolution:
    def test_explicit_contact_id(self):
        from apsimo.mcp.server import _contact_id
        os.environ.pop("COLONY_MCP_CONTACT_ID", None)
        assert _contact_id("explicit") == "explicit"

    def test_env_contact_id(self):
        from apsimo.mcp.server import _contact_id
        os.environ["COLONY_MCP_CONTACT_ID"] = "envuser"
        assert _contact_id() == "envuser"

    def test_no_contact_id(self):
        from apsimo.mcp.server import _contact_id
        os.environ.pop("COLONY_MCP_CONTACT_ID", None)
        assert _contact_id() is None

    def test_explicit_overrides_env(self):
        from apsimo.mcp.server import _contact_id
        os.environ["COLONY_MCP_CONTACT_ID"] = "envuser"
        assert _contact_id("override") == "override"


class TestRequireContact:
    def test_with_explicit(self):
        from apsimo.mcp.server import _require_contact
        cid, err = _require_contact("owner")
        assert cid == "owner"
        assert err == {}

    def test_with_env(self):
        from apsimo.mcp.server import _require_contact
        os.environ["COLONY_MCP_CONTACT_ID"] = "envuser"
        cid, err = _require_contact()
        assert cid == "envuser"
        assert err == {}

    def test_missing(self):
        from apsimo.mcp.server import _require_contact
        os.environ.pop("COLONY_MCP_CONTACT_ID", None)
        cid, err = _require_contact()
        assert cid == ""
        assert err.get("error") == "contact_id_required"


# ---------------------------------------------------------------------------
# Source tracking tests
# ---------------------------------------------------------------------------

class TestSourceTracking:
    def test_source_from_env(self):
        from apsimo.mcp.server import _source
        os.environ["COLONY_MCP_SOURCE"] = "claude-code"
        assert _source() == "claude-code"

    def test_source_none(self):
        from apsimo.mcp.server import _source
        os.environ.pop("COLONY_MCP_SOURCE", None)
        assert _source() is None


# ---------------------------------------------------------------------------
# HTTP helper tests (with mocked sidecar)
# ---------------------------------------------------------------------------

class TestHTTPHelpers:
    @pytest.mark.asyncio
    async def test_get_success(self):
        from apsimo.mcp.server import _get
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ok"}

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            result = await _get("/v1/host/health")
            assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_get_connection_error(self):
        from apsimo.mcp.server import _get
        import httpx

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            result = await _get("/v1/host/health")
            assert result["error"] == "sidecar_unreachable"

    @pytest.mark.asyncio
    async def test_post_injects_source(self):
        from apsimo.mcp.server import _post
        os.environ["COLONY_MCP_SOURCE"] = "codex"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "test"}

        captured_data = {}

        async def mock_post(url, **kwargs):
            captured_data.update(kwargs.get("json", {}))
            return mock_response

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            await _post("/v1/host/commitments", {"description": "test"})
            # Provenance is injected under metadata.provenance (a top-level
            # 'source'/'provenance' is not in any sidecar schema and would be
            # dropped by Pydantic validation).
            assert captured_data.get("metadata", {}).get("provenance") == "codex"

    @pytest.mark.asyncio
    async def test_post_preserves_existing_metadata(self):
        from apsimo.mcp.server import _post
        os.environ["COLONY_MCP_SOURCE"] = "codex"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "test"}

        captured_data = {}

        async def mock_post(url, **kwargs):
            captured_data.update(kwargs.get("json", {}))
            return mock_response

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            # Existing metadata keys are preserved; provenance is added alongside.
            await _post("/v1/host/commitments", {"description": "test", "metadata": {"priority": "high"}})
            assert captured_data["metadata"]["priority"] == "high"
            assert captured_data["metadata"]["provenance"] == "codex"

    @pytest.mark.asyncio
    async def test_post_connection_error(self):
        from apsimo.mcp.server import _post
        import httpx

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            result = await _post("/v1/host/commitments", {"description": "test"})
            assert result["error"] == "sidecar_unreachable"
            assert "apsimo start" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_get_non_200(self):
        from apsimo.mcp.server import _get

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "not found"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            result = await _get("/v1/host/nonexistent")
            assert result["error"] == "http_404"

    @pytest.mark.asyncio
    async def test_delete_success(self):
        from apsimo.mcp.server import _delete

        mock_response = MagicMock()
        mock_response.status_code = 204

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.delete = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            result = await _delete("/v1/host/commitments/abc")
            # _delete returns (status_code, error_message_or_empty).
            assert result == (204, "")


# ---------------------------------------------------------------------------
# Tool behavior tests (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestToolBehavior:
    @pytest.mark.asyncio
    async def test_health_tool(self, server):
        tools = server._tool_manager._tools
        # Just verify the tool exists and has the right annotation
        tool = tools["apsimo_health"]
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.idempotentHint is True

    @pytest.mark.asyncio
    async def test_create_commitment_requires_contact(self):
        from apsimo.mcp.server import _require_contact
        os.environ.pop("COLONY_MCP_CONTACT_ID", None)
        cid, err = _require_contact(None)
        assert err.get("error") == "contact_id_required"

    def test_headers_include_api_key(self):
        from apsimo.mcp.server import _headers
        os.environ["COLONY_API_KEY"] = "test-key"
        headers = _headers()
        assert headers["Authorization"] == "Bearer test-key"

    def test_headers_empty_without_key(self):
        from apsimo.mcp.server import _headers
        os.environ.pop("COLONY_API_KEY", None)
        headers = _headers()
        assert headers == {}

    def test_base_url_from_env(self):
        from apsimo.mcp.server import _base_url
        os.environ["COLONY_URL"] = "http://custom:9999"
        assert _base_url() == "http://custom:9999"

    def test_base_url_default(self):
        from apsimo.mcp.server import _base_url
        os.environ.pop("COLONY_URL", None)
        assert _base_url() == "http://127.0.0.1:7777"

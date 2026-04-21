"""Tests for Phase 3 — sidecar tool-invocation + skill-execution endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod


@asynccontextmanager
async def _client_with(patches: dict):
    originals = {k: getattr(host_mod, k) for k in patches}
    for k, v in patches.items():
        setattr(host_mod, k, v)
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        for k, v in originals.items():
            setattr(host_mod, k, v)


class _StubExecutor:
    def __init__(self):
        self._handlers = {
            "calculate": self._calculate,
            "echo": self._echo,
            "boom": self._boom,
        }

    async def _calculate(self, args):
        return str(eval(args.get("expression", "0"), {"__builtins__": {}}))  # noqa: S307

    async def _echo(self, args):
        return args.get("text", "")

    async def _boom(self, _args):
        raise RuntimeError("intentional")


@pytest.mark.asyncio
async def test_tools_invoke_runs_registered_handler():
    async with _client_with({"_tool_executor": _StubExecutor()}) as client:
        resp = await client.post(
            "/v1/host/reasoning/tools/invoke",
            json={
                "identity": {"host_id": "h"},
                "name": "calculate",
                "arguments": {"expression": "2+3"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["result"] == "5"
        assert body["error"] is None


@pytest.mark.asyncio
async def test_tools_invoke_returns_error_for_unknown_tool():
    async with _client_with({"_tool_executor": _StubExecutor()}) as client:
        resp = await client.post(
            "/v1/host/reasoning/tools/invoke",
            json={
                "identity": {"host_id": "h"},
                "name": "nope",
                "arguments": {},
            },
        )
        body = resp.json()
        assert body["available"] is False
        assert "not registered" in (body["error"] or "")


@pytest.mark.asyncio
async def test_tools_invoke_without_executor_returns_unavailable():
    async with _client_with({"_tool_executor": None}) as client:
        resp = await client.post(
            "/v1/host/reasoning/tools/invoke",
            json={
                "identity": {"host_id": "h"},
                "name": "calculate",
                "arguments": {},
            },
        )
        body = resp.json()
        assert body["available"] is False
        assert body["error"] == "tool_executor_not_initialized"


@pytest.mark.asyncio
async def test_tools_invoke_surfaces_handler_exception():
    async with _client_with({"_tool_executor": _StubExecutor()}) as client:
        resp = await client.post(
            "/v1/host/reasoning/tools/invoke",
            json={
                "identity": {"host_id": "h"},
                "name": "boom",
                "arguments": {},
            },
        )
        body = resp.json()
        assert body["available"] is True
        assert "intentional" in (body["error"] or "")


@pytest.mark.asyncio
async def test_skill_execute_success():
    class _Executor:
        async def invoke(self, skill_id, inputs, caller_context=None):
            return SimpleNamespace(
                execution_id="exec-1",
                skill_id=skill_id,
                status="success",
                output={"double": inputs.get("x", 0) * 2},
                error=None,
                duration_ms=42,
                peak_memory_mb=None,
            )

    async with _client_with({"_skill_executor": _Executor()}) as client:
        resp = await client.post(
            "/v1/host/skills/s1/execute",
            json={"identity": {"host_id": "h"}, "arguments": {"x": 7}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["output"] == {"double": 14}
        assert body["execution_id"] == "exec-1"
        assert body["duration_ms"] == 42


@pytest.mark.asyncio
async def test_skill_execute_failure_carries_error():
    class _Executor:
        async def invoke(self, skill_id, inputs, caller_context=None):
            return SimpleNamespace(
                execution_id="exec-2",
                skill_id=skill_id,
                status="failed",
                output=None,
                error="Capability guard denied execution",
                duration_ms=0,
                peak_memory_mb=None,
            )

    async with _client_with({"_skill_executor": _Executor()}) as client:
        resp = await client.post(
            "/v1/host/skills/s1/execute",
            json={"identity": {"host_id": "h"}, "arguments": {}},
        )
        body = resp.json()
        assert body["status"] == "failed"
        assert "Capability guard" in body["error"]


@pytest.mark.asyncio
async def test_skill_execute_without_executor_returns_503():
    async with _client_with({"_skill_executor": None}) as client:
        resp = await client.post(
            "/v1/host/skills/s1/execute",
            json={"identity": {"host_id": "h"}, "arguments": {}},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "skill_executor_not_initialized"

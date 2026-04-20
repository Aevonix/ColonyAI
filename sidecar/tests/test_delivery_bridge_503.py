"""Delivery endpoints must 503 when the bridge isn't wired, not silently empty."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod


@asynccontextmanager
async def _app_with_bridge(bridge):
    """Build a minimal FastAPI app that only mounts the host router."""
    from fastapi import FastAPI
    prev = host_mod._delivery_bridge
    host_mod._delivery_bridge = bridge
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        host_mod._delivery_bridge = prev


@pytest.mark.asyncio
async def test_pending_returns_503_when_bridge_missing():
    async with _app_with_bridge(None) as client:
        resp = await client.get("/v1/host/delivery/pending")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "delivery_bridge_not_initialized"


@pytest.mark.asyncio
async def test_mark_sent_returns_503_when_bridge_missing():
    async with _app_with_bridge(None) as client:
        resp = await client.post(
            "/v1/host/delivery/mark-sent",
            json={
                "identity": {"host_id": "test"},
                "delivery_id": "abc",
            },
        )
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_pending_returns_ok_when_bridge_wired():
    class _FakeBridge:
        def get_pending(self, gateway_id: str = "", limit: int = 20):
            return []

    async with _app_with_bridge(_FakeBridge()) as client:
        resp = await client.get("/v1/host/delivery/pending")
        assert resp.status_code == 200
        assert resp.json()["pending"] == []

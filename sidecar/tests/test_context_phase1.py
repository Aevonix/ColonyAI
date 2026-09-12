"""Tests for Phase 1 context-injection extensions.

Covers:
- IdentityStatusResponse gains trust_tier + node_cert_fingerprint + trust_anchor_verified
- HostIdentity accepts colony_id/node_id/trust_tier
- The obsolete enriched context endpoint is absent; canonical context has separate behavior tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apsimo.api.routers import host as host_mod


@asynccontextmanager
async def _client_with(patches: dict):
    """Patch host-router globals for the duration of one request."""
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


def test_host_identity_accepts_colony_fields():
    from apsimo.api.schemas.host import HostIdentity
    ident = HostIdentity(
        host_id="h",
        colony_id="c1",
        node_id="n1",
        node_cert_fingerprint="ab" * 16,
        trust_tier="GENESIS",
    )
    assert ident.colony_id == "c1"
    assert ident.node_id == "n1"
    assert ident.trust_tier == "GENESIS"


def test_identity_status_response_has_new_fields():
    from apsimo.api.schemas.host import IdentityStatusResponse
    resp = IdentityStatusResponse(
        colony_id="c1",
        trust_tier="REGULAR",
        trust_anchor_verified=True,
        node_cert_fingerprint="ff" * 16,
    )
    assert resp.trust_tier == "REGULAR"
    assert resp.trust_anchor_verified is True
    assert resp.node_cert_fingerprint == "ff" * 16


@pytest.mark.asyncio
async def test_identity_status_returns_trust_tier_when_genesis(monkeypatch):
    """When is_genesis() returns True, the router reports trust_tier=GENESIS."""
    key_mgr = SimpleNamespace(public_key_hex=lambda: "deadbeef")
    chain = SimpleNamespace(colony_id="col-1", _key_manager=key_mgr)

    monkeypatch.setattr(
        "apsimo.chain.identity.is_genesis",
        lambda _cid, _pk: True,
    )
    monkeypatch.setattr(
        "apsimo.chain.identity.get_genesis_manifest",
        lambda: {"colony_id": "col-1"},
    )
    monkeypatch.setattr(
        "apsimo.chain.node.get_node_info",
        lambda _sd: {"node_id": "n1", "node_public_key": "pk1"},
    )
    monkeypatch.setattr(
        "apsimo.chain.node.load_node_certificate",
        lambda _sd: {"signature": "sig", "node_public_key": "pk1"},
    )

    async with _client_with({"_chain_manager": chain}) as client:
        resp = await client.get("/v1/host/identity/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trust_tier"] == "GENESIS"
        assert body["trust_anchor_verified"] is True
        assert body["is_genesis"] is True
        # Fingerprint is 32 hex chars (truncated sha256).
        assert body["node_cert_fingerprint"] is not None
        assert len(body["node_cert_fingerprint"]) == 32


@pytest.mark.asyncio
async def test_identity_status_null_trust_when_no_anchor(monkeypatch):
    """Without a loaded genesis manifest, trust_tier stays None."""
    key_mgr = SimpleNamespace(public_key_hex=lambda: "deadbeef")
    chain = SimpleNamespace(colony_id="col-1", _key_manager=key_mgr)
    monkeypatch.setattr(
        "apsimo.chain.identity.is_genesis",
        lambda _cid, _pk: False,
    )
    monkeypatch.setattr(
        "apsimo.chain.identity.get_genesis_manifest",
        lambda: None,
    )

    async with _client_with({"_chain_manager": chain}) as client:
        resp = await client.get("/v1/host/identity/status")
        body = resp.json()
        assert body["trust_tier"] is None
        assert body["trust_anchor_verified"] is False


@pytest.mark.asyncio
async def test_obsolete_enriched_context_route_is_absent():
    async with _client_with({}) as client:
        response = await client.post('/v1/host/context/enriched', json={})
        assert response.status_code == 404

"""Tests for Phase 1 context-injection extensions.

Covers:
- IdentityStatusResponse gains trust_tier + node_cert_fingerprint + trust_anchor_verified
- HostIdentity accepts colony_id/node_id/trust_tier
- /v1/host/context/enriched emits the new sections when the subsystems are wired
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod


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
    from colony_sidecar.api.schemas.host import HostIdentity
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
    from colony_sidecar.api.schemas.host import IdentityStatusResponse
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
        "colony_sidecar.chain.identity.is_genesis",
        lambda _cid, _pk: True,
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.identity.get_genesis_manifest",
        lambda: {"colony_id": "col-1"},
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.node.get_node_info",
        lambda _sd: {"node_id": "n1", "node_public_key": "pk1"},
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.node.load_node_certificate",
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
        "colony_sidecar.chain.identity.is_genesis",
        lambda _cid, _pk: False,
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.identity.get_genesis_manifest",
        lambda: None,
    )

    async with _client_with({"_chain_manager": chain}) as client:
        resp = await client.get("/v1/host/identity/status")
        body = resp.json()
        assert body["trust_tier"] is None
        assert body["trust_anchor_verified"] is False


@pytest.mark.asyncio
async def test_enriched_context_includes_identity_section(monkeypatch):
    """When identity feature is requested + chain is wired, a colony-identity
    section is produced."""
    key_mgr = SimpleNamespace(public_key_hex=lambda: "deadbeef")
    chain = SimpleNamespace(colony_id="col-42", _key_manager=key_mgr)

    monkeypatch.setattr(
        "colony_sidecar.chain.identity.is_genesis",
        lambda _cid, _pk: True,
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.identity.get_genesis_manifest",
        lambda: {"colony_id": "col-42"},
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.node.get_node_info",
        lambda _sd: {"node_id": "node-7", "node_public_key": "pk"},
    )
    monkeypatch.setattr(
        "colony_sidecar.chain.node.load_node_certificate",
        lambda _sd: None,
    )

    async with _client_with({"_chain_manager": chain}) as client:
        resp = await client.post(
            "/v1/host/context/enriched",
            json={
                "identity": {"host_id": "h"},
                "context": {"session_id": "s", "contact_id": "c"},
                "message": "hello",
                "features": {"identity": True},
            },
        )
        assert resp.status_code == 200
        sections = resp.json()["sections"]
        ids = [s["id"] for s in sections]
        assert "colony-identity" in ids
        identity_section = next(s for s in sections if s["id"] == "colony-identity")
        assert "col-42" in identity_section["body"]
        assert "node-7" in identity_section["body"]
        assert "GENESIS" in identity_section["body"]


@pytest.mark.asyncio
async def test_enriched_context_includes_briefings(monkeypatch):
    engine = SimpleNamespace(
        get_recent=lambda limit=3: [
            {"title": "Morning scan", "body": "Three new things to review."},
            {"title": "Goal check", "body": "On track for weekly target."},
        ]
    )
    async with _client_with({"_briefings_engine": engine}) as client:
        resp = await client.post(
            "/v1/host/context/enriched",
            json={
                "identity": {"host_id": "h"},
                "context": {"session_id": "s", "contact_id": "c"},
                "message": "hi",
                "features": {"briefings": True},
            },
        )
        body = resp.json()
        ids = [s["id"] for s in body["sections"]]
        assert "colony-briefings" in ids
        brief = next(s for s in body["sections"] if s["id"] == "colony-briefings")
        assert "Morning scan" in brief["body"]


@pytest.mark.asyncio
async def test_enriched_context_includes_contacts_list(monkeypatch):
    class _Store:
        async def list(self):
            return [
                {"contact_id": "alice", "display_name": "Alice", "trust_tier": "friend"},
                {"contact_id": "bob", "display_name": "Bob"},
            ]

    async with _client_with({"_contacts_store": _Store()}) as client:
        resp = await client.post(
            "/v1/host/context/enriched",
            json={
                "identity": {"host_id": "h"},
                "context": {"session_id": "s", "contact_id": "c"},
                "message": "hi",
                "features": {"contactsList": True},
            },
        )
        body = resp.json()
        ids = [s["id"] for s in body["sections"]]
        assert "colony-contacts" in ids
        sec = next(s for s in body["sections"] if s["id"] == "colony-contacts")
        assert "Alice" in sec["body"]
        assert "Bob" in sec["body"]


@pytest.mark.asyncio
async def test_enriched_context_includes_cognition(monkeypatch):
    cpi = SimpleNamespace(overall=0.82, memory=0.9, reasoning=0.75, social=0.8, autonomy=0.7)

    class _Learner:
        async def evaluate(self):
            return cpi

    async with _client_with({"_metalearner": _Learner()}) as client:
        resp = await client.post(
            "/v1/host/context/enriched",
            json={
                "identity": {"host_id": "h"},
                "context": {"session_id": "s", "contact_id": "c"},
                "message": "hi",
                "features": {"cognition": True},
            },
        )
        body = resp.json()
        ids = [s["id"] for s in body["sections"]]
        assert "colony-cognition" in ids
        sec = next(s for s in body["sections"] if s["id"] == "colony-cognition")
        assert "overall: 0.82" in sec["body"]
        assert "memory: 0.90" in sec["body"]


@pytest.mark.asyncio
async def test_enriched_context_omits_new_sections_by_default(monkeypatch):
    """Opt-in flags: without them the new sections aren't produced even when
    subsystems are wired."""
    engine = SimpleNamespace(get_recent=lambda limit=3: [{"title": "t", "body": "b"}])
    async with _client_with({"_briefings_engine": engine}) as client:
        resp = await client.post(
            "/v1/host/context/enriched",
            json={
                "identity": {"host_id": "h"},
                "context": {"session_id": "s", "contact_id": "c"},
                "message": "hi",
                "features": {},
            },
        )
        ids = [s["id"] for s in resp.json()["sections"]]
        assert "colony-briefings" not in ids
        assert "colony-contacts" not in ids
        assert "colony-cognition" not in ids

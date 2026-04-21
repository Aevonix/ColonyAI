"""Phase 4 tests — signed chain-verify attestation + host LLM configure."""

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


@pytest.mark.asyncio
async def test_chain_verify_returns_signed_attestation():
    """When a key manager is attached, chain/verify produces a signed attestation."""

    class _FakeKeyManager:
        def sign(self, payload: bytes) -> str:
            return "ab" * 32  # deterministic fake signature

        def public_key_hex(self) -> str:
            return "cd" * 32

    class _FakeState:
        height = 1

    class _FakeChain:
        colony_id = "colony-xyz"
        _key_manager = _FakeKeyManager()

        async def get_state(self):
            return _FakeState()

    async with _client_with({"_chain_manager": _FakeChain()}) as client:
        resp = await client.post(
            "/v1/host/chain/verify",
            json={
                "identity": {"host_id": "h"},
                "data": "claim: I am genesis",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["colony_id"] == "colony-xyz"
        assert body["signed_attestation"] == "ab" * 32
        assert body["signer_public_key"] == "cd" * 32
        assert body["attested_at"] is not None


@pytest.mark.asyncio
async def test_chain_verify_without_key_manager_omits_attestation():
    """Verify bit is still computed even if no key manager is loaded."""

    class _FakeState:
        height = 1

    class _FakeChain:
        colony_id = "colony-xyz"
        _key_manager = None

        async def get_state(self):
            return _FakeState()

    async with _client_with({"_chain_manager": _FakeChain()}) as client:
        resp = await client.post(
            "/v1/host/chain/verify",
            json={"identity": {"host_id": "h"}, "data": "x"},
        )
        body = resp.json()
        assert body["valid"] is True
        assert body["signed_attestation"] is None
        assert body["signer_public_key"] is None


@pytest.mark.asyncio
async def test_chain_verify_no_chain_returns_invalid():
    async with _client_with({"_chain_manager": None}) as client:
        resp = await client.post(
            "/v1/host/chain/verify",
            json={"identity": {"host_id": "h"}, "data": "x"},
        )
        body = resp.json()
        assert body["valid"] is False


@pytest.mark.asyncio
async def test_configure_host_rebuilds_router(tmp_path, monkeypatch):
    """configure_host rebuilds the LLMRouter and persists the config."""
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))

    # Reuse real LLMRouter / ReasoningLoop wiring — only the builder is stubbed.
    captured: dict = {}

    from colony_sidecar.router import tiers as _tiers_mod
    from colony_sidecar.router import router as _router_mod
    from colony_sidecar import reasoning as _reasoning_mod

    class _FakeRouter:
        pass

    class _FakeLoop:
        def __init__(self, model=None, tools=None):
            captured["loop_built"] = True

    def _fake_build_tiers(cfg):
        captured["provider"] = cfg.get("provider")
        return []

    monkeypatch.setattr(_tiers_mod, "build_tiers_from_host", _fake_build_tiers)
    monkeypatch.setattr(_router_mod, "LLMRouter", lambda tiers=None: _FakeRouter())
    monkeypatch.setattr(_reasoning_mod, "ReasoningLoop", _FakeLoop)

    # Pretend an existing reasoning loop is wired so the re-wire branch runs.
    prev_loop = host_mod._reasoning_loop
    host_mod._reasoning_loop = _FakeLoop()

    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/v1/host/configure",
                json={
                    "identity": {"host_id": "h"},
                    "llm": {
                        "provider": "openai",
                        "api_key": "sk-test",
                        "models": {"medium": "gpt-4o-mini"},
                    },
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["configured"] is True
            assert body["provider"] == "openai"
            assert body["models"] == {"medium": "gpt-4o-mini"}
    finally:
        host_mod._reasoning_loop = prev_loop

    # Persisted config ended up in state dir.
    persisted = tmp_path / ".colony-llm-config.json"
    assert persisted.exists()
    assert captured["provider"] == "openai"


@pytest.mark.asyncio
async def test_configure_host_missing_llm_returns_not_configured():
    app = FastAPI()
    app.include_router(host_mod.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/v1/host/configure",
            json={"identity": {"host_id": "h"}},
        )
        body = resp.json()
        assert body["configured"] is False

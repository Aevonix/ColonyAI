"""Phase 4 tests — signed chain-verify attestation + host LLM configure."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

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
async def test_configure_host_preserves_router_and_executor_references(tmp_path, monkeypatch):
    """A host update reaches retained consumers without replacing tool state."""
    pytest.importorskip("litellm")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
    previous = LLMRouter(tiers={})
    previous.configure({'provider': 'vllm', 'baseUrl': 'http://127.0.0.1:8080/v1',
                        'models': {'small': 'old-neutral'}})
    retained_extractor = previous
    tools = ToolExecutor()
    tools.register("preserved_test_tool", lambda _args: None)
    loop = ReasoningLoop(model=previous, tools=tools)
    monkeypatch.setattr(host_mod, '_llm_router', previous)
    monkeypatch.setattr(host_mod, '_tool_executor', tools)
    monkeypatch.setattr(host_mod, '_reasoning_loop', loop)
    cfg = {'provider': 'vllm', 'baseUrl': 'http://127.0.0.1:8081/v1',
           'apiKey': 'neutral-key', 'models': {'small': 'new-neutral'}}
    app = FastAPI(); app.include_router(host_mod.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post('/v1/host/configure', json={'identity': {'host_id': 'h'}, 'llm': cfg})
        assert resp.status_code == 200, resp.text
        assert resp.json()['routing']['models']['small']['model_id'] == 'openai/new-neutral'
        assert retained_extractor is host_mod._llm_router is loop._model
        assert host_mod._reasoning_loop is loop and loop._tools is tools
        old_revision = retained_extractor.routing_status()['config_revision']
        cfg['functionRoles'] = {'vision': ['missing-binding']}
        invalid = await client.post('/v1/host/configure', json={'identity': {'host_id': 'h'}, 'llm': cfg})
        assert invalid.status_code == 422
        assert retained_extractor.routing_status()['config_revision'] == old_revision
    persisted = tmp_path / '.colony-llm-config.json'
    assert json.loads(persisted.read_text())['models']['small'] == 'new-neutral'
    assert persisted.stat().st_mode & 0o077 == 0


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

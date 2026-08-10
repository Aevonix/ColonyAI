"""The /response-guard/check endpoint enforces the explicit surface contract."""

import hashlib

import pytest
from pydantic import ValidationError

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import ResponseGuardCheckRequest
from colony_sidecar.gate.context_provenance import (
    ContextProvenanceStore, ProvenanceCrossContextGuard)
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard


@pytest.mark.asyncio
async def test_endpoint_flags_cross_context_leak(monkeypatch):
    # Written against legacy all-checks enforcement; the per-check enforce
    # allowlist (H6.3, default secret_leak) is covered in
    # test_guard_enforce_policy.py.
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    store = ContextProvenanceStore(":memory:")
    store.record("rcs:conv-A", ["Project Falcon"])
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    monkeypatch.setattr(host_mod, "_response_guard", guard)

    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat", response_text="re: Project Falcon", target_gateway="rcs",
        conversation_key="rcs:conv-B", mentioned_entities=["Project Falcon"]))
    assert out["decision"] == "revise"
    assert any(f["check"] == "cross_context" for f in out["findings"])


@pytest.mark.asyncio
async def test_endpoint_shadow_request_cannot_weaken_configured_enforce(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    store = ContextProvenanceStore(":memory:")
    store.record("rcs:conv-A", ["Project Falcon"])
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat", response_text="re: Project Falcon", target_gateway="rcs",
        conversation_key="rcs:conv-B", mentioned_entities=["Project Falcon"], mode="shadow"))
    assert out["decision"] == "revise" and out["mode"] == "enforce"
    assert out["findings"]


@pytest.mark.asyncio
async def test_endpoint_missing_guard_allows_shadow_text(monkeypatch):
    monkeypatch.setattr(host_mod, "_response_guard", None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "shadow")
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat", response_text="hi"))
    assert out["decision"] == "allow" and out["mode"] == "shadow"
    assert out["guard_status"] == "degraded"


@pytest.mark.asyncio
async def test_endpoint_missing_guard_blocks_enforce_text(monkeypatch):
    monkeypatch.setattr(host_mod, "_response_guard", None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_message", response_text="hi"))
    assert out["decision"] == "block" and out["mode"] == "enforce"
    assert out["guard_status"] == "degraded"
    assert out["candidate_digest"] == hashlib.sha256(b"hi").hexdigest()


@pytest.mark.asyncio
async def test_endpoint_missing_guard_still_bypasses_speech(monkeypatch):
    monkeypatch.setattr(host_mod, "_response_guard", None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="realtime_voice", response_text="hi"))
    assert out["decision"] == "allow" and out["mode"] == "excluded"
    assert out["guard_status"] == "bypassed"


def test_endpoint_request_requires_exact_surface_and_mode():
    with pytest.raises(ValidationError):
        ResponseGuardCheckRequest(response_text="hi")
    with pytest.raises(ValidationError):
        ResponseGuardCheckRequest(surface="voice", response_text="hi")
    with pytest.raises(ValidationError):
        ResponseGuardCheckRequest(
            surface="text_chat", response_text="hi", mode="off")

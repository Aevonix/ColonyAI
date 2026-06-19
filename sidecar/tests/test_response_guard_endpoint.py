"""The /response-guard/check endpoint: evaluates an outbound reply via the configured
ResponseGuard; allows everything when no guard is configured (opt-in)."""

import pytest

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import ResponseGuardCheckRequest
from colony_sidecar.gate.context_provenance import (
    ContextProvenanceStore, ProvenanceCrossContextGuard)
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard


@pytest.mark.asyncio
async def test_endpoint_flags_cross_context_leak(monkeypatch):
    store = ContextProvenanceStore(":memory:")
    store.record("rcs:conv-A", ["Project Falcon"])
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    monkeypatch.setattr(host_mod, "_response_guard", guard)

    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="re: Project Falcon", target_gateway="rcs",
        conversation_key="rcs:conv-B", mentioned_entities=["Project Falcon"]))
    assert out["decision"] == "revise"
    assert any(f["check"] == "cross_context" for f in out["findings"])


@pytest.mark.asyncio
async def test_endpoint_shadow_override_reports_but_allows(monkeypatch):
    store = ContextProvenanceStore(":memory:")
    store.record("rcs:conv-A", ["Project Falcon"])
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        response_text="re: Project Falcon", target_gateway="rcs",
        conversation_key="rcs:conv-B", mentioned_entities=["Project Falcon"], mode="shadow"))
    assert out["decision"] == "allow" and out["mode"] == "shadow"
    assert out["findings"]   # still reported


@pytest.mark.asyncio
async def test_endpoint_disabled_allows(monkeypatch):
    monkeypatch.setattr(host_mod, "_response_guard", None)
    out = await host_mod.response_guard_check(ResponseGuardCheckRequest(response_text="hi"))
    assert out["decision"] == "allow" and out["mode"] == "disabled"

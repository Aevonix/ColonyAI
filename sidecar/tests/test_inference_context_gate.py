"""Tests for context-gate wiring in the inference task handler."""

from __future__ import annotations

import asyncio

from colony_sidecar.router.tiers import ModelTier, TierConfig
from colony_sidecar.task_queue.handlers.inference import InferenceHandler


class _FakeRouter:
    def __init__(self, useful_ctx: int):
        self._cfg = TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/big",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1000,
            useful_context_tokens=useful_ctx,
        )

    def tier_config(self, tier):
        return self._cfg if tier == ModelTier.LARGE else None

    def route(self, prompt, context):
        return ModelTier.LARGE, self._cfg.model_id


def _handler(useful_ctx: int) -> InferenceHandler:
    return InferenceHandler(router=_FakeRouter(useful_ctx))


def _big_doc(n: int = 400) -> str:
    paras = [f"Paragraph {i}. " + ("lorem ipsum dolor sit amet " * 10) for i in range(n)]
    paras[n // 2] = "The database outage started at 03:14 UTC."
    return "\n\n".join(paras)


def test_gate_shrinks_oversized_user_message():
    doc = _big_doc()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": doc + "\n\nWhen did the database outage start?"},
    ]
    h = _handler(useful_ctx=4000)
    gated = asyncio.run(
        h._gate_context(messages, messages[-1]["content"], ModelTier.LARGE, {})
    )
    assert len(gated[-1]["content"]) < len(messages[-1]["content"])
    assert "03:14" in gated[-1]["content"]
    assert gated[0]["content"] == "sys"  # untouched
    # original list not mutated
    assert messages[-1]["content"].endswith("When did the database outage start?")


def test_gate_noop_when_fits():
    messages = [{"role": "user", "content": "short question?"}]
    h = _handler(useful_ctx=2000)
    gated = asyncio.run(h._gate_context(messages, "short question?", ModelTier.LARGE, {}))
    assert gated is messages


def test_gate_noop_without_budget():
    doc = _big_doc()
    messages = [{"role": "user", "content": doc}]
    h = _handler(useful_ctx=0)
    gated = asyncio.run(h._gate_context(messages, doc, ModelTier.LARGE, {}))
    assert gated is messages


def test_gate_payload_opt_out():
    doc = _big_doc()
    messages = [{"role": "user", "content": doc}]
    h = _handler(useful_ctx=2000)
    gated = asyncio.run(
        h._gate_context(messages, doc, ModelTier.LARGE, {"context_gate": "off"})
    )
    assert gated is messages


def test_gate_env_off(monkeypatch):
    monkeypatch.setenv("COLONY_CONTEXT_GATE", "off")
    doc = _big_doc()
    messages = [{"role": "user", "content": doc}]
    h = _handler(useful_ctx=2000)
    gated = asyncio.run(h._gate_context(messages, doc, ModelTier.LARGE, {}))
    assert gated is messages


def test_gate_survives_router_without_tier_config():
    class _Bare:
        def route(self, prompt, context):
            raise RuntimeError("no scorer")

    doc = _big_doc()
    messages = [{"role": "user", "content": doc}]
    h = InferenceHandler(router=_Bare())
    gated = asyncio.run(h._gate_context(messages, doc, None, {}))
    assert gated is messages

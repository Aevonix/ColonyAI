"""Tests for per-tier endpoint overrides (multi-endpoint model tiers)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from colony_sidecar.router.router import LLMRouter
from colony_sidecar.router.tiers import ModelTier, TierConfig, build_tiers_from_host


HOST_CFG = {
    "provider": "vllm",
    "apiKey": "default-key",
    "baseUrl": "http://fast-host:8000/v1",
    "models": {
        "small": "fast-model",
        "medium": {
            "model": "mid-model",
            "base_url": "http://mid-host:8000/v1",  # snake_case alias
            "extra_body": {"priority": 5},
        },
        "large": {
            "model": "big-model",
            "baseUrl": "http://big-host:8000/v1",
            "apiKey": "big-key",
            "extraBody": {"priority": 10},
            "usefulContextTokens": 65536,
            "maxTokens": 32768,
        },
    },
}


def test_string_specs_still_work():
    tiers = build_tiers_from_host(
        {"provider": "vllm", "baseUrl": "http://x/v1", "models": {"large": "m"}}
    )
    cfg = tiers[ModelTier.LARGE]
    assert cfg.model_id == "openai/m"
    assert cfg.base_url == ""
    assert cfg.extra_body is None
    assert cfg.useful_context_tokens == 0


def test_object_specs_parse_camel_and_snake():
    tiers = build_tiers_from_host(dict(HOST_CFG))

    small = tiers[ModelTier.SMALL]
    assert small.model_id == "openai/fast-model"
    assert small.base_url == ""  # inherits provider-wide endpoint

    medium = tiers[ModelTier.MEDIUM]
    assert medium.model_id == "openai/mid-model"
    assert medium.base_url == "http://mid-host:8000/v1"
    assert medium.extra_body == {"priority": 5}

    large = tiers[ModelTier.LARGE]
    assert large.model_id == "openai/big-model"
    assert large.base_url == "http://big-host:8000/v1"
    assert large.api_key == "big-key"
    assert large.extra_body == {"priority": 10}
    assert large.useful_context_tokens == 65536
    assert large.max_tokens == 32768


def test_object_spec_prefix_preserved():
    tiers = build_tiers_from_host(
        {
            "provider": "vllm",
            "baseUrl": "http://x/v1",
            "models": {"large": {"model": "openai/already-prefixed"}},
        }
    )
    assert tiers[ModelTier.LARGE].model_id == "openai/already-prefixed"


def test_object_spec_without_model_skipped():
    tiers = build_tiers_from_host(
        {
            "provider": "vllm",
            "baseUrl": "http://x/v1",
            "models": {"large": {"baseUrl": "http://y/v1"}},
        }
    )
    # Preset model retained; override skipped
    assert tiers[ModelTier.LARGE].model_id == "openai/vllm-large"
    assert tiers[ModelTier.LARGE].base_url == ""


def test_invalid_numeric_fields_ignored():
    tiers = build_tiers_from_host(
        {
            "provider": "vllm",
            "baseUrl": "http://x/v1",
            "models": {"large": {"model": "m", "usefulContextTokens": "lots"}},
        }
    )
    assert tiers[ModelTier.LARGE].useful_context_tokens == 0


def test_router_tier_config_accessor():
    tiers = build_tiers_from_host(dict(HOST_CFG))
    router = LLMRouter(tiers=tiers)
    assert router.tier_config(ModelTier.LARGE).useful_context_tokens == 65536
    assert router.tier_config(ModelTier.HEURISTIC) is None


@pytest.mark.parametrize(
    "tier,expect_base,expect_key,expect_extra",
    [
        (ModelTier.SMALL, None, None, None),
        (ModelTier.MEDIUM, "http://mid-host:8000/v1", None, {"priority": 5}),
        (ModelTier.LARGE, "http://big-host:8000/v1", "big-key", {"priority": 10}),
    ],
)
def test_litellm_call_carries_per_tier_kwargs(monkeypatch, tier, expect_base, expect_key, expect_extra):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content="ok", model_extra={}, tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return SimpleNamespace(choices=[choice], usage=usage, model="m")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    tiers = build_tiers_from_host(dict(HOST_CFG))
    router = LLMRouter(tiers=tiers, self_learner=None)

    asyncio.run(
        router.complete([{"role": "user", "content": "hi"}], force_tier=tier)
    )
    assert captured.get("api_base") == expect_base
    assert captured.get("api_key") == expect_key
    assert captured.get("extra_body") == expect_extra

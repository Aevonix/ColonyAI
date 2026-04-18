"""LLMRouter — route LLM requests to the appropriate model tier.

Wraps LiteLLM's completion API. All Colony code that calls an LLM
MUST go through LLMRouter rather than calling LiteLLM directly.
This centralises cost tracking, fallback logic, and self-learning.

Usage::

    router = LLMRouter()

    # Simple routing — scorer picks the cheapest capable tier
    response = await router.complete(messages)

    # Force a specific tier
    response = await router.complete(messages, force_tier=ModelTier.LARGE)

    # Provide task context to improve tier selection
    response = await router.complete(
        messages,
        context={"tools": tool_defs, "user_tier": "developer"},
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import litellm  # type: ignore[import]

from colony_sidecar.router.complexity_scorer import ComplexityScorer
from colony_sidecar.router.fallback import FallbackHandler
from colony_sidecar.router.self_learning import RouterSelfLearner
from colony_sidecar.router.tiers import DEFAULT_TIERS, ModelTier, TierConfig

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    request_id: str
    tier_used: ModelTier
    model_id: str
    content: str
    usage: dict[str, int]       # prompt_tokens, completion_tokens, total_tokens
    latency_ms: int
    cost_usd: float
    raw: Any = field(default=None, repr=False)


class LLMRouter:
    """Route LLM requests to the appropriate model tier."""

    def __init__(
        self,
        tiers: dict[ModelTier, TierConfig] | None = None,
        scorer: ComplexityScorer | None = None,
        self_learner: RouterSelfLearner | None = None,
        fallback_handler: FallbackHandler | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._tiers = tiers or DEFAULT_TIERS
        self._scorer = scorer or ComplexityScorer()
        self._fallback = fallback_handler or FallbackHandler()
        self._bus = event_bus
        # Self-learner is optional — skip if SQLite is unavailable
        try:
            self._learner: RouterSelfLearner | None = self_learner or RouterSelfLearner()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RouterSelfLearner unavailable: %s", exc)
            self._learner = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        *,
        force_tier: ModelTier | None = None,
        context: dict | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """Call the LLM at the cheapest capable tier.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        force_tier:
            Skip scoring and use this tier.
        context:
            Hints for the complexity scorer (keys: ``user_tier``, ``messages``,
            ``tools``, ``task``).
        tools:
            Tool definitions to pass to LiteLLM.
        stream:
            If True, returns the first assembled chunk (streaming not yet
            fully implemented — kept for API compatibility).
        """
        request_id = str(uuid.uuid4())
        ctx = context or {}
        if tools:
            ctx = {**ctx, "tools": tools}

        # Determine prompt text for scoring (last user message)
        prompt = _last_user_text(messages)

        if force_tier is not None:
            tier = force_tier
        else:
            tier = self._select_tier(prompt, ctx)

        return await self._call_with_fallback(
            request_id=request_id,
            messages=messages,
            tier=tier,
            tools=tools,
            stream=stream,
            prompt=prompt,
        )

    def route(self, prompt: str, context: dict | None = None) -> tuple[ModelTier, str]:
        """Select a model tier without making an LLM call.

        Used by the gateway for pre-call model selection: returns the chosen
        tier and the LiteLLM model string for that tier.

        Parameters
        ----------
        prompt:
            The user's message text to score.
        context:
            Optional scoring hints (``user_tier``, ``tools``, ``messages``).

        Returns
        -------
        (tier, model_id)
        """
        tier = self._select_tier(prompt, context or {})
        config = self._tiers.get(tier) or self._tiers.get(ModelTier.MEDIUM)
        model_id = config.model_id if config else ""
        return tier, model_id

    def record_outcome(
        self,
        request_id: str,
        tier_used: ModelTier,
        quality_rating: float,
        tokens_used: int,
        latency_ms: int,
        prompt: str = "",
    ) -> None:
        """Feed outcome back to the self-learner to improve future routing."""
        if self._learner is None:
            return
        config = self._tiers.get(tier_used)
        cost = 0.0
        if config:
            cost = tokens_used * config.cost_per_1k_output / 1000

        score = self._scorer.score(prompt)
        self._learner.record(score, tier_used, quality_rating, cost)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_tier(self, prompt: str, context: dict) -> ModelTier:
        if self._learner is not None:
            small_cutoff, medium_cutoff = self._learner.get_thresholds()
        else:
            small_cutoff, medium_cutoff = 0.3, 0.65

        score = self._scorer.score(prompt, context)
        if score < small_cutoff:
            return ModelTier.SMALL
        elif score < medium_cutoff:
            return ModelTier.MEDIUM
        else:
            return ModelTier.LARGE

    async def _call_with_fallback(
        self,
        *,
        request_id: str,
        messages: list[dict],
        tier: ModelTier,
        tools: list[dict] | None,
        stream: bool,
        prompt: str,
    ) -> LLMResponse:
        current_tier = tier
        last_exc: Exception | None = None

        while True:
            config = self._tiers.get(current_tier)
            if config is None:
                raise ValueError(f"No TierConfig for tier {current_tier}")

            try:
                response = await self._litellm_call(
                    request_id=request_id,
                    config=config,
                    messages=messages,
                    tools=tools,
                    stream=stream,
                )
                self._emit_cost_event(response)
                return response

            except Exception as exc:
                last_exc = exc
                if self._fallback.should_escalate(exc, current_tier):
                    next_t = self._fallback.next_tier(current_tier)
                    if next_t is None:
                        break
                    logger.warning(
                        "LLMRouter: escalating %s → %s for request %s",
                        current_tier.value,
                        next_t.value,
                        request_id,
                    )
                    current_tier = next_t
                else:
                    break

        raise RuntimeError(
            f"LLMRouter: all tiers exhausted for request {request_id}"
        ) from last_exc

    async def _litellm_call(
        self,
        *,
        request_id: str,
        config: TierConfig,
        messages: list[dict],
        tools: list[dict] | None,
        stream: bool,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
            "max_tokens": config.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            kwargs["stream"] = True

        t0 = time.monotonic()
        # LiteLLM's async completion
        raw = await litellm.acompletion(**kwargs)
        latency_ms = int((time.monotonic() - t0) * 1000)

        choice = raw.choices[0]
        content = choice.message.content or ""
        usage = {
            "prompt_tokens": raw.usage.prompt_tokens if raw.usage else 0,
            "completion_tokens": raw.usage.completion_tokens if raw.usage else 0,
            "total_tokens": raw.usage.total_tokens if raw.usage else 0,
        }

        cost_usd = _estimate_cost(config, usage)

        return LLMResponse(
            request_id=request_id,
            tier_used=config.tier,
            model_id=config.model_id,
            content=content,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            raw=raw,
        )

    def _emit_cost_event(self, response: LLMResponse) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(
                "llm_router.cost",
                {
                    "request_id": response.request_id,
                    "tier": response.tier_used.value,
                    "model": response.model_id,
                    "cost_usd": response.cost_usd,
                    "latency_ms": response.latency_ms,
                    "tokens": response.usage,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLMRouter: failed to emit cost event: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last_user_text(messages: list[dict]) -> str:
    """Extract the text of the last user message for scoring."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                ]
                return " ".join(parts)
    # Fall back to all message content
    return " ".join(
        str(m.get("content", "")) for m in messages if isinstance(m.get("content"), str)
    )


def _estimate_cost(config: TierConfig, usage: dict[str, int]) -> float:
    input_cost = usage.get("prompt_tokens", 0) * config.cost_per_1k_input / 1000
    output_cost = usage.get("completion_tokens", 0) * config.cost_per_1k_output / 1000
    return round(input_cost + output_cost, 8)

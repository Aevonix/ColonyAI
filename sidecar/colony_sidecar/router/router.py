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
import os
import time
import uuid
import json
import ipaddress
import socket
import threading
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

# Colony loads its own explicit configuration.  LiteLLM otherwise asks
# python-dotenv to search parent directories during import, which can pull an
# unrelated operator .env into embedded processes and test collection.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
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
    function_role: str = ""
    config_revision: str = ""
    model_revision: str = "unknown"
    binding: str = ""


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
        self._tiers = DEFAULT_TIERS if tiers is None else tiers
        self._snapshot = None
        self._config_path = None
        self._config_stamp = None
        self._config_lock = threading.RLock()
        self._reload_error = None
        self._recent_calls = deque(maxlen=20)
        from .endpoints import EndpointRuntime
        self._endpoints = EndpointRuntime()
        self._scorer = scorer or ComplexityScorer()
        self._fallback = fallback_handler or FallbackHandler()
        self._bus = event_bus
        # Self-learner is optional — skip if SQLite is unavailable
        try:
            self._learner: RouterSelfLearner | None = self_learner or RouterSelfLearner()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RouterSelfLearner unavailable: %s", exc)
            self._learner = None

    @property
    def supports_function_routing(self):
        return self._snapshot is not None

    def configure(self, host_config, *, config_path=None):
        """Update this object so every retained consumer sees the same router."""
        from .tiers import build_tiers_from_host
        from .functions import build_snapshot
        if not isinstance(host_config, dict):
            raise ValueError('Host model config must be an object')
        # Only explicitly named deployed models enter function routing. Provider
        # presets and discovery size guesses are not capability declarations.
        raw_tiers = build_tiers_from_host(host_config, configure_environment=False, discover=False)
        tiers = {tier: cfg for tier, cfg in raw_tiers.items() if tier.value in host_config.get('models', {})}
        snapshot = build_snapshot(host_config, tiers)
        with self._config_lock:
            self._snapshot = snapshot
            self._tiers = snapshot.tiers
            if config_path is not None:
                self._config_path = Path(config_path)
                self._config_stamp = None
            self._reload_error = None
        return snapshot.status()

    def watch_config(self, path):
        self._config_path = Path(path)
        self._config_stamp = None

    def adopt_configuration(self, prepared, *, config_path):
        with self._config_lock:
            self._snapshot = prepared._snapshot
            self._tiers = self._snapshot.tiers
            self.watch_config(config_path)
            self._reload_error = None

    def _reload(self):
        if self._config_path is None:
            return
        with self._config_lock:
            try:
                stat = self._config_path.stat()
                stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if stamp == self._config_stamp:
                    return
                if stat.st_size > 262144:
                    raise ValueError('Routing config is too large')
                config = json.loads(self._config_path.read_text())
                self.configure(config)
                self._config_stamp = stamp
            except Exception as exc:
                # Keep the last validated config; never replace it with cloud
                # defaults after a partial write or invalid candidate binding.
                self._reload_error = type(exc).__name__

    def routing_status(self):
        self._reload()
        snapshot = self._snapshot
        status = snapshot.status() if snapshot else {'config_revision': None, 'roles': {}}
        if snapshot:
            status.update(self._endpoints.status(snapshot))
        return {**status, 'reload_error': self._reload_error, 'recent_calls': list(self._recent_calls)}

    async def discover_models(self):
        """Observe configured pool endpoints without changing their declarations."""
        self._reload()
        snapshot = self._snapshot
        if snapshot is None:
            return None
        await self._endpoints.refresh(snapshot, self._probe_models)
        status = {**snapshot.status(), **self._endpoints.status(snapshot),
                  'reload_error': self._reload_error, 'recent_calls': list(self._recent_calls)}
        models = {}
        for item in status['model_inventory']:
            if item['available'] and not item['stale']:
                for model in item['models']:
                    models.setdefault(model['id'], model)
        return {'models': list(models.values()), 'routing': status,
                'provider': snapshot.provider, 'base_url': snapshot.base_url or None}

    async def _local_addresses(self, snapshot, binding):
        from .functions import endpoint_host
        host = endpoint_host(binding, snapshot)
        if host is None:
            return []
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                host, None, type=socket.SOCK_STREAM), 2)
            if not addresses or any(not any(ipaddress.ip_address(item[4][0]) in net
                    for net in snapshot.networks) for item in addresses):
                return []
            return list(dict.fromkeys(item[4][0] for item in addresses))[:8]

    async def _at_endpoint(self, snapshot, binding, operation):
        # One configured hostname can resolve to an unavailable address family.
        # Retry only connection failures, within the caller's existing deadline.
        addresses = await self._local_addresses(snapshot, binding)
        if not addresses:
            raise ConnectionError('Endpoint has no eligible resolved address')
        for index, address in enumerate(addresses):
            try:
                return await operation(address)
            except Exception as error:
                if index == len(addresses)-1 or not _connection_failure(error):
                    raise

    async def _probe_models(self, snapshot, binding):
        from .endpoints import models_url
        headers = {'Authorization': 'Bearer '+binding.config.api_key} if binding.config.api_key else {}
        async def read(pinned):
            async with _local_http_client(binding.config, pinned) as client:
                async with client.stream('GET', models_url(binding.config.base_url), headers=headers) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 262144:
                            raise ValueError('Model listing is too large')
                    return data
        data = await self._at_endpoint(snapshot, binding, read)
        rows = json.loads(data).get('data')
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError('Expected a bounded model listing')
        models = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 256:
                continue
            context = row.get('max_model_len')
            models.append({'id': row['id'],
                'owned_by': row.get('owned_by')[:128] if isinstance(row.get('owned_by'), str) else None,
                'advertised_context_tokens': context if type(context) is int and 0 < context <= 10**9 else None})
        return models

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
            Reserved legacy argument. The function path rejects True before
            dispatch; Colony consumers currently request complete responses.
        """
        request_id = str(uuid.uuid4())
        ctx = context or {}
        if tools:
            ctx = {**ctx, "tools": tools}

        self._reload()
        snapshot = self._snapshot
        if snapshot is not None:
            return await self._call_function(snapshot, messages, ctx, tools, force_tier, stream, request_id)
        if not self._tiers:
            raise ValueError('No validated model configuration is available')

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
            max_output_tokens=(
                int(ctx["max_output_tokens"])
                if ctx.get("max_output_tokens") is not None else None
            ),
            allow_fallback=ctx.get("allow_fallback", True) is not False,
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
        self._reload()
        if self._snapshot is not None:
            cfg = self.function_config(context=context)
            return cfg.tier, cfg.model_id
        tier = self._select_tier(prompt, context or {})
        config = self._tiers.get(tier) or self._tiers.get(ModelTier.MEDIUM)
        model_id = config.model_id if config else ""
        return tier, model_id

    def tier_config(self, tier: ModelTier) -> TierConfig | None:
        """Return the TierConfig for *tier* (None if unconfigured).

        Used by callers that need tier metadata without making a call —
        e.g. the context gate reads ``useful_context_tokens`` to decide
        whether an input needs chunking/retrieval before dispatch.
        """
        self._reload()
        return self._tiers.get(tier)

    def function_config(self, *, context=None):
        """Capability hint for the existing context gate; dispatch rechecks DNS."""
        from .functions import TASK_ROLES, candidates
        self._reload()
        ctx = context or {}
        role = ctx.get('function_role') or TASK_ROLES.get(ctx.get('task'), 'reasoning')
        if self._snapshot is None or role not in self._snapshot.roles:
            return None
        eligible = candidates(self._snapshot, role, ctx, has_images=False, has_tools=bool(ctx.get('tools')))
        if not eligible:
            raise ValueError('No eligible local model for function ' + role)
        return eligible[0].config

    def function_deadline_seconds(self, *, context=None):
        """Configured total role budget for bounded background callers."""
        from .functions import TASK_ROLES
        self._reload()
        ctx = context or {}
        role = ctx.get('function_role') or TASK_ROLES.get(ctx.get('task'), 'reasoning')
        snapshot = self._snapshot
        if snapshot is None or role not in snapshot.roles:
            return None
        return snapshot.roles[role].deadline_seconds

    async def _call_function(self, snapshot, messages, context, tools, force_tier, stream, request_id):
        from .functions import TASK_ROLES, candidates
        role_name = context.get('function_role') or TASK_ROLES.get(context.get('task'), 'reasoning')
        if force_tier == ModelTier.VISION:
            role_name = 'vision'
        if role_name not in snapshot.roles:
            raise ValueError('Unknown function role')
        if stream:
            raise ValueError('Function routing currently returns complete responses')
        has_images = any(isinstance(m.get('content'), list) and any(
            isinstance(b, dict) and b.get('type') in {'image_url', 'input_image'} for b in m['content']) for m in messages)
        from colony_sidecar.contextgate import estimate_tokens
        text = '\n'.join(m['content'] if isinstance(m.get('content'), str) else '\n'.join(
            b.get('text', '') for b in (m.get('content') or []) if isinstance(b, dict) and isinstance(b.get('text'), str)) for m in messages)
        selection_context = {**context, 'estimated_input_tokens': estimate_tokens(text) + 8 * len(messages)}
        available = candidates(snapshot, role_name, selection_context, has_images=has_images, has_tools=bool(tools))
        if force_tier is not None:
            # Explicit legacy tier selection remains exact, but must still
            # satisfy this call's capabilities and local network restrictions.
            forced = getattr(force_tier, 'value', force_tier)
            available = [b for b in available if b.name == forced]
        if context.get('allow_fallback') is False:
            available = available[:1]
        role = snapshot.roles[role_name]
        deadline = time.monotonic() + role.deadline_seconds
        failures = []
        seen = set()
        for binding in available:
            cfg = binding.config
            # This finite transport path pins a local OpenAI-compatible client.
            # Provider prefixes may not silently select a cloud SDK instead.
            if not cfg.model_id.startswith('openai/'):
                continue
            endpoint_key = (cfg.base_url, cfg.model_id, binding.weight_revision)
            if endpoint_key in seen:
                continue
            seen.add(endpoint_key)
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            if not self._endpoints.acquire(snapshot, binding, request_id):
                failures.append('EndpointCoolingDown')
                continue
            try:
                async def complete_on_address(pinned_ip):
                    return await self._litellm_call(
                        request_id=request_id, config=cfg, messages=messages, tools=tools,
                        stream=False, max_output_tokens=context.get('max_output_tokens', context.get('max_tokens')),
                        local_endpoint=True, pinned_ip=pinned_ip)
                response = await asyncio.wait_for(self._at_endpoint(snapshot, binding, complete_on_address),
                                                  timeout=min(role.timeout_seconds, remaining))
                response.function_role = role_name
                response.config_revision = snapshot.revision
                response.model_revision = binding.weight_revision
                response.binding = binding.name
                self._endpoints.success(snapshot, binding, response)
                self._recent_calls.append({'request_id': request_id, 'function_role': role_name,
                    'model_id': cfg.model_id, 'binding': binding.name, 'config_revision': snapshot.revision,
                    'weight_revision': binding.weight_revision, 'latency_ms': response.latency_ms})
                self._emit_cost_event(response)
                return response
            except Exception as exc:
                failures.append(type(exc).__name__)
                if not _retryable(exc):
                    break
                # An oversized request may use a larger candidate, but says
                # nothing about this binding's availability for other requests.
                if 'contextwindow' not in type(exc).__name__.lower():
                    self._endpoints.failure(snapshot, binding, exc)
            finally:
                self._endpoints.release(snapshot, binding, request_id)
        raise RuntimeError('No eligible local model completed function ' + role_name +
                           '; attempts=' + ','.join(failures))

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
        max_output_tokens: int | None = None,
        allow_fallback: bool = True,
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
                    max_output_tokens=max_output_tokens,
                )
                self._emit_cost_event(response)
                return response

            except Exception as exc:
                last_exc = exc
                if allow_fallback and self._fallback.should_escalate(exc, current_tier):
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
        max_output_tokens: int | None = None,
        local_endpoint: bool = False,
        pinned_ip: str | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
            "max_tokens": (
                min(config.max_tokens, max(1, max_output_tokens))
                if max_output_tokens is not None else config.max_tokens
            ),
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            kwargs["stream"] = True
        # Per-tier endpoint overrides — different tiers may live on
        # different servers (see TierConfig). When unset, LiteLLM falls
        # back to the provider-wide env config (OPENAI_API_BASE etc.).
        if config.base_url:
            kwargs["api_base"] = config.base_url
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.extra_body:
            kwargs["extra_body"] = dict(config.extra_body)

        t0 = time.monotonic()
        # LiteLLM's async completion
        if local_endpoint:
            # Prevent environment proxy/credential inheritance and HTTP
            # redirects from turning a local fallback into a remote request.
            from openai import AsyncOpenAI
            async with _local_http_client(config, pinned_ip) as http_client:
                async with AsyncOpenAI(base_url=config.base_url, api_key=config.api_key or 'local-no-key',
                                       max_retries=0, http_client=http_client) as client:
                    raw = await litellm.acompletion(**kwargs, client=client, num_retries=0)
        else:
            raw = await litellm.acompletion(**kwargs)
        latency_ms = int((time.monotonic() - t0) * 1000)

        choice = raw.choices[0]
        content = choice.message.content or ""

        # Reasoning models (e.g. GLM-5.1, DeepSeek-R1) may put thinking in
        # reasoning_content and leave content empty. Fall back to reasoning
        # content when the final answer is blank but reasoning exists.
        if not content and hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
            content = choice.message.reasoning_content
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
                    "function_role": response.function_role,
                    "config_revision": response.config_revision,
                    "model_revision": response.model_revision,
                    "binding": response.binding,
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


def _local_http_client(config, pinned_ip):
    """The same explicit local transport for completion and model observation."""
    import httpx
    from urllib.parse import urlsplit
    origin = urlsplit(config.base_url)
    async def pin_request(request):
        if request.url.host != origin.hostname:
            raise ValueError('Unexpected model request host')
        if pinned_ip:
            request.headers['Host'] = origin.netloc
            request.extensions['sni_hostname'] = origin.hostname
            request.url = request.url.copy_with(host=pinned_ip)
    return httpx.AsyncClient(trust_env=False, follow_redirects=False,
                             event_hooks={'request': [pin_request]})


def _retryable(exc):
    """Finite same-role failover, never arbitrary validation/auth retries."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    status = getattr(exc, 'status_code', None)
    if status in {404, 408, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    return any(word in name for word in ('timeout', 'connection', 'ratelimit', 'contextwindow'))


def _connection_failure(error):
    """Recognize transport connect failures even through SDK exception wrappers."""
    from httpx import ConnectError, ConnectTimeout
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ConnectionError, ConnectError, ConnectTimeout)):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__)
                       if isinstance(item, BaseException))
    return False

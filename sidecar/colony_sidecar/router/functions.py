"""Declared function bindings and immutable per-call routing snapshots.

No machine discovery or model-name capability inference. Hosts update the same
configuration when endpoints move; measurements remain labelled declarations.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .tiers import ModelTier, TierConfig, _has_litellm_prefix

FUNCTIONS = {'chat', 'reasoning', 'planning', 'extraction', 'judging', 'vision', 'coding'}
DEFAULT_ROLES = {
    'chat': ['small'], 'reasoning': ['large', 'medium', 'small'],
    'planning': ['large', 'medium', 'small'], 'extraction': ['small', 'medium'],
    'judging': ['large', 'medium'], 'vision': ['vision'], 'coding': ['large', 'medium'],
}
DEFAULT_NETWORKS = ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7')
TASK_ROLES = {
    'source_claim_extraction': 'extraction', 'source_image_description': 'vision',
    'project_planning': 'planning', 'thought_job': 'reasoning',
    'tom_affect_extraction': 'extraction', 'tom_belief_extraction': 'extraction',
    'tom_intention_extraction': 'extraction',
    'tom_fact_extraction': 'extraction', 'tom_engagement_extraction': 'extraction',
    'context_compression': 'extraction',
    'workspace_thinking': 'reasoning', 'internal_thinking': 'reasoning',
    'toolsmith_draft': 'coding', 'skill_distillation': 'judging',
}


@dataclass(frozen=True)
class Binding:
    name: str
    config: TierConfig
    context_tokens: int = 0
    supports_tools: bool | None = None
    latency_ms: int = 0
    tokens_per_second: float = 0
    concurrency: int = 0
    weight_revision: str = 'unknown'
    legacy: bool = False


@dataclass(frozen=True)
class FunctionRole:
    candidates: tuple[str, ...]
    timeout_seconds: float = 20
    deadline_seconds: float = 40
    min_context_tokens: int = 0
    max_latency_ms: int = 0
    min_tokens_per_second: float = 0
    min_concurrency: int = 0


@dataclass(frozen=True)
class RoutingSnapshot:
    revision: str
    tiers: dict
    bindings: dict[str, Binding]
    roles: dict[str, FunctionRole]
    networks: tuple
    declared_hosts: frozenset[str]

    def status(self):
        return {'config_revision': self.revision, 'capability_basis': 'deployment declarations, not measured by this router',
                'roles': {key: list(value.candidates) for key, value in self.roles.items()},
                'models': {name: {'model_id': b.config.model_id, 'weight_revision': b.weight_revision,
                    'context_tokens': b.context_tokens, 'supports_vision': b.config.supports_vision,
                    'supports_tools': b.supports_tools, 'latency_ms': b.latency_ms,
                    'tokens_per_second': b.tokens_per_second, 'concurrency': b.concurrency,
                    'legacy_unknown_tools_allowed': b.legacy and b.supports_tools is None}
                    for name, b in self.bindings.items()}}


def number(value, *, minimum=0, maximum=10**9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError('Invalid routing capability or limit')
    return value


def _binding(name, config, spec, *, legacy=False):
    tools = spec.get('supportsTools')
    if tools is not None and type(tools) is not bool:
        raise ValueError('supportsTools must be an explicit boolean')
    return Binding(name, config,
        int(number(spec.get('contextTokens', config.useful_context_tokens))), tools,
        int(number(spec.get('latencyMs', 0))), float(number(spec.get('tokensPerSecond', 0))),
        int(number(spec.get('concurrency', 0))), str(spec.get('weightRevision') or 'unknown')[:160], legacy)


def _transport(config, host, spec):
    protocol = spec.get('protocol', host.get('protocol'))
    if protocol not in {None, 'openai-chat'}:
        raise ValueError('Function routing requires the openai-chat protocol')
    if config.model_id.startswith('ollama/') and protocol == 'openai-chat':
        if not config.base_url.rstrip('/').endswith('/v1'):
            raise ValueError('Declared Ollama OpenAI compatibility requires an explicit /v1 base URL')
        config = replace(config, model_id='openai/' + config.model_id.removeprefix('ollama/'))
    if not config.model_id.startswith('openai/'):
        raise ValueError('Function routing requires an explicit OpenAI-compatible endpoint; declare protocol openai-chat for compatible Ollama /v1 endpoints')
    return config


def build_snapshot(host: dict, tiers: dict) -> RoutingSnapshot:
    """Build privately, then replace one router reference after validation."""
    bindings, materialized = {}, {}
    for tier, original in tiers.items():
        # Pin inherited endpoint/key into this snapshot. Later provider env
        # changes cannot redirect a request that already selected this config.
        spec = host.get('models', {}).get(tier.value, {})
        spec = spec if isinstance(spec, dict) else {}
        cfg = replace(deepcopy(original),
            base_url=original.base_url or host.get('baseUrl', ''),
            api_key=original.api_key or host.get('apiKey', ''))
        cfg = _transport(cfg, host, spec)
        materialized[tier] = cfg
        bindings[tier.value] = _binding(tier.value, cfg, spec, legacy=True)
    pool = host.get('modelPool', {})
    if not isinstance(pool, dict) or len(pool) > 64:
        raise ValueError('modelPool must contain at most 64 explicit bindings')
    for name, spec in pool.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', name) or name in bindings:
            raise ValueError('Invalid or duplicate modelPool binding')
        if not isinstance(spec, dict) or not isinstance(spec.get('model'), str) or not spec['model']:
            raise ValueError('Every modelPool binding requires a model')
        model = spec['model']
        provider = str(spec.get('provider', host.get('provider', 'vllm')))
        if not _has_litellm_prefix(model):
            if provider == 'ollama': model = 'ollama/' + model
            elif provider in {'local', 'custom', 'lmstudio', 'vllm', 'openai'}: model = 'openai/' + model
        cfg = TierConfig(tier=ModelTier(spec.get('tier', 'medium')), model_id=model,
            max_tokens=int(number(spec.get('maxTokens', 8192), minimum=1)),
            cost_per_1k_input=0, cost_per_1k_output=0, latency_p50_ms=0,
            base_url=spec.get('baseUrl', host.get('baseUrl', '')),
            api_key=spec.get('apiKey', host.get('apiKey', '')),
            extra_body=deepcopy(spec.get('extraBody')),
            useful_context_tokens=int(number(spec.get('contextTokens', 0))),
            supports_vision=spec.get('supportsVision') is True)
        if not isinstance(cfg.base_url, str) or not isinstance(cfg.api_key, str):
            raise ValueError('Invalid model endpoint configuration')
        cfg = _transport(cfg, host, spec)
        bindings[name] = _binding(name, cfg, spec)
    if not bindings:
        raise ValueError('At least one deployed model must be declared explicitly')
    role_config = host.get('functionRoles', {})
    if not isinstance(role_config, dict) or set(role_config) - FUNCTIONS:
        raise ValueError('Unknown function role; speech/embedding/rerank use their own transports')
    roles = {}
    for name, defaults in DEFAULT_ROLES.items():
        raw = role_config.get(name, [key for key in defaults if key in bindings])
        raw = {'candidates': raw} if isinstance(raw, list) else raw
        if not isinstance(raw, dict): raise ValueError('Invalid function role')
        if set(raw) - {'candidates', 'timeoutSeconds', 'deadlineSeconds', 'minContextTokens', 'maxLatencyMs', 'minTokensPerSecond', 'minConcurrency'}:
            raise ValueError('Unknown function role constraint')
        candidates = raw.get('candidates', [])
        if not isinstance(candidates, list) or len(candidates) > 8 or any(not isinstance(key, str) or key not in bindings for key in candidates):
            raise ValueError('Role candidates must name at most eight configured bindings')
        slow = name in {'reasoning', 'planning', 'judging', 'coding'}
        roles[name] = FunctionRole(tuple(dict.fromkeys(candidates)),
            float(number(raw.get('timeoutSeconds', 120 if slow else 20), minimum=.05, maximum=300)),
            float(number(raw.get('deadlineSeconds', 180 if slow else 40), minimum=.05, maximum=600)),
            int(number(raw.get('minContextTokens', 0))), int(number(raw.get('maxLatencyMs', 0))),
            float(number(raw.get('minTokensPerSecond', 0))), int(number(raw.get('minConcurrency', 0))))
    raw_networks = host.get('localNetworks', DEFAULT_NETWORKS)
    if not isinstance(raw_networks, (list, tuple)) or len(raw_networks) > 64:
        raise ValueError('localNetworks must be a bounded list of CIDRs')
    networks = tuple(ipaddress.ip_network(value, strict=False) for value in raw_networks)
    if any(n.prefixlen == 0 for n in networks): raise ValueError('A default route cannot declare the whole internet local')
    hosts = host.get('localHosts', ['localhost'])
    if not isinstance(hosts, list) or any(not isinstance(h, str) or len(h) > 253 for h in hosts):
        raise ValueError('localHosts must list deployment hostnames')
    revision = hashlib.sha256(json.dumps(host, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]
    return RoutingSnapshot(revision, materialized, bindings, roles, networks, frozenset(h.casefold() for h in hosts))


def endpoint_host(binding, snapshot):
    """Return literal eligible IP or declared hostname needing DNS verification.

    No suffix such as .local is trusted. Every resolved address must be inside
    deployment networks; endpoints with URL credentials or query strings fail.
    """
    parsed = urlsplit(binding.config.base_url)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    host = (parsed.hostname or '').casefold()
    try:
        address = ipaddress.ip_address(host)
        return host if any(address in net for net in snapshot.networks) else None
    except ValueError:
        return host if host in snapshot.declared_hosts else None


def candidates(snapshot, role_name, context, *, has_images, has_tools):
    role = snapshot.roles[role_name]
    minimum = max(role.min_context_tokens, int(context.get('required_context_tokens', 0)))
    output = []
    for name in role.candidates:
        b = snapshot.bindings[name]
        if (has_images or role_name == 'vision') and not b.config.supports_vision: continue
        if has_tools and b.supports_tools is not True and not (b.legacy and b.supports_tools is None): continue
        if minimum and b.context_tokens < minimum: continue
        # Reuse the existing text estimator to avoid an obviously undersized
        # fallback. Image token accounting is model-specific and remains unknown.
        requested_output = context.get('max_output_tokens', context.get('max_tokens', b.config.max_tokens))
        if b.context_tokens and context.get('estimated_input_tokens', 0) + min(b.config.max_tokens, int(requested_output)) > b.context_tokens: continue
        if role.max_latency_ms and (not b.latency_ms or b.latency_ms > role.max_latency_ms): continue
        if b.tokens_per_second < role.min_tokens_per_second or b.concurrency < role.min_concurrency: continue
        if endpoint_host(b, snapshot) is None: continue
        output.append(b)
    return output

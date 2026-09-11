"""Resolve a local function binding for a native Hermes caller.

This runs in the sidecar interpreter. The caller captures the private JSON pipe;
Hermes retains its own dependencies, agent loop, fallback and execution identity.
"""
import argparse
import asyncio
import json
from pathlib import Path

from .functions import candidates
from .router import LLMRouter


async def planning(configuration):
    if 'planning' not in configuration.get('functionRoles', {}):
        raise ValueError('An explicit planning role is required')
    router = LLMRouter(tiers={}, self_learner=object())
    router.configure(configuration)
    snapshot = router._snapshot
    selected = candidates(snapshot, 'planning', {}, has_images=False, has_tools=True)
    if not selected:
        raise ValueError('No tool-capable local planning binding is available')
    for binding in selected:
        if not await router._local_addresses(snapshot, binding):
            raise ValueError('Planning endpoint has no eligible local address')
    if any(binding.config.extra_body != selected[0].config.extra_body for binding in selected):
        raise ValueError('Planning candidates require different native request overrides')
    role = snapshot.roles['planning']
    entries = [dict(provider='openai', model=b.config.model_id.removeprefix('openai/'),
                    base_url=b.config.base_url, api_key=b.config.api_key or 'local-no-key',
                    api_mode='chat_completions') for b in selected]
    maximum = min(binding.config.max_tokens for binding in selected)
    options = {**entries[0], 'requested_provider':'openai', 'fallback_model':entries[1:] or None,
               'max_tokens':maximum, 'request_overrides':{'extra_body':selected[0].config.extra_body or {}}}
    policy = {'role':'planning', 'configuration_revision':snapshot.revision,
              'candidates':[{'binding':b.name, 'model':b.config.model_id, 'weight_revision':b.weight_revision} for b in selected],
              'max_output_tokens':maximum, 'request_timeout_seconds':role.timeout_seconds,
              'run_deadline_seconds':role.deadline_seconds,
              'fallback_owner':'Hermes native runtime'}
    return options, policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(planning(json.loads(args.config.read_text())))))


if __name__ == '__main__':
    main()

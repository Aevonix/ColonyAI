"""Opt-in native CLI-loop cases. Each attempt owns a fresh Hermes process/home."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

import yaml

from .records import digest, read, write_once


_PROVIDER_FIELDS = {
    'name', 'base_url', 'url', 'api', 'api_key', 'key_env', 'api_key_env',
    'default_model', 'enabled', 'transport', 'api_mode', 'extra_headers',
    'extra_body', 'request_overrides', 'request_timeout_seconds', 'stale_timeout_seconds',
    'models', 'supports_tools', 'supports_vision', 'supports_reasoning', 'context_length',
}


def configuration(path, binding):
    """Read only the selected provider. Never import a deployed home or auth store."""
    raw = Path(path).read_bytes()
    supplied = yaml.safe_load(raw)
    provider = supplied.get('providers', {}).get(binding) if isinstance(supplied, dict) else None
    if not isinstance(provider, dict) or provider.get('enabled') is False:
        raise ValueError('Native binding must name an enabled Hermes provider')
    if not any(provider.get(k) for k in ('base_url', 'url', 'api')):
        raise ValueError('Native qualification requires an explicit model endpoint')
    provider = {k: deepcopy(v) for k, v in provider.items() if k in _PROVIDER_FIELDS}
    model = provider.get('default_model')
    current = supplied.get('model', {})
    if not model and isinstance(current, dict) and current.get('provider') == binding:
        model = current.get('default')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Native binding requires a configured default model')
    provider['default_model'] = model
    agent = supplied.get('agent', {})
    reasoning = {k: deepcopy(agent[k]) for k in ('reasoning_effort', 'reasoning_overrides', 'service_tier')
                 if isinstance(agent, dict) and k in agent}
    selected = {'model': {'provider': binding, 'default': model}, 'providers': {binding: provider},
        'agent': {**reasoning, 'environment_probe': False, 'api_max_retries': 0},
        'auxiliary': {'title_generation': {'enabled': False}}, 'fallback_providers': []}
    if isinstance(current, dict) and current.get('provider') == binding and 'context_length' in current:
        selected['model']['context_length'] = current['context_length']
    spec = importlib.util.find_spec('run_agent')
    native_source = Path(spec.origin) if spec and spec.origin else None
    recipe = {'binding': binding, 'declared': {}, 'configured_model': model,
        'config_sha256': hashlib.sha256(raw).hexdigest(), 'selected_config_sha256': digest(selected),
        'boundary': 'native_hermes', 'consumer': 'isolated_native_cli_loop',
        'native_source_sha256': hashlib.sha256(native_source.read_bytes()).hexdigest() if native_source else None,
        'native_worker_sha256': hashlib.sha256(Path(__file__).with_name('native_worker.py').read_bytes()).hexdigest(),
        'request_timeout_seconds': provider.get('request_timeout_seconds'),
        'stale_timeout_seconds': provider.get('stale_timeout_seconds'),
        'returned_model': None, 'observed_weight_revision': None,
        'basis': 'configured CLI-loop recipe; no gateway, channel, tool or memory qualification'}
    return selected, recipe


def cases(roles, *, deadline_seconds=60, cleanup_seconds=5):
    from .cases import STANDARD
    if roles != ['chat']:
        raise ValueError('The native suite currently provides the chat role only')
    if isinstance(cleanup_seconds, bool) or not .01 <= cleanup_seconds <= 30:
        raise ValueError('Native cleanup allowance must be .01..30 seconds')
    original = next(c for c in STANDARD if c.id == 'chat.grounded-note')
    return [replace(original, id='native.chat.grounded-note', boundary='native_hermes',
        consumer='native_cli', timeout_seconds=deadline_seconds,
        inputs={**deepcopy(original.inputs), 'cleanup_seconds': cleanup_seconds,
                'max_output_tokens': 1024})]


def _environment(state, config):
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if k in os.environ}
    # Only credentials explicitly referenced by this provider are forwarded.
    names = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', json.dumps(config)))
    for provider in config['providers'].values():
        names.update(provider[k] for k in ('key_env', 'api_key_env') if provider.get(k))
    env.update({k: os.environ[k] for k in names if k in os.environ})
    env.update(HOME=str(state), HERMES_HOME=str(state), HERMES_SKIP_DOTENV='1',
               PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1')
    return env


async def native_cli(inputs, context):
    """Cancel only our child; its SIGTERM handler calls its agent's hard_interrupt."""
    state = context.state_dir
    binding, config = context.router.binding, context.router.native_config
    write_once(state/'config.yaml', config)  # JSON is valid YAML; credentials stay in private state.
    write_once(state/'input.json', {'binding': binding, 'inputs': inputs})
    observed = {'boundary': 'native_cli_loop', 'outcome': 'starting', 'role': inputs['role'],
        'selected_binding': None, 'returned_model': None, 'prior_attempts': None,
        'deadline_semantics': 'suite_elapsed', 'cleanup_allowance_seconds': inputs['cleanup_seconds'],
        'hard_interrupt_requested': False, 'process_exited': False, 'forced_termination': False}
    started = time.monotonic()
    # A real file avoids pipe backpressure; native logs stay with the isolated attempt.
    log_path = state/'native.log'
    with log_path.open('xb') as log:
        log_path.chmod(0o600)
        context.state_cleanup_safe = False
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, '-I', '-B',
            str(Path(__file__).with_name('native_worker.py')), str(state/'input.json'),
            cwd=state, env=_environment(state, config), stdout=log, stderr=log))
        proc = None
        try:
            proc = await asyncio.shield(spawning)
            await proc.wait()
        except asyncio.CancelledError:
            observed['outcome'] = 'cancelled'
            if proc is None:
                proc = await asyncio.shield(spawning)
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), inputs['cleanup_seconds'])
            except (TimeoutError, asyncio.CancelledError):
                observed['forced_termination'] = True
                context.state_cleanup_safe = False
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()
            raise
        finally:
            observed['process_exited'] = proc is not None and proc.returncode is not None
            observed['exit_code'] = proc.returncode if proc else None
            result_path = state/'native-result.json'
            try:
                result = read(result_path) if result_path.exists() else {}
            except (ValueError, OSError):
                result = {}
            # Exit alone does not prove cooperative native cleanup.
            clean = result.get('worker_stopped') is True and result.get('agent_close_returned') is True
            observed.update(native_stage=result.get('stage', 'no_result'),
                hard_interrupt_requested=result.get('hard_interrupt_requested', False),
                owned_worker_stopped=result.get('worker_stopped', False),
                agent_close_returned=result.get('agent_close_returned', False),
                error_type=result.get('error_type'), configured_model=result.get('model'))
            observed['native_turn'] = result.get('turn')
            if observed['outcome'] != 'cancelled':
                observed['outcome'] = result.get('stage', 'error')
            context.state_cleanup_safe = clean and observed['process_exited'] and not observed['forced_termination']
            observed['elapsed_ms'] = round((time.monotonic()-started)*1000, 3)
            context.observe(observed)
    if proc.returncode != 0 or result.get('stage') != 'returned':
        raise RuntimeError('Native qualification did not return a completed result')
    return {'output': result.get('output'), 'effects': {'consumer': 'isolated_native_cli_loop',
        'process_exited': observed['process_exited'], 'worker_stopped': result['worker_stopped'],
        'agent_close_returned': result['agent_close_returned']}}

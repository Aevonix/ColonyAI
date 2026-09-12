"""Finite, sequential attempts with explicit interruption and resume semantics."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import importlib.metadata
import inspect
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from .records import SCHEMA, digest, encode, read, write_once


MAX_COMPLETION_TEXT_BYTES = 65536
_ROUTER_FAILURE = re.compile(
    r'No eligible local model completed function (chat|reasoning|planning|extraction|judging|vision|coding); attempts=([a-zA-Z_,]*)')
_KNOWN_ATTEMPT_REASONS = {'RequestBudgetExceeded', 'EndpointCoolingDown',
    'missing_final_answer', 'incomplete_final_answer', 'TimeoutError', 'ConnectionError',
    'OSError', 'APIConnectionError', 'APITimeoutError', 'APIStatusError', 'RateLimitError',
    'InternalServerError', 'ServiceUnavailableError', 'BadRequestError',
    'AuthenticationError', 'PermissionDeniedError', 'NotFoundError', 'ContextWindowExceededError'}


def completion_text_budget(case):
    return min(case.max_output_bytes, MAX_COMPLETION_TEXT_BYTES)


def router_failure(exc):
    """Decode only the existing router's fixed, nonsecret failure vocabulary."""
    if type(exc) is not RuntimeError or len(exc.args) != 1 or not isinstance(exc.args[0], str):
        return None
    if len(exc.args[0]) > 1024 or not (match := _ROUTER_FAILURE.fullmatch(exc.args[0])):
        return None
    reasons = match[2].split(',') if match[2] else []
    if len(reasons) > 32 or any(reason not in _KNOWN_ATTEMPT_REASONS for reason in reasons):
        return None
    return {'source': 'router_message_allowlist', 'role': match[1], 'attempt_reasons': reasons}


def now():
    return datetime.now(timezone.utc).isoformat()


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class ObservedRouter:
    """Observe the existing consumer call, not a new inference transport."""
    def __init__(self, router, observations, requested_binding, *, qualification_role=None,
                 completion_budget_bytes=MAX_COMPLETION_TEXT_BYTES):
        self._router, self._observations = router, observations
        self._requested_binding = requested_binding
        self._qualification_role = qualification_role
        self._completion_bytes_left = completion_budget_bytes

    def _completion_evidence(self, response):
        """Bound final consumer-visible text, never serialize the raw SDK envelope.

        Charge serialized JSON text bytes across the case, including escaping.
        The original response remains untouched even when evidence is truncated.
        """
        text = value(response, 'content')
        if not isinstance(text, str):
            return {'status': 'no_text_content'}
        raw = encode(text)
        limit = self._completion_bytes_left
        prefix = text
        if len(raw) > limit:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if len(encode(text[:middle])) <= limit:
                    low = middle
                else:
                    high = middle - 1
            prefix = text[:low]
        retained = len(encode(prefix)) if len(encode(prefix)) <= limit else 0
        self._completion_bytes_left -= retained
        return {'status': 'captured', 'text': prefix if retained else None,
                'sha256': hashlib.sha256(raw).hexdigest(), 'hash_encoding': 'records.encode',
                'original_json_bytes': len(raw), 'retained_json_bytes': retained,
                'truncated': retained < len(raw)}

    def __getattr__(self, name):
        return getattr(self._router, name)

    async def complete(self, messages, **kwargs):
        started = time.monotonic()
        observation = {'boundary': 'router_complete', 'input_sha256': digest(messages),
                       'role': (kwargs.get('context') or {}).get('function_role'),
                       # Retain the legacy arm label; it was never a support-role override.
                       'requested_binding': self._requested_binding, 'selected_binding': None,
                       'requested_binding_semantics': 'qualification_candidate',
                       'candidate_binding': self._requested_binding,
                       'qualification_role': self._qualification_role,
                       'configured_model': None, 'returned_model': None,
                       'weight_revision': None, 'usage': None, 'prior_attempts': None}
        self._observations.append(observation)
        try:
            response = await self._router.complete(messages, **kwargs)
            raw = value(response, 'raw')
            usage = value(raw, 'usage')
            if usage is not None:
                observation['usage'] = {k: value(usage, k) for k in
                    ('prompt_tokens', 'completion_tokens', 'total_tokens')}
            observation.update(selected_binding=value(response, 'binding') or None,
                role=value(response, 'function_role') or observation['role'],
                configured_model=value(response, 'model_id') or None,
                returned_model=value(raw, 'model') or None,
                weight_revision=value(response, 'model_revision') or None,
                config_revision=value(response, 'config_revision') or None,
                request_id=value(response, 'request_id') or None,
                prior_attempts=value(response, 'prior_attempts'), outcome='returned',
                completion_evidence=self._completion_evidence(response))
            return response
        except BaseException as exc:
            observation.update(outcome='error', error_type=type(exc).__name__)
            if failure := router_failure(exc):
                observation['router_failure'] = failure
            raise
        finally:
            role = observation.get('role')
            observation['binding_purpose'] = ('unknown' if not role or not self._qualification_role
                else 'target' if role == self._qualification_role else 'supporting')
            observation['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)


class RunContext:
    def __init__(self, router, state_dir, observations, binding=None, *, qualification_role=None,
                 completion_budget_bytes=MAX_COMPLETION_TEXT_BYTES):
        self.router = ObservedRouter(router, observations, binding,
            qualification_role=qualification_role, completion_budget_bytes=completion_budget_bytes)
        self.state_dir = Path(state_dir)
        self.observations = observations
        self.state_cleanup_safe = True

    def observe(self, observation):
        """Native/media adapters can add their own explicitly labelled evidence."""
        encode(observation)
        self.observations.append(deepcopy(observation))


def inspect_binding(config, binding):
    from pacomind.router import LLMRouter
    router = LLMRouter(tiers={})
    status = router.configure(config)
    if binding not in status['models']:
        raise ValueError('Binding does not exist in supplied configuration')
    try:
        version = importlib.metadata.version('pacomind')
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {'schema': SCHEMA, 'binding': binding, 'observed_at': now(),
            'config_revision': status['config_revision'], 'declared': status['models'][binding],
            'configured_roles': [role for role, names in status['roles'].items() if binding in names],
            'runtime_version': version, 'returned_model': None, 'observed_weight_revision': None,
            'routing_snapshot': status,
            'quantization': None, 'serving_engine': None, 'tokenizer_revision': None,
            'prompt_revision': None, 'hardware_observation': None,
            'basis': 'configuration inspection only; no request, serving identity or role quality observed'}


def router_for(config, binding, cases):
    """Pin only a private copy; neither write nor publish deployed role settings."""
    from pacomind.router import LLMRouter
    selected = deepcopy(config)
    roles = selected.setdefault('functionRoles', {})
    for case in cases:
        current = roles.get(case.role, {})
        roles[case.role] = {**(current if isinstance(current, dict) else {}), 'candidates': [binding]}
        for task in case.target_tasks:
            selected.setdefault('taskRoles', {})[task] = case.role
    router = LLMRouter(tiers={})
    router.configure(selected)
    return router


async def evaluate(directory, recipe, cases, consumers, evaluators, router_factory, *, resume=False,
                   evidence_mode='controlled', suite_version='1'):
    """One declared attempt per case. Resume never replays a started attempt."""
    directory = Path(directory)
    records = [case.record() for case in cases]
    if not records or len(records) > 128 or len({c['id'] for c in records}) != len(records):
        raise ValueError('Select 1..128 unique cases')
    if sum(c['timeout_seconds'] for c in records) > 3600:
        raise ValueError('Declared run exceeds one hour')
    if evidence_mode not in {'controlled', 'actual_inference'}:
        raise ValueError('Invalid evidence mode')
    suite = {'version': suite_version, 'cases': records}
    # observed_at is the original recipe snapshot time, not a moving resume key.
    stable_recipe = {k: v for k, v in recipe.items() if k != 'observed_at'}
    identity = {'recipe_sha256': digest(stable_recipe), 'suite_sha256': digest(suite),
                'evidence_mode': evidence_mode}
    implementation = {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    evaluator_identities = {}
    for key, function in {**{'consumer:' + k:v for k,v in consumers.items()},
                          **{'evaluator:' + k:v for k,v in evaluators.items()}}.items():
        source = inspect.getsourcefile(function)
        source_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None
        implementation[key] = {'module': function.__module__, 'name': function.__qualname__,
                               'source_sha256': source_hash}
        if key.startswith('evaluator:'):
            evaluator_identities[key.removeprefix('evaluator:')] = {
                'module': function.__module__, 'name': function.__qualname__,
                'source_sha256': source_hash}
    identity['implementation_sha256'] = digest(implementation)
    directory.mkdir(mode=0o700, parents=True, exist_ok=resume)
    with (directory / '.runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = directory / 'run.json'
        if resume:
            manifest = read(manifest_path)
            if any(manifest.get(key) != val for key, val in identity.items()):
                raise ValueError('Resume requires identical recipe and suite')
        else:
            manifest = {'schema': SCHEMA, 'id': uuid.uuid4().hex, 'created_at': now(),
                        'recipe': recipe, 'cases': records, 'suite_version': suite_version,
                        'completion_evidence': {'source': 'returned_response.content',
                            'case_text_json_bytes': {case.id: completion_text_budget(case) for case in cases},
                            'run_text_json_bytes': sum(completion_text_budget(case) for case in cases)},
                        'implementation': implementation, 'evaluator_identities': evaluator_identities, **identity}
            write_once(manifest_path, manifest)
        for case, record in zip(cases, records):
            target = directory / 'attempts' / case.id
            terminal = target / 'result.json'
            if terminal.exists():
                previous = read(terminal)
                if previous.get('run_id') != manifest['id'] or previous.get('case_sha256') != record['sha256']:
                    raise ValueError('Stored attempt does not belong to this run')
                if previous.get('cleanup') == 'state_directory_retained':
                    break
                continue
            started_path = target / 'started.json'
            result = {'schema': SCHEMA, 'run_id': manifest['id'], 'case_id': case.id,
                      'case_sha256': record['sha256'], 'attempt': 1, 'evidence_mode': evidence_mode,
                      'outcome': 'interrupted', 'primary_outcome': 'unverified', 'checks': {},
                      'observations': [], 'output': None, 'effects': {}, 'elapsed_ms': None,
                      'qualification_routing': {'scope': ('isolated_hermes_profile' if case.boundary == 'native_hermes'
                                                         else 'isolated_router_copy'),
                          'role': case.role, 'binding': recipe['binding'],
                          'target_task_role_overrides': {task: case.role for task in case.target_tasks}},
                      'cleanup': 'not_started', 'failure_category': None}
            if started_path.exists():
                previous = read(started_path)
                if previous.get('run_id') != manifest['id'] or previous.get('case_sha256') != record['sha256']:
                    raise ValueError('Stored start does not belong to this run')
                result.update(started_at=previous['started_at'], ended_at=now(),
                              failure_category='interrupted_before_result', cleanup='unconfirmed')
                write_once(terminal, result)
                continue
            result['started_at'] = now()
            write_once(started_path, {k: result[k] for k in ('run_id', 'case_id', 'case_sha256', 'started_at')})
            began, phase, temporary, context = time.monotonic(), 'setup', None, None
            try:
                missing = [name for name in case.required_capabilities
                           if recipe.get('declared', {}).get(name) is not True]
                if missing:
                    result.update(outcome='unsupported', failure_category='capability_unavailable', missing_capabilities=missing)
                else:
                    async with asyncio.timeout(case.timeout_seconds):
                        temporary = tempfile.mkdtemp(prefix='state-', dir=target)
                        state = temporary
                        router = router_factory(case)
                        consumer, evaluator = consumers[case.consumer], evaluators[case.evaluator]
                        context = RunContext(router, state, result['observations'], recipe['binding'],
                            qualification_role=case.role, completion_budget_bytes=completion_text_budget(case))
                        phase = 'consumer'
                        observed = await consumer(deepcopy(case.inputs), context)
                        if not isinstance(observed, dict) or 'output' not in observed:
                            raise ValueError('Consumer returned no output record')
                        if len(encode(observed)) > case.max_output_bytes:
                            raise ValueError('Consumer result exceeds declared bound')
                        result.update(output=observed['output'], effects=observed.get('effects', {}))
                        if observed['output'] is None or observed['output'] == '' or observed['output'] == {}:
                            result.update(outcome='fail', failure_category='no_output')
                        else:
                            phase = 'evaluator'
                            checks = evaluator(deepcopy(observed), deepcopy(case.oracle))
                            if not checks or any(v is not None and type(v) is not bool for v in checks.values()):
                                raise ValueError('Evaluator must return named boolean or unknown checks')
                            result['checks'] = checks
                            result['outcome'] = 'pass' if all(v is True for v in checks.values()) else 'fail'
                            observed_routes = [r for r in result['observations'] if r.get('role') == case.role]
                            if observed_routes and all(r.get('selected_binding') == recipe['binding']
                                    and r.get('prior_attempts') == [] and r.get('outcome') == 'returned'
                                    for r in observed_routes):
                                result['primary_outcome'] = result['outcome']
                            elif any(r.get('prior_attempts') for r in observed_routes):
                                result['primary_outcome'] = 'fail'
            except asyncio.CancelledError:
                result.update(outcome='interrupted', failure_category='cancelled')
                raise
            except TimeoutError:
                result.update(outcome='timeout', failure_category='deadline')
            except Exception as exc:
                result.update(outcome='setup_error' if phase == 'setup' else 'error',
                              failure_category=phase + ':' + type(exc).__name__)
            finally:
                if temporary is not None and context is not None and not context.state_cleanup_safe:
                    result.update(cleanup='state_directory_retained', retained_state_dir=temporary,
                                  primary_outcome='unverified')
                    if result['outcome'] == 'pass':
                        result.update(outcome='error', failure_category='native_cleanup_unconfirmed')
                elif temporary is not None:
                    try:
                        shutil.rmtree(temporary)
                    except Exception as exc:
                        result.update(cleanup='failed', cleanup_error_type=type(exc).__name__,
                                      outcome='error', primary_outcome='unverified', failure_category='state_cleanup')
                    else:
                        result['cleanup'] = 'state_directory_removed'
                result['consumer_resource_cleanup'] = 'not_observed_by_runner'
                result.update(ended_at=now(), elapsed_ms=round((time.monotonic() - began) * 1000, 3))
                write_once(terminal, result)
            if context is not None and not context.state_cleanup_safe:
                break  # Preserve the first incomplete cleanup; do not start more native work.
        return manifest

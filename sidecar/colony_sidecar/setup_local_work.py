"""Attach accepted source drafts to Hermes' native board and worker profile."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx
import yaml

BOARD = PROFILE = 'colony-drafts'


def native_root(home):
    # Match native get_default_hermes_root/resolve_profile_env. A selected
    # named conversation profile shares its root's board/profile namespace.
    return home.parent.parent if home.parent.name == 'profiles' else home


def verify_tools(endpoint, model, key):
    tool = {'type':'function', 'function':{'name':'colony_setup_echo',
        'description':'Return the supplied neutral setup token; no action is executed.',
        'parameters':{'type':'object','properties':{'token':{'type':'string'}},'required':['token']}}}
    response = httpx.post(endpoint+'/chat/completions', headers={'Authorization':'Bearer '+key},
        json={'model':model,'messages':[{'role':'user','content':'Call colony_setup_echo with token colony-ready.'}],
              'tools':[tool],'tool_choice':{'type':'function','function':{'name':'colony_setup_echo'}},
              'max_tokens':256}, timeout=60, trust_env=False)
    response.raise_for_status()
    choices = response.json().get('choices') or [{}]
    calls = choices[0].get('message', {}).get('tool_calls') or []
    if len(calls) != 1 or calls[0].get('function', {}).get('name') != 'colony_setup_echo':
        raise ValueError('Local drafts require a model with function calling; choose another model or omit --local-work')
    if json.loads(calls[0]['function']['arguments']) != {'token':'colony-ready'}:
        raise ValueError('The local model did not return the requested setup function arguments')


def planning_configuration(configuration):
    configuration['modelPool'] = {'local-planning':{
        'model':configuration['models']['large'], 'provider':'local',
        'supportsTools':True, 'maxTokens':4096}}
    configuration['functionRoles'] = {'planning':{
        'candidates':['local-planning'], 'timeoutSeconds':120, 'deadlineSeconds':600}}
    return configuration


def model_configuration(state, *, configuration_path=None):
    from .router.native_policy import planning
    path = Path(configuration_path) if configuration_path else state/'.colony-llm-config.json'
    options, policy = asyncio.run(planning(json.loads(path.read_text())))
    providers, entries = {}, []
    for index, entry in enumerate([options, *(options['fallback_model'] or [])]):
        name = 'colony-planning-'+str(index)
        providers[name] = {'base_url':entry['base_url'], 'api_key':entry['api_key'],
            'api_mode':'chat_completions', 'default_model':entry['model'],
            'discover_models':False, 'models':{entry['model']:{}},
            'extra_body':options['request_overrides']['extra_body']}
        entries.append({'provider':'custom:'+name, 'model':entry['model'],
                        'base_url':entry['base_url'], 'api_key':entry['api_key']})
    # Hermes resolves named OpenAI-compatible endpoints to the custom runtime.
    providers['custom'] = {'request_timeout_seconds':policy['request_timeout_seconds']}
    return {'model':{'provider':entries[0]['provider'], 'default':entries[0]['model'],
                     'max_tokens':options['max_tokens']},
            'providers':providers, 'fallback_model':entries[1:]}, policy


def worker_configuration(model, policy, plugin, lane):
    """Native profile bytes shared by the public installer and private stagers."""
    worker_plugin = {key:plugin[key] for key in (
        'url','api_key','owner_contact_id','instance_dir','turn_outbox_path') if key in plugin}
    worker_plugin.update(execution_registry_enabled=True, attested_system_platforms=['cli'],
        enabled_action_tools=[], enabled_message_tools=[], enabled_read_tools=[],
        native_local_work={**lane, 'worker':True, 'routing_policy':policy})
    return {**model, 'agent':{'max_turns':12},
        'toolsets':['colony','kanban'], 'platform_toolsets':{'cli':['colony','kanban']},
        'plugins':{'enabled':['colony'], 'colony':worker_plugin,
                   'entries':{'colony':{'allow_tool_override':True}}},
        'memory':{'memory_enabled':False, 'user_profile_enabled':False},
        'kanban':{'dispatch_in_gateway':False, 'auto_decompose':False}}


def refresh_role(state):
    """Refresh a managed profile before future native worker processes start."""
    from .setup import _atomic_hermes_config_write
    state = Path(state).resolve()
    manifest = json.loads((state/'instance.json').read_text())
    binding = manifest['local_work']
    if binding.get('executor') != 'kanban':
        raise ValueError('A native local-work binding is required')
    home = Path(manifest['hermes_home'])
    worker = native_root(home)/'profiles'/binding['worker_profile']
    path = worker/'config.yaml'
    before = path.read_bytes()
    config = yaml.safe_load(before)
    if config['plugins']['colony']['instance_dir'] != str(state):
        raise ValueError('The worker profile belongs to another instance')
    model, policy = model_configuration(state, configuration_path=manifest.get('model_configuration_path'))
    config.update(model)
    config['plugins']['colony']['native_local_work']['routing_policy'] = policy
    after = yaml.safe_dump(config, sort_keys=False).encode()
    if before != after:
        _atomic_hermes_config_write(path, before, after)
    return policy


def install(state):
    """Create a private native profile; retain legacy in-flight work to drain."""
    from .setup import _atomic_hermes_config_write
    from .setup_hermes import _private_write, _json, _forwarder
    state = Path(state).resolve()
    manifest_path = state/'instance.json'
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    home = Path(manifest['hermes_home'])
    previous = manifest.get('local_work') or {}
    if manifest.get('version') != 1 or manifest.get('profile') != 'local':
        raise ValueError('Expected a local Colony instance')
    adapter = manifest['adapter_binding']
    adapter_root = (state/'adapter/colony_hermes' if adapter['mode'] == 'private-directory'
                    else Path(adapter['sources']['colony_hermes']))
    if not (adapter_root/'native_drafts.py').is_file():
        raise ValueError('Upgrade this instance\'s native adapter before installing Kanban drafts')
    if previous.get('executor') == 'kanban':
        refresh_role(state)
        return previous
    model, policy = model_configuration(state)
    path = home/'config.yaml'; config_before = path.read_bytes()
    config = yaml.safe_load(config_before)
    if config.get('kanban', {}).get('dispatch_in_gateway') is False:
        raise ValueError('Enable the selected Hermes gateway dispatcher before installing local drafts')
    suffix = '-'+hashlib.sha256(str(home).encode()).hexdigest()[:8] if native_root(home) != home else ''
    board, profile = BOARD+suffix, PROFILE+suffix
    worker = native_root(home)/'profiles'/profile
    preparation = state/'local-work-install.json'
    marker = {'hermes_home':str(home), 'worker_profile':profile}
    owned = preparation.is_file() and json.loads(preparation.read_text()) == marker
    if worker.exists() and not owned:
        raise ValueError('The colony-drafts profile already exists; retain or reconcile its binding')
    from dotenv import dotenv_values
    secret = dotenv_values(home/'.env').get('COLONY_NATIVE_API_KEY')
    if not secret:
        raise ValueError('The selected native adapter credential is unavailable')
    if not preparation.exists():
        _private_write(preparation, _json(marker))
    native = manifest['hermes_python']
    environment = dict(os.environ, HERMES_HOME=str(home))
    # Native creation has no model calls and never clones the owner's channels,
    # history, skills or broad toolset into the constrained draft worker.
    result = subprocess.run([native, '-B', '-c',
        'import os; os.umask(0o077); '
        'from hermes_cli.profiles import create_profile,profile_exists; from hermes_cli import kanban_db as kb; '
        f'profile_exists({profile!r}) or create_profile({profile!r},no_alias=True,no_skills=True); '
        f'kb.create_board({board!r},name="Accepted local drafts")'],
        env=environment, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Native board/profile creation failed; prepared native files are retained')
    binding = {'executor':'kanban', 'board':board, 'worker_profile':profile,
               'role':'planning', 'scope':'explicitly_accepted_local_sources'}
    lane = {'board':board, 'worker_profile':profile, 'destination':str(state/'drafts'),
            'worker':False, 'instance_dir':str(state)}
    if previous.get('job_id'):
        binding['legacy_job_id'] = lane['legacy_job_id'] = previous['job_id']
    plugin = config['plugins']['colony']
    plugin['native_local_work'] = lane
    worker_config = worker_configuration(model, policy, plugin, lane)
    worker_path = worker/'config.yaml'
    _atomic_hermes_config_write(worker_path, worker_path.read_bytes(),
                               yaml.safe_dump(worker_config, sort_keys=False).encode())
    # Native stock worker startup loads only its profile's environment. Reuse
    # the adapter credential, not the owner's channel credentials/environment.
    env_path = worker/'.env'
    env_before = env_path.read_bytes() if env_path.exists() else None
    _atomic_hermes_config_write(env_path, env_before,
        ('COLONY_NATIVE_API_KEY='+json.dumps(secret)+'\nCOLONY_GENERAL_PLUGIN_ACTIVE=1\n'
         'COLONY_MEMORY_WORKER_TOOLS=0\nCOLONY_MEMORY_TURN_WRITER=disabled\n').encode())
    def write_owned(path, value):
        before = path.read_bytes() if path.exists() else None
        _atomic_hermes_config_write(path, before, value if isinstance(value, bytes) else value.encode())
    if manifest['adapter_binding']['mode'] == 'private-directory':
        (worker/'plugins/colony').mkdir(parents=True, exist_ok=True, mode=0o700)
        write_owned(worker/'plugins/colony/__init__.py', _forwarder(state/'adapter', 'colony_hermes'))
        write_owned(worker/'plugins/colony/plugin.yaml', (state/'adapter/colony_hermes/plugin.yaml').read_bytes())
    write_owned(worker/'SOUL.md', 'You execute one accepted local draft as part of the same Colony agent.\n'
        'Use only the accepted sources. Retain uncertainty and citations. Finish through kanban_complete.\n')
    # Publish the enabled binding only after its native worker is fully prepared.
    manifest['local_work'] = binding
    env_path = state/'.env'; env_before = env_path.read_bytes()
    lines = [line for line in env_before.decode().splitlines() if not line.startswith((
        'COLONY_LOCAL_WORK_ENABLED=', 'COLONY_LOCAL_WORK_EXECUTOR=',
        'COLONY_LOCAL_WORK_BOARD=', 'COLONY_LOCAL_WORK_PROFILE='))]
    lines += ['COLONY_LOCAL_WORK_ENABLED=true', 'COLONY_LOCAL_WORK_EXECUTOR=kanban',
              'COLONY_LOCAL_WORK_BOARD='+board, 'COLONY_LOCAL_WORK_PROFILE='+profile]
    _atomic_hermes_config_write(env_path, env_before, ('\n'.join(lines)+'\n').encode())
    _atomic_hermes_config_write(path, config_before, yaml.safe_dump(config, sort_keys=False).encode())
    _atomic_hermes_config_write(manifest_path, original, _json(manifest).encode())
    preparation.unlink()
    return binding


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-role', type=Path, required=True)
    print(json.dumps(refresh_role(parser.parse_args().refresh_role)))

"""Install one bounded local task class in the selected native Hermes scheduler."""
import json
import os
from pathlib import Path
import subprocess

import httpx

SCRIPT = 'colony-local-drafts.py'
NAME = 'Accepted local source drafts'


def native_jobs(python, environment):
    result = subprocess.run([python, '-B', '-c',
        'import json; from cron.jobs import list_jobs; '
        'print(json.dumps([{k:j.get(k) for k in ("id","name","script")} '
        'for j in list_jobs(include_disabled=True)]))'],
        env=environment, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('The selected Hermes job list is unavailable')
    return json.loads(result.stdout.splitlines()[-1])


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


def install(state):
    """Called after fresh attachment and before starting the new sidecar."""
    from .setup import _atomic_hermes_config_write
    from .setup_hermes import _private_write, _json
    state = Path(state).resolve()
    manifest_path = state/'instance.json'
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    home = Path(manifest['hermes_home'])
    if manifest.get('version') != 1 or manifest.get('profile') != 'local' or manifest.get('local_work'):
        raise ValueError('Expected a new local instance without a task binding')
    runner = '''import json,os,sys
from pathlib import Path
from dotenv import load_dotenv
state=Path(sys.argv[1]); manifest=json.loads((state/'instance.json').read_text())
home=Path(manifest['hermes_home'])
os.environ['HERMES_HOME']=str(home)
load_dotenv(home/'.env',override=True)
if manifest['adapter_binding']['mode']=='private-directory':sys.path.insert(0,str(state/'adapter'))
from colony_hermes.local_work_runner import main
raise SystemExit(main(['--instance',str(state)]))
'''
    script = ('import json,os\nfrom pathlib import Path\n'
              f'state=Path({str(state)!r})\n'
              "manifest=json.loads((state/'instance.json').read_text())\n"
              "python=manifest['hermes_python']\n"
              f'os.execve(python,[python,"-B","-c",{runner!r},str(state)],dict(os.environ,HERMES_HOME=manifest["hermes_home"]))\n')
    script_path = home/'scripts'/SCRIPT
    environment = dict(os.environ, HERMES_HOME=str(home))
    native = manifest['hermes_python']
    before_jobs = native_jobs(native, environment)
    if any(row['script'] == SCRIPT for row in before_jobs):
        raise ValueError('This Hermes home already has a local draft job; retain or reconcile its binding')
    _private_write(script_path, script)
    job_id = None
    try:
        created = subprocess.run([native, '-B', '-c',
            'import json; from cron.jobs import create_job; '
            'job=create_job(prompt=None,schedule="every 5m",name="Accepted local source drafts",'
            f'script={SCRIPT!r},no_agent=True,deliver="local",attach_to_session=False); '
            'print(json.dumps({"job_id":job["id"]}))'],
            env=environment, capture_output=True, text=True, timeout=30)
        if created.returncode:
            raise ValueError('The selected Hermes scheduler could not register local drafts')
        job_id = json.loads(created.stdout.splitlines()[-1])['job_id']
        manifest['local_work'] = {'job_id':job_id, 'script':SCRIPT, 'role':'planning',
                                  'schedule_minutes':5, 'scope':'explicitly_accepted_local_sources'}
        _atomic_hermes_config_write(manifest_path, original, _json(manifest).encode())
        path = state/'.env'; before = path.read_bytes()
        _atomic_hermes_config_write(path, before, before +
            f'COLONY_LOCAL_WORK_ENABLED=true\nCOLONY_LOCAL_WORK_JOB_ID={job_id}\n'.encode())
    except BaseException:
        # Creation can persist before its child output is lost. Recover only
        # this install's new name/script pair through the native job API.
        old_ids = {row['id'] for row in before_jobs}
        own = [row for row in native_jobs(native, environment)
               if row['id'] not in old_ids and row['script'] == SCRIPT and row['name'] == NAME]
        if len(own) > 1:
            raise ValueError('Local draft registration is ambiguous; its script is retained') from None
        if own:
            removed = subprocess.run([native,'-B','-c',
                'from cron.jobs import remove_job; assert remove_job('+repr(own[0]['id'])+')'],
                env=environment, capture_output=True, timeout=30)
            if removed.returncode:
                raise ValueError('Local draft registration could not be removed; its script is retained') from None
        if script_path.is_file() and script_path.read_text() == script:
            script_path.unlink()
        current = manifest_path.read_bytes()
        if job_id and json.loads(current).get('local_work', {}).get('job_id') == job_id:
            _atomic_hermes_config_write(manifest_path, current, original)
        raise
    return manifest['local_work']

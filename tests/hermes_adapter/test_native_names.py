"""Canonical native discovery and exact governed action identity."""
import importlib.util
import json
import os

import pytest

from conftest import run_python


PROBE = r'''
import hashlib,importlib,json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
selected,first,duplicate=sys.argv[3],sys.argv[4],sys.argv[5]=='duplicate'
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
settings={'url':'http://127.0.0.1:17777','owner_contact_id':'fixture-owner',
 'attested_system_platforms':['cli'],'turn_writer_platforms':[],
 'enabled_action_tools':[selected+'_task_snooze'],'enabled_read_tools':[],
 'action_mediator_url':'http://127.0.0.1:18802/v1/action-intents',
 'action_mediator_api_key':'disposable-fixture-key','action_mediator_principal':'fixture-adapter',
 'turn_outbox_path':str(home/'state/turn-outbox.db')}
config={'plugins':{'enabled':['pacomind','pacomind'] if duplicate else [selected],selected:settings},
 'toolsets':[selected],'tools':{'tool_search':{'enabled':False}},
 'memory':{'memory_enabled':False,'user_profile_enabled':False}}
(home/'config.yaml').write_text(json.dumps(config))
if duplicate:
 # A retained profile forwarder can coexist with canonical installed discovery.
 path=home/'plugins/pacomind';path.mkdir(parents=True)
 (path/'plugin.yaml').write_text('name: pacomind\nversion: 1.0.0\nentry_point: __init__.py\n')
 (path/'__init__.py').write_text('from pacomind_hermes import register\n')
def no_network(*args,**kwargs):raise AssertionError('In-process fixture transport only')
socket.socket.connect=no_network;socket.create_connection=no_network
import httpx
requests=[];intents=[]
def respond(request):
 path=request.url.path;requests.append(path)
 if path=='/v1/action-intents':
  value=json.loads(request.content);intents.append(value)
  return httpx.Response(200,json={'schema':'HermesToolActionAdmissionV1','version':1,'status':'pending',
   'effect_performed':False,'intent_id':value['intent_id'],
   'action_id':'11111111-1111-4111-8111-111111111111','action_digest':'a'*64,'approval_id':'fixture-admission'})
 if path=='/v1/host/memory/sources/erasures':
  return httpx.Response(200,json={'contact_id':'fixture-owner','events':[],'through':0,'head':0,'complete':True})
 return httpx.Response(200,json={})
original_client=httpx.Client
httpx.Client=lambda *args,**kwargs:original_client(*args,**{**kwargs,'transport':httpx.MockTransport(respond)})
canonical=importlib.import_module('pacomind_hermes')
with canonical.input_provenance.supplied_input(contact_id='fixture-owner',session_id='native-session',
 input_refs=[{'source_id':'neutral-source','input_message_hash':'b'*64}]) as supplied:
 assert canonical.input_provenance.current() is supplied
assert canonical.input_provenance.current() is None
from hermes_cli.plugins import get_plugin_manager,invoke_hook
manager=get_plugin_manager();manager.discover_and_load()
assert selected in manager._plugins and manager._plugins[selected].enabled,manager.list_plugins()
assert not manager._plugins[selected].error,manager.list_plugins()
assert len(manager._hooks.get('pre_llm_call',[]))==1,manager.list_plugins()
assert len(manager._middleware.get('tool_execution',[]))==1,manager.list_plugins()
assert len(manager._middleware.get('llm_request',[]))==2,manager.list_plugins()
counts=({name:len(values) for name,values in manager._hooks.items()},
        {name:len(values) for name,values in manager._middleware.items()})
manager.discover_and_load()
assert counts==({name:len(values) for name,values in manager._hooks.items()},
                {name:len(values) for name,values in manager._middleware.items()})
from model_tools import get_tool_definitions,handle_function_call
schemas=get_tool_definitions(enabled_toolsets=[selected],quiet_mode=True)
names={schema['function']['name'] for schema in schemas}
name=selected+'_task_snooze'
assert name in names,(names,manager.list_plugins())
assert all(item.startswith('pacomind_') for item in names),names
assert not names.intersection({'tool_call','tool_search','tool_describe'}),names
context={'session_id':'native-session','task_id':'native-task','turn_id':'native-turn',
         'tool_call_id':'native-call','api_request_id':'native-request'}
invoke_hook('pre_llm_call',**context,platform='cli',sender_id='',
 user_message='Snooze the neutral task for two hours.',conversation_history=[])
scope=canonical._TRANSPORT_SCOPES.for_execution(**{k:context[k] for k in ('session_id','task_id','turn_id')})
assert scope is not None and scope.contact_id=='fixture-owner' and scope.authority_lane=='system',scope
from hermes_cli.middleware import apply_llm_request_middleware
request=apply_llm_request_middleware({'messages':[{'role':'user','content':'Snooze the neutral task.'}],
 'tools':schemas},**context,platform='cli')
assert any(row.get('reason')=='source_erasure_checked' for row in request.trace),request.trace
args={'task_id':'neutral-task','hours':2,'reason':'later'}
result=json.loads(handle_function_call(name,args,**context,enabled_toolsets=[selected]))
assert result['status']=='pending' and result['effect_performed'] is False,(result,requests)
assert len(intents)==1,intents
intent=intents[0]
assert intent['schema']=='HermesToolActionIntentV1' and intent['tool_name']=='pacomind_task_snooze',intent
assert intent['args']==args
assert intent['context']=={**context,'authority_lane':'system','contact_id':'fixture-owner',
 'platform':'cli','sender_id':''},intent
# Recompute the exact canonical wire fields independently of the adapter builder.
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
assert intent['args_sha256']==digest(args)
assert intent['context_sha256']==digest(intent['context'])
call_digest=digest({'schema':'HermesActionCallV1','tool_name':'pacomind_task_snooze',**context})
assert intent['idempotency_key']==call_digest and intent['intent_id']=='hti_'+call_digest[:32]
assert intent['intent_digest']==digest({k:v for k,v in intent.items() if k!='intent_digest'})
assert canonical._TOOL_EXECUTION_CONTEXT.get() is None
assert result==json.loads(handle_function_call(name,args,**context,enabled_toolsets=[selected]))
assert len(intents)==2 and intents[0]==intents[1]
print(json.dumps({'selected':selected,'duplicate_discovery':duplicate,'first_import':first,
 'native_tool':name,'wire_tool':intent['tool_name'],'intent_digest':intent['intent_digest'],
 'tool_hooks':len(manager._middleware['tool_execution']),'model_calls':0,'network':0}))
manager.unload()
'''


@pytest.mark.parametrize('discovery', ['installed', 'duplicate'])
@pytest.mark.parametrize('first', ['pacomind_hermes'])
@pytest.mark.parametrize('selected', ['pacomind'])
def test_native_names_preserve_governed_identity(artifacts, tmp_path, selected, first, discovery):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'), HERMES_HOME=str(tmp_path/'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', PACOMIND_GENERAL_PLUGIN_ACTIVE='1',
        PACOMIND_MEMORY_WORKER_TOOLS='0', PACOMIND_MEMORY_TURN_WRITER='disabled',
        PACOMIND_GUARD_CHAT_MODE='off')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, first, discovery, cwd=tmp_path, env=env)
    row = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"selected":'))
    assert row['wire_tool'] == 'pacomind_task_snooze'


SELECTION_PROBE = r'''
import json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
selected=sys.argv[3]
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[selected]},
 'tools':{'tool_search':{'enabled':False}}}))
def no_network(*args,**kwargs):raise AssertionError('Discovery must not call services')
socket.socket.connect=no_network;socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
plugin=manager._plugins.get(selected)
assert plugin is not None and plugin.enabled and not plugin.error,manager.list_plugins()
assert len(manager._hooks.get('pre_llm_call',[]))==1,manager.list_plugins()
assert len(manager._middleware.get('tool_execution',[]))==1,manager.list_plugins()
assert plugin.tools_registered and all(name.startswith(selected+'_') for name in plugin.tools_registered),manager.list_plugins()
print(json.dumps({'explicit_enabled_name':selected,'settings_namespace_required':False,'network':0}))
manager.unload()
'''


@pytest.mark.parametrize('selected', ['pacomind'])
def test_explicit_native_plugin_selection_without_settings_namespace(artifacts, tmp_path, selected):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'), HERMES_HOME=str(tmp_path/'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', PACOMIND_GENERAL_PLUGIN_ACTIVE='1',
        PACOMIND_MEMORY_WORKER_TOOLS='0', PACOMIND_MEMORY_TURN_WRITER='disabled',
        PACOMIND_GUARD_CHAT_MODE='off')
    run_python('-I', '-B', '-c', SELECTION_PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, cwd=tmp_path, env=env)


REFRESH_PROBE = r'''
import json,os,socket,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
import yaml
from pacomind import setup_hermes
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
state=home/'pacomind';adapter=state/'adapter'
resources=setup_hermes._adapter_resources(sys.argv[3])
# Retained directory names use canonical implementation imports.
for name,raw in resources.items():
 path=adapter/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
for name,module in [('pacomind','pacomind_hermes'),('pacomind-memory','pacomind_memory')]:
 path=home/'plugins'/name;path.mkdir(parents=True)
 (path/'__init__.py').write_text(setup_hermes._forwarder(adapter,module,module=='pacomind_memory'))
 (path/'plugin.yaml').write_bytes(resources[module+'/plugin.yaml'])
config={'plugins':{'enabled':['pacomind'],'pacomind':{'instance_dir':str(state),
 'turn_outbox_path':str(home/'turn-outbox.db'),'enabled_read_tools':[]}},
 'memory':{'provider':'pacomind-memory','memory_enabled':False,'user_profile_enabled':False},
 'tools':{'tool_search':{'enabled':False}}}
(home/'config.yaml').write_text(yaml.safe_dump(config))
manifest={'version':1,'profile':'local','hermes_home':str(home),'hermes_python':sys.executable,
 'adapter_binding':{'mode':'private-directory'},'adapter_sha256':setup_hermes._resource_digest(resources)}
(state/'instance.json').write_text(json.dumps(manifest))
def no_network(*args,**kwargs):raise AssertionError('Refresh and native loading must stay offline')
socket.socket.connect=no_network;socket.create_connection=no_network
setup_hermes.refresh_adapter(state,SimpleNamespace(adapter_wheel=sys.argv[3],hermes_python=sys.executable))
config=yaml.safe_load((home/'config.yaml').read_text())
assert config['plugins']['enabled']==['pacomind']
assert config['memory']['provider']=='pacomind-memory'
assert (home/'plugins/pacomind-memory').is_dir()
from hermes_cli.plugins import get_plugin_manager
from plugins.memory import find_provider_dir,load_memory_provider
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['pacomind'].enabled,manager.list_plugins()
assert not manager._plugins['pacomind'].error,manager.list_plugins()
assert len(manager._hooks['pre_llm_call'])==1
assert find_provider_dir(config['memory']['provider'])==home/'plugins/pacomind-memory'
provider=load_memory_provider(config['memory']['provider'],register_skills=False)
assert provider is not None
assert type(provider).__module__=='pacomind_memory.provider'
assert {schema['name'] for schema in provider.get_tool_schemas()}=={
 'pacomind_check_commitments','pacomind_get_affect','pacomind_get_facts','pacomind_timeline'}
assert all(name.startswith('pacomind_') for name in manager._plugins['pacomind'].tools_registered)
print(json.dumps({'retained_directory_provider':'pacomind-memory','implementation':type(provider).__module__,
 'general_plugin':'pacomind','native_loads':1,'network':0,'model_calls':0}))
manager.unload()
'''


def test_refreshed_retained_directory_loads_canonical_native_provider(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    from conftest import ROOT
    from test_setup import _native_interpreter
    import subprocess
    native_python = _native_interpreter(tmp_path, artifacts[1], 'absent')
    env = {key:os.environ[key] for key in ('PATH','LANG','TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'),HERMES_HOME=str(tmp_path/'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1',PYTHON_DOTENV_DISABLED='1',
        PACOMIND_SKIP_DOTENV='1',PACOMIND_STATE_DIR=str(tmp_path/'state'),
        LITELLM_LOCAL_MODEL_COST_MAP='True',PACOMIND_GENERAL_PLUGIN_ACTIVE='1',
        PACOMIND_MEMORY_WORKER_TOOLS='0',PACOMIND_MEMORY_TURN_WRITER='disabled')
    result = subprocess.run([str(native_python),'-I','-B','-c',REFRESH_PROBE,str(ROOT/'sidecar'),
        os.environ.get('HERMES_TEST_SOURCE',''),str(artifacts[1])],cwd=tmp_path,env=env,
        text=True,capture_output=True,timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

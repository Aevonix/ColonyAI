"""Native names change discovery, never retained action or participant identity."""
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
config={'plugins':{'enabled':['apsimo','colony'] if duplicate else [selected],selected:settings},
 'toolsets':[selected],'tools':{'tool_search':{'enabled':False}},
 'memory':{'memory_enabled':False,'user_profile_enabled':False}}
(home/'config.yaml').write_text(json.dumps(config))
if duplicate:
 # A retained profile forwarder can coexist with canonical installed discovery.
 path=home/'plugins/colony';path.mkdir(parents=True)
 (path/'plugin.yaml').write_text('name: colony\nversion: 1.0.0\nentry_point: __init__.py\n')
 (path/'__init__.py').write_text('from colony_hermes import register\n')
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
initial=importlib.import_module(first)
canonical=importlib.import_module('apsimo_hermes');legacy=importlib.import_module('colony_hermes')
assert initial is canonical is legacy
for suffix in ('client','input_provenance','local_work','native_scope'):
 a=importlib.import_module('apsimo_hermes.'+suffix);b=importlib.import_module('colony_hermes.'+suffix)
 assert a is b,(suffix,a,b)
assert canonical.ApsimoClient is canonical.ColonyClient is legacy.ColonyClient
assert canonical._TOOL_EXECUTION_CONTEXT is legacy._TOOL_EXECUTION_CONTEXT
assert canonical._TRANSPORT_SCOPES is legacy._TRANSPORT_SCOPES
assert canonical.input_provenance._CURRENT is legacy.input_provenance._CURRENT
with legacy.input_provenance.supplied_input(contact_id='fixture-owner',session_id='native-session',
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
if duplicate:
 inactive='colony' if selected=='apsimo' else 'apsimo'
 assert manager._plugins[inactive].enabled,manager.list_plugins()
 assert not manager._plugins[inactive].tools_registered,manager.list_plugins()
 assert not manager._plugins[inactive].hooks_registered,manager.list_plugins()
 assert not manager._plugins[inactive].middleware_registered,manager.list_plugins()
from model_tools import get_tool_definitions,handle_function_call
schemas=get_tool_definitions(enabled_toolsets=[selected],quiet_mode=True)
names={schema['function']['name'] for schema in schemas}
name=selected+'_task_snooze'
assert name in names,(names,manager.list_plugins())
assert all(not item.startswith(('colony_' if selected=='apsimo' else 'apsimo_')) for item in names),names
assert not names.intersection({'tool_call','tool_search','tool_describe'}),names
for schema in schemas:
 assert schema['function']['name']==schema['function']['name'].replace('colony_',selected+'_')
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
assert intent['schema']=='HermesToolActionIntentV1' and intent['tool_name']=='colony_task_snooze',intent
assert intent['args']==args
assert intent['context']=={**context,'authority_lane':'system','contact_id':'fixture-owner',
 'platform':'cli','sender_id':''},intent
# These hashes are recomputed from the historical wire fields independently of
# the adapter builder. No canonical tool spelling may enter retained identity.
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
assert intent['args_sha256']==digest(args)
assert intent['context_sha256']==digest(intent['context'])
call_digest=digest({'schema':'HermesActionCallV1','tool_name':'colony_task_snooze',**context})
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
@pytest.mark.parametrize('first', ['apsimo_hermes', 'colony_hermes'])
@pytest.mark.parametrize('selected', ['apsimo', 'colony'])
def test_native_names_preserve_governed_identity(artifacts, tmp_path, selected, first, discovery):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'), HERMES_HOME=str(tmp_path/'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_GUARD_CHAT_MODE='off')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, first, discovery, cwd=tmp_path, env=env)
    row = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"selected":'))
    assert row['wire_tool'] == 'colony_task_snooze'


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


@pytest.mark.parametrize('selected', ['apsimo', 'colony'])
def test_explicit_native_plugin_selection_without_settings_namespace(artifacts, tmp_path, selected):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'), HERMES_HOME=str(tmp_path/'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', COLONY_GENERAL_PLUGIN_ACTIVE='1',
        COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled',
        COLONY_GUARD_CHAT_MODE='off')
    run_python('-I', '-B', '-c', SELECTION_PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, cwd=tmp_path, env=env)

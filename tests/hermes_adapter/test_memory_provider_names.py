"""Canonical discovery and selected directory providers load one implementation."""
import importlib.util
import os
from pathlib import Path

import pytest

from conftest import ROOT, run_python


PROBE = r'''
import importlib,json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
def blocked(*args,**kwargs):raise AssertionError('Provider-name qualification makes no network calls')
socket.socket.connect=blocked;socket.create_connection=blocked
home=Path(os.environ['HERMES_HOME']);home.mkdir()
selected=sys.argv[3];first=sys.argv[4];local_source=sys.argv[5]=='profile'
if local_source:
 import shutil
 selected_source=home/'plugins'/selected
 shutil.copytree(Path(sys.argv[6])/'plugins/pacomind-memory',selected_source)
 with (selected_source/'provider.py').open('a') as stream:stream.write('\nQUALIFIED_SOURCE_MARKER = \"selected-profile-source\"\n')
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[]},'memory':{'provider':selected}}))
first_module=importlib.import_module(first+'.provider')
canonical=importlib.import_module('pacomind_memory.provider')
assert first_module is canonical
from plugins import memory
found={entry.name:entry.value for entry in memory._iter_entry_points()}
assert found['pacomind-memory']=='pacomind_memory'
from agent.memory_provider import MemoryProvider
created=[]
def counted(cls,*args,**kwargs):
 value=object.__new__(cls);created.append(value);return value
with patch.object(MemoryProvider,'__new__',staticmethod(counted)):
 provider=memory.load_memory_provider(selected,register_skills=False)
assert provider is not None and len(created)==1, (type(provider).__name__,len(created))
assert provider is created[0]
if local_source:
 selected_module=importlib.import_module(type(provider).__module__)
 assert selected_module.QUALIFIED_SOURCE_MARKER=='selected-profile-source'
 assert Path(selected_module.__file__).resolve()==(selected_source/'provider.py').resolve()
else:
 assert isinstance(provider,canonical.PacoMindMemoryProvider)
assert provider.name=='pacomind' and provider.is_available()
assert not any(name == 'pacomind' or name.startswith(('pacomind.',)) for name in sys.modules)
provider.shutdown()
print(json.dumps({'selected':selected,'first_import':first,'provider_instances':len(created),'name':provider.name,'module_identity_shared':True,'selected_profile_source':local_source,'model_calls':0}))
'''


@pytest.mark.parametrize(('selected', 'source'), [
    ('pacomind-memory', 'entrypoint'), ('pacomind-memory', 'profile')])
@pytest.mark.parametrize('first', ['pacomind_memory'])
def test_native_memory_selection_loads_one_provider(artifacts, tmp_path, selected, first, source):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes runtime')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path / 'user'), HERMES_HOME=str(tmp_path / 'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True')
    run_python('-I', '-B', '-c', PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, first, source, ROOT, cwd=tmp_path, env=env)
